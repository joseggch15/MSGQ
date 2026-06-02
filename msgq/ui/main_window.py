"""Ventana principal del monitor FMS AdaptIQ (Newmont Merian).

Dashboard de escritorio que:

  • Configura la conexion (endpoint, token, intervalo de polling, modo demo).
  • Arranca/detiene el motor de polling (`ingest.Poller`, en su propio hilo).
  • Refresca ciclicamente las vistas leyendo de la replica SQLite (un `QTimer`,
    igual que en los reportes), nunca de la API directamente.
  • Proyecta KPIs, movimientos, equipos, consolas AdaptMAC y un panel de alertas
    de trazabilidad (bypass, despacho a equipo no operativo, contaminacion...).

Ejecutar:  python run.py
"""
from __future__ import annotations

import os
import traceback

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFrame, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget,
)

from msgq.config import Settings
from msgq.core import alerts as al
from msgq.ingest import Poller
from msgq.io import load_equipment_csv
from msgq.storage import Database
from msgq.ui.common import kpi_label, make_table, warn_label, wrap_with_search

PRIMARY = "#1F4E78"
ACCENT = "#2E7D32"
DANGER = "#C62828"
BG = "#F4F6F9"

STYLESHEET = f"""
QMainWindow, QWidget {{ background: {BG}; color: #1A1A1A; }}
QGroupBox {{
    font-weight: bold; color: {PRIMARY};
    border: 1px solid #C9D3DF; border-radius: 8px;
    margin-top: 14px; padding: 10px;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; }}
QPushButton {{
    background: {PRIMARY}; color: white; border: none;
    border-radius: 6px; padding: 7px 14px; font-weight: bold;
}}
QPushButton:hover {{ background: #2A5F92; }}
QPushButton:disabled {{ background: #9AA8B8; color: white; }}
QPushButton#accent {{ background: {ACCENT}; }}
QPushButton#danger {{ background: {DANGER}; }}
QTabWidget::pane {{ border: 1px solid #C9D3DF; border-radius: 6px; background: white; }}
QTabBar::tab {{
    background: #E3E9F0; color: #1A1A1A; padding: 8px 16px; margin-right: 2px;
    border-top-left-radius: 6px; border-top-right-radius: 6px;
}}
QTabBar::tab:selected {{ background: white; color: {PRIMARY}; font-weight: bold; }}
QTableView {{
    background: white; alternate-background-color: #EAF1F8;
    gridline-color: #DCE3EB; color: #1A1A1A;
    selection-background-color: #D0E4F7; selection-color: #1A1A1A;
}}
QHeaderView::section {{
    background: {PRIMARY}; color: white; padding: 6px; border: none; font-weight: bold;
}}
QLineEdit, QSpinBox {{
    background: white; color: #1A1A1A; border: 1px solid #C9D3DF;
    border-radius: 5px; padding: 4px;
}}
"""

