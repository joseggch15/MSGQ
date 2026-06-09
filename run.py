"""Punto de entrada del monitor FMS AdaptIQ — MSGQ.

Ejecutar:  python run.py

Variables de entorno opcionales (todas con prefijo MSGQ_):
    MSGQ_ENDPOINT     URL del endpoint GraphQL.
    MSGQ_TOKEN        token de API (si esta vacio, arranca en modo demo).
    MSGQ_POLL_SECONDS intervalo de polling en segundos (def. 20).
    MSGQ_DEMO         '1' para forzar el simulador.
    MSGQ_DB_PATH      ruta del archivo SQLite de la replica.
"""
import sys

from msgq.logging_setup import setup_logging
from msgq.ui import launch


if __name__ == "__main__":
    # Configura el logging ANTES de arrancar la UI, para que toda la actividad
    # (backend y frontend) se vea en la terminal y quede en logs/msgq.log.
    setup_logging()
    sys.exit(launch())
