"""Documentos GraphQL para la API de AdaptIQ (AdaptFMS).

Basado en el esquema oficial «Customer Facing GraphQL APIs (July 2023)». Claves
del modelo real que estas queries respetan:

  • Todo es *site-scoped*: se entra por `site(id: ID!) { ... }`. No hay query
    top-level de movimientos ni de equipos.
  • Los movimientos son TRES conexiones distintas: `dispenses`, `deliveries`,
    `transfers` (cada una implementa la interface Movement). Cada `node` lleva
    sus campos propios + los comunes de Movement.
  • Nombres de campo en camelCase (graphql-ruby): `recordCollectedAt`,
    `updatedFrom`, `maxContamination4`, `equipmentId`, `keyBypass`, ...
  • Filtro incremental: `dispenses(filter: { updatedFrom: "ISO8601" }, first: N,
    after: "cursor")` con `pageInfo { hasNextPage endCursor }`.
  • `adaptMacs` es una LISTA en el site (no una conexion): no se pagina.

Importante: el tipo `Equipment Item` del doc NO se puede *listar* (solo aparece
como `target` de un dispense / `serviceTruck` de un transfer). Si un tenant
expone una conexion de equipos, su nombre se descubre por introspeccion
(`SITE_FIELDS_INTROSPECTION`) y la query se arma con `build_equipment_query()`.
"""
from __future__ import annotations

# --- Descubrimiento de sitios (tambien valida el token) --------------------
SITES_QUERY = "{ sites { id code description } }"

# --- Campos comunes de la interface Movement -------------------------------
_MOVEMENT_COMMON = """
        id
        volume
        uom
        recordCollectedAt
        recordCreatedAt
        recordUpdatedAt
        transactionTemperature
        peakFlowRate
        maxContamination4 avgContamination4 medContamination4
        maxContamination6 avgContamination6 medContamination6
        maxContamination14 avgContamination14 medContamination14
        cost
        rebateAmount
        gpsCoordinates
        operator
        product { code description }
        costCentre { code description }
        equipmentGroup { code description }
        equipmentCategory { code description }
        site { code description }
        adaptMac { code }
""".rstrip()

# Campos especificos por tipo de movimiento.
_DISPENSE_EXTRA = """
        status
        type
        smuValue
        smuType
        source { code name }
        target { equipmentId description status }
        fieldUser { name }
""".rstrip()

_DELIVERY_EXTRA = """
        status
        type
        volumeSource
        secondaryVolume
        secondaryVolumeSource
        docketNumber
        driver
        company
        target { code name }
""".rstrip()

_TRANSFER_EXTRA = """
        status
        type
        source { code name }
        target { code name }
        serviceTruck { equipmentId description }
""".rstrip()


def _connection_query(connection: str, node_extra: str) -> str:
    """Arma una query paginada y filtrable para una conexion de movimientos."""
    return f"""
query {connection.capitalize()}($siteId: ID!, $filter: MovementQuery, $first: Int, $after: String) {{
  site(id: $siteId) {{
    {connection}(filter: $filter, first: $first, after: $after) {{
      pageInfo {{ hasNextPage endCursor }}
      edges {{ node {{
{_MOVEMENT_COMMON}
{node_extra}
      }} }}
    }}
  }}
}}
""".strip()


DISPENSES_QUERY = _connection_query("dispenses", _DISPENSE_EXTRA)
DELIVERIES_QUERY = _connection_query("deliveries", _DELIVERY_EXTRA)
TRANSFERS_QUERY  = _connection_query("transfers", _TRANSFER_EXTRA)

# Mapeo conexion -> (query, kind canonico) que recorre el cliente.
MOVEMENT_CONNECTIONS = {
    "dispenses":  (DISPENSES_QUERY, "DISPENSE"),
    "deliveries": (DELIVERIES_QUERY, "DELIVERY"),
    "transfers":  (TRANSFERS_QUERY, "TRANSFER"),
}

