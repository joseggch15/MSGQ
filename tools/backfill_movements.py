# -*- coding: utf-8 -*-
"""Mantenimiento: completa el backfill historico de movimientos en la replica real.

Usa EXACTAMENTE el mismo codigo del poller (`Poller._sync_movements`, ahora
reanudable) contra el endpoint real. Sirve para recuperar de inmediato un hueco
historico (p. ej. los despachos de 2025 que faltaban porque el backfill se cortaba
antes de llegar a lo reciente) sin tener que dejar la app abierta el rato que tarda.

Solo escribe en la replica LOCAL (upsert idempotente); no muta nada en el FMS.
Salida ASCII. Uso:  python tools/backfill_movements.py
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys

sys.path.insert(0, ".")

from PySide6.QtCore import QCoreApplication

from msgq.api import make_source
from msgq.config import Settings, load_embedded_settings
from msgq.ingest.poller import Poller, _MOVEMENTS_BACKFILL_FLAG
from msgq.storage import Database


def _year_counts(path: str) -> dict:
    con = sqlite3.connect(path)
    try:
        cur = con.execute(
            "SELECT substr(record_collected_at,1,4) yr, COUNT(*) "
            "FROM movements WHERE kind='DISPENSE' GROUP BY yr ORDER BY yr")
        return {row[0]: row[1] for row in cur.fetchall()}
    finally:
        con.close()


def main() -> int:
    s = load_embedded_settings() or Settings.from_env()
    if not s.token:
        print("[!] No hay token (ni embedded_config ni MSGQ_TOKEN). Aborto.")
        return 2
    print(f"Endpoint: {s.endpoint}  DB: {s.db_path}")
    print("Conteo de despachos por anio ANTES:")
    for y, n in _year_counts(s.db_path).items():
        print(f"   {y}: {n:,}")

    QCoreApplication.instance() or QCoreApplication([])
    db = Database(s.db_path)
    # Tolera el lock si la app esta abierta a la vez (espera en vez de fallar).
    db._conn.execute("PRAGMA busy_timeout = 30000")
    src = make_source(s)

    poller = Poller(s, db)
    poller.status.connect(lambda m: print("  .", m))

    async def _run() -> None:
        # _sync_movements pagina TODAS las conexiones en una pasada; si se cortara,
        # el estado reanudable permite re-ejecutar este script y continuar.
        for attempt in range(1, 6):
            n = await poller._sync_movements(src)
            done = db.get_flag(_MOVEMENTS_BACKFILL_FLAG) == "1"
            print(f"  -> pasada {attempt}: {n:,} filas procesadas, backfill_done={done}")
            if done:
                break
        await src.aclose()

    try:
        asyncio.run(_run())
    finally:
        after = _year_counts(s.db_path)
        db.close()

    print("\nConteo de despachos por anio DESPUES:")
    for y, n in after.items():
        print(f"   {y}: {n:,}")
    print(f"\n>>> Despachos 2025 ahora en la replica: {after.get('2025', 0):,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
