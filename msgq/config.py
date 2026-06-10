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

# Categoria de alerta para un despacho cuyo volumen excede el Safe Fill Level
# (SFL) del equipo para ese producto: un sobrellenado que no deberia ocurrir.
ALERT_SFL_EXCEEDED = "Despacho excede Safe Fill Level"

# Tolerancia relativa antes de marcar un exceso de SFL: solo se reporta si
# volume > sfl * (1 + SFL_TOLERANCE_PCT). Filtra el ruido de medicion (los
# medidores tienen ~0.5-1% de error), para que solo se vean sobrellenados
# REALES y no excesos marginales de decimas de litro. 0.02 = 2%.
SFL_TOLERANCE_PCT = 0.02

# Categoria de alerta para un despacho SIN equipo valido (status 'no_equip' o
# tipo 'Unauthorised') cuyo volumen supera el SFL maximo de la flota para ese
# producto: combustible despachado sin trazabilidad y por encima de lo seguro.
ALERT_SFL_CONFLICT = "Despacho sin equipo / no autorizado"

# SFL de RESPALDO por categoria para el reporte 'Dispensas por Equipo'
# (core/dispense_report.py). La fuente primaria del SFL es SIEMPRE el limite
# real por equipo/producto que el poller replica de la API (consumption_limits);
# este mapeo TEMPORAL solo cubre los equipos sin limite cargado en el FMS.
# Se cruza por PALABRA CLAVE contra la categoria del equipo (sin distinguir
# mayusculas); gana la primera coincidencia. Litros, ajustables por el auditor.
SFL_FALLBACK_BY_CATEGORY: tuple[tuple[str, float], ...] = (
    ("LIGHT VEHICLE", 80.0),
    ("LIGHT TRUCK", 150.0),
    ("EXCAVATOR", 7450.0),
)

# ---------------------------------------------------------------------------
# Auditoria de Burn Rate (consumo de combustible, L/h)
# ---------------------------------------------------------------------------
# El burn rate de un equipo es el combustible que quema por unidad de SMU
# (horas-motor para la mayoria de la flota; odometro en vehiculos ligeros). Se
# calcula por el metodo 'tanque-a-tanque' a partir de los despachos: entre dos
# despachos CONSECUTIVOS del mismo equipo, los litros del despacho posterior
# reponen lo quemado desde el anterior, y el burn rate del intervalo es esos
# litros divididos por el avance del SMU. Es el mismo metodo que AdaptIQ
# pre-calcula (Litres Consumed / SMU Increase), pero reconstruido en vivo desde
# el endpoint para poder auditarlo y graficarlo. Las anomalias se detectan con
# estadistica ROBUSTA (mediana + MAD), inmune a los outliers que justamente se
# quieren marcar.

# Avance minimo de SMU (mismas unidades del SMU) para que un intervalo sea
# valido: por debajo de esto el cociente litros/ΔSMU se dispara por division
# entre casi-cero (ruido del medidor, no consumo real).
BURN_RATE_MIN_SMU_DELTA = 0.1

# Techo de plausibilidad (L/h): un burn rate por encima de esto no es fisicamente
# posible para una sola maquina; es un artefacto del dato (p. ej. el SMU de un
# tanque que avanza '1' y se le imputan miles de litros). Esos intervalos se
# descartan de las muestras para no contaminar las lineas base.
BURN_RATE_MAX_PLAUSIBLE = 2000.0

# Intervalos minimos (despachos consecutivos validos) para considerar confiable
# el burn rate de UN equipo: con menos muestras la mediana es inestable.
BURN_RATE_MIN_SAMPLES = 3

# Equipos minimos (con burn rate confiable) para fijar la linea base de UNA
# categoria: con menos no hay con que comparar.
BURN_RATE_MIN_CAT_EQUIPMENT = 3

# |z robusto| (desviacion respecto a la mediana de la categoria, escalada por
# MAD) a partir del cual un equipo se marca como anomalo.
BURN_RATE_Z_THRESHOLD = 3.5

# Ademas del z, se exige una desviacion relativa minima vs la linea base, para no
# marcar diferencias estadisticamente 'significativas' pero operativamente
# triviales en categorias muy homogeneas.
BURN_RATE_MIN_DEV_PCT = 15.0

