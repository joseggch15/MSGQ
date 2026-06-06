"""Motor de polling incremental sobre la API del FMS.

La API de AdaptIQ no expone GraphQL Subscriptions (push por WebSocket), asi que
el 'tiempo real' se aproxima con polling inteligente: cada `poll_seconds` se
piden unicamente los registros modificados desde el ultimo `updated_from`
(watermark) almacenado, se transforman y se hace upsert en la replica SQLite.

Se implementa como `QThread` (mismo enfoque de hilos que `_BackgroundWorker`
en los reportes) que mantiene UN event loop asyncio vivo durante toda su vida,
de modo que el cliente httpx asincrono se reutiliza entre ciclos. La interfaz
se entera de los avances por señales Qt y nunca habla con la API directamente.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta

import pandas as pd
from PySide6.QtCore import QThread, Signal

from msgq import config
from msgq.api import make_source
from msgq.config import Settings
from msgq.core import transform
from msgq.storage.db import Database

# Pequeño solapamiento al usar el watermark como `updated_from`, para no perder
# registros con timestamp en el limite exacto (el upsert los hace idempotentes).
_WATERMARK_EPSILON = timedelta(seconds=1)

# Primer arranque: ventana de reconciliaciones a traer (luego es incremental).
_RECON_INITIAL_DAYS = 365

# Marca persistente de que el backfill historico de movimientos ya se completo.
# Mientras no exista, el poller reconstruye TODO el historial desde
# `MOVEMENTS_HISTORY_START`, aunque ya haya un watermark de una sincronizacion
# anterior de ventana corta (asi una replica creada con la logica vieja de 7 dias
# tambien recupera el historial completo). Ver `_sync_movements`.
_MOVEMENTS_BACKFILL_FLAG = "movements_backfill_done"

# Version del esquema de movimientos. Al ampliarlo (campos de hardware en v2;
# `secondary_volume` —volumen digitado de la guia, para auditar desviaciones de
# entrega— en v3) se fuerza UN re-backfill para poblar los campos nuevos en el
# historico ya replicado. Subir este numero re-dispara la carga.
_MOVEMENTS_SCHEMA_FLAG = "movements_schema_version"
_MOVEMENTS_SCHEMA_VERSION = "3"


class Poller(QThread):
    """Hilo de sincronizacion continua FMS -> SQLite."""

    # Emitida al cerrar cada ciclo con estadisticas (filas por entidad, etc.).
    cycle_completed = Signal(dict)
    # Mensajes de estado legibles para la barra inferior.
    status = Signal(str)
    # Error recuperable (red/token/GraphQL): el hilo sigue intentando.
    failed = Signal(str)

    def __init__(self, settings: Settings, database: Database):
        super().__init__()
        self._settings = settings
        self._db = database
        self._stop_event = threading.Event()

    # -- control -----------------------------------------------------------

    def stop(self) -> None:
        """Solicita la detencion ordenada y despierta la espera."""
        self._stop_event.set()

    # -- bucle principal ---------------------------------------------------

    def run(self) -> None:  # noqa: D401 - QThread entrypoint
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        source = make_source(self._settings)
        mode = "DEMO (simulador)" if self._settings.demo_mode else "API AdaptIQ"
        self.status.emit(f"Polling iniciado — fuente: {mode}, cada {self._settings.poll_seconds}s")

        cycle_index = 0
        try:
            while not self._stop_event.is_set():
                try:
                    stats = loop.run_until_complete(self._cycle(source, cycle_index))
                    self.cycle_completed.emit(stats)
                    self.status.emit(
                        "Ultima sync %s — mov:%d eq:%d mac:%d"
                        % (datetime.now().strftime("%H:%M:%S"),
                           stats.get("movements", 0), stats.get("equipment", 0),
                           stats.get("adaptmac", 0))
                    )
                except Exception as exc:  # noqa: BLE001 - errores recuperables
                    self.failed.emit(str(exc))
                cycle_index += 1
                # Espera interrumpible hasta el proximo ciclo.
                self._stop_event.wait(timeout=self._settings.poll_seconds)
        finally:
            try:
                loop.run_until_complete(source.aclose())
            except Exception:  # noqa: BLE001
                pass
            loop.close()
            self.status.emit("Polling detenido.")

    # -- un ciclo de sincronizacion ----------------------------------------

    async def _cycle(self, source, cycle_index: int) -> dict:
        """Movimientos en cada ciclo (incremental); equipos y consolas (datos
        maestros) cada `slow_refresh_cycles` ciclos, y siempre en el primero."""
        stats: dict[str, int] = {"movements": await self._sync_movements(source)}
        stats["reconciliations"] = await self._sync_reconciliations(source)

        every = max(1, self._settings.slow_refresh_cycles)
        if cycle_index % every == 0:
            eq_n, cl_n = await self._sync_equipment(source)
            stats["equipment"] = eq_n
            stats["consumption_limits"] = cl_n
            stats["rfid_history"] = self._record_rfid_history()
            stats["product_history"] = self._record_product_history()
            stats["adaptmac"] = await self._sync_master(
                source, "adaptmac", "fetch_adaptmacs", transform.adaptmacs_to_df)
            stats["tanks"] = await self._sync_master(
                source, "tanks", "fetch_tanks", transform.tanks_to_df)
            stats["changes"] = await self._sync_changes(source)
        return stats

    async def _sync_reconciliations(self, source) -> int:
        """Reconciliacion diaria por tanque, incremental por watermark sobre
        `updated_at` (primer arranque = ultimos `_RECON_INITIAL_DAYS` dias)."""
        watermark = self._db.get_watermark("reconciliations")
        if watermark:
            updated_from = watermark - _WATERMARK_EPSILON
        else:
            updated_from = datetime.now() - timedelta(days=_RECON_INITIAL_DAYS)
        df = transform.reconciliations_to_df(
            await source.fetch_reconciliations(updated_from))
        n = self._db.upsert("reconciliations", df)
        new_wm = self._max_updated(df)
        if new_wm is not None:
            self._db.set_watermark(
                "reconciliations", max(new_wm, watermark) if watermark else new_wm)
        return n

    async def _sync_changes(self, source) -> int:
        """Trae el log de auditoria de equipos/RFID (semi-maestro) de forma
        PROGRESIVA: hace upsert por pagina y reporta avance, asi los datos
        aparecen poco a poco en vez de todos al final. Primer run = historico
        completo; luego incremental por watermark sobre `changed_at`."""
        watermark = self._db.get_watermark("change_events")
        if watermark:
            changes_from = watermark - _WATERMARK_EPSILON
        else:
            changes_from = datetime.fromisoformat(
                config.CHANGES_HISTORY_START.replace("Z", ""))

        state: dict = {"rows": 0, "max_ts": None}

        def on_page(nodes: list[dict]) -> None:
            df = transform.change_events_to_df(nodes)
            self._db.upsert("change_events", df)
            state["rows"] += len(df)
            ts = self._max_ts(df, "changed_at")
            if ts is not None:
                state["max_ts"] = ts if state["max_ts"] is None else max(state["max_ts"], ts)
            if watermark is None:   # solo en el primer arranque (carga larga)
                self.status.emit(
                    f"Sincronizando historial de cambios… {state['rows']:,} eventos")

        for record_type in config.CHANGE_RECORD_TYPES:
            await source.fetch_changes_paged(record_type, changes_from, on_page)

        if state["max_ts"] is not None:
            self._db.set_watermark(
                "change_events", max(state["max_ts"], watermark) if watermark else state["max_ts"])
        return state["rows"]

    async def _sync_movements(self, source) -> int:
        """Movimientos. Incremental por watermark UNA VEZ que el backfill historico
        se completo; mientras no exista esa marca hace un backfill PROGRESIVO desde
        `MOVEMENTS_HISTORY_START`, asi el software refleja el FMS y se pueden auditar
        anomalias historicas. La marca (no solo el watermark) es lo que decide: una
        replica creada con la logica vieja (ventana corta) ya tiene watermark pero NO
        la marca, por lo que tambien reconstruye todo el historial. Upsert por pagina
        (idempotente)."""
        self._migrate_movements_schema()
        watermark = self._db.get_watermark("movements")
        backfilled = self._db.get_flag(_MOVEMENTS_BACKFILL_FLAG) == "1"
        if watermark and backfilled:
            updated_from = watermark - _WATERMARK_EPSILON
            df = transform.movements_to_df(await source.fetch_movements(updated_from))
            n = self._db.upsert("movements", df)
            new_wm = self._max_updated(df)
            if new_wm is not None:
                self._db.set_watermark("movements", max(new_wm, watermark))
            return n

        # Backfill historico completo (carga inicial larga, 1 sola vez): primer
        # arranque, o replica previa sin la marca de backfill.
        history_from = datetime.fromisoformat(
            config.MOVEMENTS_HISTORY_START.replace("Z", ""))
        state: dict = {"rows": 0, "max_ts": None}

        def on_page(nodes: list[dict]) -> None:
            df = transform.movements_to_df(nodes)
            self._db.upsert("movements", df)
            state["rows"] += len(df)
            ts = self._max_ts(df, "updated_at")
            if ts is not None:
                state["max_ts"] = ts if state["max_ts"] is None else max(state["max_ts"], ts)
            self.status.emit(
                f"Sincronizando historial de movimientos… {state['rows']:,}")

        await source.fetch_movements_paged(history_from, on_page)
        if state["max_ts"] is not None:
            self._db.set_watermark(
                "movements", max(state["max_ts"], watermark) if watermark else state["max_ts"])
        self._db.set_flag(_MOVEMENTS_BACKFILL_FLAG, "1")
        return state["rows"]

    def _migrate_movements_schema(self) -> None:
        """Migracion unica al ampliar el esquema de movimientos con los campos de
        hardware: fuerza UN re-backfill para poblarlos en el historico. Idempotente
        (se guarda la version aplicada; corre una sola vez por replica)."""
        if self._db.get_flag(_MOVEMENTS_SCHEMA_FLAG) == _MOVEMENTS_SCHEMA_VERSION:
            return
        self._db.set_flag(_MOVEMENTS_BACKFILL_FLAG, "0")   # invalida el backfill -> re-descarga
        self._db.set_flag(_MOVEMENTS_SCHEMA_FLAG, _MOVEMENTS_SCHEMA_VERSION)
        self.status.emit(
            "Esquema de movimientos actualizado: re-descargando historial para "
            "poblar los campos de hardware (medidor/caudal/SMU)…")

    async def _sync_equipment(self, source) -> tuple[int, int]:
        """Refresca el maestro de equipos y, de los MISMOS nodes (una sola query),
        el limite de combustible por equipo/producto (Safe Fill Level, de
        `consumptionTanks`). Devuelve (filas_equipo, filas_limite)."""
        nodes = await source.fetch_equipment(None)
        eq_df = transform.equipment_to_df(nodes)
        n_eq = self._db.upsert("equipment", eq_df)
        new_wm = self._max_updated(eq_df)
        if new_wm is not None:
            wm = self._db.get_watermark("equipment")
            self._db.set_watermark("equipment", max(new_wm, wm) if wm else new_wm)
        n_cl = self._db.upsert(
            "consumption_limits", transform.consumption_limits_to_df(nodes))
        return n_eq, n_cl

    async def _sync_master(self, source, entity: str, method: str, to_df) -> int:
        """Refresco completo de un dato maestro (consolas / tanques)."""
        df = to_df(await getattr(source, method)(None))
        n = self._db.upsert(entity, df)
        new_wm = self._max_updated(df)
        if new_wm is not None:
            wm = self._db.get_watermark(entity)
            self._db.set_watermark(entity, max(new_wm, wm) if wm else new_wm)
        return n

    def _record_rfid_history(self) -> int:
        """Observa el maestro vigente y acumula el historial tag->equipo. Permite
        al modulo de inventario de tags resolver a que equipo pertenecia un tag
        aunque luego se remueva/reemplace (el API no expone ese vinculo). Cada tag
        actual se reinserta con `last_seen=ahora`; los tags ya removidos conservan
        su ultima observacion (no se vuelven a ver, no se borran)."""
        eq = self._db.get_equipment()
        df = transform.rfid_assignments_df(eq, datetime.now())
        return self._db.upsert("rfid_history", df)

    def _record_product_history(self) -> int:
        """Observa los productos HABILITADOS vigentes (consumption_limits, que el
        ciclo acaba de refrescar) y acumula el historial de habilitacion
        producto->equipo (ventanas [first_seen, last_seen]). Permite a la auditoria
        de coherencia producto<->equipo distinguir un despacho legitimo de uno con
        producto ajeno, aunque el producto se deshabilite despues (la API no expone
        ese vinculo temporal). Misma filosofia que `_record_rfid_history`."""
        limits = self._db.get_consumption_limits()
        existing = self._db.get_product_history()
        df = transform.enabled_products_df(limits, existing, datetime.now())
        return self._db.upsert("product_history", df)

    @staticmethod
    def _max_updated(df: pd.DataFrame) -> datetime | None:
        """Mayor `updated_at` del lote, como watermark del proximo ciclo."""
        return Poller._max_ts(df, "updated_at")

    @staticmethod
    def _max_ts(df: pd.DataFrame, col: str) -> datetime | None:
        """Mayor timestamp de la columna `col`, o None si no hay."""
        if df is None or df.empty or col not in df.columns:
            return None
        mx = pd.to_datetime(df[col], errors="coerce").max()
        if pd.isna(mx):
            return None
        return mx.to_pydatetime()
