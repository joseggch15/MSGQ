"""Transformacion de la respuesta GraphQL (camelCase) a DataFrames planos.

La API entrega los datos anidados (`edges` -> `node`, con sub-objetos como
`site { code description }` o `target { equipmentId ... }`) y en camelCase. Aqui
se aplanan al esquema canonico interno (snake_case) de `config`, tolerando
campos ausentes (lo que falte queda como `NA`).

Semantica por tipo de movimiento (el cliente etiqueta cada node con `kind`):
  • Dispense  -> `target` es un Equipment Item (equipmentId/description/status),
                 `source` es el tanque, hay `fieldUser`, `smuValue`/`smuType`.
  • Transfer  -> `source`/`target` son tanques, `serviceTruck` es el equipo.
  • Delivery  -> `target` es el tanque; trae `volumeSource`, `docketNumber`...
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from msgq import config

# ===========================================================================
# Helpers de navegacion / parseo
# ===========================================================================

def _dig(node: dict, *path: str) -> Any:
    """Navega un dict anidado de forma segura. Devuelve None si algo falta."""
    cur: Any = node
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _label(obj: Any) -> Any:
    """Etiqueta legible de un sub-objeto: name > description > code."""
    if not isinstance(obj, dict):
        return None
    return obj.get("name") or obj.get("description") or obj.get("code")


def _join_rfids(rfids: Any) -> Any:
    if isinstance(rfids, list):
        return ", ".join(str(r) for r in rfids if r) or pd.NA
    return rfids if rfids else pd.NA


def _first_product(enabled_products: Any) -> Any:
    """Infiere el producto principal de `enabled_products` (lista o string CSV).

    Regla del ecosistema: DIESEL -> 'Diesel'; UNLEADED/GASOL/PETROL/ULP ->
    'Unleaded Gasoline'; resto -> el nombre tal cual; vacio -> NA.
    Usado por el importador CSV (`io/equipment_csv.py`).
    """
    name = None
    if isinstance(enabled_products, list) and enabled_products:
        first = enabled_products[0]
        name = first.get("name") if isinstance(first, dict) else first
    elif isinstance(enabled_products, str):
        name = enabled_products.split("|")[0].split(":")[0]
    if not isinstance(name, str) or not name.strip():
        return pd.NA
    up = name.strip().upper()
    if "DIESEL" in up:
        return "Diesel"
    if any(k in up for k in ("UNLEADED", "GASOL", "PETROL", "ULP")):
        return "Unleaded Gasoline"
    return name.strip()


# ===========================================================================
# Aplanado por entidad (lee la forma camelCase real)
# ===========================================================================

def flatten_movement(node: dict) -> dict:
    target = node.get("target") or {}
    service_truck = node.get("serviceTruck") or {}
    # `target` es Equipment Item solo en dispenses; en transfer/delivery es Tank.
    target_equipment_id = target.get("equipmentId")
    return {
        "id":                       node.get("id"),
        "kind":                     node.get("kind"),
        "type":                     node.get("type"),
        "status":                   node.get("status"),
        "volume":                   node.get("volume"),
        "record_collected_at":      node.get("recordCollectedAt"),
        "created_at":               node.get("recordCreatedAt"),
        "updated_at":               node.get("recordUpdatedAt"),
        "transaction_temperature":  node.get("transactionTemperature"),
        "peak_flow_rate":           node.get("peakFlowRate"),
        "primary_volume_source":    node.get("volumeSource"),
        "secondary_volume_source":  node.get("secondaryVolumeSource"),
        "max_contamination_4":      node.get("maxContamination4"),
        "avg_contamination_4":      node.get("avgContamination4"),
        "med_contamination_4":      node.get("medContamination4"),
        "max_contamination_6":      node.get("maxContamination6"),
        "avg_contamination_6":      node.get("avgContamination6"),
        "med_contamination_6":      node.get("medContamination6"),
        "max_contamination_14":     node.get("maxContamination14"),
        "avg_contamination_14":     node.get("avgContamination14"),
        "med_contamination_14":     node.get("medContamination14"),
        "smu_value":                node.get("smuValue"),
        "smu_type":                 node.get("smuType"),
        "gps_coordinates":          node.get("gpsCoordinates"),
        "cost":                     node.get("cost"),
        "cost_centre":              _label(node.get("costCentre")),
        "rebate_amount":            node.get("rebateAmount"),
        "site":                     _label(node.get("site")),
        "product":                  _label(node.get("product")),
        "tank":                     _label(node.get("source")) or _label(target),
        "equipment_id":             target_equipment_id,
        "equipment_description":    target.get("description") if target_equipment_id else None,
        "equipment_status":         target.get("status"),
        "is_service_truck":         bool(service_truck) if service_truck else (False if target_equipment_id else None),
        "service_truck":            service_truck.get("equipmentId"),
        "field_user":               _dig(node, "fieldUser", "name"),
    }


def flatten_equipment(node: dict) -> dict:
    """Aplana un Equipment Item del esquema vivo de Merian (campos ricos).

    Nota: el SMU value/type no estan en EquipmentItem (viven por-movimiento);
    aqui quedan NA. `lastChangedAt` actua como `updated_at`.
    """
    return {
        "equipment_id":         node.get("equipmentId"),
        "internal_id":          node.get("id"),
        "field_id":             node.get("fieldId"),
        "description":          node.get("description"),
        "registration_number":  node.get("fieldDescription"),
        "group":                _label(node.get("equipmentGroup")),
        "category":             _label(node.get("equipmentCategory")),
        "status":               node.get("status"),
        "make":                 node.get("make"),
        "model":                node.get("model"),
        "product":              pd.NA,   # no existe en EquipmentItem (va por-movimiento)
        "is_light_vehicle":     node.get("isLightVehicle"),
        "is_pod":               pd.NA,
        "is_service_truck":     pd.NA,   # el Site expone serviceTrucks aparte
        "is_contractor_vehicle": node.get("isContractorVehicle"),
        "rfid":                 _join_rfids(node.get("rfidTags")),
        "site":                 pd.NA,
        "zone":                 pd.NA,
        "department":           _label(node.get("department")),
        "cost_centre":          _label(node.get("costCentre")),
        "project_code":         node.get("projectCode"),
        "service_interval":     node.get("serviceInterval"),
        "service_interval_type": node.get("serviceIntervalType"),
        "smu_value":            pd.NA,
        "smu_type":             pd.NA,
        "smu_value_date":       pd.NA,
        "dispense_limited":     node.get("dispenseLimited"),
        "dispense_limit_period": node.get("dispenseLimitPeriod"),
        "erp_reference":        node.get("erpReference"),
        "order_number":         node.get("orderNumber"),
        "order_item":           node.get("orderItem"),
        "sap_measurement_point": node.get("sap"),
        "updated_at":           node.get("lastChangedAt"),
    }


def flatten_tank(node: dict) -> dict:
    """Aplana un Tank del sitio. `parent_tank` (code) enlaza satelites -> virtual."""
    return {
        "tank_id":      node.get("id"),
        "code":         node.get("code"),
        "description":  node.get("description"),
        "name":         node.get("name"),
        "product":      _label(node.get("product")),
        "virtual":      node.get("virtual"),
        "capacity":     node.get("capacity"),
        "volume_unit":  node.get("volumeUnit"),
        "enabled":      node.get("enabled"),
        "parent_tank":  _dig(node, "parentTank", "code"),
        "tank_type":    _label(node.get("tankType")),
    }


def flatten_reconciliation(node: dict) -> dict:
    """Aplana una Reconciliation (diaria por tanque). `error` = campo `volume`
    de la API = (closing-opening) - (inflow-outflow). `tank` = code del target."""
    target = node.get("target") or {}
    return {
        "id":               node.get("id"),
        "period_start":     node.get("periodStart"),
        "period_end":       node.get("periodEnd"),
        "tank":             target.get("code"),
        "tank_description": target.get("description"),
        "product":          _label(node.get("product")),
        "opening_stock":    node.get("openingStock"),
        "closing_stock":    node.get("closingStock"),
        "inflow":           node.get("inflowVolume"),
        "outflow":          node.get("outflowVolume"),
        "error":            node.get("volume"),
        "status":           node.get("status"),
        "updated_at":       node.get("recordUpdatedAt"),
    }


def flatten_adaptmac(node: dict) -> dict:
    return {
        "code":                  node.get("code"),
        "description":           node.get("description"),
        "site":                  _label(node.get("site")),
        "online":                node.get("online"),
        "key_bypass":            node.get("keyBypass"),
        "last_successful_comms": node.get("lastSuccessfulComms"),
        "last_failed_comms":     node.get("lastFailedComms"),
        "updated_at":            node.get("updatedAt"),
    }


# ===========================================================================
# Construccion de DataFrames tipados
# ===========================================================================

_MOVEMENT_NUMERIC = [
    "volume", "transaction_temperature", "peak_flow_rate",
    "max_contamination_4", "avg_contamination_4", "med_contamination_4",
    "max_contamination_6", "avg_contamination_6", "med_contamination_6",
    "max_contamination_14", "avg_contamination_14", "med_contamination_14",
    "smu_value", "cost", "rebate_amount",
]
_MOVEMENT_DATETIME = ["record_collected_at", "created_at", "updated_at"]

_EQUIPMENT_NUMERIC = ["service_interval", "smu_value"]
_EQUIPMENT_DATETIME = ["smu_value_date", "updated_at"]

_ADAPTMAC_DATETIME = ["last_successful_comms", "last_failed_comms", "updated_at"]

_TANK_NUMERIC = ["capacity"]
_RECON_NUMERIC = ["opening_stock", "closing_stock", "inflow", "outflow", "error"]
_RECON_DATETIME = ["period_start", "period_end", "updated_at"]


def _build_df(rows: list[dict], columns: list[str],
              numeric: list[str] | None = None,
              datetime_cols: list[str] | None = None) -> pd.DataFrame:
    """Crea un DataFrame con el orden de columnas canonico y dtypes coercidos."""
    df = pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)
    for col in (numeric or []):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in (datetime_cols or []):
        if col in df.columns:
            # La API devuelve ISO8601 con offset (+11:00); el simulador, naive.
            # Normalizamos a UTC y quitamos tz -> datetime naive consistente.
            df[col] = pd.to_datetime(
                df[col], errors="coerce", utc=True, format="ISO8601"
            ).dt.tz_localize(None)
    return df


def movements_to_df(nodes: list[dict]) -> pd.DataFrame:
    rows = [flatten_movement(n) for n in nodes]
    return _build_df(rows, config.MOVEMENT_COLS, _MOVEMENT_NUMERIC, _MOVEMENT_DATETIME)


def equipment_to_df(nodes: list[dict]) -> pd.DataFrame:
    rows = [flatten_equipment(n) for n in nodes]
    return _build_df(rows, config.EQUIPMENT_COLS, _EQUIPMENT_NUMERIC, _EQUIPMENT_DATETIME)


def adaptmacs_to_df(nodes: list[dict]) -> pd.DataFrame:
    rows = [flatten_adaptmac(n) for n in nodes]
    return _build_df(rows, config.ADAPTMAC_COLS, None, _ADAPTMAC_DATETIME)


def tanks_to_df(nodes: list[dict]) -> pd.DataFrame:
    rows = [flatten_tank(n) for n in nodes]
    return _build_df(rows, config.TANK_COLS, _TANK_NUMERIC)


def reconciliations_to_df(nodes: list[dict]) -> pd.DataFrame:
    rows = [flatten_reconciliation(n) for n in nodes]
    return _build_df(rows, config.RECONCILIATION_COLS, _RECON_NUMERIC, _RECON_DATETIME)


def consumption_limits_to_df(nodes: list[dict]) -> pd.DataFrame:
    """Aplana los `consumptionTanks` de cada EquipmentItem a UNA fila por
    (equipo, producto) con su Safe Fill Level. `product` = description (la misma
    etiqueta que `_label` pone en el despacho, para poder cruzarlos). Descarta los
    tanques sin `sfl` (no hay limite definido -> nada que auditar)."""
    rows: list[dict] = []
    for n in nodes:
        eq_id = n.get("equipmentId")
        internal = n.get("id")
        for ct in (n.get("consumptionTanks") or []):
            if ct.get("sfl") in (None, ""):
                continue
            prod = ct.get("product") or {}
            rows.append({
                "id":           ct.get("id"),
                "equipment_id": eq_id,
                "internal_id":  internal,
                "product":      _label(prod),
                "product_code": prod.get("code") if isinstance(prod, dict) else None,
                "sfl":          ct.get("sfl"),
            })
    return _build_df(rows, config.CONSUMPTION_LIMIT_COLS, ["sfl"])


def rfid_assignments_df(equipment: pd.DataFrame, when) -> pd.DataFrame:
    """Aplana el maestro a UNA fila por (tag, equipo) para el historial de
    asignaciones RFID: `tag` (mayusculas), `equipment_id`, `internal_id`,
    `last_seen` = `when`. El campo `rfid` del maestro viene unido por ", "
    (ver `_join_rfids`); aqui se reparte en tags individuales. Vacio si el
    maestro no trae equipos o tags.
    """
    rows: list[dict] = []
    if equipment is not None and not equipment.empty and "rfid" in equipment.columns:
        ts = pd.Timestamp(when).isoformat()
        for _, e in equipment.iterrows():
            raw = e.get("rfid")
            try:
                blank = raw is None or pd.isna(raw)
            except (TypeError, ValueError):
                blank = False
            if blank:
                continue
            for tag in str(raw).split(","):
                tag = tag.strip()
                if tag and tag.lower() not in ("<na>", "nan"):
                    rows.append({
                        "tag": tag.upper(),
                        "equipment_id": e.get("equipment_id"),
                        "internal_id": e.get("internal_id"),
                        "last_seen": ts,
                    })
    return _build_df(rows, config.RFID_HISTORY_COLS, None, ["last_seen"])


# ===========================================================================
# Log de auditoria: una fila por atributo cambiado
# ===========================================================================

_CHANGE_DATETIME = ["changed_at"]


def change_events_to_df(nodes: list[dict]) -> pd.DataFrame:
    """Aplana ChangeEvents a una fila por ChangedAttribute.

    `event_key` (PK) = recordType:recordId:changedAt:attribute, para upsert
    idempotente al re-descargar ventanas solapadas.
    """
    rows: list[dict] = []
    for n in nodes:
        changed_at = n.get("changedAt")
        record_type = n.get("recordType")
        record_id = n.get("recordId")
        event = n.get("event")
        whodunnit = n.get("whodunnit")
        for ch in (n.get("changes") or []):
            attr = ch.get("attribute")
            rows.append({
                "event_key": f"{record_type}:{record_id}:{changed_at}:{attr}",
                "changed_at": changed_at,
                "record_type": record_type,
                "record_id": record_id,
                "event": event,
                "whodunnit": whodunnit,
                "attribute": attr,
                "before": ch.get("before"),
                "after": ch.get("after"),
            })
    return _build_df(rows, config.CHANGE_EVENT_COLS, None, _CHANGE_DATETIME)
