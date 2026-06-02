"""Carga de snapshots CSV de equipos exportados desde AdaptIQ.

Permite poblar la replica con TODO el maestro de equipos (miles de registros)
sin necesidad de token ni red, usando el export que AdaptIQ genera desde
Equipment ▸ export. Es el mismo formato 'completo' (~53 columnas, encabezado
'Equipment ID') que ya consume el proyecto `Inventory_Equipment`.

Las columnas del CSV se mapean por NOMBRE al esquema canonico `EQUIPMENT_COLS`,
de modo que el resultado entra al mismo `Database.upsert('equipment', df)` que
usan el simulador y el cliente GraphQL. Asi, la tabla 'Equipos' del dashboard
muestra exactamente el mismo universo que AdaptIQ.
"""
from __future__ import annotations

import os

import pandas as pd

from msgq import config
from msgq.core.transform import _first_product

# Mapeo nombre-en-CSV -> nombre canonico interno. El resto de columnas del
# export (Volume Unit, IDT, NGER Group, etc.) no se usan y se ignoran.
_CSV_TO_CANONICAL: dict[str, str] = {
    "Equipment ID":                   "equipment_id",
    "Field ID":                       "field_id",
    "Description":                    "description",
    "Registration Number":            "registration_number",
    "Equipment Group Description":    "group",
    "Equipment Category Description": "category",
    "Status Description":             "status",   # texto 'In Service' / 'Out of Service'
    "Make":                           "make",
    "Model":                          "model",
    "RFID":                           "rfid",
    "Site":                           "site",
    "Zone":                           "zone",
    "Department":                     "department",
    "Cost Centre":                    "cost_centre",
    "Project Code":                   "project_code",
    "Service Interval":               "service_interval",
    "Service Interval Type":          "service_interval_type",
    "Last SMU Value":                 "smu_value",
    "Last SMU Type":                  "smu_type",
    "Last SMU Date":                  "smu_value_date",
    "Dispense Limit Period":          "dispense_limit_period",
    "ERP Reference":                  "erp_reference",
    "Order Number":                   "order_number",
    "Order Item":                     "order_item",
    "SAP Measurement Point":          "sap_measurement_point",
    "Last Capture Date":              "updated_at",
    "Is Light Vehicle?":              "is_light_vehicle",
    "Is Pod?":                        "is_pod",
    "Is Service Truck?":              "is_service_truck",
    "Is Contractor Vehicle?":         "is_contractor_vehicle",
}


def load_equipment_csv(path: str) -> pd.DataFrame:
    """Lee un export CSV de equipos de AdaptIQ y lo normaliza a EQUIPMENT_COLS.

    Raises
    ------
    ValueError  Si el archivo no existe, no es legible o no parece un export
                de equipos (sin la columna 'Equipment ID').
    """
    if not os.path.isfile(path):
        raise ValueError(f"Archivo no encontrado: {path}")

    try:
        raw = pd.read_csv(path, dtype=str, keep_default_na=False, skipinitialspace=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"No se pudo leer '{os.path.basename(path)}': {exc}") from exc

    raw.columns = [c.strip() for c in raw.columns]
    if "Equipment ID" not in raw.columns:
        raise ValueError(
            f"'{os.path.basename(path)}' no parece un export de equipos del FMS "
            "(falta la columna 'Equipment ID')."
        )

    out = pd.DataFrame(index=raw.index)
    for csv_col, canon in _CSV_TO_CANONICAL.items():
        out[canon] = raw[csv_col] if csv_col in raw.columns else pd.NA

    # Producto principal derivado de 'Enabled Products' (misma regla del ecosistema).
    if "Enabled Products" in raw.columns:
        out["product"] = raw["Enabled Products"].apply(_first_product)
    else:
        out["product"] = pd.NA

    # Booleanos del export ('true'/'false' como texto) -> bool nativo o NA.
    # astype(object) evita el dtype numpy.bool_ (mantiene True/False/NA de Python).
    for c in ("is_light_vehicle", "is_pod", "is_service_truck", "is_contractor_vehicle"):
        out[c] = out[c].map(_to_bool_or_na).astype(object)

    # 'dispense_limited' no viene como booleano: se infiere de que exista periodo.
    period = out["dispense_limit_period"].astype("string").str.strip()
    out["dispense_limited"] = period.ne("") & period.ne("nan")

    # Garantiza todas las columnas canonicas y su orden.
    for c in config.EQUIPMENT_COLS:
        if c not in out.columns:
            out[c] = pd.NA
    out = out[config.EQUIPMENT_COLS]

    # Normaliza vacios a NA en columnas de texto (no toca los booleanos).
    bool_cols = {"is_light_vehicle", "is_pod", "is_service_truck",
                 "is_contractor_vehicle", "dispense_limited"}
    text_cols = [c for c in out.columns if c not in bool_cols]
    out[text_cols] = out[text_cols].replace({"": pd.NA})

    # Descarta filas sin clave primaria.
    out = out[out["equipment_id"].notna()].reset_index(drop=True)
    return out


def _to_bool_or_na(val):
    """Convierte 'true'/'false'/'' del export a bool nativo o NA."""
    if val is None:
        return pd.NA
    s = str(val).strip().lower()
    if s in {"true", "1", "yes", "si"}:
        return True
    if s in {"false", "0", "no"}:
        return False
    return pd.NA