# --- Consolas AdaptMAC (conexion paginada en el site) ----------------------
ADAPTMACS_QUERY = """
query AdaptMacs($siteId: ID!, $first: Int, $after: String) {
  site(id: $siteId) {
    adaptMacs(first: $first, after: $after) {
      pageInfo { hasNextPage endCursor }
      edges { node { code description erpReference keyBypass online } }
    }
  }
}
""".strip()

# --- Log de auditoria de cambios (Query.changes, top-level) ----------------
# Cada ChangeEvent trae el diff `changes` (atributo, valor antes/despues), quien
# (`whodunnit`) y cuando (`changedAt`). Filtrable por recordType y changesFrom.
CHANGES_QUERY = """
query Changes($filter: ChangeEventQuery, $first: Int, $after: String) {
  changes(filter: $filter, first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      changedAt
      recordType
      recordId
      event
      whodunnit
      changes { attribute before after }
    } }
  }
}
""".strip()

# --- Tanques del sitio (conexion paginada; registro maestro) ---------------
# Confirmado en vivo: `tanks` expone code/description/virtual/parentTank (para
# reconstruir circuitos y el Virtual Tank), capacity, product, tankType.
TANKS_QUERY = """
query Tanks($siteId: ID!, $first: Int, $after: String) {
  site(id: $siteId) {
    tanks(first: $first, after: $after) {
      pageInfo { hasNextPage endCursor }
      edges { node {
        id code description name virtual enabled capacity volumeUnit
        product { code description }
        parentTank { code }
        tankType { description }
      } }
    }
  }
}
""".strip()

# --- Reconciliacion diaria por tanque ('Detailed Reconciliation' nativo) ----
# Confirmado en vivo: una fila por tanque/dia con openingStock, closingStock,
# inflowVolume, outflowVolume y `volume` (= error de reconciliacion). Filtrable
# incremental por `filter:{updatedFrom}` (tipo MovementQuery), igual que los
# movimientos. `status` ∈ {all_ok, unconfirmed, pending}.
RECONCILIATIONS_QUERY = """
query Reconciliations($siteId: ID!, $filter: MovementQuery, $first: Int, $after: String) {
  site(id: $siteId) {
    reconciliations(filter: $filter, first: $first, after: $after) {
      pageInfo { hasNextPage endCursor }
      edges { node {
        id periodStart periodEnd
        openingStock closingStock inflowVolume outflowVolume volume
        status recordUpdatedAt
        target { code description }
        product { code description }
      } }
    }
  }
}
""".strip()

# --- Introspeccion: campos del tipo Site (para hallar conexion de equipos) -
SITE_FIELDS_INTROSPECTION = '{ __type(name: "Site") { fields { name } } }'

# Nombres candidatos para la conexion/lista de equipos (segun tenant).
EQUIPMENT_FIELD_CANDIDATES = (
    "equipmentItems", "equipment_items", "equipments", "equipment",
)


def build_equipment_query(field_name: str) -> str:
    """Arma la query de equipos para el nombre de campo descubierto en el Site.

    Selecciona los campos documentados de Equipment Item. Asume conexion
    paginada (lo habitual en este esquema); si el tenant lo expone como lista
    simple, el cliente lo maneja por el error de `pageInfo`.
    """
    return f"""
query EquipmentItems($siteId: ID!, $first: Int, $after: String) {{
  site(id: $siteId) {{
    {field_name}(first: $first, after: $after) {{
      pageInfo {{ hasNextPage endCursor }}
      edges {{ node {{
        id
        equipmentId
        fieldId
        description
        fieldDescription
        status
        make
        model
        division
        contractor
        isLightVehicle
        isContractorVehicle
        isRebateEligible
        dispenseLimited
        dispenseLimitPeriod
        serviceInterval
        serviceIntervalType
        smuValueSource
        rfidTags
        projectCode
        sap
        orderNumber
        orderItem
        erpReference
        gpsCoordinates
        volumeUnit
        expiryDate
        lastChangedAt
        consumptionTanks {{ id sfl product {{ code description }} }}
        equipmentGroup {{ code description }}
        equipmentCategory {{ code description }}
        costCentre {{ code description }}
        department {{ code description }}
      }} }}
    }}
  }}
}}
""".strip()
