"""Internacionalización (ES/EN) del monitor MSGQ.

Modelo: el idioma **canónico** del código es el español para las cadenas de
interfaz y los encabezados analíticos; las columnas de datos crudos viajan en
`snake_case` (su nombre real en el DataFrame). La traducción se aplica solo en la
frontera de presentación (tablas, gráficas, Excel), nunca cambia las claves ni
los valores con los que opera la lógica.

API:
  • `set_language(lang)` / `current_language()` — estado global del idioma.
  • `t(s)`        — traduce una cadena de interfaz o un encabezado de columna.
  • `tr_value(s)` — traduce un valor de celda SOLO si está en la lista blanca de
                    tokens conocidos (placeholders, estados de auditoría, etc.);
                    así nunca traduce datos reales (IDs, descripciones…).
  • `tr_fmt(key, **kw)` — plantillas con interpolación (detalles de alertas, etc.).

Los estados del FMS ('In Service' / 'Out of Service' / 'Decommissioned') NO se
traducen: son el vocabulario exacto de AdaptIQ y aparecen también como valores
crudos de los datos.
"""
from __future__ import annotations

LANGUAGES: tuple[tuple[str, str], ...] = (("es", "Español"), ("en", "English"))
_VALID = {code for code, _ in LANGUAGES}
_DEFAULT = "es"
_lang = _DEFAULT


def set_language(lang: str) -> None:
    global _lang
    _lang = lang if lang in _VALID else _DEFAULT


def current_language() -> str:
    return _lang


# ===========================================================================
# Columnas de datos crudos: snake_case -> (español, inglés)
# ===========================================================================
_RAW: dict[str, tuple[str, str]] = {
    # --- comunes / movimientos ---
    "id": ("ID", "ID"),
    "kind": ("Tipo mov.", "Kind"),
    "type": ("Modo", "Type"),
    "status": ("Estado", "Status"),
    "volume": ("Volumen", "Volume"),
    "record_collected_at": ("Recolectado", "Collected at"),
    "created_at": ("Creado", "Created at"),
    "updated_at": ("Actualizado", "Updated at"),
    "transaction_temperature": ("Temp. transacción", "Transaction temp."),
    "peak_flow_rate": ("Flujo pico", "Peak flow rate"),
    "primary_volume_source": ("Fuente vol. primaria", "Primary volume source"),
    "secondary_volume_source": ("Fuente vol. secundaria", "Secondary volume source"),
    "max_contamination_4": ("Contam. máx 4µm", "Max contamination 4µm"),
    "avg_contamination_4": ("Contam. prom 4µm", "Avg contamination 4µm"),
    "med_contamination_4": ("Contam. med 4µm", "Med contamination 4µm"),
    "max_contamination_6": ("Contam. máx 6µm", "Max contamination 6µm"),
    "avg_contamination_6": ("Contam. prom 6µm", "Avg contamination 6µm"),
    "med_contamination_6": ("Contam. med 6µm", "Med contamination 6µm"),
    "max_contamination_14": ("Contam. máx 14µm", "Max contamination 14µm"),
    "avg_contamination_14": ("Contam. prom 14µm", "Avg contamination 14µm"),
    "med_contamination_14": ("Contam. med 14µm", "Med contamination 14µm"),
    "smu_value": ("Valor SMU", "SMU value"),
    "smu_type": ("Tipo SMU", "SMU type"),
    "smu_value_date": ("Fecha SMU", "SMU date"),
    "gps_coordinates": ("Coordenadas GPS", "GPS coordinates"),
    "cost": ("Costo", "Cost"),
    "cost_centre": ("Centro de costo", "Cost centre"),
    "rebate_amount": ("Descuento", "Rebate amount"),
    "site": ("Sitio", "Site"),
    "product": ("Producto", "Product"),
    "tank": ("Tanque", "Tank"),
    "equipment_id": ("ID equipo", "Equipment ID"),
    "equipment_description": ("Descripción equipo", "Equipment description"),
    "equipment_status": ("Estado equipo", "Equipment status"),
    "is_service_truck": ("Es camión servicio", "Is service truck"),
    "service_truck": ("Camión de servicio", "Service truck"),
    "field_user": ("Usuario campo", "Field user"),
    # --- equipos ---
    "internal_id": ("ID interno", "Internal ID"),
    "field_id": ("Field ID", "Field ID"),
    "description": ("Descripción", "Description"),
    "registration_number": ("Matrícula", "Registration"),
    "group": ("Grupo", "Group"),
    "category": ("Categoría", "Category"),
    "make": ("Marca", "Make"),
    "model": ("Modelo", "Model"),
    "is_light_vehicle": ("Vehículo ligero", "Light vehicle"),
    "is_pod": ("Es pod", "Is pod"),
    "is_contractor_vehicle": ("Es contratista", "Is contractor"),
    "rfid": ("RFID", "RFID"),
    "zone": ("Zona", "Zone"),
    "department": ("Departamento", "Department"),
    "project_code": ("Código proyecto", "Project code"),
    "service_interval": ("Intervalo servicio", "Service interval"),
    "service_interval_type": ("Tipo intervalo", "Interval type"),
    "dispense_limited": ("Despacho limitado", "Dispense limited"),
    "dispense_limit_period": ("Periodo límite", "Limit period"),
    "erp_reference": ("Ref ERP", "ERP reference"),
    "order_number": ("Número orden", "Order number"),
    "order_item": ("Ítem orden", "Order item"),
    "sap_measurement_point": ("Punto medición SAP", "SAP measurement point"),
    # --- consolas AdaptMAC ---
    "code": ("Código", "Code"),
    "online": ("En línea", "Online"),
    "key_bypass": ("Bypass de llave", "Key bypass"),
    "last_successful_comms": ("Última comm. exitosa", "Last successful comms"),
    "last_failed_comms": ("Última comm. fallida", "Last failed comms"),
    # --- log de auditoría / alertas ---
    "record_id": ("ID registro", "Record ID"),
    "record_type": ("Tipo registro", "Record type"),
    "whodunnit": ("Usuario", "User"),
    "changed_at": ("Fecha cambio", "Changed at"),
    "event": ("Evento", "Event"),
    "event_key": ("Clave evento", "Event key"),
    "attribute": ("Atributo", "Attribute"),
    "before": ("Antes", "Before"),
    "after": ("Después", "After"),
    "timestamp": ("Fecha/hora", "Timestamp"),
    "severity": ("Severidad", "Severity"),
    "detail": ("Detalle", "Detail"),
    "source_id": ("ID origen", "Source ID"),
}