# |z robusto| (respecto al propio historial del equipo) para marcar UN intervalo
# (un despacho puntual) como atipico: un pico o caida que merece investigarse.
BURN_RATE_INTERVAL_Z = 4.0

# Categoria de alerta para un equipo cuyo burn rate se desvia de su categoria
# (sobre-consumo: posible fuga/robo/falla mecanica; sub-consumo: posible
# medidor mal o despachos sin registrar).
ALERT_BURN_RATE_ANOMALY = "Burn rate anomalo"

# ---------------------------------------------------------------------------
# Auditoria de Salud de Hardware y Sensores
# ---------------------------------------------------------------------------
# El SMU (horometro/odometro) SIEMPRE debe avanzar. Una caida respecto a una
# lectura anterior significa sensor roto, reiniciado o manipulado. Un valor que
# no cambia en varias cargas de un equipo operativo significa que el sensor no
# envia pulsos al AdaptMAC.

# Una regresion se marca cuando el SMU da un paso ATRAS respecto a la lectura
# inmediatamente anterior y NO se recupera en la siguiente (la lectura siguiente
# sigue por debajo del nivel previo). Asi cada reset/manipulacion se reporta UNA
# vez (el evento), no cada despacho posterior; y un bache transitorio que se
# recupera no cuenta. La caida debe superar este minimo (filtra ruido de medicion).
SMU_REGRESSION_MIN_DROP = 1.0      # caida minima de SMU para marcar (unidades del SMU)

# Estancamiento: mismo SMU crudo en >= N despachos consecutivos abarcando >= D
# dias, en un equipo In Service -> el sensor no reporta (ticket de mantenimiento).
SMU_STAGNATION_MIN_REPEATS = 5
SMU_STAGNATION_MIN_DAYS = 5

# Re-tagueo sospechoso: mas de N cambios de RFID del MISMO equipo en una ventana
# movil de D dias -> el operador podria estar destruyendo los tags para forzar
# despachos manuales / bypass.
RETAG_MAX_CHANGES = 3
RETAG_WINDOW_DAYS = 30

# Degradacion del medidor: por manguera (Meter ID), si el caudal promedio
# reciente cae >= PCT% respecto a su linea base historica, los filtros estan
# obstruidos o la bomba falla. Requiere muestras suficientes a cada lado.
METER_RECENT_DAYS = 7
METER_DROP_PCT = 40.0
METER_MIN_SAMPLES = 5

# Categorias de alerta de hardware (valores canonicos en espanol).
ALERT_SMU_REGRESSION = "SMU en regresion (sensor)"
ALERT_SMU_STAGNATION = "SMU estancado (sensor sin pulsos)"
ALERT_RETAG = "Re-tagueo RFID sospechoso"
ALERT_METER_DEGRADED = "Caudal de medidor degradado"

# ---------------------------------------------------------------------------
# Auditoria de coherencia Producto <-> Equipo (posible tag clonado)
# ---------------------------------------------------------------------------
# Un equipo solo deberia recibir los productos que tiene habilitados
# (`consumptionTanks`). Despacharle un producto AJENO —p. ej. "Coolant" o
# "Hydraulic Fluid" a un equipo solo-DIESEL, o viceversa— suele indicar un tag
# RFID clonado o un equipo mal configurado en el maestro.
#
# El reto temporal: un producto pudo estar habilitado y luego deshabilitarse,
# dejando despachos LEGITIMOS en el historico. La API no expone CUANDO se
# habilito/deshabilito cada producto, asi que un producto se considera legitimo
# para un equipo si tiene HUELLA REAL en el propio historial de despachos del
# equipo: cumple cualquiera de estos umbrales -> "establecido por uso".
PRODUCT_MISMATCH_MIN_EVENTS = 3      # despachos del mismo producto en el equipo
PRODUCT_MISMATCH_MIN_DAYS   = 14     # o span (primer..ultimo) >= 14 dias
PRODUCT_MISMATCH_MIN_SHARE  = 0.15   # o >= 15% de los despachos del equipo

# Categorias de alerta (valores canonicos en espanol): producto de OTRA clase
# (combustible vs fluido) es la senal fuerte de tag clonado (CRITICO); producto
# de la MISMA clase pero fuera del maestro es probable mala configuracion (WARN).
ALERT_PRODUCT_FOREIGN    = "Producto ajeno al equipo (posible tag clonado)"
ALERT_PRODUCT_OFF_MASTER = "Producto fuera del maestro del equipo"

