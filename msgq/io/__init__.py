"""Cargadores de archivos del FMS (snapshots CSV exportados desde AdaptIQ)."""
from __future__ import annotations

from msgq.io.equipment_csv import load_equipment_csv

__all__ = ["load_equipment_csv"]
