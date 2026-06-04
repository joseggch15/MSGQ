# -*- coding: utf-8 -*-
"""Probe de integridad (solo lectura): cuantifica los problemas detectados.

1) Colision de equipment_id (mismo ID humano en >1 equipo) -> el replica los
   colapsa (PK equipment_id) y el cruce SFL se vuelve ambiguo.
2) recordTypes del log de auditoria `changes` -> ver si la reasignacion de un
   despacho (cambio de equipo) es auditable (para detectarla y refrescar).
3) Cobertura de despachos en vivo (rango de fechas disponible) -> dimensionar
   cuanto historico falta en el replica.

ASCII. Usa la auth de MSGQ.  Uso: python tools/probe_integrity.py
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from msgq.api import queries
from msgq.api.client import AdaptIQClient
from msgq.config import Settings, load_embedded_settings


async def main() -> int:
    s = load_embedded_settings() or Settings.from_env()
    c = AdaptIQClient(s)
    try:
        site = await c._resolve_site_id()
        # --- 1) Colision de equipment_id en el maestro ---
        eq = await c.fetch_equipment(None)
        by_eid: dict[str, list] = {}
        for n in eq:
            by_eid.setdefault(n.get("equipmentId"), []).append(n.get("id"))
        dups = {k: v for k, v in by_eid.items() if k and len(v) > 1}
        print(f"Equipos: {len(eq)} | equipmentId distintos: {len(by_eid)} | "
              f"equipmentId DUPLICADOS: {len(dups)}")
        for k, v in list(dups.items())[:12]:
            descs = [next((x.get('description') for x in eq if x.get('id') == iid), '?') for iid in v]
            print(f"   {k!r}: internal_ids={v}  desc={descs}")

        # --- 2) recordTypes del log de cambios (ultimos 120 dias) ---
        since = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        cursor, types = None, Counter()
        attrs_by_type: dict[str, Counter] = {}
        for _ in range(60):
            pv = {"filter": {"siteId": site, "changesFrom": since}, "first": 100}
            if cursor:
                pv["after"] = cursor
            data = await c._execute(queries.CHANGES_QUERY, pv)
            conn = data.get("changes") or {}
            for e in conn.get("edges", []):
                n = e.get("node") or {}
                rt = n.get("recordType")
                types[rt] += 1
                ab = attrs_by_type.setdefault(rt, Counter())
                for ch in (n.get("changes") or []):
                    ab[ch.get("attribute")] += 1
            pi = conn.get("pageInfo") or {}
            if not pi.get("hasNextPage"):
                break
            cursor = pi.get("endCursor")
        print(f"\nrecordTypes en `changes` (120d): {dict(types)}")
        for rt, ab in attrs_by_type.items():
            top = ", ".join(f"{a}:{n}" for a, n in ab.most_common(8))
            print(f"   {rt}: {top}")

        # --- 3) Cobertura de despachos en vivo (rango de fechas) ---
        # Trae una pagina amplia de dispenses sin filtro para ver el rango.
        q = queries.MOVEMENT_CONNECTIONS["dispenses"][0]
        data = await c._execute(q, {"siteId": site, "filter": {}, "first": 100})
        edges = (((data.get("site") or {}).get("dispenses")) or {}).get("edges", [])
        dates = sorted([e["node"].get("recordCollectedAt") for e in edges if e.get("node")])
        if dates:
            print(f"\nDispenses (primera pagina, {len(dates)}): "
                  f"{dates[0]}  ..  {dates[-1]}")
        # Busca el de ~2759 L
        big = [e["node"] for e in edges if e.get("node") and
               2700 <= float(e["node"].get("volume") or 0) <= 2820]
        print(f"Despachos 2700-2820 L en esa pagina: {len(big)}")
        return 0
    finally:
        await c.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
