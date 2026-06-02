"""Constantes de dominio y configuracion de conexion del monitor FMS.

Centraliza:

  • El vocabulario del dominio AdaptIQ/AdaptFMS (estados de equipo, tipos de
    transaccion, fuentes de volumen, umbrales de contaminacion) — alineado con
    el que ya usa `Inventory_Equipment` para que ambos software hablen el mismo
    idioma (mismos textos 'In Service' / 'Out of Service' / 'Decommissioned').

  • El esquema canonico de columnas con el que viajan los DataFrames de
    Movimientos, Equipos y consolas AdaptMAC entre capas.

  • La configuracion de conexion (`Settings`), que se lee de variables de
    entorno con valores por defecto y puede sobreescribirse desde la interfaz.

La API se documenta en «AdaptIQ Customer Facing GraphQL APIs (July 2023)».
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# ===========================================================================
# Vocabulario del dominio
# ===========================================================================

# --- Estados operativos del equipo (texto exacto del FMS) ------------------
STATUS_IN    = "In Service"
STATUS_OUT   = "Out of Service"
STATUS_DECOM = "Decommissioned"

# --- Tipos de movimiento (familia de transaccion) --------------------------
KIND_DISPENSE = "DISPENSE"
KIND_DELIVERY = "DELIVERY"
KIND_TRANSFER = "TRANSFER"
MOVEMENT_KINDS = (KIND_DISPENSE, KIND_DELIVERY, KIND_TRANSFER)

# --- Tipos / modos de transaccion (campo `type`) ---------------------------
# Los modos marcados como anomalos disparan alertas (ver core/alerts.py).
# Valores exactos de los enums DispenseTransactionType / DeliveryTransactionType
# / TransferTransactionType del esquema (ojo: 'Unauthorised' no va en mayusculas).
TYPE_AUTO         = "AUTO"
TYPE_MANUAL       = "MANUAL"
TYPE_KEY_BYPASS   = "KEY_BYPASS"      # consola en modo bypass de autorizacion
TYPE_SUP_OVERRIDE = "SUP_OVERRIDE"    # anulacion de supervisor
TYPE_SPILLAGE     = "SPILLAGE"        # derrame (solo dispense)
TYPE_UNAUTHORISED = "Unauthorised"    # transaccion no autorizada

# Modos que se consideran criticos para la trazabilidad.
ANOMALOUS_TYPES = frozenset({
    TYPE_KEY_BYPASS, TYPE_SUP_OVERRIDE, TYPE_SPILLAGE, TYPE_UNAUTHORISED,
})

# --- Fuente del volumen (primario / secundario) ----------------------------
SOURCE_DOCKET = "DOCKET"
SOURCE_METER  = "METER"

# --- Umbrales de telemetria (heuristicas iniciales, ajustables) ------------
# Contaminacion ISO 4406 por micraje: a partir de estos valores se marca el
# despacho como sospechoso de calidad de combustible.
CONTAMINATION_WARN = {
    "4um":  18,   # codigo ISO en el canal de 4 micras
    "6um":  16,
    "14um": 13,
}
# Volumen acumulado (L) sobre el cual un service truck en bypass se considera
# critico — el doc menciona acumulados >24.000 L sin trazabilidad estandar.
SERVICE_TRUCK_BYPASS_VOLUME_L = 24_000.0

# Minutos sin comunicacion exitosa tras los cuales una consola AdaptMAC se
# reporta como "stale" aunque el flag `online` siga en verdadero.
ADAPTMAC_STALE_MINUTES = 30

# ===========================================================================
# Esquema canonico de columnas (contrato entre capas)
# ===========================================================================

# Un registro de movimiento aplanado (dispense / delivery / transfer).
MOVEMENT_COLS = [
    "id", "kind", "type", "status",
    "volume", "record_collected_at", "created_at", "updated_at",
    "transaction_temperature", "peak_flow_rate",
    "primary_volume_source", "secondary_volume_source",
    "max_contamination_4", "avg_contamination_4", "med_contamination_4",
    "max_contamination_6", "avg_contamination_6", "med_contamination_6",
    "max_contamination_14", "avg_contamination_14", "med_contamination_14",
    "smu_value", "smu_type", "gps_coordinates",
    "cost", "cost_centre", "rebate_amount",
    "site", "product", "tank",
    "equipment_id", "equipment_description", "equipment_status",
    "is_service_truck", "service_truck", "field_user",
]

# Un equipo (Equipment Item) — superset de lo que ya normaliza Inventory_Equipment.
EQUIPMENT_COLS = [
    "equipment_id", "internal_id", "field_id", "description", "registration_number",
    "group", "category", "status",
    "make", "model", "product",
    "is_light_vehicle", "is_pod", "is_service_truck", "is_contractor_vehicle",
    "rfid", "site", "zone", "department", "cost_centre", "project_code",
    "service_interval", "service_interval_type",
    "smu_value", "smu_type", "smu_value_date",
    "dispense_limited", "dispense_limit_period",
    "erp_reference", "order_number", "order_item", "sap_measurement_point",
    "updated_at",
]

# Una consola/hardware de campo AdaptMAC (salud de la infraestructura IoT).
ADAPTMAC_COLS = [
    "code", "description", "site", "online", "key_bypass",
    "last_successful_comms", "last_failed_comms", "updated_at",
]

# Un evento del log de auditoria, aplanado a UNA fila por atributo cambiado
# (Query.changes -> ChangeEvent -> ChangedAttribute). `event_key` es la PK
# sintetica que hace idempotente el upsert.
CHANGE_EVENT_COLS = [
    "event_key", "changed_at", "record_type", "record_id",
    "event", "whodunnit", "attribute", "before", "after",
]

# ---------------------------------------------------------------------------
# Auditoria de equipos (validado contra el tenant de Merian)
# ---------------------------------------------------------------------------
# Tipos de registro que se sincronizan del log para el analisis de flota.
CHANGE_RECORD_EQUIPMENT = "EquipmentItem"
CHANGE_RECORD_RFID      = "EquipmentRfid"   # cambios de tag RFID (atributo 'rfid')
CHANGE_RECORD_TYPES = (CHANGE_RECORD_EQUIPMENT, CHANGE_RECORD_RFID)

# Atributos clave dentro del diff de cambios.
ATTR_STATUS = "equipment_status_id"   # en EquipmentItem
ATTR_RFID   = "rfid"                  # en EquipmentRfid

# Mapa id->estado (enum INS/OUTS/DECOMM == 1/2/3, confirmado en vivo).
EQUIPMENT_STATUS_BY_ID = {
    "1": STATUS_IN, "2": STATUS_OUT, "3": STATUS_DECOM,
}

# Inicio del historico al sincronizar cambios por primera vez (sin watermark).
CHANGES_HISTORY_START = "2022-01-01T00:00:00Z"

# ===========================================================================
# Configuracion de conexion
# ===========================================================================

DEFAULT_ENDPOINT = "https://merian.veridapt.io/graphql"   # tenant Newmont Merian
DEFAULT_POLL_SECONDS = 20
DEFAULT_PAGE_SIZE = 100        # la API limita a 100 registros por pagina
DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "msgq_replica.sqlite3")


@dataclass
class Settings:
    """Parametros de conexion y del motor de polling.

    Se construye desde variables de entorno con `from_env()`, pero la interfaz
    puede modificar los campos en caliente (p. ej. pegar el token o alternar el
    modo demo) antes de arrancar el poller.
    """
    endpoint: str = DEFAULT_ENDPOINT
    token: str = ""
    poll_seconds: int = DEFAULT_POLL_SECONDS
    page_size: int = DEFAULT_PAGE_SIZE
    demo_mode: bool = True          # arranca en simulador hasta que haya token
    db_path: str = DEFAULT_DB_PATH
    request_timeout: float = 30.0
    verify_tls: bool = True
    # Sitio a consultar. La API es 'site-scoped': todo cuelga de site(id:).
    # Si site_id queda vacio, el cliente lo auto-descubre via la query `sites`
    # eligiendo aquel cuyo code/description contenga `site_match`.
    site_id: str = ""
    site_match: str = "Merian"
    # En el primer ciclo (sin watermark) solo se traen los movimientos de los
    # ultimos N dias, para no descargar todo el historico de golpe.
    initial_lookback_days: int = 7
    # Equipos y consolas son datos maestros: se refrescan cada N ciclos, no en
    # cada poll (los movimientos si van en cada ciclo).
    slow_refresh_cycles: int = 15
    # Cabeceras extra opcionales (p. ej. un proxy corporativo).
    extra_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        """Lee la configuracion del entorno (prefijo `MSGQ_`)."""
        token = os.getenv("MSGQ_TOKEN", "").strip()
        return cls(
            endpoint=os.getenv("MSGQ_ENDPOINT", DEFAULT_ENDPOINT).strip(),
            token=token,
            poll_seconds=_int_env("MSGQ_POLL_SECONDS", DEFAULT_POLL_SECONDS),
            page_size=_int_env("MSGQ_PAGE_SIZE", DEFAULT_PAGE_SIZE),
            # Sin token no se puede hablar con la API real -> modo demo.
            demo_mode=_bool_env("MSGQ_DEMO", default=not bool(token)),
            db_path=os.getenv("MSGQ_DB_PATH", DEFAULT_DB_PATH).strip(),
            verify_tls=_bool_env("MSGQ_VERIFY_TLS", default=True),
            site_id=os.getenv("MSGQ_SITE_ID", "").strip(),
            site_match=os.getenv("MSGQ_SITE", "Merian").strip(),
            initial_lookback_days=_int_env("MSGQ_LOOKBACK_DAYS", 7),
            slow_refresh_cycles=_int_env("MSGQ_SLOW_CYCLES", 15),
        )

    def auth_header(self) -> dict[str, str]:
        """Cabecera de autenticacion documentada: `Authorization: Token token=<token>`."""
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Token token={self.token}"
        headers.update(self.extra_headers)
        return headers


# ---------------------------------------------------------------------------
# Helpers de entorno
# ---------------------------------------------------------------------------

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "si"}