# ---------------------------------------------------------------------------
# Auditoria de Desviacion de Volumen en Entregas (Metered vs Field Entered)
# ---------------------------------------------------------------------------
# En una entrega (delivery) el sistema guarda DOS volumenes: el MEDIDO (`volume`,
# del medidor digital de la linea o del gauge del tanque) y el DIGITADO en campo
# a partir de la guia del camion de combustible (`secondary_volume`). Una
# diferencia sostenida entre ambos significa que el proveedor factura litros que
# nunca entraron al tanque, o que el medidor esta descalibrado. Se audita la
# desviacion relativa entre ambos, sobre el volumen MEDIDO (la referencia fisica).
#
# Confirmado en los CSV reales de Merian: las entregas MANUAL traen
# `Metered Volume` y `Field Entered Volume` por separado (p. ej. 39.810,5 medido
# vs 40.000 de guia = 0,48%, dentro de tolerancia), y las GAUGED comparan el gauge
# (`GTS Volume`, que viaja en `volume`) contra la guia (8-11% en la muestra).

# Desviacion relativa minima (%) para marcar una entrega. 1% es el umbral pedido.
DELIVERY_VOLUME_DEVIATION_PCT = 1.0
# Desviacion (%) a partir de la cual la alerta escala a CRITICA (no es ruido de
# medicion: hay una discrepancia grande de volumen/dinero con el proveedor).
DELIVERY_VOLUME_DEVIATION_CRITICAL_PCT = 5.0
# Entregas por debajo de este volumen (L) se ignoran: una guia de pocos litros con
# una diferencia absoluta minima dispara un % enorme sin relevancia operativa.
DELIVERY_MIN_VOLUME_L = 100.0

# Categoria de alerta para una entrega cuya desviacion medidor-vs-guia supera el
# umbral (posible sobre-facturacion del proveedor o medidor descalibrado).
ALERT_VOLUME_DEVIATION = "Desviacion de volumen en entrega (medidor vs guia)"

# Etiquetas (canonicas en espanol) de la direccion de la desviacion: la guia
# reclama MAS de lo medido (sobre-facturacion) o MENOS (sub-registro / medidor).
DELIVERY_DIR_OVERBILL  = "Guia sobre lo medido"
DELIVERY_DIR_UNDERBILL = "Guia bajo lo medido"

# ---------------------------------------------------------------------------
# Auditoria de Tag Hopping ("el tag en el bolsillo")
# ---------------------------------------------------------------------------
# El tag RFID identifica al equipo: cada despacho queda imputado al equipo cuyo
# tag se leyo. Si el MISMO tag autoriza dos despachos en puntos de despacho
# fisicamente distintos en un lapso imposible (el equipo no pudo viajar entre
# ellos), alguien removio el tag del equipo para robar combustible (o el tag esta
# clonado). Se detecta de dos formas complementarias (el usuario pidio AMBAS):
#
#   1. SOLAPAMIENTO temporal: dos despachos del mismo equipo en puntos distintos
#      cuyos intervalos [inicio, inicio+duracion] se solapan -> fisicamente
#      imposible. NO necesita coordenadas: cubre el ~99% de despachos de islas
#      fijas (sin GPS por transaccion). Es la senal de mayor confianza (CRITICO).
#   2. VELOCIDAD implicita: cuando ambos despachos traen coordenadas (GPS por
#      transaccion, presente en los surtidores moviles) o el punto esta en el mapa
#      OPCIONAL de coordenadas de puntos fijos, se calcula distancia/tiempo; por
#      encima de una velocidad implausible para ese tipo de equipo se marca (WARN).
#
# El "punto de despacho" (la ubicacion) se deriva del tanque/medidor del despacho;
# dos mangueras del mismo tanque cuentan como el MISMO lugar (no es hopping).

