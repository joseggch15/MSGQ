"""Replica local en SQLite del estado del FMS.

Por que una base intermedia y no consultar la API desde la interfaz:

  • Desacopla la velocidad de refresco visual (1 s) de la del polling (10-30 s).
  • Da un historico local consultable aunque la API o la red caigan.
  • Permite recuperar el ultimo `updated_from` (watermark) para sincronizar de
    forma incremental, sin re-descargar todo en cada ciclo.

Concurrencia: el hilo de polling escribe y el hilo de la GUI lee. Se usa una
unica conexion con `check_same_thread=False` protegida por un `Lock`, que es
suficiente y seguro para el volumen de un sitio.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from typing import Any, Iterable

import pandas as pd

from msgq import config

# Columnas que se guardan como REAL / como fecha ISO / como booleano (0-1).
_NUMERIC = {
    "movements": {
        "volume", "secondary_volume", "transaction_temperature", "peak_flow_rate", "smu_value",
        "average_flow_rate", "flow_duration_s", "raw_smu_value", "calculated_smu_value",
        "cost", "rebate_amount",
        "max_contamination_4", "avg_contamination_4", "med_contamination_4",
        "max_contamination_6", "avg_contamination_6", "med_contamination_6",
        "max_contamination_14", "avg_contamination_14", "med_contamination_14",
    },
    "equipment": {"service_interval", "smu_value"},
    "adaptmac": set(),
    "change_events": set(),
    "tanks": {"capacity"},
    "reconciliations": {"opening_stock", "closing_stock", "inflow", "outflow", "error"},
    "rfid_history": set(),
    "consumption_limits": {"sfl"},
    "product_history": set(),
}
_DATETIME = {
    "movements": {"record_collected_at", "created_at", "updated_at"},
    "equipment": {"smu_value_date", "updated_at"},
    "adaptmac": {"last_successful_comms", "last_failed_comms", "updated_at"},
    "change_events": {"changed_at"},
    "tanks": set(),
    "reconciliations": {"period_start", "period_end", "updated_at"},
    "rfid_history": {"last_seen"},
    "consumption_limits": set(),
    "product_history": {"first_seen", "last_seen"},
}
_BOOL = {
    "movements": {"is_service_truck"},
    "equipment": {"is_light_vehicle", "is_pod", "is_service_truck",
                  "is_contractor_vehicle", "dispense_limited"},
    "adaptmac": {"online", "key_bypass"},
    "change_events": set(),
    "tanks": {"virtual", "enabled"},
    "reconciliations": set(),
    "rfid_history": set(),
    "consumption_limits": set(),
    "product_history": set(),
}

# Metadatos por entidad: (tabla, columnas, clave primaria).
_ENTITIES = {
    "movements": (config.MOVEMENT_COLS, "id"),
    "equipment": (config.EQUIPMENT_COLS, "equipment_id"),
    "adaptmac":  (config.ADAPTMAC_COLS, "code"),
    "change_events": (config.CHANGE_EVENT_COLS, "event_key"),
    "tanks": (config.TANK_COLS, "tank_id"),
    "reconciliations": (config.RECONCILIATION_COLS, "id"),
    "rfid_history": (config.RFID_HISTORY_COLS, "tag"),
    "consumption_limits": (config.CONSUMPTION_LIMIT_COLS, "id"),
    "product_history": (config.PRODUCT_HISTORY_COLS, "key"),
}

# Columna temporal de cada entidad (para indice y watermark). None = sin tiempo.
_TS_COL = {
    "movements": "updated_at", "equipment": "updated_at",
    "adaptmac": "updated_at", "change_events": "changed_at",
    "tanks": None, "reconciliations": "updated_at",
    "rfid_history": "last_seen", "consumption_limits": None,
    "product_history": "last_seen",
}


class Database:
    """Acceso a la replica SQLite. Crea el esquema si no existe."""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_schema()

    @property
    def path(self) -> str:
        return self._path

    # -- esquema ------------------------------------------------------------

    def _sql_type(self, entity: str, col: str) -> str:
        if col in _NUMERIC[entity]:
            return "REAL"
        if col in _BOOL[entity]:
            return "INTEGER"
        return "TEXT"

    def _create_schema(self) -> None:
        with self._lock, self._conn:
            for entity, (cols, pk) in _ENTITIES.items():
                defs = []
                for c in cols:
                    pk_suffix = " PRIMARY KEY" if c == pk else ""
                    defs.append(f'"{c}" {self._sql_type(entity, c)}{pk_suffix}')
                self._conn.execute(
                    f'CREATE TABLE IF NOT EXISTS {entity} ({", ".join(defs)})'
                )
                # Migracion suave: agrega columnas canonicas que falten en una
                # tabla preexistente (permite evolucionar el esquema sin borrar
                # la replica).
                existing = {
                    row["name"]
                    for row in self._conn.execute(f'PRAGMA table_info("{entity}")')
                }
                for c in cols:
                    if c not in existing:
                        self._conn.execute(
                            f'ALTER TABLE {entity} ADD COLUMN "{c}" '
                            f'{self._sql_type(entity, c)}'
                        )
                ts_col = _TS_COL.get(entity)
                if ts_col and ts_col in cols:
                    self._conn.execute(
                        f'CREATE INDEX IF NOT EXISTS idx_{entity}_ts '
                        f'ON {entity} ("{ts_col}")'
                    )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS sync_state ("
                "entity TEXT PRIMARY KEY, watermark TEXT, last_run TEXT)"
            )

    # -- escritura (upsert) -------------------------------------------------

    def upsert(self, entity: str, df: pd.DataFrame) -> int:
        """Inserta o reemplaza filas por clave primaria. Devuelve cuantas filas."""
        if df is None or df.empty:
            return 0
        cols, _pk = _ENTITIES[entity]
        records = list(self._df_to_records(entity, df, cols))
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(f'"{c}"' for c in cols)
        sql = f"INSERT OR REPLACE INTO {entity} ({col_list}) VALUES ({placeholders})"
        with self._lock, self._conn:
            self._conn.executemany(sql, records)
        return len(records)

    def _df_to_records(self, entity: str, df: pd.DataFrame,
                       cols: list[str]) -> Iterable[tuple]:
        bool_cols = _BOOL[entity]
        for _, row in df.iterrows():
            out: list[Any] = []
            for c in cols:
                val = row.get(c) if c in df.columns else None
                out.append(_to_sqlite(val, c in bool_cols))
            yield tuple(out)

    # -- watermark de sincronizacion ---------------------------------------

    def get_watermark(self, entity: str) -> datetime | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT watermark FROM sync_state WHERE entity = ?", (entity,)
            )
            row = cur.fetchone()
        if row and row["watermark"]:
            try:
                return datetime.fromisoformat(row["watermark"])
            except ValueError:
                return None
        return None

    def set_watermark(self, entity: str, watermark: datetime | None) -> None:
        wm = watermark.isoformat() if watermark else None
        now = datetime.now().isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sync_state (entity, watermark, last_run) "
                "VALUES (?, ?, ?) ON CONFLICT(entity) DO UPDATE SET "
                "watermark = COALESCE(excluded.watermark, sync_state.watermark), "
                "last_run = excluded.last_run",
                (entity, wm, now),
            )

    # -- banderas persistentes (one-shot) ----------------------------------
    # Marcadores de progreso que NO son timestamps (p. ej. "el backfill historico
    # de movimientos ya se completo"). Se guardan en la misma tabla `sync_state`
    # bajo una `entity` propia que no colisiona con las entidades reales; el valor
    # va en la columna `watermark` como texto libre.

    def get_flag(self, name: str) -> str | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT watermark FROM sync_state WHERE entity = ?", (name,))
            row = cur.fetchone()
        return row["watermark"] if row and row["watermark"] is not None else None

    def set_flag(self, name: str, value: str) -> None:
        now = datetime.now().isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sync_state (entity, watermark, last_run) "
                "VALUES (?, ?, ?) ON CONFLICT(entity) DO UPDATE SET "
                "watermark = excluded.watermark, last_run = excluded.last_run",
                (name, value, now),
            )

    # -- lectura ------------------------------------------------------------

    def read(self, entity: str, where: str = "", params: tuple = (),
             order_by: str = "", limit: int | None = None) -> pd.DataFrame:
        cols, _pk = _ENTITIES[entity]
        sql = f"SELECT * FROM {entity}"
        if where:
            sql += f" WHERE {where}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
        return self._records_to_df(entity, rows, cols)

    def get_movements(self, limit: int = 500) -> pd.DataFrame:
        return self.read("movements", order_by='"updated_at" DESC', limit=limit)

    def recent_movements(self, hours: int = 24) -> pd.DataFrame:
        cutoff = (pd.Timestamp.now() - pd.Timedelta(hours=hours)).isoformat()
        return self.read("movements", where='"updated_at" >= ?',
                         params=(cutoff,), order_by='"updated_at" DESC')

    def get_equipment(self) -> pd.DataFrame:
        return self.read("equipment", order_by='"equipment_id"')

    def get_adaptmac(self) -> pd.DataFrame:
        return self.read("adaptmac", order_by='"code"')

    def get_change_events(self, record_type: str | None = None) -> pd.DataFrame:
        if record_type:
            return self.read("change_events", where='"record_type" = ?',
                             params=(record_type,), order_by='"changed_at" DESC')
        return self.read("change_events", order_by='"changed_at" DESC')

    def get_tanks(self) -> pd.DataFrame:
        return self.read("tanks", order_by='"code"')

    def get_reconciliations(self) -> pd.DataFrame:
        return self.read("reconciliations", order_by='"period_end" DESC')

    def get_rfid_history(self) -> pd.DataFrame:
        return self.read("rfid_history", order_by='"tag"')

    def get_consumption_limits(self) -> pd.DataFrame:
        return self.read("consumption_limits", order_by='"equipment_id"')

    def get_product_history(self) -> pd.DataFrame:
        return self.read("product_history", order_by='"equipment_id"')

    def row_count(self, entity: str) -> int:
        with self._lock:
            cur = self._conn.execute(f"SELECT COUNT(*) AS n FROM {entity}")
            return int(cur.fetchone()["n"])

    def purge_simulator_movements(self) -> int:
        """Borra movimientos del SIMULADOR (id 'SIM-%') que pudieran haber quedado
        en un replica de PRODUCCION (p. ej. tras correr el modo demo sobre el mismo
        archivo). Evita falsos positivos al auditarlos contra SFL reales. Devuelve
        cuantas filas borro."""
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM movements WHERE id LIKE 'SIM-%'")
            return max(0, cur.rowcount or 0)

    def _records_to_df(self, entity: str, rows: list[dict],
                       cols: list[str]) -> pd.DataFrame:
        df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
        for c in _NUMERIC[entity]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        for c in _DATETIME[entity]:
            if c in df.columns:
                df[c] = pd.to_datetime(df[c], errors="coerce")
        for c in _BOOL[entity]:
            if c in df.columns:
                # astype(object) preserva bool de Python (no numpy.bool_), para
                # que la tabla los formatee como 'Si'/'No' y no 'True'/'False'.
                df[c] = df[c].map(_to_bool).astype(object)
        return df

    # -- cierre -------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ---------------------------------------------------------------------------
# Conversiones de valores
# ---------------------------------------------------------------------------

def _to_sqlite(val: Any, is_bool: bool) -> Any:
    """Normaliza un valor de pandas a algo que SQLite acepte."""
    if val is None:
        return None
    # NaN / NaT / NA
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(val, pd.Timestamp):
        return val.isoformat()
    if is_bool or isinstance(val, bool):
        if isinstance(val, str):
            return 1 if val.strip().lower() in {"true", "1", "yes", "si"} else 0
        return 1 if bool(val) else 0
    return val


def _to_bool(val: Any) -> Any:
    if val is None:
        return pd.NA
    try:
        if pd.isna(val):
            return pd.NA
    except (TypeError, ValueError):
        pass
    if isinstance(val, str):
        return val.strip().lower() in {"true", "1", "yes", "si"}
    return bool(val)
