# -*- coding: utf-8 -*-
"""Probe de muestreo de TANQUES y RECONCILIACIONES (solo lectura, minimo).

Objetivo: ver la forma REAL de `Site.tanks` y `Site.reconciliations` para
disenar las queries del modulo de tanques:
  - argumentos de cada campo (filtro / paginacion),
  - input fields del tipo de filtro (p. ej. ReconciliationQuery),
  - una muestra pequena de nodos reales (tanques + primeras reconciliaciones).

Uso:  python tools/probe_tanks_sample.py
"""
from __future__ import annotations

import asyncio
import json
import sys

sys.path.insert(0, ".")

from msgq.api.client import AdaptIQClient
from msgq.config import Settings, load_embedded_settings

Q_SITE_ARGS = (
    '{ __type(name:"Site"){ fields { name '
    "args { name type { name kind ofType { name kind ofType { name kind } } } } } } }"
)


def _q_input_fields(name: str) -> str:
    return (
        '{ __type(name:"%s"){ name kind inputFields { name '
        "type { name kind ofType { name kind ofType { name } } } } } }" % name
    )


def _unwrap(t: dict | None) -> str | None:
    while t and not t.get("name"):
        t = t.get("ofType")
    return t.get("name") if t else None


SAMPLE = """
query Sample($siteId: ID!) {
  site(id: $siteId) {
    tanks(first: 100) {
      nodes {
        id code description name virtual capacity volumeUnit
        product { description } parentTank { code } tankType { description }
      }
    }
    reconciliations(first: 5) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id periodStart periodEnd
        openingStock closingStock inflowVolume outflowVolume volume
        recordUpdatedAt
        target { code description } product { description }
      }
    }
  }
}
""".strip()


async def main() -> int:
    settings = load_embedded_settings() or Settings.from_env()
    print(f"Endpoint: {settings.endpoint}")
    client = AdaptIQClient(settings)
    try:
        # --- 1. Argumentos de los campos tanks / reconciliations ---
        data = await client._execute(Q_SITE_ARGS, {})
        fields = {f["name"]: f for f in ((data.get("__type") or {}).get("fields") or [])}
        filter_types: list[str] = []
        for fname in ("tanks", "reconciliations"):
            f = fields.get(fname)
            if not f:
                print(f"\n[campo {fname} ausente]")
                continue
            print(f"\n=== args de Site.{fname} ===")
            for a in f.get("args", []):
                tn = _unwrap(a.get("type"))
                print(f"   {a['name']}: {tn}")
                if a["name"] == "filter" and tn:
                    filter_types.append(tn)

        # --- 2. Input fields del/los tipo(s) de filtro ---
        for ft in dict.fromkeys(filter_types):
            try:
                d = await client._execute(_q_input_fields(ft), {})
                inp = (d.get("__type") or {}).get("inputFields") or []
                print(f"\n=== inputFields de {ft} ===")
                for i in inp:
                    print(f"   {i['name']}: {_unwrap(i.get('type'))}")
            except Exception as exc:  # noqa: BLE001
                print(f"\n[no se pudo introspeccionar {ft}: {exc}]")

        # --- 3. Muestra real ---
        site_id = await client._resolve_site_id()
        print(f"\n=== Muestra (site id = {site_id}) ===")
        try:
            d = await client._execute(SAMPLE, {"siteId": site_id})
            site = d.get("site") or {}
            tanks = ((site.get("tanks") or {}).get("nodes")) or []
            recs = ((site.get("reconciliations") or {}).get("nodes")) or []
            print(f"\n-- Tanks ({len(tanks)}) --")
            for tk in tanks:
                print("  ", json.dumps(tk, ensure_ascii=False))
            print(f"\n-- Reconciliations (muestra {len(recs)}) --")
            for r in recs:
                print("  ", json.dumps(r, ensure_ascii=False))
            pi = (site.get("reconciliations") or {}).get("pageInfo")
            print("  pageInfo:", pi)
        except Exception as exc:  # noqa: BLE001
            print(f"[la query de muestra fallo: {exc}]")
            print("(probablemente un nombre de campo no existe; ajustar SAMPLE)")
            return 2
        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
