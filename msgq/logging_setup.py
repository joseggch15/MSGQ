"""Configuracion central de logging para MSGQ.

Objetivo: que al correr la app (`python run.py`) TODA la actividad relevante del
backend (poller, cliente GraphQL, base de datos) y del frontend (ventanas,
refrescos, alertas) se vea en la TERMINAL en tiempo real, y ademas quede en un
archivo rotativo (`logs/msgq.log`) para diagnosticar fallos despues.

Uso: llamar `setup_logging()` UNA vez al arrancar (lo hace `run.py` y, por las
dudas, `main_window.launch`). Cada modulo obtiene su logger con
`logging.getLogger("msgq.<area>")`. El nivel se controla con la variable de
entorno `MSGQ_LOG_LEVEL` (DEBUG/INFO/WARNING; por defecto INFO).
"""
from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler

# Logger raiz del proyecto: todos los modulos cuelgan de "msgq.*".
_ROOT_NAME = "msgq"
_CONFIGURED = False

# Formato legible: hora, nivel, area (logger) y mensaje.
_FMT = "%(asctime)s %(levelname)-7s %(name)-18s | %(message)s"
_DATEFMT = "%H:%M:%S"


class _FlushStreamHandler(logging.StreamHandler):
    """StreamHandler que vacia el buffer tras CADA registro: garantiza que los logs
    aparezcan en la terminal (VS Code) al instante, aunque la app se cuelgue justo
    despues — clave para ver el ultimo mensaje antes de un bloqueo."""

    def emit(self, record):
        super().emit(record)
        try:
            self.flush()
        except Exception:  # noqa: BLE001
            pass


def setup_logging(level: str | None = None, *, log_file: str | None = None) -> logging.Logger:
    """Configura el logger raiz de MSGQ (idempotente).

    - Consola (stdout, con flush inmediato): actividad en vivo en la terminal.
    - Archivo rotativo `logs/msgq.log` (5 MB x 3): historico para diagnostico.
    - Captura de excepciones NO atendidas (hilo principal e hilos) y faulthandler:
      para que un error critico o un cuelgue queden registrados y no los trague Qt.
    """
    global _CONFIGURED
    root = logging.getLogger(_ROOT_NAME)
    if _CONFIGURED:
        return root

    # Fuerza stdout/stderr a line-buffering (que nada quede atrapado en el buffer).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)   # py3.7+
        except Exception:  # noqa: BLE001
            pass

    level_name = (level or os.getenv("MSGQ_LOG_LEVEL") or "INFO").upper()
    root.setLevel(getattr(logging, level_name, logging.INFO))
    root.propagate = False
    formatter = logging.Formatter(_FMT, _DATEFMT)

    # 1) Consola en vivo (con flush por registro).
    console = _FlushStreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # 2) Archivo rotativo (best-effort: si no se puede escribir, seguimos solo en consola).
    try:
        path = log_file or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "msgq.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fileh = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fileh.setFormatter(formatter)
        root.addHandler(fileh)
        root.info("Logging iniciado (nivel=%s) -> consola + %s", level_name, path)
    except Exception as exc:  # noqa: BLE001
        root.warning("No se pudo abrir el archivo de log (%s); solo consola.", exc)

    _install_excepthooks(root)
    try:
        faulthandler.enable()   # ante un crash/cuelgue grave, vuelca el traceback a stderr
    except Exception:  # noqa: BLE001
        pass

    _CONFIGURED = True
    return root


def _install_excepthooks(root: logging.Logger) -> None:
    """Registra en el log toda excepcion NO capturada (hilo principal e hilos),
    para que Qt no se las trague silenciosamente."""
    prev = sys.excepthook

    def _hook(exc_type, exc, tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            root.critical("Excepcion no atendida", exc_info=(exc_type, exc, tb))
        prev(exc_type, exc, tb)

    sys.excepthook = _hook

    def _thread_hook(args):
        root.critical("Excepcion no atendida en hilo %s",
                      getattr(args.thread, "name", "?"),
                      exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    try:
        threading.excepthook = _thread_hook   # py3.8+
    except Exception:  # noqa: BLE001
        pass


def get_logger(area: str) -> logging.Logger:
    """Atajo: logger de un area concreta, p. ej. get_logger('poller')."""
    return logging.getLogger(f"{_ROOT_NAME}.{area}")
