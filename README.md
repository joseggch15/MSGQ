# MSGQ — Monitor *near-real-time* del FMS AdaptIQ

Dashboard de escritorio que se conecta a la **API GraphQL de AdaptIQ (AdaptFMS)**
de la mina **Newmont Merian**, replica los datos operativos y de telemetría en
una base local y los proyecta en vivo: movimientos de combustible, equipos,
salud de consolas IoT y un panel de **alertas de trazabilidad**.

Comparte stack y convenciones con sus proyectos hermanos
[`Inventory_Equipment`](../Inventory_Equipment/) (del que reutiliza el modelo de
tablas y el vocabulario de equipos),
[`diesel_report`](../diesel_report/), [`m_diesel_report`](../m_diesel_report/) y
[`lubes_report`](../lubes_report/) (PySide6 + pandas + arquitectura en capas).

## Por qué "near-real-time" y no "tiempo real" puro

La API documentada (*Customer Facing GraphQL APIs, July 2023*) expone **Queries**
y **Mutations**, pero **no Subscriptions** (no hay push por WebSocket). El tiempo
real se aproxima con **polling incremental**: cada pocos segundos se piden solo
los registros modificados desde la última sincronización (`updated_from`), con
**paginación por cursor** (`pageInfo.hasNextPage` / `endCursor`, límite de 100
por página).

## Stack

- **Python 3.10+** (probado en 3.12)
- **PySide6** — interfaz de escritorio
- **httpx** — cliente HTTP asíncrono para GraphQL *(solo modo real)*
- **pandas** — aplanado y manejo de datos
- **SQLite** (stdlib) — réplica local, sin servidor

```bash
pip install -r requirements.txt
```

> En **modo demo** no hace falta `httpx` ni red: se importa de forma perezosa.

## Uso

```bash
python run.py
```

1. **Modo demo** (por defecto si no hay token): un simulador genera una flota
   minera realista y un flujo continuo de transacciones con anomalías
   esporádicas. Sirve para ver el dashboard funcionando sin credenciales.
2. **Modo real**: desmarca *Modo demo*, pega el **token**, el **endpoint** y el
   **Site** (id o nombre, p. ej. `Merian`), y pulsa **Iniciar monitoreo**. El
   token viaja como `Authorization: Token token=<token>`. La API es *site-scoped*:
   los movimientos (`dispenses` / `deliveries` / `transfers`) y las consolas
   (`adaptMacs`) se consultan bajo `site(id:)` en camelCase.

   Antes de conectar, valida el esquema real de tu tenant:
   ```bash
   set MSGQ_TOKEN=<token>
   python -m msgq.diagnose
   ```
   Reporta los `sites` (con su `id`), los campos reales y **si existe una
   conexión para listar equipos**.
3. **Importar equipos (CSV)** *(fallback)*: el botón *Importar equipos (CSV de AdaptIQ)…*
   carga el **maestro completo** de equipos desde un export CSV de AdaptIQ
   (Equipment ▸ export) — miles de registros, sin token ni red. Útil para ver
   todo el universo de equipos de inmediato; déjalo con el *Modo demo* apagado
   para que el simulador no sobrescriba esos registros.

La configuración también se puede fijar por entorno (ver [`.env.example`](.env.example)).

## Arquitectura

```
                ┌────────────────────────────────────────────┐
                │                  ui/ (PySide6)              │
                │  KPIs · Movimientos · Equipos · Consolas ·  │
                │  Alertas      ← QTimer lee de SQLite (2 s)  │
                └───────────────▲────────────────┬───────────┘
                                │ señales Qt      │ lee
                ┌───────────────┴─────────┐   ┌───▼───────────┐
                │   ingest/ (Poller)      │   │  storage/     │
                │   QThread + asyncio     │──▶│  SQLite       │
                │   polling incremental   │   │  (watermark)  │
                └───────────────▲─────────┘   └───────────────┘
                                │ usa
        ┌───────────────────────┴───────────────────────┐
        │                     core/                       │
        │  transform (edges/nodes → DataFrame) · alerts   │
        └───────────────────────▲───────────────────────┘
                                │ datos crudos
                ┌───────────────┴───────────────┐
                │             api/               │
                │  AdaptIQClient (httpx) │ Simulator
                └────────────────────────────────┘
```

| Capa | Módulo | Responsabilidad |
|---|---|---|
| `config` | `msgq/config.py` | Vocabulario del dominio, esquema canónico, `Settings`. |
| `api` | `client.py`, `simulator.py`, `queries.py` | Fuente de datos (real o simulada) tras un mismo contrato `DataSource`. |
| `core` | `transform.py`, `alerts.py` | Aplanado JSON→DataFrame y detección de anomalías/KPIs. |
| `storage` | `db.py` | Réplica SQLite, upserts idempotentes y *watermark* de sync. |
| `ingest` | `poller.py` | Motor de polling en `QThread` con su event loop asyncio. |
| `ui` | `main_window.py`, `table_model.py`, `common.py` | Dashboard, modelo de tabla y helpers (reutilizados del ecosistema). |

## Qué información proyecta

