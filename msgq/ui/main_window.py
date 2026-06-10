"""Ventana principal del monitor FMS AdaptIQ (Newmont Merian).

Dashboard de escritorio que:

  • Configura la conexion (endpoint, token, intervalo de polling, modo demo).
  • Arranca/detiene el motor de polling (`ingest.Poller`, en su propio hilo).
  • Refresca ciclicamente las vistas leyendo de la replica SQLite (un `QTimer`,
    igual que en los reportes), nunca de la API directamente.
  • Proyecta KPIs, movimientos, equipos, consolas AdaptMAC y un panel de alertas
    de trazabilidad (bypass, despacho a equipo no operativo, contaminacion...).

El idioma (ES/EN) se elige con el selector de la barra superior y se recuerda
entre sesiones (QSettings). El cambio reconstruye la interfaz para que TODO —
chrome, tablas, gráficas y exports— quede en el idioma elegido.

Ejecutar:  python run.py
"""
from __future__ import annotations

import os
import time
import traceback

from PySide6.QtCore import QSettings, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QSpinBox, QStyle, QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget,
)

from msgq.config import Settings, load_embedded_settings, demo_db_path, DEFAULT_DB_PATH
from msgq.core import alerts as al
from msgq.i18n import LANGUAGES, current_language, set_language, t, tr_fmt
from msgq.ingest import Poller
from msgq.io import load_equipment_csv
from msgq.logging_setup import get_logger
from msgq.storage import Database
from msgq.ui import theme
from msgq.ui.common import (
    FlowLayout, flow_bar, kpi_label, language_selector, make_button, make_table,
    theme_selector, warn_label, wrap_with_search,
)

log = get_logger("ui.main")

# Colores semánticos de las tarjetas KPI (kpi_label los ajusta al tema activo).
PRIMARY = "#1F4E78"
ACCENT = "#2E7D32"
DANGER = "#C62828"

# Refresco visual desacoplado del polling (lee de SQLite).
_REFRESH_MS = 2000

# Intervalo minimo (s) entre recalculos de las alertas PESADAS (burn rate, hardware,
# producto, desviacion de volumen, tag hopping). Recorren TODO el historico (sobre
# cientos de miles de filas tardan minutos), asi que se "debouncean": como mucho una
# vez cada estos segundos. Antes era 8s, lo que con un calculo de ~130s producia un
# bucle infinito de recalculos. Configurable por MSGQ_HEAVY_ALERTS_INTERVAL.
_HEAVY_ALERTS_MIN_INTERVAL = float(os.getenv("MSGQ_HEAVY_ALERTS_INTERVAL", "300"))

# Presets de columnas por TIPO de movimiento. Como en AdaptIQ (pestañas Deliveries /
# Transfers / Dispenses), cada tipo tiene su propia vista con SUS columnas pertinentes;
# asi no aparecen campos vacios (p. ej. Equipment ID en una transferencia). El orden
# replica el de AdaptIQ. Una columna del preset que no exista o quede totalmente vacia
# en el subconjunto se omite; cualquier columna con datos fuera del preset se anexa al
# final, para no ocultar nunca informacion real.
_MOV_KIND_COLS = {
    "DISPENSE": [
        "id", "record_collected_at", "equipment_id", "equipment_description",
        "equipment_status", "smu_value", "smu_type", "volume", "type", "status",
        "product", "tank", "site", "field_user", "is_service_truck",
        "cost_centre", "cost", "peak_flow_rate", "average_flow_rate",
        "flow_duration_s", "meter_id", "meter_description",
        "transaction_temperature", "created_at", "updated_at",
    ],
    "TRANSFER": [
        "id", "record_collected_at", "type", "product", "volume", "tank", "site",
        "status", "service_truck", "peak_flow_rate", "transaction_temperature",
        "created_at", "updated_at",
    ],
    "DELIVERY": [
        "id", "record_collected_at", "tank", "product", "volume",
        "secondary_volume", "primary_volume_source", "secondary_volume_source",
        "type", "status", "flow_duration_s", "peak_flow_rate",
        "transaction_temperature", "created_at", "updated_at",
    ],
}


def _project_for_kind(df, kind):
    """Proyecta el preset de columnas del tipo `kind` sobre `df` (ya filtrado a ese
    tipo). Descarta columnas vacias para ese subconjunto y anexa al final las que
    tengan datos pero no esten en el preset. Devuelve un DataFrame listo para la tabla."""
    if df is None or df.empty:
        return df

    def has_data(col):
        s = df[col]
        nonnull = s.notna()
        if s.dtype == object:
            nonnull &= s.astype(str).str.strip() != ""
        return bool(nonnull.any())

    preset = _MOV_KIND_COLS.get(kind, list(df.columns))
    front = [c for c in preset if c in df.columns and has_data(c)]
    rest = [c for c in df.columns
            if c not in preset and c != "kind" and has_data(c)]
    return df[front + rest]


class _ViewsWorker(QThread):
    """Lee la replica y prepara TODO lo que el refresco visual proyecta (tablas de
    movimientos/equipos/consolas, alertas livianas de 24h combinadas con la cache
    de pesadas, KPIs) en un hilo aparte. Antes estas lecturas corrian en el hilo
    de la GUI en cada ciclo de sync; durante el backfill quedaban en cola detras
    de las escrituras del poller y la ventana entraba en 'No responde'."""

    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, db_path: str, heavy: dict, parent=None):
        super().__init__(parent)
        self._path = db_path
        self._heavy = heavy   # caches de alertas pesadas (solo lectura)

    def run(self) -> None:  # noqa: D401 - QThread entrypoint
        try:
            rdb = Database(self._path, create=False)
            try:
                disp = rdb.get_movements(limit=1000, kind="DISPENSE")
                trans = rdb.get_movements(limit=1000, kind="TRANSFER")
                deliv = rdb.get_movements(limit=1000, kind="DELIVERY")
                eq = rdb.get_equipment()
                mac = rdb.get_adaptmac()
                recent = rdb.recent_movements(hours=24)
                limits = rdb.get_consumption_limits()
            finally:
                rdb.close()
            sfl_alerts = al.detect_sfl_alerts(recent, limits)
            sfl_conflicts = al.detect_sfl_conflict_alerts(recent, limits)
            h = self._heavy
            all_alerts = al.combine(
                al.detect_movement_alerts(recent),
                al.detect_adaptmac_alerts(mac),
                sfl_alerts,
                sfl_conflicts,
                h["burn"], h["hw"], h["product"], h["vd"], h["th"],
                h["activity"],
            )
            self.done.emit({
                "disp": _project_for_kind(disp, "DISPENSE"),
                "trans": _project_for_kind(trans, "TRANSFER"),
                "deliv": _project_for_kind(deliv, "DELIVERY"),
                "eq": eq,
                "mac": mac,
                "alerts": all_alerts,
                "summary": al.alert_summary(all_alerts),
                "kpis": al.compute_kpis(recent, eq, mac, all_alerts),
                "sfl_live": al.combine(sfl_alerts, sfl_conflicts),
            })
        except Exception as exc:  # noqa: BLE001
            log.exception("Worker de vistas fallo")
            self.failed.emit(str(exc))


