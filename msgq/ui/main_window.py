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
import traceback

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QSpinBox, QStyle, QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget,
)

from msgq.config import Settings, load_embedded_settings, demo_db_path, DEFAULT_DB_PATH
from msgq.core import alerts as al
from msgq.i18n import LANGUAGES, current_language, set_language, t, tr_fmt
from msgq.ingest import Poller
from msgq.io import load_equipment_csv
from msgq.storage import Database
from msgq.ui import theme
from msgq.ui.common import (
    kpi_label, language_selector, make_table, theme_selector, warn_label, wrap_with_search,
)

# Colores semánticos de las tarjetas KPI (kpi_label los ajusta al tema activo).
PRIMARY = "#1F4E78"
ACCENT = "#2E7D32"
DANGER = "#C62828"

# Refresco visual desacoplado del polling (lee de SQLite).
_REFRESH_MS = 2000


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
        # Alarma de escritorio para despachos sobre Safe Fill Level (SFL).
        self._tray = self._make_tray()
        self._seen_sfl_ids: set[str] = set()
        self._sfl_initialized = False

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
        for child in (self._eq_window, self._tank_window, self._inv_window, self._sfl_window):
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

        self.btn_start = QPushButton(t("Iniciar monitoreo"))
        self.btn_start.setObjectName("accent")
        self.btn_start.clicked.connect(self._on_start)
        r2.addWidget(self.btn_start)

        self.btn_stop = QPushButton(t("Detener"))
        self.btn_stop.setObjectName("danger")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop)
        r2.addWidget(self.btn_stop)
        col.addLayout(r2)

        # Fila 3 — carga de snapshots locales (sin token ni red).
        r3 = QHBoxLayout()
        self.btn_import_eq = QPushButton(t("Importar equipos (CSV de AdaptIQ)…"))
        self.btn_import_eq.clicked.connect(self._on_import_equipment)
        r3.addWidget(self.btn_import_eq)
        hint = QLabel(t("Carga el maestro completo de equipos desde un export CSV "
                        "(no requiere token ni red)."))
        hint.setStyleSheet("color:#5A6B7B;")
        r3.addWidget(hint)
        r3.addStretch(1)
        self.btn_analyze = QPushButton(t("Analizar equipos…"))
        self.btn_analyze.setObjectName("accent")
        self.btn_analyze.setToolTip(t(
            "Abre el análisis de flota: filtros, frecuencia de cambio de RFID, "
            "transiciones In↔Out, auditoría y gráficas."))
        self.btn_analyze.clicked.connect(self._on_open_equipment)
        r3.addWidget(self.btn_analyze)
        self.btn_tanks = QPushButton(t("Analizar tanques…"))
        self.btn_tanks.setToolTip(t(
            "Abre el análisis de tanques: reconciliación, niveles, despachos y gráficas "
            "(en vivo desde el endpoint)."))
        self.btn_tanks.clicked.connect(self._on_open_tanks)
        r3.addWidget(self.btn_tanks)
        self.btn_inventory = QPushButton(t("Inventario de tags RFID…"))
        self.btn_inventory.setToolTip(t(
            "Abre el reporte de instalación de tags RFID (alta/reemplazo/remoción) con "
            "la fecha real del cambio, en vivo desde el endpoint."))
        self.btn_inventory.clicked.connect(self._on_open_inventory)
        r3.addWidget(self.btn_inventory)
        self.btn_sfl = QPushButton(t("Despachos sobre SFL…"))
        self.btn_sfl.setObjectName("danger")
        self.btn_sfl.setToolTip(t(
            "Audita los despachos cuyo volumen excede el Safe Fill Level del equipo "
            "(sobrellenado), en vivo desde el endpoint."))
        self.btn_sfl.clicked.connect(self._on_open_sfl)
        r3.addWidget(self.btn_sfl)
        col.addLayout(r3)
        return box

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
        """Banner de la version turnkey (sin token por pantalla) + acceso al análisis."""
        box = QGroupBox(t("Monitoreo en tiempo real"))
        row = QHBoxLayout(box)
        live = QLabel(f"●  {t('EN VIVO')}")
        live.setStyleSheet("color:#2E7D32; font-weight:bold;")
        row.addWidget(live)
        site = self._settings.site_id or self._settings.site_match
        info = QLabel(f"{self._settings.endpoint}    ·    {t('Site:')} {site}    ·    "
                      f"{t('actualiza cada')} {self._settings.poll_seconds}s")
        info.setStyleSheet("color:#5A6B7B;")
        row.addWidget(info)
        row.addStretch(1)
        row.addWidget(language_selector(self._on_language_changed))
        row.addWidget(theme_selector(self._on_theme_changed))
        self.btn_analyze = QPushButton(t("Analizar equipos…"))
        self.btn_analyze.setObjectName("accent")
        self.btn_analyze.clicked.connect(self._on_open_equipment)
        row.addWidget(self.btn_analyze)
        self.btn_tanks = QPushButton(t("Analizar tanques…"))
        self.btn_tanks.clicked.connect(self._on_open_tanks)
        row.addWidget(self.btn_tanks)
        self.btn_inventory = QPushButton(t("Inventario de tags RFID…"))
        self.btn_inventory.clicked.connect(self._on_open_inventory)
        row.addWidget(self.btn_inventory)
        self.btn_sfl = QPushButton(t("Despachos sobre SFL…"))
        self.btn_sfl.setObjectName("danger")
        self.btn_sfl.clicked.connect(self._on_open_sfl)
        row.addWidget(self.btn_sfl)
        return box

    def _build_kpi_strip(self) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet(f"QFrame{{background:{theme.panel_bg()}; border-radius:6px;}}")
        self._kpi_layout = QHBoxLayout(frame)
        self._kpi_layout.setContentsMargins(8, 6, 8, 6)
        self._kpi_layout.addWidget(QLabel(t("Sin datos todavia.")))
        self._kpi_layout.addStretch(1)
        return frame

    def _build_tabs(self) -> QTabWidget:
        self.tabs = QTabWidget()

        self.tbl_mov, self.m_mov = make_table()
        self.tabs.addTab(wrap_with_search(self.tbl_mov), t("Movimientos"))

        self.tbl_eq, self.m_eq = make_table()
        self.tabs.addTab(wrap_with_search(self.tbl_eq), t("Equipos"))

        self.tbl_mac, self.m_mac = make_table()
        self.tabs.addTab(wrap_with_search(self.tbl_mac), t("Consolas AdaptMAC"))

        self.tabs.addTab(self._build_alerts_tab(), t("Alertas"))
        return self.tabs

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

    def _on_cycle(self, _stats: dict):
        # Refresco inmediato y completo al cerrar un ciclo de sincronizacion.
        self._refresh_views(force=True)

    def _on_failed(self, message: str):
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
        try:
            mv = self._db.get_movements(limit=1000)
            eq = self._db.get_equipment()
            mac = self._db.get_adaptmac()
            recent = self._db.recent_movements(hours=24)
            limits = self._db.get_consumption_limits()
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"{t('Error leyendo la replica')}: {exc}")
            return

        self.m_mov.set_dataframe(mv)
        self.m_eq.set_dataframe(eq)
        self.m_mac.set_dataframe(mac)

        sfl_alerts = al.detect_sfl_alerts(recent, limits)
        sfl_conflicts = al.detect_sfl_conflict_alerts(recent, limits)
        all_alerts = al.combine(
            al.detect_movement_alerts(recent),
            al.detect_adaptmac_alerts(mac),
            sfl_alerts,
            sfl_conflicts,
        )
        self.m_alerts.set_dataframe(all_alerts)
        self.m_alert_sum.set_dataframe(al.alert_summary(all_alerts))

        kpis = al.compute_kpis(recent, eq, mac, all_alerts)
        self._refresh_kpis(kpis)
        self._notify_sfl(al.combine(sfl_alerts, sfl_conflicts))

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
        self._kpi_layout.addStretch(1)

    # =======================================================================
    # Cierre
    # =======================================================================

    def closeEvent(self, event):  # noqa: N802 - override Qt
        self._refresh_timer.stop()
        self._stop_poller()
        try:
            self._db.close()
        except Exception:  # noqa: BLE001
            pass
        event.accept()


def launch() -> int:
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setOrganizationName("NewmontMerian")
    app.setApplicationName("MSGQ")
    try:
        window = MainWindow()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(launch())