# Velocidad implicita (km/h) sobre la cual un equipo PESADO no pudo recorrer la
# distancia entre dos puntos de despacho en el tiempo transcurrido (no circula por
# vias a esa velocidad; suele transportarse en cama baja).
TAG_HOP_MAX_SPEED_KMH = 40.0
# Idem para VEHICULOS LIGEROS, que si se desplazan rapido por el sitio: su umbral
# es mas alto para no marcar viajes legitimos (se distinguen por is_light_vehicle).
TAG_HOP_LIGHT_MAX_SPEED_KMH = 100.0
# Distancia (km) por debajo de la cual se ignora la diferencia de GPS: filtra el
# jitter del receptor (dos lecturas del mismo punto difieren decenas de metros).
TAG_HOP_MIN_DISTANCE_KM = 0.5
# Holgura (minutos) de solapamiento que se exige antes de marcar por imposibilidad
# temporal, para absorber el desfase de reloj entre consolas (no marcar por ruido).
TAG_HOP_CLOCK_SLACK_MIN = 1.0

# Categoria de alerta para el mismo tag en dos lugares en un lapso imposible.
ALERT_TAG_HOPPING = "Tag en dos lugares a la vez (posible robo de combustible)"

# Razones (canonicas en espanol) por las que un par de despachos se marca.
TAG_HOP_REASON_OVERLAP = "Solapamiento temporal"
TAG_HOP_REASON_SPEED   = "Velocidad imposible"

# Clase de producto: distingue combustible de fluido de servicio para escalar
# los cruces entre clases (lo que el usuario describio: DIESEL vs Coolant/Hidraulico).
PRODUCT_CLASS_FUEL  = "FUEL"
PRODUCT_CLASS_FLUID = "FLUID"
PRODUCT_CLASS_OTHER = "OTHER"
# Clasificacion por substring en la etiqueta del producto (en MAYUSCULAS).
# `product_audit.product_class` evalua FUEL ANTES que FLUID, para que un
# combustible como "Gas Oil" (que contiene la subcadena 'OIL', keyword de FLUID)
# se clasifique correctamente como FUEL por su keyword "GAS OIL"/"GASOIL".
PRODUCT_CLASS_KEYWORDS = {
    PRODUCT_CLASS_FUEL: (
        "DIESEL", "GASOIL", "GAS OIL", "UNLEADED", "GASOLINE", "PETROL",
        "ULP", "LFO", "FUEL",
    ),
    PRODUCT_CLASS_FLUID: (
        "COOLANT", "HYDRAUL", "HIDRA", "OIL", "LUBRIC", "GREASE", "GRASA",
        "ADBLUE", "DEF", "GLYCOL", "GLICOL", "ANTIFREEZE", "ANTICONG",
        "REFRIG", "ATF", "15W", "10W", "5W", "80W", "85W",
    ),
}

# ===========================================================================
# Esquema canonico de columnas (contrato entre capas)
# ===========================================================================

