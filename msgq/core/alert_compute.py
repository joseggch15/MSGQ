"""Calculo de las alertas PESADAS, aislado para correr en OTRO PROCESO.

Por que un modulo aparte (sin Qt): las alertas que recorren todo el historico
(burn rate, salud de hardware, coherencia producto<->equipo, desviacion de
volumen y tag hopping) son CPU-bound y de Python puro. Un QThread no las
paraleliza —el GIL hace que el hilo de la GUI se quede sin CPU y la interfaz se
congele igual—. La solucion correcta es ejecutarlas en un PROCESO separado (con
su propio GIL), via `concurrent.futures.ProcessPoolExecutor`. Para que el proceso
hijo (spawn en Windows) sea liviano y picklable, este modulo NO importa PySide6:
solo pandas + la capa de dominio. Recibe la RUTA de la replica, lee por su cuenta
(conexion de solo lectura, WAL) y devuelve los DataFrames de alertas (pequenos).
"""
from __future__ import annotations

import pandas as pd


def _safe(fn, *args) -> pd.DataFrame:
    """Ejecuta un detector devolviendo alertas vacias si algo falla (un calculo
    pesado no debe tumbar el proceso ni dejar la GUI sin respuesta)."""
    from msgq.core import alerts as al
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001
        return al._empty_alerts()


def compute_heavy_alerts(db_path: str) -> dict:
    """Lee la replica y calcula TODAS las alertas pesadas. Devuelve un dict con los
    DataFrames de alertas + el conteo (movimientos, cambios) usado, para que la GUI
    sepa con que estado se calcularon. Pensado para correr en un proceso aparte:
    solo se transfiere `db_path` al hijo y vuelven DataFrames de alertas (chicos)."""
    from msgq.core import alerts as al
    from msgq.storage.db import Database

    rdb = Database(db_path, create=False)
    try:
        mv = rdb.read("movements")
        eq = rdb.get_equipment()
        changes = rdb.get_change_events()
        limits = rdb.get_consumption_limits()
        prod_hist = rdb.get_product_history()
    finally:
        rdb.close()

    return {
        "burn":    _safe(al.detect_burn_rate_alerts, mv, eq),
        "hw":      _safe(al.detect_hardware_alerts, mv, eq, changes),
        "product": _safe(al.detect_product_mismatch_alerts, mv, limits, prod_hist),
        "vd":      _safe(al.detect_volume_deviation_alerts, mv),
        "th":      _safe(al.detect_tag_hopping_alerts, mv, eq),
        "counts":  (len(mv), len(changes)),
    }
