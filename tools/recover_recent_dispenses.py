# -*- coding: utf-8 -*-
"""Recuperacion RAPIDA de los despachos recientes que faltan en la replica.

Contexto: la conexion `dispenses` se ordena por recordCollectedAt ascendente, asi
que el backfill completo (desde 2022) debe recorrer TODO el historial antes de
llegar a lo reciente -> los despachos de 2025 tardan en aparecer. Este utilitario
trae SOLO lo reciente filtrando por `updatedFrom` (excluye el grueso del historico
ya replicado) y con una query LIGERA (la mitad de tiempo por pagina), llevando los
campos justos que necesita la auditoria SFL. Upsert idempotente: cuando el backfill
completo de la app alcance esos registros, los completara con los campos de hardware.

Solo escribe en la replica LOCAL. Salida ASCII con flush (para seguir el avance).
Uso:  python tools/recover_recent_dispenses.py [updatedFrom_iso]
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys

sys.path.insert(0, ".")

from msgq.api.client import AdaptIQClient
from msgq.config import Settings, load_embedded_settings
from msgq.core import transform
from msgq.storage import Database

# Query ligera: SOLO los campos que la auditoria SFL (y el listado) necesitan.
#   source -> punto de despacho (tank);  target -> equipo;  fieldUser -> operador.
_LIGHT_DISPENSES = """
query Dispenses($siteId: ID!, $filter: MovementQuery, $first: Int, $after: String) {
  site(id: $siteId) {
    dispenses(filter: $filter, first: $first, after: $after) {
      pageInfo { hasNextPage endCursor }
      edges { node {
        id volume uom recordCollectedAt recordCreatedAt recordUpdatedAt
        status type
        product { code description }
        source { code name }
        target { equipmentId description status }
        fieldUser { name }
      } }
    }
  }
}
""".strip()


def _year_counts(path: str) -> dict:
    con = sqlite3.connect(path)
    try:
        cur = con.execute(
            "SELECT substr(record_collected_at,1,4) yr, COUNT(*) "
            "FROM movements WHERE kind='DISPENSE' GROUP BY yr ORDER BY yr")
        return {row[0]: row[1] for row in cur.fetchall()}
    finally:
        con.close()


async def main() -> int:
    updated_from = sys.argv[1] if len(sys.argv) > 1 else "2024-06-01T00:00:00"
    s = load_embedded_settings() or Settings.from_env()
    if not s.token:
        print("[!] No hay token. Aborto.", flush=True)
        return 2
    print(f"Endpoint: {s.endpoint}  DB: {s.db_path}", flush=True)
    print(f"Recuperando despachos con updatedFrom={updated_from}", flush=True)
    print("Antes:", _year_counts(s.db_path), flush=True)

    db = Database(s.db_path)
    db._conn.execute("PRAGMA busy_timeout = 30000")   # espera el lock de la app
    c = AdaptIQClient(s)
    total = 0
    pages = 0
    try:
        site = await c._resolve_site_id()
        cursor = None
        while True:
            qv = {"siteId": site, "filter": {"updatedFrom": updated_from}, "first": 100}
            if cursor:
                qv["after"] = cursor
            data = await c._execute(_LIGHT_DISPENSES, qv)
            conn = (((data or {}).get("site") or {}).get("dispenses") or {})
            nodes = [e["node"] for e in conn.get("edges", []) if e.get("node")]
            for n in nodes:
                n["kind"] = "DISPENSE"
            if nodes:
                db.upsert("movements", transform.movements_to_df(nodes))
                total += len(nodes)
            pages += 1
            if pages % 10 == 0:
                last = (nodes[-1].get("recordCollectedAt") if nodes else "")[:10]
                y = _year_counts(s.db_path).get("2025", 0)
                print(f"  pagina {pages}: {total:,} traidos, ult.collectedAt={last}, "
                      f"2025 en replica={y:,}", flush=True)
            pi = conn.get("pageInfo") or {}
            if not pi.get("hasNextPage"):
                break
            cursor = pi.get("endCursor")
            if not cursor:
                break
        print(f"\nListo. {total:,} despachos recuperados en {pages} paginas.", flush=True)
    finally:
        after = _year_counts(s.db_path)
        db.close()
        await c.aclose()
    print("Despues:", after, flush=True)
    print(f">>> Despachos 2025 ahora en la replica: {after.get('2025', 0):,}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