_ES: dict[str, str] = {k: es for k, (es, _en) in _RAW.items()}
_EN: dict[str, str] = {k: en for k, (_es, en) in _RAW.items()}

# Severidades de alerta (enum canónico en inglés): forma en español.
_ES.update({"CRITICAL": "CRÍTICO", "WARNING": "ADVERTENCIA", "INFO": "INFO"})

# ===========================================================================
# Cadenas de interfaz y encabezados analíticos: canónico ES -> inglés
# (en español, `t()` devuelve la propia clave; solo se necesita el inglés)
# ===========================================================================
_EN.update({
    # --- títulos de ventana / branding ---
    "MSGQ — Monitor FMS AdaptIQ  ·  Newmont Merian":
        "MSGQ — AdaptIQ FMS Monitor  ·  Newmont Merian",
    "MSGQ — Análisis de Equipos  ·  Newmont Merian":
        "MSGQ — Equipment Analysis  ·  Newmont Merian",
    # --- ventana principal: conexión ---
    "Conexion y motor de polling": "Connection & polling engine",
    "Endpoint GraphQL:": "GraphQL endpoint:",
    "Token:": "Token:",
    "Intervalo (s):": "Interval (s):",
    "Site:": "Site:",
    "ID del sitio, o un texto del nombre (p. ej. 'Merian') para "
    "auto-descubrirlo via la query 'sites'.":
        "Site ID, or part of its name (e.g. 'Merian') to auto-discover it "
        "via the 'sites' query.",
    "Modo demo (simulador, sin red)": "Demo mode (simulator, no network)",
    "Iniciar monitoreo": "Start monitoring",
    "Detener": "Stop",
    "Importar equipos (CSV de AdaptIQ)…": "Import equipment (AdaptIQ CSV)…",
    "Carga el maestro completo de equipos desde un export CSV "
    "(no requiere token ni red).":
        "Loads the full equipment master from a CSV export "
        "(no token or network required).",
    "Analizar equipos…": "Analyze equipment…",
    "Abre el análisis de flota: filtros, frecuencia de cambio de RFID, "
    "transiciones In↔Out, auditoría y gráficas.":
        "Opens the fleet analysis: filters, RFID change frequency, "
        "In↔Out transitions, audit and charts.",
    "Monitoreo en tiempo real": "Real-time monitoring",
    "EN VIVO": "LIVE",
    "actualiza cada": "updates every",
    "Sin datos todavia.": "No data yet.",
    # --- pestañas / chrome general ---
    "Movimientos": "Movements",
    "Equipos": "Equipment",
    "Consolas AdaptMAC": "AdaptMAC consoles",
    "Alertas": "Alerts",
    "Todas": "All",
    "Todos": "All",
    "Resumen ejecutivo": "Executive summary",
    "Filtrar por cualquier texto...": "Filter by any text...",
    "Si": "Yes",
    "No": "No",
    "Claro": "Light",
    "Oscuro": "Dark",
    # --- KPIs ventana principal ---
    "Movimientos (24h)": "Movements (24h)",
    "Volumen 24h (L)": "Volume 24h (L)",
    "Alertas criticas": "Critical alerts",
    "Advertencias": "Warnings",
    "Equipos In Service": "Equipment In Service",
    "Out of Service": "Out of Service",
    "Consolas online": "Consoles online",
    # --- mensajes de estado / diálogos ventana principal ---
    "Conectando y cargando datos en tiempo real…":
        "Connecting and loading real-time data…",
    "Listo. Configura la conexion y pulsa «Iniciar monitoreo».":
        "Ready. Configure the connection and press «Start monitoring».",
    "Monitoreo detenido.": "Monitoring stopped.",
    "Error de sincronizacion": "Sync error",
    "Error leyendo la replica": "Error reading replica",
    "Falta pyqtgraph": "pyqtgraph missing",
    "No se pudo abrir el análisis de equipos:":
        "Could not open the equipment analysis:",
    "Instala la dependencia: pip install pyqtgraph":
        "Install the dependency: pip install pyqtgraph",
    "Falta token": "Token missing",
    "Para conectar a la API real necesitas un token, o activa el "
    "modo demo (simulador).":
        "To connect to the real API you need a token, or enable "
        "demo mode (simulator).",
    "Importar CSV de equipos de AdaptIQ": "Import AdaptIQ equipment CSV",
    "CSV (*.csv);;Todos (*.*)": "CSV (*.csv);;All (*.*)",
    "Error al importar": "Import error",
    "Equipos importados": "Equipment imported",
    # --- ventana de análisis de equipos: chrome ---
    "Filtros": "Filters",
    "Estado:": "Status:",
    "Tipo:": "Type:",
    "Categoría:": "Category:",
    "Grupo:": "Group:",
    "Buscar:": "Search:",
    "Propios": "Own",
    "Contratistas": "Contractors",
    "Buscar por ID, descripción, marca, modelo...":
        "Search by ID, description, make, model...",
    "Actualizar": "Refresh",
    "Exportar a Excel…": "Export to Excel…",
    "Doble clic en un equipo para ver su <b>Audit Log</b> completo.":
        "Double-click an equipment to see its full <b>Audit Log</b>.",
    "cambios registrados en la réplica.": "changes recorded in the replica.",
    "Sin cambios de este equipo en la réplica todavía "
    "(el log se llena al sincronizar).":
        "No changes for this equipment in the replica yet "
        "(the log fills in as it syncs).",
    # --- KPIs análisis de equipos ---
    "Total equipos": "Total equipment",
    "Disponibilidad": "Availability",
    "Eventos RFID": "RFID events",
    # --- pestañas análisis de equipos ---
    "Inventario": "Inventory",
    "Agrupaciones": "Groupings",
    "Cambios de RFID": "RFID changes",
    "Transiciones de estado": "Status transitions",
    "Cost center": "Cost center",
    "Atributos": "Attributes",
    "Auditoría (quién)": "Audit (who)",
    "Calidad de datos": "Data quality",
    "Gráficas": "Charts",
    "Transiciones": "Transitions",
    "Resumen": "Summary",
    "Top Out→In": "Top Out→In",
    "Top In→Out": "Top In→Out",
    "Por grupo": "By group",
    "Por cost centre": "By cost centre",
    "Tiempo en servicio": "Time in service",
    "Eventos de RFID por mes (asignado / cambiado / removido)":
        "RFID events per month (assigned / changed / removed)",
    "Tags con más cambios (re-tagueo)": "Tags with most changes (re-tagging)",
    "Equipos que más cambian de cost centre":
        "Equipment that changes cost centre the most",
    "Cost centres con más actividad de reasignación (por CC actual del equipo)":
        "Cost centres with most reassignment activity (by equipment's current CC)",
    # --- línea de resumen de RFID ---
    "Asignados": "Assigned",
    "Cambiados": "Changed",
    "Removidos": "Removed",
    "Tags": "Tags",
    # --- gráficas (títulos / ejes / series) ---
    "Equipos por estado": "Equipment by status",
    "Disponibilidad por categoría (%)": "Availability by category (%)",
    "Cambios de RFID por mes": "RFID changes per month",
    "Transiciones In→Out por mes": "In→Out transitions per month",
    "Top equipos Out→In": "Top equipment Out→In",
    "Transiciones In→Out por grupo": "In→Out transitions by group",
    "eventos": "events",
    "transiciones": "transitions",
    "veces": "times",
    "Asignado": "Assigned",
    "Cambiado": "Changed",
    "Removido": "Removed",
    # --- exportación: mensajes y nombres de hoja ---
    "Exportar análisis de equipos": "Export equipment analysis",
    "Analisis_Equipos_MSGQ.xlsx": "Equipment_Analysis_MSGQ.xlsx",
    "Exportado": "Exported",
    "Análisis generado:": "Analysis generated:",
    "Error al exportar": "Export error",
    "Error al analizar": "Analysis error",
    "Error al filtrar": "Filter error",
    "Por categoria": "By category",
    "Por departamento": "By department",
    "Por marca": "By make",
    "RFID por mes": "RFID per month",
    "RFID churn": "RFID churn",
    "Transiciones resumen": "Transitions summary",
    "Top Out-In": "Top Out-In",
    "Top In-Out": "Top In-Out",
    "Transic por grupo": "Transitions by group",
    "Transic por costcentre": "Transitions by cost centre",
    "Equipos cambio CC": "Equipment CC changes",
    "Cost centres activos": "Active cost centres",
    "Atributos cambiados": "Changed attributes",
    "Auditoria usuarios": "User audit",
    # --- encabezados analíticos (columnas de DataFrame en español) ---
    "Total": "Total",
    "En servicio": "In service",
    "Fuera de servicio": "Out of service",
    "Dados de baja": "Decommissioned",
    "Disponibilidad %": "Availability %",
    "Estado": "Status",
    "Campo": "Field",
    "Con datos": "With data",
    "Sin datos": "Missing",
    "Completitud %": "Completeness %",
    "Periodo": "Period",
    "Eventos": "Events",
    "Ultimo cambio": "Last change",
    "De": "From",
    "A": "To",
    "Veces": "Times",
    "Ultimo": "Last",
    "Cambios": "Changes",
    "Transicion": "Transition",
    "Salidas a Out": "Exits to Out",
    "Dias prom. en servicio": "Avg days in service",
    "Usuario": "User",
    "Categoria": "Category",
    "Categoría": "Category",
    "Grupo": "Group",
    "Departamento": "Department",
    "Marca": "Make",
    "Contratista/Depto": "Contractor/Dept.",
    "Cost Centre": "Cost Centre",
    "Severidad": "Severity",
    "Alertas": "Alerts",
    "Tags (registros)": "Tags (records)",
    "Cambios CC": "CC changes",
    # --- etiquetas de atributos del audit log (config.ATTR_LABELS) ---
    "Intervalo servicio": "Service interval",
    "Tipo intervalo": "Interval type",
    "Periodo limite": "Limit period",
    "Codigo": "Code",
    "Descripcion": "Description",
    "Division": "Division",
    "Matricula": "Registration",
    "Aprobador": "Approver",
    "Contratista": "Contractor",
    "Vehiculo ligero": "Light vehicle",
    "Es contratista": "Is contractor",
    "Es cisterna": "Is tanker",
    "Es pod": "Is pod",
    "SAP exportable": "SAP exportable",
    # --- placeholders de datos faltantes ---
    "(sin dato)": "(no data)",
    "(alta)": "(created)",
    "(desconocido)": "(unknown)",
    # --- categorías de alerta (valores canónicos en español) ---
    "Modo de transaccion anomalo": "Anomalous transaction mode",
    "Despacho a equipo no operativo": "Dispense to non-operational equipment",
    "Contaminacion de combustible alta": "High fuel contamination",
    "Service truck en bypass (volumen acumulado)":
        "Service truck in bypass (accumulated volume)",
    "Consola en modo bypass": "Console in bypass mode",
    "Consola offline": "Console offline",
    "Comunicacion stale": "Stale communication",
})