# Un registro de movimiento aplanado (dispense / delivery / transfer).
MOVEMENT_COLS = [
    "id", "kind", "type", "status",
    # `volume` = volumen MEDIDO (medidor/gauge); `secondary_volume` = volumen
    # DIGITADO en campo desde la guia del camion (solo entregas) — su diferencia
    # es lo que audita core/volume_deviation (medidor vs guia).
    "volume", "secondary_volume", "record_collected_at", "created_at", "updated_at",
    "transaction_temperature", "peak_flow_rate",
    # Salud del medidor/manguera (auditoria de hardware): que medidor entrego el
    # despacho y su caudal/duracion. Solo se pueblan si el endpoint los expone
    # (se descubren por introspeccion; ver api/queries.build_dispenses_query).
    "average_flow_rate", "flow_duration_s", "meter_id", "meter_description", "meter_erp",
    "primary_volume_source", "secondary_volume_source",
    "max_contamination_4", "avg_contamination_4", "med_contamination_4",
    "max_contamination_6", "avg_contamination_6", "med_contamination_6",
    "max_contamination_14", "avg_contamination_14", "med_contamination_14",
    "smu_value", "smu_type",
    # SMU crudo vs calculado + fuente (auditoria de hardware): el crudo es la
    # mejor senal de "el sensor no envia pulsos" (estancamiento).
    "raw_smu_value", "calculated_smu_value", "smu_source", "smu_value_source",
    "gps_coordinates",
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

# Un tanque del sitio (registro maestro). `virtual`=tanque logico cuyo nivel es
# la suma de sus hijos (parent_tank apunta al virtual). `code` es la clave.
TANK_COLS = [
    "tank_id", "code", "description", "name", "product", "virtual",
    "capacity", "volume_unit", "enabled", "parent_tank", "tank_type",
]

# Historial de asignaciones de tag RFID -> equipo, acumulado por el poller en
# cada refresco del maestro. Permite resolver a que equipo pertenecia un tag
# aunque luego se haya removido/reemplazado: el API NO expone ese vinculo
# historico (el log de RFID no trae el equipo y `rfidTags` es solo valores), asi
# que se reconstruye observando el maestro en el tiempo. `tag` (mayusculas) = PK.
RFID_HISTORY_COLS = ["tag", "equipment_id", "internal_id", "last_seen"]

# Limite de combustible/producto por equipo: el Safe Fill Level (SFL) es el
# maximo seguro a despachar en UN repostaje. Viene de `EquipmentItem.consumptionTanks`
# (validado en vivo: `ConsumptionTank{id, sfl, product{code description}}`). `id` =
# ConsumptionTank id (PK); `product` = description (llave de cruce con el despacho).
CONSUMPTION_LIMIT_COLS = ["id", "equipment_id", "internal_id", "product", "product_code", "sfl"]

# Historial de productos HABILITADOS por equipo, acumulado por el poller en cada
# refresco del maestro (analogo a RFID_HISTORY_COLS). Reconstruye la VENTANA en
# que cada producto estuvo habilitado en un equipo, dato que la API no expone
# (consumptionTanks es solo el estado actual). Permite distinguir un despacho
# legitimo (cae dentro de [first_seen, last_seen]) de uno ajeno. `key` (=
# "equipment_id|PRODUCTO_MAYUS") es la PK sintetica que hace idempotente el upsert;
# los productos ya deshabilitados no se reinsertan -> su `last_seen` queda congelado.
PRODUCT_HISTORY_COLS = [
    "key", "equipment_id", "product", "product_code", "internal_id",
    "first_seen", "last_seen",
]

# Una reconciliacion diaria por tanque (el reporte 'Detailed Reconciliation' que
# AdaptIQ pre-calcula): stock medido (opening/closing) vs movimiento
# (inflow/outflow). `error` = (closing-opening) - (inflow-outflow), tal cual lo
# entrega el campo `volume` de la Reconciliation. `tank` = code del tanque.
RECONCILIATION_COLS = [
    "id", "period_start", "period_end", "tank", "tank_description", "product",
    "opening_stock", "closing_stock", "inflow", "outflow", "error",
    "status", "updated_at",
]

# ---------------------------------------------------------------------------
# Auditoria de equipos (validado contra el tenant de Merian)
# ---------------------------------------------------------------------------
# Tipos de registro que se sincronizan del log para el analisis de flota.
CHANGE_RECORD_EQUIPMENT = "EquipmentItem"
CHANGE_RECORD_RFID      = "EquipmentRfid"   # cambios de tag RFID (atributo 'rfid')
CHANGE_RECORD_TYPES = (CHANGE_RECORD_EQUIPMENT, CHANGE_RECORD_RFID)

# Atributos clave dentro del diff de cambios (confirmados en vivo).
ATTR_STATUS     = "equipment_status_id"   # en EquipmentItem (1/2/3)
ATTR_RFID       = "rfid"                  # en EquipmentRfid
ATTR_COST_CENTRE = "cost_centre_id"
ATTR_GROUP      = "equipment_group_id"
ATTR_CATEGORY   = "equipment_category_id"
ATTR_DEPARTMENT = "department_id"

# Etiquetas legibles para los atributos del log (para la vista de Audit Log y
# el resumen de "atributos mas cambiados").
ATTR_LABELS = {
    "equipment_status_id": "Estado", "cost_centre_id": "Cost Centre",
    "equipment_group_id": "Grupo", "equipment_category_id": "Categoria",
    "department_id": "Departamento", "smu_value": "SMU Value",
    "smu_value_source": "SMU Source", "service_interval": "Intervalo servicio",
    "service_interval_type": "Tipo intervalo", "dispense_limited": "Dispense Limited",
    "dispense_limit_period": "Periodo limite", "make": "Marca", "model": "Modelo",
    "code": "Codigo", "field_id": "Field ID", "description": "Descripcion",
    "division": "Division", "registration_number": "Matricula",
    "erp_reference": "ERP Ref", "approver": "Aprobador", "contractor": "Contratista",
    "field_description": "Field Desc", "rfid": "RFID", "fill_point_location": "Fill point",
    "is_light_vehicle": "Vehiculo ligero", "is_contractor_vehicle": "Es contratista",
    "is_tanker": "Es cisterna", "is_pod": "Es pod", "is_sap_exportable": "SAP exportable",
}

# Mapa id->estado (enum INS/OUTS/DECOMM == 1/2/3, confirmado en vivo).
EQUIPMENT_STATUS_BY_ID = {
    "1": STATUS_IN, "2": STATUS_OUT, "3": STATUS_DECOM,
}

# Inicio del historico al sincronizar cambios por primera vez (sin watermark).
CHANGES_HISTORY_START = "2022-01-01T00:00:00Z"

# Inicio del historico de MOVIMIENTOS en el primer arranque (sin watermark): se
# trae todo el historial para que el software refleje el FMS y se puedan auditar
# anomalias historicas (p. ej. un exceso de SFL de hace meses). Luego es
# incremental por watermark. Es una carga inicial larga, una sola vez.
MOVEMENTS_HISTORY_START = "2022-01-01T00:00:00Z"

# ---------------------------------------------------------------------------
# Reporte de instalacion de tags RFID ('Inventory Tag Installed')
# ---------------------------------------------------------------------------
# Tipos de operacion del reporte semanal — vocabulario exacto que entrega
# `Inventory_Equipment`. Se derivan del evento del log de auditoria de RFID:
#   create  (None -> tag)  -> NEW INSTALLATION
#   update  (tag  -> tag') -> REPLACEMENT
#   destroy (tag  -> None) -> REMOVAL
# No se traducen (son la jerga del reporte, igual que los estados FMS).
TYPE_NEW         = "NEW INSTALLATION"
TYPE_REPLACEMENT = "REPLACEMENT"
TYPE_REMOVAL     = "REMOVAL"

# Esquema exacto del reporte semanal de instalaciones (orden de columnas del
# archivo 'Inventory Tag Installed *.xlsx'). DATE = fecha REAL del cambio
# (changedAt del log), no la fecha del inventario.
WEEKLY_REPORT_COLS = ["TYPE", "DATE", "ID", "Tag", "Cost Center", "Department", "Product"]

# Marcador para el ID de una fila cuyo equipo no se pudo identificar (el tag ya
# no esta en el maestro ni en el historial de asignaciones). Se muestra en vez de
# dejar el ID en blanco, para no confundir.
UNIDENTIFIED = "(no identificado)"

# ===========================================================================
# Configuracion de conexion
# ===========================================================================

DEFAULT_ENDPOINT = "https://merian.veridapt.io/graphql"   # tenant Newmont Merian
DEFAULT_POLL_SECONDS = 20
DEFAULT_PAGE_SIZE = 100        # la API limita a 100 registros por pagina
DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "msgq_replica.sqlite3")