- **Movimientos** (dispense / delivery / transfer): volumen, tipo y estado,
  producto, tanque, sitio, *field user*, equipo destino y su estado, service
  truck, contaminación ISO (4/6/14 µm), caudal pico, temperatura, SMU, GPS,
  costo, centro de costo y *rebate*.
- **Equipos** (Equipment Items): identidad y clasificación (grupo, categoría,
  marca/modelo, *light vehicle / pod / service truck / contractor*), estado
  operativo, RFID, SMU e intervalo de servicio, límites de despacho, e
  integración ERP (`erp_reference`, `order_number`, `sap`…).
- **Consolas AdaptMAC**: `online`, `key_bypass`, últimas comunicaciones
  exitosas/fallidas (salud de la infraestructura IoT).
- **Alertas de trazabilidad** (ver reglas abajo) + **resumen ejecutivo**.

## Reglas de alerta (`core/alerts.py`)

| Severidad | Categoría | Disparo |
|---|---|---|
| 🔴 Crítica | Modo de transacción anómalo | `KEY_BYPASS`, `UNAUTHORISED` |
| 🟠 Advertencia | Modo de transacción anómalo | `SUP_OVERRIDE`, `SPILLAGE` |
| 🔴 Crítica | Despacho a equipo no operativo | dispense a equipo `Out of Service` / `Decommissioned` |
| 🟠 Advertencia | Contaminación de combustible alta | `avg_contamination_{4,6,14}` sobre umbral ISO |
| 🔴 Crítica | Service truck en bypass (volumen) | acumulado en bypass > ~24.000 L |
| 🔴/🟠 | Salud AdaptMAC | consola en `key_bypass`, offline o comunicación *stale* |

Los umbrales viven en `config.py` y son ajustables.

## Análisis de equipos

El botón **"Analizar equipos…"** abre una ventana dedicada (lee de la réplica
SQLite; no toca el poller) con:

- **Inventario filtrable** por estado (In Service / Out of Service /
  Decommissioned), tipo (propios / contratistas), categoría, grupo y texto.
- **KPIs de flota**: total, en servicio, fuera de servicio, disponibilidad %,
  contratistas, eventos RFID, transiciones In→Out.
- **Agrupaciones**: por categoría, grupo, departamento y marca (con disponibilidad).
- **Cambios de RFID**: frecuencia (asignado / cambiado / removido) por mes y
  "re-tagueo" por registro de tag.
- **Transiciones de estado**: cada cambio In↔Out↔Decom con equipo, fecha y
  **quién** (`whodunnit`); resumen por tipo; y **tiempo medio en servicio** antes
  de salir a Out.
- **Auditoría (quién)**: cambios por usuario.
- **Gráficas** (pyqtgraph): equipos por estado, disponibilidad por categoría,
  cambios de RFID por mes y transiciones In→Out por mes.

**Fuente de datos:** el log de auditoría GraphQL (`Query.changes` →
`ChangeEvent` con `changedAt` / `recordType` / `whodunnit` / diff `changes`).
Las transiciones de estado salen de `EquipmentItem.equipment_status_id`
(1=In Service, 2=Out of Service, 3=Decommissioned) enlazado al equipo por
`internal_id`; los cambios de RFID, de `EquipmentRfid` (atributo `rfid`). El
primer arranque sincroniza el histórico completo en segundo plano; luego es
incremental por watermark sobre `changed_at`.

## Pruebas

E2E con el pipeline real y la fuente simulada (sin mocks):

```bash
python tests/test_e2e.py      # o:  pytest tests/test_e2e.py -v
```

## Notas para producción

- Las queries siguen el esquema *site-scoped* en camelCase del documento de
  julio 2023. Antes de conectar a un tenant real conviene **validar el esquema
  vivo** con `python -m msgq.diagnose` y ajustar lo que difiera en
  `api/queries.py` (el resto del pipeline tolera campos ausentes).
- **Validado contra el tenant de Merian (2026-06):** endpoint
  `https://merian.veridapt.io/graphql`, **site id = 1**. El esquema vivo es más
  completo que el doc de 2023: **`site.equipmentItems` SÍ existe** y trae campos
  ricos (make, model, equipmentGroup, equipmentCategory, costCentre, department,
  isLightVehicle, serviceInterval, dispenseLimited, rfidTags…). El cliente
  descubre esa conexión por introspección automáticamente y trajo los **2.046
  equipos**, los movimientos y **16 consolas** reales.
- **Volumen:** la API entrega el volumen **en litros** (no aplicar el `/100`
  que usa el Power BI; ese ajuste es específico del docket de diésel del LFO).
- **SMU del equipo:** no viaja en `EquipmentItem` (queda NA); el SMU real está
  por-movimiento (`smuValue`/`smuType` en cada dispense).
- El **token** no se persiste en disco; introdúcelo en la interfaz o por
  variable de entorno `MSGQ_TOKEN`.

## Roadmap sugerido

- Exportación a Excel del histórico de alertas (reutilizar el estilo de
  `Inventory_Equipment/export/excel.py`).
- Notificaciones (sonido / bandeja) ante alertas críticas nuevas.
- Mapa de calor de `gps_coordinates` y mantenimiento predictivo sobre SMU +
  `serviceInterval`.
- Cruce con maestros ERP (SAP) vía `erp_reference` para conciliación.
