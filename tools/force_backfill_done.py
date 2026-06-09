# -*- coding: utf-8 -*-
"""Intervencion manual: marca el backfill historico de movimientos como COMPLETADO.

Para que: cuando una recuperacion EXTERNA (p. ej. tools/recover_recent_dispenses.py)
ya descargo los datos faltantes, no tiene sentido que el poller INTERNO de la app
siga haciendo su propio backfill masivo en paralelo —dos escritores pesados
compiten por el lock de SQLite y saturan el I/O—. Este script pone el flag
`movements_backfill_done = 1` y limpia el progreso (`movements_backfill_state`),
de modo que al reabrir la app el poller pase a polling INCREMENTAL ligero.

IMPORTANTE: usar SOLO cuando la data historica ya este completa (este script no
descarga nada; solo cambia el estado). Reinicia la app despues para que tome efecto.

Solo toca la tabla `sync_state`. Uso:  python tools/force_backfill_done.py
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, ".")

from msgq.config import Settings, load_embedded_settings


def _set_flag(con: sqlite3.Connection, entity: str, value: str) -> None:
    con.execute(
        "INSERT INTO sync_state (entity, watermark, last_run) VALUES (?, ?, ?) "
        "ON CONFLICT(entity) DO UPDATE SET watermark=excluded.watermark, "
        "last_run=excluded.last_run",
        (entity, value, datetime.now().isoformat()),
    )


def main() -> int:
    s = load_embedded_settings() or Settings.from_env()
    path = s.db_path
    print(f"DB: {path}")
    con = sqlite3.connect(path, timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        row = con.execute(
            "SELECT watermark FROM sync_state WHERE entity='movements_backfill_done'"
        ).fetchone()
        print(f"Antes:  movements_backfill_done = {row[0] if row else '(no existe)'}")

        _set_flag(con, "movements_backfill_done", "1")
        _set_flag(con, "movements_backfill_state", "")   # sin progreso pendiente -> no reanuda
        con.commit()

        # Resumen de cobertura para confirmar que la data esta completa.
        years = con.execute(
            "SELECT substr(record_collected_at,1,4) yr, COUNT(*) "
            "FROM movements WHERE kind='DISPENSE' GROUP BY yr ORDER BY yr"
        ).fetchall()
        print("Despues: movements_backfill_done = 1, movements_backfill_state limpiado.")
        print("Despachos por anio en la replica:")
        for yr, n in years:
            print(f"   {yr}: {n:,}")
        print("\nEl poller hara SOLO polling incremental ligero (sin backfill masivo).")
        print(">>> Reinicia la app (python run.py) para que tome efecto.")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