# --- Cortesia con el endpoint (anti fuerza-bruta / DDoS) -------------------
# El monitor es un BUEN ciudadano de la API de Veridapt AdaptIQ: espacia sus
# peticiones para que el alto volumen (sobre todo el backfill historico, que
# pagina miles de veces seguidas) no se confunda con un escaneo, fuerza bruta o
# negacion de servicio. Estos valores se aplican en `AdaptIQClient._execute`.
#
# Segundos minimos entre el INICIO de dos peticiones consecutivas. A 0.3s el
# techo es ~3 req/s, muy por debajo de lo que un IDS/WAF considera abuso, y
# apenas anade ~1s a un ciclo incremental normal (pocas paginas).
DEFAULT_REQUEST_INTERVAL = 0.3
# Jitter aleatorio (0..N s) que se suma al intervalo, para que el ritmo no sea
# perfectamente regular (un patron metronomico tambien delata a un bot).
DEFAULT_REQUEST_JITTER = 0.2
# Reintentos ante 429/503/timeout antes de propagar el error al ciclo.
DEFAULT_MAX_RETRIES = 4
# Backoff exponencial entre reintentos (segundos) y su techo.
DEFAULT_RETRY_BACKOFF = 1.0
DEFAULT_RETRY_BACKOFF_MAX = 60.0


