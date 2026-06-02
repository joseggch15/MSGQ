"""Capa de acceso a datos del FMS.

Define el contrato `DataSource` que consume el motor de polling y una fabrica
`make_source()` que devuelve, segun la configuracion:

  • `AdaptIQClient`  — cliente GraphQL real (httpx) contra la API de AdaptIQ.
  • `SimulatorSource` — generador de datos realistas para modo demo / pruebas
                         (no requiere token ni red).

Ambas implementaciones exponen los mismos metodos `async`, de modo que el resto
del sistema es agnostico a la fuente.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from msgq.config import Settings


@runtime_checkable
class DataSource(Protocol):
    """Fuente de datos del FMS. Toda implementacion debe paginar internamente
    y devolver listas de `node` dicts crudos (tal cual los entrega GraphQL)."""

    async def fetch_movements(self, updated_from: datetime | None) -> list[dict]:
        """Movimientos (dispense/delivery/transfer) modificados desde `updated_from`."""
        ...

    async def fetch_equipment(self, updated_from: datetime | None) -> list[dict]:
        """Equipos (Equipment Items) modificados desde `updated_from`."""
        ...

    async def fetch_adaptmacs(self, updated_from: datetime | None) -> list[dict]:
        """Consolas AdaptMAC modificadas desde `updated_from`."""
        ...

    async def fetch_changes(self, record_type: str,
                            changes_from: datetime | None) -> list[dict]:
        """Eventos del log de auditoria de `record_type` desde `changes_from`."""
        ...

    async def aclose(self) -> None:
        """Libera recursos (conexiones HTTP)."""
        ...


def make_source(settings: Settings) -> DataSource:
    """Construye la fuente de datos apropiada para la configuracion dada."""
    if settings.demo_mode or not settings.token:
        from msgq.api.simulator import SimulatorSource
        return SimulatorSource(settings)
    from msgq.api.client import AdaptIQClient
    return AdaptIQClient(settings)


__all__ = ["DataSource", "make_source"]
