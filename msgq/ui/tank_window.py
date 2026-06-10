"""Ventana de análisis de tanques y reconciliación de combustible.

Equivale al **FMS Tank Analyzer** (proyecto TLS) pero alimentado EN VIVO desde el
endpoint de AdaptIQ (sin cargar CSVs): lee de la réplica SQLite que el poller
sincroniza (`reconciliations`, `tanks`, `movements`, `equipment`) y ofrece:

  • Reconciliación detallada por tanque (stock medido vs movimiento, error) — el
    reporte 'Detailed Reconciliation' que la API pre-calcula.
  • Reconciliación diaria (error día por día).
  • Niveles: stock final por día y por tanque (gráfico + tabla).
  • Despachos: consumo por tanque / producto / dimensión del equipo.
  • Gráficas: tendencia de stock, error por tanque, burn rate.

Separa por circuito (Diesel / Gasolina), nunca los mezcla. i18n + tema como el
resto de MSGQ; exporta todo a Excel en el idioma activo.
"""
from __future__ import annotations

import traceback

import pandas as pd
from PySide6.QtCore import Qt, QSettings, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QPushButton, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from msgq import config
from msgq.core import tank_analytics as ta
from msgq.export import export_sheets
from msgq.i18n import current_language, set_language, t, tr_fmt
from msgq.storage import Database
from msgq.ui import theme
from msgq.ui.charts import BarChart, TimeSeriesChart
from msgq.ui.common import (
    BusyOverlay, kpi_label, language_selector, make_table, theme_selector,
    warn_label, wrap_with_search,
)

# Opciones de rango temporal: (etiqueta canónica, días hacia atrás | None=todo).
_RANGES = [("Todo el rango", None), ("Últimos 7 días", 7),
           ("Últimos 30 días", 30), ("Últimos 90 días", 90), ("Últimos 12 meses", 365)]


class _LoadWorker(QThread):
    """Lee la réplica en un hilo aparte para no congelar la interfaz: la lectura
    de 20.000 movimientos en el hilo de la GUI era lo que trababa esta ventana
    en cada auto-refresco. Emite los DataFrames listos; la GUI solo proyecta."""

    done = Signal(object, object, object, object, object)  # recons, tanks, mov, eq, counts
    failed = Signal(str)

    # Columnas de `movements` que el análisis de tanques consume (tank_analytics):
    # filtrar el SELECT reduce varias veces la conversión SQLite -> DataFrame.
    _MOVEMENT_COLS = [
        "id", "kind", "product", "volume", "tank", "updated_at",
        "equipment_id", "equipment_description", "status", "cost_centre",
    ]

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self._db = db

    def run(self) -> None:  # noqa: D401 - QThread entrypoint
        try:
            rdb = Database(self._db.path, create=False)
            try:
                recons = rdb.get_reconciliations()
                tanks = rdb.get_tanks()
                mov = rdb.read("movements", order_by='"updated_at" DESC',
                               limit=20000, columns=self._MOVEMENT_COLS)
                eq = rdb.get_equipment()
                counts = (rdb.row_count("reconciliations"), rdb.row_count("movements"))
            finally:
                rdb.close()
            self.done.emit(recons, tanks, mov, eq, counts)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")