# Refresco visual desacoplado del polling (lee de SQLite).
_REFRESH_MS = 2000


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("MSGQ — Monitor FMS AdaptIQ  ·  Newmont Merian")
        self.resize(1480, 880)
        self.setMinimumSize(1100, 700)
        self.setStyleSheet(STYLESHEET)

        self._settings = Settings.from_env()
        self._db = Database(self._settings.db_path)
        self._poller: Poller | None = None

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(_REFRESH_MS)
        self._refresh_timer.timeout.connect(self._refresh_views)

        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setSpacing(6)
        lay.addWidget(self._build_connection())
        lay.addWidget(self._build_kpi_strip())
        lay.addWidget(self._build_tabs(), stretch=1)

        self.statusBar().showMessage("Listo. Configura la conexion y pulsa «Iniciar monitoreo».")
        self._refresh_views()  # muestra lo que ya hubiera en la replica

    # =======================================================================
    # Construccion de la interfaz
    # =======================================================================

    def _build_connection(self) -> QGroupBox:
        box = QGroupBox("Conexion y motor de polling")
        col = QVBoxLayout(box)

        r1 = QHBoxLayout()
        r1.addWidget(QLabel("Endpoint GraphQL:"))
        self.ed_endpoint = QLineEdit(self._settings.endpoint)
        r1.addWidget(self.ed_endpoint, stretch=2)
        r1.addWidget(QLabel("Token:"))
        self.ed_token = QLineEdit(self._settings.token)
        self.ed_token.setEchoMode(QLineEdit.Password)
        self.ed_token.setPlaceholderText("Authorization: Token token=<...>")
        r1.addWidget(self.ed_token, stretch=1)
        col.addLayout(r1)

        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Intervalo (s):"))
        self.spin_poll = QSpinBox()
        self.spin_poll.setRange(3, 600)
        self.spin_poll.setValue(self._settings.poll_seconds)
        r2.addWidget(self.spin_poll)
        r2.addSpacing(16)
        r2.addWidget(QLabel("Site:"))
        self.ed_site = QLineEdit(self._settings.site_id or self._settings.site_match)
        self.ed_site.setMaximumWidth(150)
        self.ed_site.setToolTip(
            "ID del sitio, o un texto del nombre (p. ej. 'Merian') para "
            "auto-descubrirlo via la query 'sites'.")
        r2.addWidget(self.ed_site)
        r2.addSpacing(16)
        self.chk_demo = QCheckBox("Modo demo (simulador, sin red)")
        self.chk_demo.setChecked(self._settings.demo_mode)
        r2.addWidget(self.chk_demo)
        r2.addStretch(1)

        self.btn_start = QPushButton("Iniciar monitoreo")
        self.btn_start.setObjectName("accent")
        self.btn_start.clicked.connect(self._on_start)
        r2.addWidget(self.btn_start)

        self.btn_stop = QPushButton("Detener")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop)
        r2.addWidget(self.btn_stop)
        col.addLayout(r2)

        # Fila 3 — carga de snapshots locales (sin token ni red).
        r3 = QHBoxLayout()
        self.btn_import_eq = QPushButton("Importar equipos (CSV de AdaptIQ)…")
        self.btn_import_eq.clicked.connect(self._on_import_equipment)
        r3.addWidget(self.btn_import_eq)
        hint = QLabel("Carga el maestro completo de equipos desde un export CSV "
                      "(no requiere token ni red).")
        hint.setStyleSheet("color:#5A6B7B;")
        r3.addWidget(hint)
        r3.addStretch(1)
        col.addLayout(r3)
        return box

    def _build_kpi_strip(self) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet("QFrame{background:#EDF1F6; border-radius:6px;}")
        self._kpi_layout = QHBoxLayout(frame)
        self._kpi_layout.setContentsMargins(8, 6, 8, 6)
        self._kpi_layout.addWidget(QLabel("Sin datos todavia."))
        self._kpi_layout.addStretch(1)
        return frame

    def _build_tabs(self) -> QTabWidget:
        self.tabs = QTabWidget()

        self.tbl_mov, self.m_mov = make_table()
        self.tabs.addTab(wrap_with_search(self.tbl_mov), "Movimientos")

        self.tbl_eq, self.m_eq = make_table()
        self.tabs.addTab(wrap_with_search(self.tbl_eq), "Equipos")

        self.tbl_mac, self.m_mac = make_table()
        self.tabs.addTab(wrap_with_search(self.tbl_mac), "Consolas AdaptMAC")

        self.tabs.addTab(self._build_alerts_tab(), "Alertas")
        return self.tabs

    def _build_alerts_tab(self) -> QWidget:
        c = QWidget()
        lay = QVBoxLayout(c)
        lay.setContentsMargins(4, 4, 4, 4)
        inner = QTabWidget()

        self.tbl_alerts, self.m_alerts = make_table()
        inner.addTab(wrap_with_search(self.tbl_alerts), "Todas")

        self.tbl_alert_sum, self.m_alert_sum = make_table()
        inner.addTab(self.tbl_alert_sum, "Resumen ejecutivo")

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
                self, "Falta token",
                "Para conectar a la API real necesitas un token, o activa el "
                "modo demo (simulador).")
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

        self._stop_poller()  # por si habia uno corriendo

        self._poller = Poller(self._settings, self._db)
        self._poller.cycle_completed.connect(self._on_cycle)
        self._poller.status.connect(self.statusBar().showMessage)
        self._poller.failed.connect(self._on_failed)
        self._poller.start()

        self._refresh_timer.start()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._set_inputs_enabled(False)

    def _on_stop(self):
        self._stop_poller()
        self._refresh_timer.stop()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._set_inputs_enabled(True)
        self.statusBar().showMessage("Monitoreo detenido.")

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
            self, "Importar CSV de equipos de AdaptIQ", "",
            "CSV (*.csv);;Todos (*.*)")
        if not path:
            return
        try:
            df = load_equipment_csv(path)
            n = self._db.upsert("equipment", df)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Error al importar",
                                 f"{exc}\n\n{traceback.format_exc()}")
            return
        self._refresh_views()
        QMessageBox.information(
            self, "Equipos importados",
            f"Se cargaron {n:,} equipos desde:\n{os.path.basename(path)}\n\n"
            "Sugerencia: deja el «Modo demo» apagado para que el simulador no "
            "sobrescriba estos registros.")

    # =======================================================================
    # Señales del poller
    # =======================================================================

    def _on_cycle(self, _stats: dict):
        # Refresco inmediato al cerrar un ciclo de sincronizacion.
        self._refresh_views()

    def _on_failed(self, message: str):
        self.statusBar().showMessage(f"⚠ Error de sincronizacion: {message}")

    # =======================================================================
    # Refresco de vistas (lee de la replica SQLite)
    # =======================================================================

    def _refresh_views(self):
        try:
            mv = self._db.get_movements(limit=1000)
            eq = self._db.get_equipment()
            mac = self._db.get_adaptmac()
            recent = self._db.recent_movements(hours=24)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Error leyendo la replica: {exc}")
            return

        self.m_mov.set_dataframe(mv)
        self.m_eq.set_dataframe(eq)
        self.m_mac.set_dataframe(mac)

        all_alerts = al.combine(
            al.detect_movement_alerts(recent),
            al.detect_adaptmac_alerts(mac),
        )
        self.m_alerts.set_dataframe(all_alerts)
        self.m_alert_sum.set_dataframe(al.alert_summary(all_alerts))

        kpis = al.compute_kpis(recent, eq, mac, all_alerts)
        self._refresh_kpis(kpis)

    def _refresh_kpis(self, k: dict):
        while self._kpi_layout.count():
            it = self._kpi_layout.takeAt(0)
            if it.widget() is not None:
                it.widget().deleteLater()

        cards = [
            kpi_label("Movimientos (24h)", f"{k['movimientos']:,}", PRIMARY),
            kpi_label("Volumen 24h (L)", f"{k['volumen_total']:,.0f}", PRIMARY),
            warn_label("Alertas criticas", f"{k['criticas']:,}", warn=k["criticas"] > 0),
            warn_label("Advertencias", f"{k['advertencias']:,}", warn=k["advertencias"] > 0),
            kpi_label("Equipos In Service", f"{k['equipos_in_service']:,}", ACCENT),
            warn_label("Out of Service", f"{k['equipos_out_service']:,}", warn=k["equipos_out_service"] > 0),
            kpi_label("Consolas online", f"{k['consolas_online']}/{k['consolas_total']}",
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
    try:
        window = MainWindow()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(launch())
