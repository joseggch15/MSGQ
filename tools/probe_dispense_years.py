# -*- coding: utf-8 -*-
"""Probe SOLO LECTURA: cuenta despachos del endpoint por anio.

Reproduce exactamente lo que hace el backfill del poller (paginar la conexion
`dispenses` con filter:{updatedFrom: 2022}) pero seleccionando solo id + fechas,
y tabula por anio de recordCollectedAt y recordUpdatedAt. Sirve para confirmar si
el endpoint DEVUELVE los despachos de 2025 (si si -> el backfill solo necesita
completarse / hay un hueco que recuperar; si no -> el problema es la query/API).

No escribe nada ni imprime datos sensibles (solo conteos y rangos de fecha).

Uso:  python tools/probe_dispense_years.py
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter

sys.path.insert(0, ".")

from msgq.api.client import AdaptIQClient
from msgq.config import Settings, load_embedded_settings

_LIGHT_DISPENSES = """
query Dispenses($siteId: ID!, $filter: MovementQuery, $first: Int, $after: String) {
  site(id: $siteId) {
    dispenses(filter: $filter, first: $first, after: $after) {
      pageInfo { hasNextPage endCursor }
      edges { node { id recordCollectedAt recordUpdatedAt } }
    }
  }
}
""".strip()


async def main() -> int:
    s = load_embedded_settings() or Settings.from_env()
    print(f"Endpoint: {s.endpoint}  site={s.site_id or '(auto)'}")
    c = AdaptIQClient(s)
    by_collected: Counter = Counter()
    by_updated: Counter = Counter()
    min_c = max_c = None
    try:
        site_id = await c._resolve_site_id()
        cursor = None
        pages = 0
        total = 0
        filt = {"updatedFrom": "2022-01-01T00:00:00"}
        while True:
            qv = {"siteId": site_id, "filter": filt, "first": 100}
            if cursor:
                qv["after"] = cursor
            data = await c._execute(_LIGHT_DISPENSES, qv)
            conn = (((data or {}).get("site") or {}).get("dispenses") or {})
            edges = conn.get("edges", [])
            for e in edges:
                n = e.get("node") or {}
                rc = (n.get("recordCollectedAt") or "")[:10]
                ru = (n.get("recordUpdatedAt") or "")[:4]
                if rc:
                    by_collected[rc[:4]] += 1
                    if min_c is None or rc < min_c:
                        min_c = rc
                    if max_c is None or rc > max_c:
                        max_c = rc
                if ru:
                    by_updated[ru] += 1
                total += 1
            pages += 1
            pi = conn.get("pageInfo") or {}
            if pages % 25 == 0:
                print(f"  ... {pages} paginas, {total:,} despachos")
            if not pi.get("hasNextPage"):
                break
            cursor = pi.get("endCursor")
            if not cursor:
                break

        print(f"\nTotal despachos en el endpoint: {total:,} ({pages} paginas)")
        print(f"recordCollectedAt rango: {min_c} .. {max_c}")
        print("\n-- por anio (recordCollectedAt) --")
        for y in sorted(by_collected):
            print(f"  {y}: {by_collected[y]:,}")
        print("\n-- por anio (recordUpdatedAt) --")
        for y in sorted(by_updated):
            print(f"  {y}: {by_updated[y]:,}")
        n2025 = by_collected.get("2025", 0)
        print(f"\n>>> Despachos 2025 en el endpoint: {n2025:,}")
        return 0
    finally:
        await c.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