class _AlertsWorker(QThread):
    """Dispara el calculo de las alertas pesadas EN OTRO PROCESO y espera el
    resultado. Por que un proceso y no solo un hilo: los detectores son CPU-bound y
    de Python puro, asi que por el GIL un QThread NO libera la CPU y la interfaz se
    congela igual (era la causa real del freeze pese a haberlos pasado a un hilo).
    Ejecutandolos en un `ProcessPoolExecutor` (otro GIL) la GUI queda 100% fluida.
    El hilo solo bloquea esperando el future (espera de E/S: libera el GIL)."""

    done = Signal(dict)   # {burn, hw, product, vd, th, counts:(mv,chg)} o {error}

    def __init__(self, db_path: str, executor, parent=None):
        super().__init__(parent)
        self._path = db_path
        self._executor = executor   # ProcessPoolExecutor o None (fallback en-hilo)

    def run(self) -> None:  # noqa: D401 - QThread entrypoint
        from msgq.core.alert_compute import compute_heavy_alerts
        out: dict = {}
        t0 = time.monotonic()
        try:
            if self._executor is not None:
                # Calculo en PROCESO separado: la GUI no se congela aunque tarde minutos.
                out = self._executor.submit(compute_heavy_alerts, self._path).result()
                where = "subproceso"
            else:
                out = compute_heavy_alerts(self._path)   # fallback (sin pool)
                where = "en-hilo"
            log.info("Alertas pesadas (%s) en %.1fs sobre %d movimientos",
                     where, time.monotonic() - t0, out.get("counts", (0, 0))[0])
        except Exception as exc:  # noqa: BLE001
            log.exception("Worker de alertas fallo")
            out = {"error": str(exc)}
        self.done.emit(out)


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self._qsettings = QSettings("NewmontMerian", "MSGQ")
        set_language(self._qsettings.value("language", "es"))
        theme.set_theme(self._qsettings.value("theme", "light"))
        theme.apply_theme()   # QSS a nivel de QApplication (cubre ambas ventanas)
        self.resize(1480, 880)
        self.setMinimumSize(1100, 700)

        # Si hay credenciales embebidas (msgq/embedded_config.py), modo kiosko:
        # arranca solo y consulta el endpoint sin pedir token por pantalla.
        embedded = load_embedded_settings()
        self._kiosk = embedded is not None
        self._settings = embedded if embedded is not None else Settings.from_env()
        # Replica separado por modo: demo usa otro archivo para NUNCA contaminar
        # los datos reales (evita falsos positivos en la auditoria SFL).
        self._live_db = self._settings.db_path or DEFAULT_DB_PATH
        self._db = Database(self._effective_db_path())
        self._heal_replica()
        self._poller: Poller | None = None
        self._monitoring = False
        self._eq_window = None
        self._tank_window = None
        self._inv_window = None
        self._sfl_window = None
        self._burn_window = None
        self._hw_window = None
        self._vd_window = None
        self._th_window = None
        self._act_window = None
        # Alarma de escritorio para despachos sobre Safe Fill Level (SFL) y para
        # equipos con burn rate anómalo.
        self._tray = self._make_tray()
        self._seen_sfl_ids: set[str] = set()
        self._sfl_initialized = False
        self._seen_burn_ids: set[str] = set()
        self._burn_initialized = False
        # Cache de alertas de burn rate: el cálculo recorre TODO el histórico de
        # movimientos, así que solo se recalcula cuando ese conteo cambia (no en
        # cada refresco visual), para no recorrer la réplica entera de más.
        self._burn_alerts = al._empty_alerts()
        self._burn_count: int | None = None
        # Cache de alertas de hardware (SMU/RFID/medidores), gated por el conteo de
        # movimientos + cambios (recorre todo el historico de despachos y el log).
        self._hw_alerts = al._empty_alerts()
        self._hw_key: tuple | None = None
        self._seen_hw_ids: set[str] = set()
        self._hw_initialized = False
        # Cache de alertas de coherencia producto<->equipo (tag clonado): juzga la
        # legitimidad de cada producto por su huella de uso en TODO el historico,
        # asi que se recalcula solo cuando cambia el conteo de movimientos.
        self._product_alerts = al._empty_alerts()
        self._product_count: int | None = None
        # Cache de alertas de desviacion de volumen en entregas (medidor vs guia) y
        # de tag hopping (mismo tag en dos lugares): ambas recorren todo el historico
        # de movimientos, asi que se recalculan solo al cambiar su conteo.
        self._vd_alerts = al._empty_alerts()
        self._vd_count: int | None = None
        self._th_alerts = al._empty_alerts()
        self._th_count: int | None = None
        self._seen_th_ids: set[str] = set()
        self._th_initialized = False
        # Cache de alertas de actividad (equipos fantasma / coherencia
        # SMU<->combustible): recorre todo el historico, va con las pesadas.
        self._activity_alerts = al._empty_alerts()
        self._activity_count: int | None = None
        # Worker en segundo plano para las alertas pesadas (no congelar la GUI). El
        # calculo real corre en un PROCESO aparte (pool perezoso); el GIL hacia que
        # un simple hilo congelara la UI igual. `_last_heavy_done` marca cuando
        # termino el ultimo, para el debounce (no relanzar hasta pasado el intervalo).
        self._alerts_worker: _AlertsWorker | None = None
        self._alerts_pool = None
        self._last_heavy_spawn = 0.0
        # Worker del refresco visual: las lecturas y la deteccion liviana corren
        # fuera del hilo de la GUI; aqui solo se proyectan los resultados.
        self._views_worker: _ViewsWorker | None = None
        self._views_pending = False

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(_REFRESH_MS)
        self._refresh_timer.timeout.connect(self._refresh_views)

        self.setWindowTitle(t("MSGQ — Monitor FMS AdaptIQ  ·  Newmont Merian"))
        self._build_central()

        self._refresh_views()  # muestra lo que ya hubiera en la replica
        if self._kiosk:
            self.statusBar().showMessage(t("Conectando y cargando datos en tiempo real…"))
            QTimer.singleShot(250, self._start_monitoring)
        else:
            self.statusBar().showMessage(
                t("Listo. Configura la conexion y pulsa «Iniciar monitoreo»."))

    # =======================================================================
    # Idioma
    # =======================================================================

    def switch_language(self, code: str) -> None:
        """Cambia el idioma global, lo recuerda y reconstruye la interfaz."""
        if not code or code == current_language():
            return
        set_language(code)
        self._qsettings.setValue("language", code)
        self._rebuild_ui()

    def switch_theme(self, name: str) -> None:
        """Cambia el tema (claro/oscuro), lo recuerda, re-aplica el QSS global y
        reconstruye la interfaz (para que tarjetas KPI y gráficas se recoloreen)."""
        if not name or name == theme.current_theme():
            return
        theme.set_theme(name)
        self._qsettings.setValue("theme", name)
        theme.apply_theme()
        self._rebuild_ui()

    def _on_language_changed(self, code: str) -> None:
        self.switch_language(code)

    def _on_theme_changed(self, name: str) -> None:
        self.switch_theme(name)

    def _rebuild_ui(self) -> None:
        """Reconstruye esta ventana (y la de análisis si está abierta) tras un
        cambio de idioma o de tema. Garantiza cobertura total de la traducción y
        del recoloreo, conservando el motor de polling activo."""
        self.setWindowTitle(t("MSGQ — Monitor FMS AdaptIQ  ·  Newmont Merian"))
        self._build_central()
        if not self._kiosk:
            self.btn_start.setEnabled(not self._monitoring)
            self.btn_stop.setEnabled(self._monitoring)
            self._set_inputs_enabled(not self._monitoring)
        self._last_counts = None
        self._refresh_views(force=True)
        for child in (self._eq_window, self._tank_window, self._inv_window,
                      self._sfl_window, self._burn_window, self._hw_window,
                      self._vd_window, self._th_window, self._act_window):
            if child is not None and child.isVisible():
                child.rebuild_ui()

    # =======================================================================
    # Construccion de la interfaz
    # =======================================================================

    def _build_central(self) -> None:
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setSpacing(6)
        lay.addWidget(self._build_kiosk_banner() if self._kiosk else self._build_connection())
        lay.addWidget(self._build_kpi_strip())
        lay.addWidget(self._build_tabs(), stretch=1)
        self.setCentralWidget(root)

    def _build_connection(self) -> QGroupBox:
        box = QGroupBox(t("Conexion y motor de polling"))
        col = QVBoxLayout(box)

        r1 = QHBoxLayout()
        r1.addWidget(QLabel(t("Endpoint GraphQL:")))
        self.ed_endpoint = QLineEdit(self._settings.endpoint)
        r1.addWidget(self.ed_endpoint, stretch=2)
        r1.addWidget(QLabel(t("Token:")))
        self.ed_token = QLineEdit(self._settings.token)
        self.ed_token.setEchoMode(QLineEdit.Password)
        self.ed_token.setPlaceholderText("Authorization: Token token=<...>")
        r1.addWidget(self.ed_token, stretch=1)
        r1.addWidget(language_selector(self._on_language_changed))
        r1.addWidget(theme_selector(self._on_theme_changed))
        col.addLayout(r1)

        r2 = QHBoxLayout()
        r2.addWidget(QLabel(t("Intervalo (s):")))
        self.spin_poll = QSpinBox()
        self.spin_poll.setRange(3, 600)
        self.spin_poll.setValue(self._settings.poll_seconds)
        r2.addWidget(self.spin_poll)
        r2.addSpacing(16)
        r2.addWidget(QLabel(t("Site:")))
        self.ed_site = QLineEdit(self._settings.site_id or self._settings.site_match)
        self.ed_site.setMaximumWidth(150)
        self.ed_site.setToolTip(t(
            "ID del sitio, o un texto del nombre (p. ej. 'Merian') para "
            "auto-descubrirlo via la query 'sites'."))
        r2.addWidget(self.ed_site)
        r2.addSpacing(16)
        self.chk_demo = QCheckBox(t("Modo demo (simulador, sin red)"))
        self.chk_demo.setChecked(self._settings.demo_mode)
        r2.addWidget(self.chk_demo)
        r2.addStretch(1)

        self.btn_start = make_button(t("Iniciar monitoreo"), self._on_start,
                                     object_name="accent")
        r2.addWidget(self.btn_start)

        self.btn_stop = make_button(t("Detener"), self._on_stop, object_name="danger")
        self.btn_stop.setEnabled(False)
        r2.addWidget(self.btn_stop)
        col.addLayout(r2)

        # Fila 3 — carga de snapshots locales (sin token ni red) + su ayuda.
        r3 = QHBoxLayout()
        self.btn_import_eq = make_button(t("Importar equipos (CSV de AdaptIQ)…"),
                                         self._on_import_equipment)
        r3.addWidget(self.btn_import_eq)
        hint = QLabel(t("Carga el maestro completo de equipos desde un export CSV "
                        "(no requiere token ni red)."))
        hint.setStyleSheet("color:#5A6B7B;")
        hint.setWordWrap(True)
        r3.addWidget(hint, stretch=1)
        col.addLayout(r3)

        # Fila 4 — análisis/auditoría: barra fluida que envuelve al redimensionar.
        col.addWidget(self._build_action_bar())
        return box

    def _build_action_bar(self) -> QWidget:
        """Botones de análisis/auditoría en un `FlowLayout`: conservan su tamaño
        completo (no se truncan) y se reacomodan en varias filas al achicar la ventana.
        Fuente única compartida por el banner kiosko y la caja de conexión."""
        bar = flow_bar()
        fl = bar.layout()
        self.btn_analyze = make_button(
            t("Analizar equipos…"), self._on_open_equipment, object_name="accent",
            tooltip=t("Abre el análisis de flota: filtros, frecuencia de cambio de RFID, "
                      "transiciones In↔Out, auditoría y gráficas."))
        fl.addWidget(self.btn_analyze)
        self.btn_tanks = make_button(
            t("Analizar tanques…"), self._on_open_tanks,
            tooltip=t("Abre el análisis de tanques: reconciliación, niveles, despachos y "
                      "gráficas (en vivo desde el endpoint)."))
        fl.addWidget(self.btn_tanks)
        self.btn_inventory = make_button(
            t("Inventario de tags RFID…"), self._on_open_inventory,
            tooltip=t("Abre el reporte de instalación de tags RFID (alta/reemplazo/remoción) "
                      "con la fecha real del cambio, en vivo desde el endpoint."))
        fl.addWidget(self.btn_inventory)
        self.btn_sfl = make_button(
            t("Despachos sobre SFL…"), self._on_open_sfl, object_name="danger",
            tooltip=t("Audita los despachos cuyo volumen excede el Safe Fill Level del "
                      "equipo (sobrellenado), en vivo desde el endpoint."))
        fl.addWidget(self.btn_sfl)
        self.btn_burn = make_button(
            t("Auditar Burn Rate…"), self._on_open_burn_rate, object_name="danger",
            tooltip=t("Audita el burn rate (consumo L/h) por equipo y categoría, marca los "
                      "comportamientos anómalos y los grafica, en vivo desde el endpoint."))
        fl.addWidget(self.btn_burn)
        self.btn_hw = make_button(
            t("Salud de Hardware…"), self._on_open_hardware, object_name="danger",
            tooltip=t("Audita la salud del hardware: SMU en regresión/estancado, re-tagueo "
                      "RFID sospechoso y degradación de medidores; genera órdenes de trabajo."))
        fl.addWidget(self.btn_hw)
        self.btn_vd = make_button(
            t("Desviación de volumen…"), self._on_open_volume_deviation, object_name="danger",
            tooltip=t("Audita la desviación entre el volumen medido y el digitado de la guía "
                      "en cada entrega (sobre-facturación / medidor descalibrado)."))
        fl.addWidget(self.btn_vd)
        self.btn_th = make_button(
            t("Tag Hopping…"), self._on_open_tag_hopping, object_name="danger",
            tooltip=t("Audita el mismo tag despachando en dos lugares en un lapso imposible "
                      "(tag removido para robar combustible), en vivo desde el endpoint."))
        fl.addWidget(self.btn_th)
        self.btn_act = make_button(
            t("Equipos fantasma…"), self._on_open_activity, object_name="danger",
            tooltip=t("Audita la actividad: equipos In Service sin despachar (fantasmas), "
                      "equipos que trabajan más de lo que su tanque permite sin repostar "
                      "(combustible no registrado) y despachos frecuentes con SMU congelado "
                      "(repostado sin operar)."))
        fl.addWidget(self.btn_act)
        return bar

    def _on_open_equipment(self):
        # Import perezoso: pyqtgraph solo se carga al abrir el análisis.
        try:
            from msgq.ui.equipment_window import EquipmentWindow
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self, t("Falta pyqtgraph"),
                f"{t('No se pudo abrir el análisis de equipos:')}\n{exc}\n\n"
                f"{t('Instala la dependencia: pip install pyqtgraph')}")
            return
        self._eq_window = EquipmentWindow(self._db, self)
        self._eq_window.show()

    def _on_open_tanks(self):
        # Import perezoso: pyqtgraph solo se carga al abrir el análisis.
        try:
            from msgq.ui.tank_window import TankWindow
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self, t("Falta pyqtgraph"),
                f"{t('No se pudo abrir el análisis de equipos:')}\n{exc}\n\n"
                f"{t('Instala la dependencia: pip install pyqtgraph')}")
            return
        self._tank_window = TankWindow(self._db, self)
        self._tank_window.show()

    def _on_open_inventory(self):
        # Import perezoso: pyqtgraph solo se carga al abrir el módulo.
        try:
            from msgq.ui.inventory_window import InventoryWindow
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self, t("Falta pyqtgraph"),
                f"{t('No se pudo abrir el análisis de equipos:')}\n{exc}\n\n"
                f"{t('Instala la dependencia: pip install pyqtgraph')}")
            return
        self._inv_window = InventoryWindow(self._db, self)
        self._inv_window.show()

    def _on_open_sfl(self):
        # Import perezoso: pyqtgraph solo se carga al abrir el módulo.
        try:
            from msgq.ui.sfl_window import SFLWindow
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self, t("Falta pyqtgraph"),
                f"{t('No se pudo abrir el análisis de equipos:')}\n{exc}\n\n"
                f"{t('Instala la dependencia: pip install pyqtgraph')}")
            return
        self._sfl_window = SFLWindow(self._db, self)
        self._sfl_window.show()

    def _on_open_burn_rate(self):
        # Import perezoso: pyqtgraph solo se carga al abrir el módulo.
        try:
            from msgq.ui.burn_rate_window import BurnRateWindow
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self, t("Falta pyqtgraph"),
                f"{t('No se pudo abrir el análisis de equipos:')}\n{exc}\n\n"
                f"{t('Instala la dependencia: pip install pyqtgraph')}")
            return
        self._burn_window = BurnRateWindow(self._db, self)
        self._burn_window.show()

    def _on_open_hardware(self):
        # Import perezoso: pyqtgraph solo se carga al abrir el módulo.
        try:
            from msgq.ui.hardware_window import HardwareWindow
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self, t("Falta pyqtgraph"),
                f"{t('No se pudo abrir el análisis de equipos:')}\n{exc}\n\n"
                f"{t('Instala la dependencia: pip install pyqtgraph')}")
            return
        self._hw_window = HardwareWindow(self._db, self)
        self._hw_window.show()

    def _on_open_volume_deviation(self):
        # Import perezoso: pyqtgraph solo se carga al abrir el módulo.
        try:
            from msgq.ui.volume_deviation_window import VolumeDeviationWindow
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self, t("Falta pyqtgraph"),
                f"{t('No se pudo abrir el análisis de equipos:')}\n{exc}\n\n"
                f"{t('Instala la dependencia: pip install pyqtgraph')}")
            return
        self._vd_window = VolumeDeviationWindow(self._db, self)
        self._vd_window.show()

    def _on_open_tag_hopping(self):
        # Import perezoso: pyqtgraph solo se carga al abrir el módulo.
        try:
            from msgq.ui.tag_hopping_window import TagHoppingWindow
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self, t("Falta pyqtgraph"),
                f"{t('No se pudo abrir el análisis de equipos:')}\n{exc}\n\n"
                f"{t('Instala la dependencia: pip install pyqtgraph')}")
            return
        self._th_window = TagHoppingWindow(self._db, self)
        self._th_window.show()

    def _on_open_activity(self):
        # Import perezoso: pyqtgraph solo se carga al abrir el módulo.
        try:
            from msgq.ui.activity_window import ActivityWindow
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self, t("Falta pyqtgraph"),
                f"{t('No se pudo abrir el análisis de equipos:')}\n{exc}\n\n"
                f"{t('Instala la dependencia: pip install pyqtgraph')}")
            return
        self._act_window = ActivityWindow(self._db, self)
        self._act_window.show()

    def _effective_db_path(self) -> str:
        """Ruta del replica segun el modo: demo en archivo aparte, real en el suyo."""
        return demo_db_path(self._live_db) if self._settings.demo_mode else self._live_db

    def _heal_replica(self) -> None:
        """En modo PRODUCCION elimina movimientos del simulador que hubieran
        quedado en el replica (datos demo que contaminaban la auditoria SFL)."""
        if self._settings.demo_mode:
            return
        try:
            n = self._db.purge_simulator_movements()
        except Exception:  # noqa: BLE001
            return
        if n:
            self.statusBar().showMessage(
                f"{t('Datos de demo eliminados del replica de producción:')} {n:,}")

    def _make_tray(self):
        """Icono de bandeja para la alarma de escritorio (None si no hay bandeja)."""
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                return None
            icon = self.style().standardIcon(QStyle.SP_MessageBoxWarning)
            tray = QSystemTrayIcon(icon, self)
            tray.setToolTip("MSGQ — Newmont Merian")
            tray.show()
            return tray
        except Exception:  # noqa: BLE001
            return None

    def _build_kiosk_banner(self) -> QGroupBox:
        """Banner de la version turnkey (sin token por pantalla) + acceso al análisis.
        Estado y selectores arriba; los botones de auditoría van en una barra fluida
        (FlowLayout) que se reacomoda en varias filas al redimensionar, sin truncarse."""
        box = QGroupBox(t("Monitoreo en tiempo real"))
        col = QVBoxLayout(box)
        col.setSpacing(8)

        top = QHBoxLayout()
        live = QLabel(f"●  {t('EN VIVO')}")
        live.setStyleSheet("color:#2E7D32; font-weight:bold;")
        top.addWidget(live)
        site = self._settings.site_id or self._settings.site_match
        info = QLabel(f"{self._settings.endpoint}    ·    {t('Site:')} {site}    ·    "
                      f"{t('actualiza cada')} {self._settings.poll_seconds}s")
        info.setStyleSheet("color:#5A6B7B;")
        top.addWidget(info)
        top.addStretch(1)
        top.addWidget(language_selector(self._on_language_changed))
        top.addWidget(theme_selector(self._on_theme_changed))
        col.addLayout(top)

        col.addWidget(self._build_action_bar())
        return box

    def _build_kpi_strip(self) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet(f"QFrame{{background:{theme.panel_bg()}; border-radius:6px;}}")
        # FlowLayout: las tarjetas KPI se reacomodan en varias filas si la ventana es
        # angosta (en vez de comprimirse o salirse). heightForWidth en el frame deja
        # que el layout padre le reserve el alto correcto al envolver.
        self._kpi_layout = FlowLayout(frame, h_spacing=8, v_spacing=6)
        self._kpi_layout.setContentsMargins(8, 6, 8, 6)
        self._kpi_layout.addWidget(QLabel(t("Sin datos todavia.")))
        sp = frame.sizePolicy()
        sp.setHeightForWidth(True)
        frame.setSizePolicy(sp)
        return frame

    def _build_tabs(self) -> QTabWidget:
        self.tabs = QTabWidget()

        self.tabs.addTab(self._build_movements_tab(), t("Movimientos"))

        self.tbl_eq, self.m_eq = make_table()
        self.tabs.addTab(wrap_with_search(self.tbl_eq), t("Equipos"))

        self.tbl_mac, self.m_mac = make_table()
        self.tabs.addTab(wrap_with_search(self.tbl_mac), t("Consolas AdaptMAC"))

        self.tabs.addTab(self._build_alerts_tab(), t("Alertas"))
        return self.tabs

    def _build_movements_tab(self) -> QWidget:
        """Movimientos divididos por tipo en sub-pestañas, como AdaptIQ (Despachos /
        Transferencias / Entregas), cada una con sus columnas pertinentes."""
        sub = QTabWidget()
        self.tbl_disp, self.m_disp = make_table()
        sub.addTab(wrap_with_search(self.tbl_disp), t("Despachos"))
        self.tbl_trans, self.m_trans = make_table()
        sub.addTab(wrap_with_search(self.tbl_trans), t("Transferencias"))
        self.tbl_deliv, self.m_deliv = make_table()
        sub.addTab(wrap_with_search(self.tbl_deliv), t("Entregas"))
        return sub

    def _build_alerts_tab(self) -> QWidget:
        c = QWidget()
        lay = QVBoxLayout(c)
        lay.setContentsMargins(4, 4, 4, 4)
        inner = QTabWidget()

        self.tbl_alerts, self.m_alerts = make_table()
        inner.addTab(wrap_with_search(self.tbl_alerts), t("Todas"))

        self.tbl_alert_sum, self.m_alert_sum = make_table()
        inner.addTab(self.tbl_alert_sum, t("Resumen ejecutivo"))

        lay.addWidget(inner)
        return c

    # =======================================================================
    # Control del poller
    # =======================================================================

    def _on_start(self):
        demo = self.chk_demo.isChecked()
        token = self.ed_token.text().strip()
        if not demo and not token:
            QMessageBox.warning(
                self, t("Falta token"),
                t("Para conectar a la API real necesitas un token, o activa el "
                  "modo demo (simulador)."))
            return

        self._settings.endpoint = self.ed_endpoint.text().strip()
        self._settings.token = token
        self._settings.poll_seconds = int(self.spin_poll.value())
        self._settings.demo_mode = demo
        # Site: si es numerico se toma como id; si no, como texto a buscar.
        site_val = self.ed_site.text().strip()
        if site_val.isdigit():
            self._settings.site_id = site_val
        else:
            self._settings.site_id = ""
            self._settings.site_match = site_val or "Merian"
        self._start_monitoring()

    def _start_monitoring(self):
        """Arranca el poller con la configuracion actual (manual o embebida)."""
        self._stop_poller()  # por si habia uno corriendo
        # El replica debe corresponder al modo (demo en archivo aparte) y, si es
        # produccion, no debe contener datos del simulador.
        desired = self._effective_db_path()
        if desired != self._db.path:
            try:
                self._db.close()
            except Exception:  # noqa: BLE001
                pass
            self._db = Database(desired)
        self._heal_replica()
        self._poller = Poller(self._settings, self._db)
        self._poller.cycle_completed.connect(self._on_cycle)
        self._poller.status.connect(self.statusBar().showMessage)
        self._poller.failed.connect(self._on_failed)
        self._poller.start()
        self._refresh_timer.start()
        self._monitoring = True
        log.info("Monitoreo iniciado (modo=%s, db=%s, poll=%ss)",
                 "DEMO" if self._settings.demo_mode else "API",
                 self._db.path, self._settings.poll_seconds)
        if not self._kiosk:
            self.btn_start.setEnabled(False)
            self.btn_stop.setEnabled(True)
            self._set_inputs_enabled(False)

    def _on_stop(self):
        self._stop_poller()
        self._refresh_timer.stop()
        self._monitoring = False
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._set_inputs_enabled(True)
        self.statusBar().showMessage(t("Monitoreo detenido."))

    def _stop_poller(self):
        if self._poller is not None:
            self._poller.stop()
            self._poller.wait(5000)
            self._poller = None

    def _set_inputs_enabled(self, enabled: bool):
        for w in (self.ed_endpoint, self.ed_token, self.ed_site,
                  self.spin_poll, self.chk_demo):
            w.setEnabled(enabled)

    # =======================================================================
    # Importacion de snapshots locales (CSV)
    # =======================================================================

    def _on_import_equipment(self):
        path, _ = QFileDialog.getOpenFileName(
            self, t("Importar CSV de equipos de AdaptIQ"), "",
            t("CSV (*.csv);;Todos (*.*)"))
        if not path:
            return
        try:
            df = load_equipment_csv(path)
            n = self._db.upsert("equipment", df)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al importar"),
                                 f"{exc}\n\n{traceback.format_exc()}")
            return
        self._refresh_views()
        QMessageBox.information(
            self, t("Equipos importados"),
            tr_fmt("import.success", n=n, file=os.path.basename(path)))

    # =======================================================================
    # Señales del poller
    # =======================================================================

    def _on_cycle(self, stats: dict):
        # Refresco inmediato y completo al cerrar un ciclo de sincronizacion.
        log.info("Ciclo de sync: %s",
                 ", ".join(f"{k}={v}" for k, v in stats.items()) or "sin cambios")
        self._refresh_views(force=True)

    def _on_failed(self, message: str):
        log.warning("Error de sincronizacion: %s", message)
        self.statusBar().showMessage(f"⚠ {t('Error de sincronizacion')}: {message}")

    # =======================================================================
    # Refresco de vistas (lee de la replica SQLite)
    # =======================================================================

    def _refresh_views(self, force: bool = False):
        # Evita recalcular si la replica no cambio (el QTimer dispara seguido,
        # pero los datos solo cambian por poll): mantiene la UI fluida.
        try:
            counts = (self._db.row_count("movements"),
                      self._db.row_count("equipment"),
                      self._db.row_count("adaptmac"))
        except Exception:  # noqa: BLE001
            counts = None
        if not force and counts is not None and counts == getattr(self, "_last_counts", None):
            return
        self._last_counts = counts

        # Las alertas PESADAS (burn/hw/producto/desviacion/tag hopping) recorren todo
        # el historico: se calculan en SEGUNDO PLANO (no congelar la GUI). Aqui solo
        # se decide si hay que relanzar el worker; el panel se arma con la cache.
        mv_count = counts[0] if counts else None
        try:
            chg_count = self._db.row_count("change_events")
        except Exception:  # noqa: BLE001
            chg_count = None
        need_heavy = (
            mv_count != self._burn_count
            or (mv_count, chg_count) != self._hw_key
            or mv_count != self._product_count
            or mv_count != self._vd_count
            or mv_count != self._th_count
            or mv_count != self._activity_count
        )
        if need_heavy:
            self._maybe_spawn_alerts_worker()

        # Lecturas + deteccion liviana en SEGUNDO PLANO (un worker a la vez; si
        # llega otro refresco mientras carga, se relanza al terminar).
        if self._views_worker is not None and self._views_worker.isRunning():
            self._views_pending = True
            return
        heavy = {"burn": self._burn_alerts, "hw": self._hw_alerts,
                 "product": self._product_alerts, "vd": self._vd_alerts,
                 "th": self._th_alerts, "activity": self._activity_alerts}
        worker = _ViewsWorker(self._db.path, heavy, self)
        worker.done.connect(self._on_views_ready)
        worker.failed.connect(self._on_views_failed)
        worker.finished.connect(worker.deleteLater)
        self._views_worker = worker
        worker.start()

    def _on_views_ready(self, out: dict) -> None:
        """Proyecta en la interfaz lo que el worker preparo (solo trabajo de Qt)."""
        self._views_worker = None
        self.m_disp.set_dataframe(out["disp"])
        self.m_trans.set_dataframe(out["trans"])
        self.m_deliv.set_dataframe(out["deliv"])
        self.m_eq.set_dataframe(out["eq"])
        self.m_mac.set_dataframe(out["mac"])
        self.m_alerts.set_dataframe(out["alerts"])
        self.m_alert_sum.set_dataframe(out["summary"])
        self._refresh_kpis(out["kpis"])
        self._notify_sfl(out["sfl_live"])
        self._notify_burn_rate(self._burn_alerts)
        self._notify_hardware(self._hw_alerts)
        self._notify_tag_hopping(self._th_alerts)
        if self._views_pending:        # la replica cambio mientras se cargaba
            self._views_pending = False
            self._refresh_views(force=True)

    def _on_views_failed(self, message: str) -> None:
        self._views_worker = None
        self._views_pending = False
        self._last_counts = None       # fuerza el reintento en el proximo tick
        self.statusBar().showMessage(f"{t('Error leyendo la replica')}: {message}")

    def _alerts_executor(self):
        """Pool de UN proceso para las alertas pesadas (creado perezosamente). Si no
        se puede crear (entorno sin multiprocessing), devuelve None y el worker cae a
        calcular en-hilo (peor para la fluidez, pero las alertas siguen saliendo)."""
        if self._alerts_pool is None:
            try:
                from concurrent.futures import ProcessPoolExecutor
                self._alerts_pool = ProcessPoolExecutor(max_workers=1)
                log.info("Pool de proceso para alertas pesadas creado.")
            except Exception as exc:  # noqa: BLE001
                log.warning("No se pudo crear el pool de proceso (%s); calculo en-hilo.", exc)
                self._alerts_pool = False   # marca 'intentado y fallo' (no reintentar)
        return self._alerts_pool or None

    def _maybe_spawn_alerts_worker(self) -> None:
        """Lanza el worker de alertas pesadas SOLO si: (a) no hay uno corriendo,
        (b) NO hay un backfill masivo activo (durante el backfill el conteo cambia
        sin parar y el calculo de minutos se encolaria al infinito), y (c) paso el
        intervalo de debounce. Asi nunca se recalcula 'en cada pagina que entra'."""
        if self._alerts_worker is not None and self._alerts_worker.isRunning():
            return
        # Durante el backfill historico NO se recalculan las alertas pesadas: los
        # datos cambian constantemente; se calcularan una vez al completarse.
        try:
            if self._db.get_flag("movements_backfill_done") != "1":
                return
        except Exception:  # noqa: BLE001
            pass
        now = time.monotonic()
        if now - self._last_heavy_spawn < _HEAVY_ALERTS_MIN_INTERVAL:
            return
        self._last_heavy_spawn = now
        worker = _AlertsWorker(self._db.path, self._alerts_executor(), self)
        worker.done.connect(self._on_heavy_alerts)
        worker.finished.connect(worker.deleteLater)
        self._alerts_worker = worker
        worker.start()

    def _on_heavy_alerts(self, result: dict) -> None:
        """Recibe del worker las alertas pesadas ya calculadas y refresca el panel."""
        self._alerts_worker = None
        if result.get("error"):
            self.statusBar().showMessage(
                f"{t('Error calculando alertas')}: {result['error']}")
            return
        self._burn_alerts = result.get("burn", self._burn_alerts)
        self._hw_alerts = result.get("hw", self._hw_alerts)
        self._product_alerts = result.get("product", self._product_alerts)
        self._vd_alerts = result.get("vd", self._vd_alerts)
        self._th_alerts = result.get("th", self._th_alerts)
        self._activity_alerts = result.get("activity", self._activity_alerts)
        mv_count, chg_count = result.get("counts", (None, None))
        self._burn_count = mv_count
        self._hw_key = (mv_count, chg_count)
        self._product_count = mv_count
        self._vd_count = mv_count
        self._th_count = mv_count
        self._activity_count = mv_count
        # Rearma el panel con la cache recien actualizada: el worker de vistas
        # relee las alertas livianas (24h) en segundo plano y las combina.
        self._refresh_views(force=True)

    def _notify_sfl(self, sfl_alerts):
        """Notificación de escritorio (toast) al detectar despachos sobre SFL nuevos.
        En la primera carga solo memoriza los existentes (no notifica el histórico)."""
        if self._tray is None:
            return
        ids = (set() if sfl_alerts is None or sfl_alerts.empty
               else set(sfl_alerts["source_id"].dropna().astype(str)))
        if not self._sfl_initialized:
            self._seen_sfl_ids = ids
            self._sfl_initialized = True
            return
        new = ids - self._seen_sfl_ids
        self._seen_sfl_ids |= ids
        if not new:
            return
        new_alerts = sfl_alerts[sfl_alerts["source_id"].astype(str).isin(new)]
        if new_alerts.empty:
            return
        n = len(new_alerts)
        detail = str(new_alerts.iloc[0].get("detail") or "")
        msg = (f"{n} {t('despachos sobre SFL nuevos')} — {detail}" if n > 1 else detail)
        try:
            self._tray.showMessage(
                t("Alarma: despacho sobre Safe Fill Level"), msg,
                QSystemTrayIcon.Critical, 10000)
        except Exception:  # noqa: BLE001
            pass

    def _notify_burn_rate(self, burn_alerts):
        """Notificación de escritorio al detectar equipos con burn rate anómalo
        NUEVOS (por equipo). En la primera carga solo memoriza los existentes."""
        if self._tray is None:
            return
        ids = (set() if burn_alerts is None or burn_alerts.empty
               else set(burn_alerts["source_id"].dropna().astype(str)))
        if not self._burn_initialized:
            self._seen_burn_ids = ids
            self._burn_initialized = True
            return
        new = ids - self._seen_burn_ids
        self._seen_burn_ids |= ids
        if not new:
            return
        new_alerts = burn_alerts[burn_alerts["source_id"].astype(str).isin(new)]
        if new_alerts.empty:
            return
        n = len(new_alerts)
        detail = str(new_alerts.iloc[0].get("detail") or "")
        msg = (f"{n} {t('equipos con burn rate anómalo nuevos')} — {detail}" if n > 1 else detail)
        try:
            self._tray.showMessage(
                t("Alarma: burn rate anómalo"), msg, QSystemTrayIcon.Warning, 10000)
        except Exception:  # noqa: BLE001
            pass

    def _notify_hardware(self, hw_alerts):
        """Notificación de escritorio al detectar problemas de hardware NUEVOS
        (sensor SMU, re-tagueo, medidor). En la primera carga solo memoriza."""
        if self._tray is None:
            return
        ids = (set() if hw_alerts is None or hw_alerts.empty
               else set(hw_alerts["source_id"].dropna().astype(str)))
        if not self._hw_initialized:
            self._seen_hw_ids = ids
            self._hw_initialized = True
            return
        new = ids - self._seen_hw_ids
        self._seen_hw_ids |= ids
        if not new:
            return
        new_alerts = hw_alerts[hw_alerts["source_id"].astype(str).isin(new)]
        if new_alerts.empty:
            return
        n = len(new_alerts)
        detail = str(new_alerts.iloc[0].get("detail") or "")
        msg = (f"{n} {t('problemas de hardware nuevos')} — {detail}" if n > 1 else detail)
        try:
            self._tray.showMessage(
                t("Alarma: salud de hardware"), msg, QSystemTrayIcon.Warning, 10000)
        except Exception:  # noqa: BLE001
            pass

    def _notify_tag_hopping(self, th_alerts):
        """Notificación de escritorio al detectar eventos de tag hopping NUEVOS
        (mismo tag en dos lugares en un lapso imposible). En la primera carga solo
        memoriza los existentes (no notifica el histórico)."""
        if self._tray is None:
            return
        ids = (set() if th_alerts is None or th_alerts.empty
               else set(th_alerts["source_id"].dropna().astype(str)))
        if not self._th_initialized:
            self._seen_th_ids = ids
            self._th_initialized = True
            return
        new = ids - self._seen_th_ids
        self._seen_th_ids |= ids
        if not new:
            return
        new_alerts = th_alerts[th_alerts["source_id"].astype(str).isin(new)]
        if new_alerts.empty:
            return
        n = len(new_alerts)
        detail = str(new_alerts.iloc[0].get("detail") or "")
        msg = (f"{n} {t('eventos de tag hopping nuevos')} — {detail}" if n > 1 else detail)
        try:
            self._tray.showMessage(
                t("Alarma: tag en dos lugares (posible robo)"), msg,
                QSystemTrayIcon.Critical, 10000)
        except Exception:  # noqa: BLE001
            pass

    def _refresh_kpis(self, k: dict):
        while self._kpi_layout.count():
            it = self._kpi_layout.takeAt(0)
            if it.widget() is not None:
                it.widget().deleteLater()

        cards = [
            kpi_label(t("Movimientos (24h)"), f"{k['movimientos']:,}", PRIMARY),
            kpi_label(t("Volumen 24h (L)"), f"{k['volumen_total']:,.0f}", PRIMARY),
            warn_label(t("Alertas criticas"), f"{k['criticas']:,}", warn=k["criticas"] > 0),
            warn_label(t("Advertencias"), f"{k['advertencias']:,}", warn=k["advertencias"] > 0),
            kpi_label(t("Equipos In Service"), f"{k['equipos_in_service']:,}", ACCENT),
            warn_label(t("Out of Service"), f"{k['equipos_out_service']:,}", warn=k["equipos_out_service"] > 0),
            kpi_label(t("Consolas online"), f"{k['consolas_online']}/{k['consolas_total']}",
                      ACCENT if k["consolas_online"] == k["consolas_total"] else DANGER),
        ]
        for c in cards:
            self._kpi_layout.addWidget(c)

    # =======================================================================
    # Cierre
    # =======================================================================

    def closeEvent(self, event):  # noqa: N802 - override Qt
        self._refresh_timer.stop()
        self._stop_poller()
        # Espera a los workers en vuelo para no destruirlos en ejecucion.
        if self._views_worker is not None and self._views_worker.isRunning():
            self._views_worker.wait(3000)
        if self._alerts_worker is not None and self._alerts_worker.isRunning():
            self._alerts_worker.wait(3000)
        # Cierra el pool de proceso de alertas (evita procesos huerfanos).
        if self._alerts_pool:
            try:
                self._alerts_pool.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001
                pass
        try:
            self._db.close()
        except Exception:  # noqa: BLE001
            pass
        log.info("Ventana principal cerrada; recursos liberados.")
        event.accept()


def launch() -> int:
    # Asegura el logging aunque se entre por aqui directamente (no via run.py).
    from msgq.logging_setup import setup_logging
    setup_logging()
    log.info("Iniciando MSGQ (interfaz)…")
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setOrganizationName("NewmontMerian")
    app.setApplicationName("MSGQ")
    try:
        window = MainWindow()
    except Exception:  # noqa: BLE001
        log.exception("Fallo al construir la ventana principal")
        traceback.print_exc()
        raise
    window.show()
    log.info("Interfaz lista.")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(launch())