# ===========================================================================
# Valores de celda traducibles (lista blanca) — nunca toca datos reales
# ===========================================================================
_VALUE_TOKENS = frozenset({
    "Si", "No",
    "(sin dato)", "(alta)", "(desconocido)",
    "Asignado", "Cambiado", "Removido",
    "CRITICAL", "WARNING", "INFO",
    # etiquetas de atributos (columna "Atributo")
    "Estado", "Cost Centre", "Grupo", "Categoria", "Departamento",
    "Intervalo servicio", "Tipo intervalo", "Periodo limite", "Marca", "Modelo",
    "Codigo", "Descripcion", "Division", "Matricula", "Aprobador", "Contratista",
    "Vehiculo ligero", "Es contratista", "Es cisterna", "Es pod", "SAP exportable",
    # categorías de alerta
    "Modo de transaccion anomalo", "Despacho a equipo no operativo",
    "Contaminacion de combustible alta", "Service truck en bypass (volumen acumulado)",
    "Consola en modo bypass", "Consola offline", "Comunicacion stale",
})

# ===========================================================================
# Plantillas con interpolación (detalles de alerta, mensajes con cifras)
# ===========================================================================
_TPL: dict[str, tuple[str, str]] = {
    "alert.anomalous_type": (
        "Transaccion en modo {type} ({kind})",
        "Transaction in {type} mode ({kind})"),
    "alert.dispense_non_op": (
        "Despacho a equipo en estado '{status}'",
        "Dispense to equipment in '{status}' status"),
    "alert.contamination": (
        "Contaminacion ISO sobre umbral: {breaches}",
        "ISO contamination above threshold: {breaches}"),
    "alert.bypass_volume": (
        "Volumen acumulado en bypass: {total:,.0f} L (umbral {threshold:,.0f} L)",
        "Accumulated bypass volume: {total:,.0f} L (threshold {threshold:,.0f} L)"),
    "alert.mac_bypass": (
        "AdaptMAC {code} con key_bypass activo",
        "AdaptMAC {code} with key_bypass active"),
    "alert.mac_offline": (
        "AdaptMAC {code} reporta offline",
        "AdaptMAC {code} reports offline"),
    "alert.mac_stale": (
        "AdaptMAC {code} sin comms exitosa hace {mins} min",
        "AdaptMAC {code} with no successful comms for {mins} min"),
    "import.success": (
        "Se cargaron {n:,} equipos desde:\n{file}\n\n"
        "Sugerencia: deja el «Modo demo» apagado para que el simulador no "
        "sobrescriba estos registros.",
        "Loaded {n:,} equipment from:\n{file}\n\n"
        "Tip: keep «Demo mode» off so the simulator does not overwrite "
        "these records."),
    "eq.loading": (
        "Cargando historial de cambios...  {events:,} eventos · {equipment:,} equipos "
        "(las pestañas de RFID/transiciones se completan al terminar)",
        "Loading change history...  {events:,} events · {equipment:,} equipment "
        "(RFID/transition tabs complete when it finishes)"),
    "eq.status": (
        "{equipment:,} equipos · {events:,} eventos de cambio · actualizado {when}",
        "{equipment:,} equipment · {events:,} change events · updated {when}"),
}


# ===========================================================================
# API pública
# ===========================================================================

def t(s) -> str:
    """Traduce una cadena de interfaz o un encabezado de columna al idioma
    actual. Devuelve la entrada sin cambios si no hay traducción (passthrough)."""
    if s is None:
        return s
    s = str(s)
    if _lang == "en":
        return _EN.get(s, s)
    return _ES.get(s, s)


def tr_value(s):
    """Traduce un valor de celda SOLO si es un token conocido; cualquier otro
    valor (datos reales) se devuelve intacto."""
    if s is None:
        return s
    s2 = str(s)
    return t(s2) if s2 in _VALUE_TOKENS else s


def tr_fmt(key: str, **kw) -> str:
    """Plantilla traducible con interpolación (`str.format`)."""
    es, en = _TPL[key]
    tpl = en if _lang == "en" else es
    return tpl.format(**kw)
