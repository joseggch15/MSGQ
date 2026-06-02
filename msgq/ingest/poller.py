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

        every = max(1, self._settings.slow_refresh_cycles)
        if cycle_index % every == 0:
            stats["equipment"] = await self._sync_master(
                source, "equipment", "fetch_equipment", transform.equipment_to_df)
            stats["adaptmac"] = await self._sync_master(
                source, "adaptmac", "fetch_adaptmacs", transform.adaptmacs_to_df)
            stats["changes"] = await self._sync_changes(source)
        return stats

    async def _sync_changes(self, source) -> int:
        """Trae el log de auditoria de equipos/RFID (semi-maestro). Primer run:
        historico completo; luego incremental por watermark sobre `changed_at`."""
        watermark = self._db.get_watermark("change_events")
        if watermark:
            changes_from = watermark - _WATERMARK_EPSILON
        else:
            changes_from = datetime.fromisoformat(
                config.CHANGES_HISTORY_START.replace("Z", ""))

        total = 0
        max_ts: datetime | None = None
        for record_type in config.CHANGE_RECORD_TYPES:
            nodes = await source.fetch_changes(record_type, changes_from)
            df = transform.change_events_to_df(nodes)
            total += self._db.upsert("change_events", df)
            ts = self._max_ts(df, "changed_at")
            if ts is not None:
                max_ts = ts if max_ts is None else max(max_ts, ts)
        if max_ts is not None:
            self._db.set_watermark(
                "change_events", max(max_ts, watermark) if watermark else max_ts)
        return total

    async def _sync_movements(self, source) -> int:
        """Trae movimientos creados/modificados desde el watermark (o desde la
        ventana inicial si es el primer arranque), y actualiza el watermark."""
        watermark = self._db.get_watermark("movements")
        if watermark:
            updated_from = watermark - _WATERMARK_EPSILON
        else:
            updated_from = datetime.now() - timedelta(
                days=self._settings.initial_lookback_days)

        df = transform.movements_to_df(await source.fetch_movements(updated_from))
        n = self._db.upsert("movements", df)
        new_wm = self._max_updated(df)
        if new_wm is not None:
            self._db.set_watermark(
                "movements", max(new_wm, watermark) if watermark else new_wm)
        return n

    async def _sync_master(self, source, entity: str, method: str, to_df) -> int:
        """Refresco completo de un dato maestro (equipos / consolas)."""
        df = to_df(await getattr(source, method)(None))
        n = self._db.upsert(entity, df)
        new_wm = self._max_updated(df)
        if new_wm is not None:
            wm = self._db.get_watermark(entity)
            self._db.set_watermark(entity, max(new_wm, wm) if wm else new_wm)
        return n

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
