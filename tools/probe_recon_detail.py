# -*- coding: utf-8 -*-
"""Probe enfocado: semantica de `reconciliations` (enums + muestra reciente).

Las reconciliaciones mas antiguas traian openingStock/closingStock en null (solo
`volume`), lo que sugiere un tipo polimorfico. Aqui:
  - introspecciona los enums type / movementType / status,
  - muestrea reconciliaciones RECIENTES (filtradas por updatedFrom),
  - separa las filas que SI traen openingStock/closingStock (la reconciliacion
    de stock estilo 'Detailed Reconciliation').

Uso:  python tools/probe_recon_detail.py
"""
from __future__ import annotations

import asyncio
import json
import sys

sys.path.insert(0, ".")

from msgq.api.client import AdaptIQClient
from msgq.config import Settings, load_embedded_settings

RECENT = "2026-05-01T00:00:00Z"


def _q_enum(name: str) -> str:
    return '{ __type(name:"%s"){ name kind enumValues { name } } }' % name


SAMPLE = """
query Recon($siteId: ID!, $filter: MovementQuery) {
  site(id: $siteId) {
    reconciliations(first: 60, filter: $filter) {
      pageInfo { hasNextPage }
      nodes {
        id type movementType status
        periodStart periodEnd
        openingStock closingStock inflowVolume outflowVolume volume
        recordUpdatedAt
        target { code description } product { description }
      }
    }
  }
}
""".strip()

# Variante reducida por si type/movementType/status no fueran seleccionables solos.
SAMPLE_MIN = SAMPLE.replace("id type movementType status", "id")


async def main() -> int:
    settings = load_embedded_settings() or Settings.from_env()
    print(f"Endpoint: {settings.endpoint}")
    client = AdaptIQClient(settings)
    try:
        for enum_name in ("ReconciliationTransaction", "MovementType", "ReconciliationStatus"):
            try:
                d = await client._execute(_q_enum(enum_name), {})
                tt = d.get("__type") or {}
                vals = [v["name"] for v in (tt.get("enumValues") or [])]
                print(f"\n{enum_name} (kind={tt.get('kind')}): {vals or '(no es enum)'}")
            except Exception as exc:  # noqa: BLE001
                print(f"\n{enum_name}: [error {exc}]")

        site_id = await client._resolve_site_id()
        variables = {"siteId": site_id, "filter": {"updatedFrom": RECENT}}
        print(f"\n=== Reconciliaciones recientes (updatedFrom={RECENT}, site={site_id}) ===")
        try:
            d = await client._execute(SAMPLE, variables)
        except Exception as exc:  # noqa: BLE001
            print(f"[SAMPLE completo fallo: {exc}] -> reintento reducido")
            d = await client._execute(SAMPLE_MIN, variables)

        nodes = (((d.get("site") or {}).get("reconciliations") or {}).get("nodes")) or []
        with_stock = [n for n in nodes if n.get("openingStock") is not None or n.get("closingStock") is not None]
        print(f"\nTotal muestra: {len(nodes)} | con opening/closing stock: {len(with_stock)}")

        print("\n-- Hasta 8 filas CON stock (reconciliacion de stock) --")
        for n in with_stock[:8]:
            print("  ", json.dumps(n, ensure_ascii=False))
        print("\n-- Hasta 6 filas SIN stock (transaccion) --")
        for n in [x for x in nodes if x not in with_stock][:6]:
            print("  ", json.dumps(n, ensure_ascii=False))

        # Distribucion por 'type' si esta disponible.
        if nodes and "type" in nodes[0]:
            from collections import Counter
            print("\nDistribucion por type:", dict(Counter(n.get("type") for n in nodes)))
            print("Distribucion por movementType:", dict(Counter(n.get("movementType") for n in nodes)))
        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