def demo_db_path(live_path: str) -> str:
    """Ruta del replica para el modo DEMO: un archivo SEPARADO del de produccion.

    Asi los datos sinteticos del simulador NUNCA se mezclan con los reales. Mezclar
    ambos producia falsos positivos en la auditoria SFL (un despacho demo, dimensionado
    contra un SFL ficticio, se cruzaba contra el SFL real del equipo y 'excedia')."""
    base, ext = os.path.splitext(live_path or DEFAULT_DB_PATH)
    return f"{base}_demo{ext or '.sqlite3'}"


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
    # Cortesia con el endpoint (ver constantes DEFAULT_REQUEST_* arriba): el
    # cliente espacia y reintenta sus peticiones para no parecer un ataque.
    request_min_interval: float = DEFAULT_REQUEST_INTERVAL
    request_jitter: float = DEFAULT_REQUEST_JITTER
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_backoff: float = DEFAULT_RETRY_BACKOFF
    retry_backoff_max: float = DEFAULT_RETRY_BACKOFF_MAX
    # Sitio a consultar. La API es 'site-scoped': todo cuelga de site(id:).
    # Si site_id queda vacio, el cliente lo auto-descubre via la query `sites`
    # eligiendo aquel cuyo code/description contenga `site_match`.
    site_id: str = ""
    site_match: str = "Merian"
    # Vestigial: el backfill de movimientos del primer arranque va desde
    # MOVEMENTS_HISTORY_START (historial completo), no por esta ventana. Se
    # conserva el campo por compatibilidad de entorno; ya NO limita la carga.
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
            request_min_interval=_float_env("MSGQ_REQUEST_INTERVAL", DEFAULT_REQUEST_INTERVAL),
            request_jitter=_float_env("MSGQ_REQUEST_JITTER", DEFAULT_REQUEST_JITTER),
            max_retries=_int_env("MSGQ_MAX_RETRIES", DEFAULT_MAX_RETRIES),
            retry_backoff=_float_env("MSGQ_RETRY_BACKOFF", DEFAULT_RETRY_BACKOFF),
            retry_backoff_max=_float_env("MSGQ_RETRY_BACKOFF_MAX", DEFAULT_RETRY_BACKOFF_MAX),
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


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "si"}


# ---------------------------------------------------------------------------
# Credenciales embebidas (version turnkey / kiosko)
# ---------------------------------------------------------------------------

def load_embedded_settings() -> "Settings | None":
    """Carga credenciales embebidas desde `msgq/embedded_config.py` si existe.

    Devuelve un `Settings` listo (demo_mode=False) cuando el modulo esta presente
    y trae un TOKEN; en caso contrario None y la app usa el flujo manual (token
    por pantalla). `embedded_config.py` esta en .gitignore: nunca se versiona.
    """
    try:
        from msgq import embedded_config as ec  # type: ignore
    except Exception:
        return None
    token = str(getattr(ec, "TOKEN", "") or "").strip()
    if not token:
        return None
    return Settings(
        endpoint=str(getattr(ec, "ENDPOINT", DEFAULT_ENDPOINT)).strip(),
        token=token,
        site_id=str(getattr(ec, "SITE_ID", "") or "").strip(),
        site_match=str(getattr(ec, "SITE_MATCH", "Merian")).strip(),
        poll_seconds=int(getattr(ec, "POLL_SECONDS", DEFAULT_POLL_SECONDS)),
        demo_mode=False,
        db_path=str(getattr(ec, "DB_PATH", "") or DEFAULT_DB_PATH),
        # Cortesia con el endpoint: configurable desde el config embebido, pero
        # con defaults conservadores (el kiosko corre desatendido contra la API real).
        request_min_interval=float(getattr(ec, "REQUEST_INTERVAL", DEFAULT_REQUEST_INTERVAL)),
        request_jitter=float(getattr(ec, "REQUEST_JITTER", DEFAULT_REQUEST_JITTER)),
        max_retries=int(getattr(ec, "MAX_RETRIES", DEFAULT_MAX_RETRIES)),
        retry_backoff=float(getattr(ec, "RETRY_BACKOFF", DEFAULT_RETRY_BACKOFF)),
        retry_backoff_max=float(getattr(ec, "RETRY_BACKOFF_MAX", DEFAULT_RETRY_BACKOFF_MAX)),
    )