class TankWindow(QMainWindow):
    """Análisis de tanques + reconciliación, alimentado por el endpoint."""

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self._db = db
        self._main = parent
        self._qsettings = QSettings("NewmontMerian", "MSGQ")
        self._recons = pd.DataFrame()
        self._tanks = pd.DataFrame()
        self._mov = pd.DataFrame()
        self._eq = pd.DataFrame()
        self._last_counts = None
        self._worker: _LoadWorker | None = None
        self._pending_refresh = False
        self._busy: BusyOverlay | None = None
        self.setWindowTitle(t("MSGQ — Análisis de Tanques  ·  Newmont Merian"))
        self.resize(1420, 880)

        self._build_central()
        self._refresh()

        self._timer = QTimer(self)
        self._timer.setInterval(10000)
        self._timer.timeout.connect(self._maybe_refresh)
        self._timer.start()

    def closeEvent(self, event):  # noqa: N802
        self._timer.stop()
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(3000)
        event.accept()

    # --- Idioma / tema ------------------------------------------------------

    def _on_language_changed(self, code: str) -> None:
        if not code or code == current_language():
            return
        if self._main is not None and hasattr(self._main, "switch_language"):
            self._main.switch_language(code)
        else:
            set_language(code); self._qsettings.setValue("language", code); self.rebuild_ui()

    def _on_theme_changed(self, name: str) -> None:
        if not name or name == theme.current_theme():
            return
        if self._main is not None and hasattr(self._main, "switch_theme"):
            self._main.switch_theme(name)
        else:
            theme.set_theme(name); self._qsettings.setValue("theme", name)
            theme.apply_theme(); self.rebuild_ui()

    def rebuild_ui(self) -> None:
        """Reconstruye en el idioma/tema actual (la invoca la ventana principal)."""
        self.setWindowTitle(t("MSGQ — Análisis de Tanques  ·  Newmont Merian"))
        self._build_central()
        self._refresh()

    # --- Construcción -------------------------------------------------------

    def _build_central(self) -> None:
        root = QWidget()
        lay = QVBoxLayout(root); lay.setSpacing(6)
        lay.addWidget(self._build_controls())
        lay.addWidget(self._build_kpis())
        lay.addWidget(self._build_tabs(), stretch=1)
        self.setCentralWidget(root)
        # Indicador de carga (cubre el área central durante la lectura inicial).
        self._busy = BusyOverlay(root, t("Cargando datos…"))

    def _build_controls(self) -> QGroupBox:
        box = QGroupBox(t("Filtros")); row = QHBoxLayout(box)
        self.cmb_circuit = QComboBox()
        self.cmb_circuit.addItem(t("Todos"), None)
        self.cmb_circuit.addItem("Diesel", "Diesel")
        self.cmb_circuit.addItem(t("Gasolina"), "Gasolina")
        self.cmb_circuit.currentIndexChanged.connect(self._apply_filters)
        self.cmb_range = QComboBox()
        for label, days in _RANGES:
            self.cmb_range.addItem(t(label), days)
        self.cmb_range.currentIndexChanged.connect(self._apply_filters)
        btn_refresh = QPushButton(t("Actualizar")); btn_refresh.clicked.connect(self._refresh)
        btn_export = QPushButton(t("Exportar a Excel…")); btn_export.clicked.connect(self._on_export)

        row.addWidget(QLabel(t("Circuito:"))); row.addWidget(self.cmb_circuit)
        row.addWidget(QLabel(t("Rango:"))); row.addWidget(self.cmb_range)
        row.addStretch(1)
        row.addWidget(btn_refresh); row.addWidget(btn_export)
        row.addWidget(language_selector(self._on_language_changed))
        row.addWidget(theme_selector(self._on_theme_changed))
        return box

    def _build_kpis(self) -> QFrame:
        frame = QFrame(); frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet(f"QFrame{{background:{theme.panel_bg()}; border-radius:6px;}}")
        self._kpi_layout = QHBoxLayout(frame)
        self._kpi_layout.setContentsMargins(8, 6, 8, 6)
        return frame

    def _build_tabs(self) -> QTabWidget:
        self.tabs = QTabWidget()
        self.tbl_recon, self.m_recon = make_table()
        self.tabs.addTab(wrap_with_search(self.tbl_recon), t("Reconciliación"))
        self.tbl_daily, self.m_daily = make_table()
        self.tabs.addTab(wrap_with_search(self.tbl_daily), t("Reconciliación diaria"))

        # Niveles: gráfico de stock + tabla diaria.
        niv = QWidget(); nlay = QVBoxLayout(niv); nlay.setContentsMargins(2, 2, 2, 2)
        split = QSplitter(Qt.Vertical)
        self.ch_stock = TimeSeriesChart(t("Stock por día (L)"), t("Stock (L)"))
        split.addWidget(self.ch_stock)
        w = QWidget(); wl = QVBoxLayout(w); wl.setContentsMargins(0, 0, 0, 0)
        self.tbl_levels, self.m_levels = make_table(); wl.addWidget(self.tbl_levels)
        split.addWidget(w); split.setSizes([420, 320])
        nlay.addWidget(split)
        self.tabs.addTab(niv, t("Niveles"))

        # Despachos: por tanque / producto / dimensión.
        desp = QTabWidget()
        self.tbl_d_tank, self.m_d_tank = make_table(); desp.addTab(self.tbl_d_tank, t("Por tanque"))
        self.tbl_d_prod, self.m_d_prod = make_table(); desp.addTab(self.tbl_d_prod, t("Por producto"))
        self.tbl_d_grp, self.m_d_grp = make_table(); desp.addTab(self.tbl_d_grp, t("Por grupo"))
        self.tbl_d_dep, self.m_d_dep = make_table(); desp.addTab(self.tbl_d_dep, t("Por departamento"))
        self.tbl_flow, self.m_flow = make_table(); desp.addTab(self.tbl_flow, t("Flujo por tanque"))
        self.tabs.addTab(desp, t("Despachos"))

        self.tabs.addTab(self._build_charts_tab(), t("Gráficas"))
        return self.tabs

    def _build_charts_tab(self) -> QWidget:
        c = QWidget(); grid = QGridLayout(c)
        self.ch_error = BarChart(t("Error de reconciliación por tanque"), t("Error (L)"))
        self.ch_trend = TimeSeriesChart(t("Tendencia de stock (L)"), t("Stock (L)"))
        self.ch_burn = TimeSeriesChart(t("Burn rate (volumen despachado)"), t("Volumen (L)"))
        grid.addWidget(self.ch_error, 0, 0); grid.addWidget(self.ch_trend, 0, 1)
        grid.addWidget(self.ch_burn, 1, 0, 1, 2)
        return c

    # --- Datos --------------------------------------------------------------

    def _refresh(self):
        """Relee la réplica EN SEGUNDO PLANO y luego proyecta. Antes leía 20.000
        movimientos en el hilo de la GUI y la ventana entera se congelaba en cada
        auto-refresco; ahora la lectura va en `_LoadWorker` y aquí solo se pinta."""
        if self._worker is not None and self._worker.isRunning():
            self._pending_refresh = True   # se relanza al terminar el actual
            return
        if self._recons.empty and self._busy is not None:
            self._busy.start(t("Cargando datos…"))
        self._worker = _LoadWorker(self._db, self)
        self._worker.done.connect(self._on_loaded)
        self._worker.failed.connect(self._on_load_failed)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_loaded(self, recons, tanks, mov, eq, counts):
        self._worker = None
        self._recons, self._tanks, self._mov, self._eq = recons, tanks, mov, eq
        self._last_counts = counts
        try:
            self._apply_filters()
            self._update_status()
        finally:
            if self._busy is not None:
                self._busy.stop()
        if self._pending_refresh:          # llegaron datos nuevos mientras cargaba
            self._pending_refresh = False
            self._refresh()

    def _on_load_failed(self, message: str):
        self._worker = None
        if self._busy is not None:
            self._busy.stop()
        QMessageBox.critical(self, t("Error al analizar"), message)

    def _maybe_refresh(self):
        try:
            counts = (self._db.row_count("reconciliations"), self._db.row_count("movements"))
        except Exception:  # noqa: BLE001
            return
        if counts != self._last_counts:
            self._refresh()

    def _range(self):
        days = self.cmb_range.currentData()
        start = (pd.Timestamp.now() - pd.Timedelta(days=days)) if days else None
        return start, None

    def _apply_filters(self):
        try:
            circuit = self.cmb_circuit.currentData()
            start, end = self._range()
            rec, mov, eq = self._recons, self._mov, self._eq

            self.m_recon.set_dataframe(ta.reconciliation_detail(rec, circuit, start, end))
            self.m_daily.set_dataframe(ta.reconciliation_daily(rec, circuit, start, end))

            # Niveles: gráfico de stock + tabla (reusa la diaria como detalle).
            periods, series = ta.stock_series(rec, circuit, start, end)
            self.ch_stock.set_series(periods, series)
            self.ch_trend.set_series(periods, series)
            self.m_levels.set_dataframe(ta.reconciliation_daily(rec, circuit, start, end))

            # Despachos (movimientos del circuito).
            mv = ta.filter_circuit(mov, circuit)
            self.m_d_tank.set_dataframe(ta.consumption_by_tank(mv))
            self.m_d_prod.set_dataframe(ta.consumption_by_product(mv))
            self.m_d_grp.set_dataframe(ta.consumption_by_dimension(mv, eq, "group", "Grupo"))
            self.m_d_dep.set_dataframe(ta.consumption_by_dimension(mv, eq, "department", "Departamento"))
            self.m_flow.set_dataframe(ta.flow_by_tank(mv))

            # Gráficas.
            det = ta.reconciliation_detail(rec, circuit, start, end)
            if not det.empty:
                self.ch_error.set_data(det["Tanque"].astype(str).tolist(),
                                       det["Error (L)"].tolist(), "#C62828")
            br = ta.burn_rate(mv, freq="D")
            if not br.empty:
                self.ch_burn.set_series(br["Periodo"].tolist(), {t("Volumen (L)"): br["Volumen (L)"].tolist()})

            self._set_kpis(ta.reconciliation_kpis(rec, circuit, start, end), mv)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al filtrar"),
                                 f"{exc}\n\n{traceback.format_exc()}")

    def _set_kpis(self, k: dict, mv: pd.DataFrame):
        while self._kpi_layout.count():
            it = self._kpi_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        disp_total = float(mv[mv["kind"] == ta.DISPENSE]["volume"].fillna(0).sum()) if (
            mv is not None and not mv.empty and "kind" in mv.columns) else 0.0
        if not k:
            self._kpi_layout.addWidget(QLabel(t("Sin reconciliaciones para el filtro.")))
            self._kpi_layout.addWidget(kpi_label(t("Volumen despachado (L)"), f"{disp_total:,.0f}", "#1F4E78"))
            self._kpi_layout.addStretch(1)
            return
        err_pct = abs(k.get("Error % outflow") or 0.0)
        color = "#2E7D32" if err_pct < 2 else "#E0A000" if err_pct < 5 else "#C62828"
        cards = [
            kpi_label(t("Tanques"), f"{k['Tanques']:,}", "#1F4E78"),
            kpi_label(t("Error total (L)"), f"{k['Error total (L)']:,.0f}", color),
            kpi_label(t("Error % outflow"), f"{k['Error % outflow']:.2f}%", color),
            warn_label(t("Peor tanque"), f"{k['Peor tanque']}", warn=err_pct >= 5),
            kpi_label(t("Volumen despachado (L)"), f"{disp_total:,.0f}", "#1F4E78"),
        ]
        for c in cards:
            self._kpi_layout.addWidget(c)
        self._kpi_layout.addStretch(1)

    def _update_status(self):
        n_rec, n_tk = len(self._recons), len(self._tanks)
        loading = self._db.get_watermark("reconciliations") is None
        if loading:
            self.statusBar().showMessage(tr_fmt("tank.loading", recons=n_rec, tanks=n_tk))
        else:
            self.statusBar().showMessage(tr_fmt("tank.status", recons=n_rec, tanks=n_tk))

    # --- Exportar -----------------------------------------------------------

    def _on_export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, t("Exportar análisis de tanques"),
            t("Analisis_Tanques_MSGQ.xlsx"), "Excel (*.xlsx)")
        if not path:
            return
        try:
            circuit = self.cmb_circuit.currentData()
            start, end = self._range()
            rec, mv, eq = self._recons, ta.filter_circuit(self._mov, circuit), self._eq
            sheets = {
                "Reconciliacion": ta.reconciliation_detail(rec, circuit, start, end),
                "Reconciliacion diaria": ta.reconciliation_daily(rec, circuit, start, end),
                "Despachos por tanque": ta.consumption_by_tank(mv),
                "Despachos por producto": ta.consumption_by_product(mv),
                "Despachos por grupo": ta.consumption_by_dimension(mv, eq, "group", "Grupo"),
                "Despachos por departamento": ta.consumption_by_dimension(mv, eq, "department", "Departamento"),
                "Flujo por tanque": ta.flow_by_tank(mv),
                "Tanques": self._tanks,
            }
            export_sheets(path, sheets)
            QMessageBox.information(self, t("Exportado"), f"{t('Análisis generado:')}\n{path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al exportar"),
                                 f"{exc}\n\n{traceback.format_exc()}")
