"""MSGQ — Monitor *near-real-time* del FMS AdaptIQ (Newmont Merian).

Paquete que ingiere datos operativos y de telemetria desde la API GraphQL de
AdaptIQ (AdaptFMS), los replica en una base SQLite local y los proyecta en un
dashboard de escritorio PySide6.

Capas (mismo espiritu que `Inventory_Equipment`):

    config        Constantes de dominio + configuracion de conexion.
    api           Cliente GraphQL async (httpx), queries y simulador offline.
    core          Modelos, transformacion JSON->DataFrame y deteccion de alertas.
    storage       Replica local en SQLite (watermark de sincronizacion + upserts).
    ingest        Motor de polling incremental (QThread) sobre `updated_from`.
    ui            Dashboard PySide6 (KPIs + tabs + tablas, refresco ciclico).
"""
from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
