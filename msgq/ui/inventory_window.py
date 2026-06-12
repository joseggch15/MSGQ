"""Ventana del modulo 'Inventario de Tags RFID' — reporte 'Inventory Tag Installed'.

Reproduce las funciones del proyecto hermano `Inventory_Equipment` (clasificar
cada cambio de RFID como NEW / REPLACEMENT / REMOVAL, KPIs, agrupaciones,
validaciones y exportacion semanal/completa) pero alimentado por el endpoint
(la replica SQLite que llena el poller), no por snapshots CSV.

A diferencia del proyecto hermano, la columna DATE es la fecha REAL del cambio de
RFID (changedAt del log de auditoria), no la fecha del inventario. Se elige un
rango de fechas y el reporte lista los cambios reales en esa ventana.

Lee de la replica: `equipment` (maestro actual), `change_events` (recordType
EquipmentRfid) y `movements` (para inferir el producto). No toca el poller.

El idioma/tema se comparte con la ventana principal (fuente unica); el selector
de la barra de controles los cambia y reconstruye la interfaz.
"""
from __future__ import annotations

import traceback
from datetime import datetime

import pandas as pd
from PySide6.QtCore import QDate, QSettings, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QDateEdit, QFileDialog, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
    QTabWidget, QVBoxLayout, QWidget,
)

from msgq import config
from msgq.core import equipment_analytics as ea
from msgq.core import rfid_inventory as ri
from msgq.export import export_sheets, export_weekly_report
from msgq.i18n import current_language, set_language, t, tr_value
from msgq.storage import Database
from msgq.ui import theme
from msgq.ui.charts import BarChart, TimeSeriesChart
from msgq.ui.common import (
    BusyOverlay, kpi_label, language_selector, make_table, theme_selector,
    warn_label, wrap_with_search,
)
from msgq.ui.equipment_window import AuditLogDialog


class _LoadWorker(QThread):
    """Lee la réplica y construye el reporte de instalación EN OTRO HILO.

    Antes la ventana releía TODO el historial de movimientos (cientos de miles de
    filas) en el hilo de la GUI; además el auto-refresco comparaba `len()` de los
    cambios RFID contra `row_count()` de TODOS los change_events (nunca iguales),
    así que esa relectura completa se disparaba cada 10 s: la ventana vivía
    congelada. Ahora la carga corre aquí y los conteos que se emiten provienen de
    las MISMAS tablas que compara `_maybe_refresh` (gating correcto)."""

    done = Signal(object)   # dict con eq, changes, movements, history, report, counts
    failed = Signal(str)

    # `movements` solo se usa para inferir el producto por equipo
    # (`ri.equipment_product_map`): con 3 columnas de 46 alcanza.
    _MOVEMENT_COLS = ["equipment_id", "product", "kind"]

    def __init__(self, db: Database, date_from: pd.Timestamp,
                 date_to: pd.Timestamp, parent=None):
        super().__init__(parent)
        self._db = db
        self._from = date_from
        self._to = date_to

    def run(self) -> None:  # noqa: D401 - QThread entrypoint
        try:
            rdb = Database(self._db.path, create=False)
            try:
                eq = rdb.get_equipment()
                changes = rdb.get_change_events(config.CHANGE_RECORD_RFID)
                movements = rdb.read("movements", columns=self._MOVEMENT_COLS)
                history = rdb.get_rfid_history()
                # Limites por equipo/producto: la fuente PRIMARIA de la columna
                # Product (productos habilitados, como AdaptIQ); sin despachos
                # el producto salia vacio.
                limits = rdb.get_consumption_limits()
                counts = (rdb.row_count("equipment"),
                          rdb.row_count("change_events"),
                          rdb.row_count("movements"),
                          rdb.row_count("rfid_history"))
            finally:
                rdb.close()
            report = ri.installation_report(
                changes, eq, movements, self._from, self._to, history,
                limits=limits)
            self.done.emit({"eq": eq, "changes": changes, "movements": movements,
                            "history": history, "report": report, "counts": counts})
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")

# Rangos rapidos (etiqueta -> dias hacia atras; None = todo el historico).
_RANGES = (
    ("Últimos 7 días", 7),
    ("Últimos 30 días", 30),
    ("Últimos 90 días", 90),
    ("Últimos 12 meses", 365),
    ("Todo el rango", None),
)
_DEFAULT_RANGE_DAYS = 30
_HISTORY_START = pd.Timestamp("2022-01-01")


class InventoryWindow(QMainWindow):
    """Reporte de instalacion de tags RFID + validaciones, en vivo desde el endpoint."""

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self._db = db
        self._main = parent
        self._qsettings = QSettings("NewmontMerian", "MSGQ")
        self._eq_all = pd.DataFrame()
        self._changes = pd.DataFrame()
        self._movements = pd.DataFrame()
        self._history = pd.DataFrame()
        self._report = pd.DataFrame()
        self._last_counts = None
        self._loading_range = False
        self._worker: _LoadWorker | None = None
        self._pending_refresh = False
        self._busy: BusyOverlay | None = None
        self.setWindowTitle(t("MSGQ — Inventario de Tags RFID  ·  Newmont Merian"))
        self.resize(1420, 880)

        self._build_central()
        self._refresh()

        self._timer = QTimer(self)
        self._timer.setInterval(10000)
        self._timer.timeout.connect(self._maybe_refresh)
        self._timer.start()

    def closeEvent(self, event):  # noqa: N802 - override Qt
        self._timer.stop()
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(3000)
        event.accept()

    # --- Idioma / tema (la ventana principal es la fuente unica) ------------

    def _on_language_changed(self, code: str) -> None:
        if not code or code == current_language():
            return
        if self._main is not None and hasattr(self._main, "switch_language"):
            self._main.switch_language(code)
        else:
            set_language(code)
            self._qsettings.setValue("language", code)
            self.rebuild_ui()

    def _on_theme_changed(self, name: str) -> None:
        if not name or name == theme.current_theme():
            return
        if self._main is not None and hasattr(self._main, "switch_theme"):
            self._main.switch_theme(name)
        else:
            theme.set_theme(name)
            self._qsettings.setValue("theme", name)
            theme.apply_theme()
            self.rebuild_ui()

    def rebuild_ui(self) -> None:
        """Reconstruye la ventana en el idioma/tema global actual (la invoca la
        ventana principal al cambiar idioma o tema)."""
        # Conserva el rango de fechas elegido entre reconstrucciones.
        prev = getattr(self, "date_from", None)
        dfrom = self.date_from.date() if prev is not None else None
        dto = self.date_to.date() if prev is not None else None
        self.setWindowTitle(t("MSGQ — Inventario de Tags RFID  ·  Newmont Merian"))
        self._build_central()
        if dfrom is not None:
            self._loading_range = True
            self.date_from.setDate(dfrom)
            self.date_to.setDate(dto)
            self._loading_range = False
        self._refresh()

    # --- Construccion -------------------------------------------------------

    def _build_central(self) -> None:
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setSpacing(6)
        lay.addWidget(self._build_controls())
        lay.addWidget(self._build_kpis())
        lay.addWidget(self._build_tabs(), stretch=1)
        self.setCentralWidget(root)
        # Indicador de carga (cubre el área central durante la lectura/recalculo).
        self._busy = BusyOverlay(root, t("Cargando datos…"))

    def _build_controls(self) -> QGroupBox:
        box = QGroupBox(t("Reporte de instalación de tags RFID (fecha real del cambio)"))
        row = QHBoxLayout(box)

        self.cmb_range = QComboBox()
        for label, days in _RANGES:
            self.cmb_range.addItem(t(label), days)
        self.cmb_range.setCurrentIndex(1)   # Últimos 30 días
        self.cmb_range.currentIndexChanged.connect(self._apply_quick_range)

        today = QDate.currentDate()
        self.date_from = QDateEdit(today.addDays(-_DEFAULT_RANGE_DAYS))
        self.date_to = QDateEdit(today)
        for de in (self.date_from, self.date_to):
            de.setDisplayFormat("dd/MM/yyyy")
            de.setCalendarPopup(True)
            de.dateChanged.connect(self._on_date_edited)

        btn_refresh = QPushButton(t("Actualizar"))
        btn_refresh.clicked.connect(self._refresh)
        btn_weekly = QPushButton(t("Exportar reporte semanal…"))
        btn_weekly.setObjectName("accent")
        btn_weekly.clicked.connect(self._on_export_weekly)
        btn_full = QPushButton(t("Exportar análisis completo…"))
        btn_full.clicked.connect(self._on_export_full)

        row.addWidget(QLabel(t("Rango:")))
        row.addWidget(self.cmb_range)
        row.addSpacing(8)
        row.addWidget(QLabel(t("Desde:")))
        row.addWidget(self.date_from)
        row.addWidget(QLabel(t("Hasta:")))
        row.addWidget(self.date_to)
        row.addStretch(1)
        row.addWidget(btn_refresh)
        row.addWidget(btn_weekly)
        row.addWidget(btn_full)
        row.addWidget(language_selector(self._on_language_changed))
        row.addWidget(theme_selector(self._on_theme_changed))
        return box

    def _build_kpis(self) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet(f"QFrame{{background:{theme.panel_bg()}; border-radius:6px;}}")
        self._kpi_layout = QHBoxLayout(frame)
        self._kpi_layout.setContentsMargins(8, 6, 8, 6)
        return frame

    def _build_tabs(self) -> QTabWidget:
        self.tabs = QTabWidget()

        # Reporte semanal (doble clic -> audit log del equipo).
        rep = QWidget(); rlay = QVBoxLayout(rep); rlay.setContentsMargins(2, 2, 2, 2)
        rlay.addWidget(QLabel(t("Doble clic en una fila para ver el <b>Audit Log</b> del equipo.")))
        self.tbl_report, self.m_report = make_table()
        self.tbl_report.doubleClicked.connect(self._on_report_double_clicked)
        rlay.addWidget(wrap_with_search(self.tbl_report))
        self.tabs.addTab(rep, t("Reporte semanal"))

        self.tbl_inv, self.m_inv = make_table()
        self.tabs.addTab(wrap_with_search(self.tbl_inv), t("Inventario actual"))

        self.tbl_dep, self.m_dep = make_table()
        self.tabs.addTab(self.tbl_dep, t("Por departamento"))
        self.tbl_cc, self.m_cc = make_table()
        self.tabs.addTab(self.tbl_cc, t("Por cost center"))
        self.tbl_cat, self.m_cat = make_table()
        self.tabs.addTab(self.tbl_cat, t("Por categoría"))

        self.tabs.addTab(self._build_validation_tab(), t("Validaciones"))
        self.tabs.addTab(self._build_charts_tab(), t("Gráficas"))
        return self.tabs

    def _build_validation_tab(self) -> QWidget:
        c = QWidget(); lay = QVBoxLayout(c); lay.setContentsMargins(4, 4, 4, 4)
        inner = QTabWidget()
        self.tbl_val_sum, self.m_val_sum = make_table()
        inner.addTab(self.tbl_val_sum, t("Resumen ejecutivo"))
        self.tbl_oos, self.m_oos = make_table()
        inner.addTab(wrap_with_search(self.tbl_oos), t("Fuera de servicio"))
        self.tbl_dtag, self.m_dtag = make_table()
        inner.addTab(wrap_with_search(self.tbl_dtag), t("Tags duplicados"))
        self.tbl_did, self.m_did = make_table()
        inner.addTab(wrap_with_search(self.tbl_did), t("IDs duplicados"))
        self.tbl_inc, self.m_inc = make_table()
        inner.addTab(wrap_with_search(self.tbl_inc), t("Registros incompletos"))
        lay.addWidget(inner)
        return c

    def _build_charts_tab(self) -> QWidget:
        c = QWidget(); grid = QGridLayout(c)
        self.ch_time = TimeSeriesChart(
            t("Eventos de RFID por mes (alta / reemplazo / remoción)"), t("eventos"))
        self.ch_type = BarChart(t("Cambios por tipo de operación"), t("eventos"))
        grid.addWidget(self.ch_time, 0, 0)
        grid.addWidget(self.ch_type, 0, 1)
        return c

    # --- Rango de fechas ----------------------------------------------------

    def _apply_quick_range(self):
        days = self.cmb_range.currentData()
        today = QDate.currentDate()
        self._loading_range = True
        if days is None:
            self.date_from.setDate(QDate(_HISTORY_START.year, _HISTORY_START.month, _HISTORY_START.day))
        else:
            self.date_from.setDate(today.addDays(-int(days)))
        self.date_to.setDate(today)
        self._loading_range = False
        self._refresh()

    def _on_date_edited(self, _date):
        # Edicion manual de fechas: refresca (ignora los cambios programaticos).
        if not self._loading_range:
            self._refresh()

    @staticmethod
    def _ts(qdate: QDate) -> pd.Timestamp:
        return pd.Timestamp(qdate.toPython())

    # --- Refresco -----------------------------------------------------------

    def _refresh(self):
        """Relee la replica y recalcula el reporte EN SEGUNDO PLANO; al terminar,
        `_on_loaded` proyecta. La GUI nunca lee el historial completo en su hilo."""
        if self._worker is not None and self._worker.isRunning():
            self._pending_refresh = True   # se relanza al terminar el actual
            return
        if self._report.empty and self._busy is not None:
            self._busy.start(t("Cargando datos…"))
        self._worker = _LoadWorker(self._db, self._ts(self.date_from.date()),
                                   self._ts(self.date_to.date()), self)
        self._worker.done.connect(self._on_loaded)
        self._worker.failed.connect(self._on_load_failed)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_load_failed(self, message: str):
        self._worker = None
        if self._busy is not None:
            self._busy.stop()
        QMessageBox.critical(self, t("Error al analizar"), message)

    def _on_loaded(self, data: dict):
        self._worker = None
        self._eq_all = data["eq"]
        self._changes = data["changes"]
        self._movements = data["movements"]
        self._history = data["history"]
        self._report = data["report"]
        self._last_counts = data["counts"]
        try:
            self._project()
        finally:
            if self._busy is not None:
                self._busy.stop()
        if self._pending_refresh:          # llegaron datos nuevos mientras cargaba
            self._pending_refresh = False
            self._refresh()

    def _project(self):
        """Proyecta el reporte ya calculado en tablas, KPIs y gráficas."""
        try:
            report = self._report

            self.m_report.set_dataframe(ri.report_display(report))
            self.m_inv.set_dataframe(ri.current_inventory(self._eq_all))
            self.m_dep.set_dataframe(ri.by_department_summary(report))
            self.m_cc.set_dataframe(ri.by_cost_center_summary(report))
            self.m_cat.set_dataframe(ri.by_category_summary(report))

            self.m_val_sum.set_dataframe(ri.validation_summary(report, self._eq_all))
            self.m_oos.set_dataframe(ri.find_out_of_service(report))
            self.m_dtag.set_dataframe(ri.find_duplicate_tags(self._eq_all))
            self.m_did.set_dataframe(ri.find_duplicate_ids(report))
            self.m_inc.set_dataframe(ri.find_incomplete_records(report))

            self._update_charts(report)
            self._set_kpis(ri.summary_kpis(report, self._eq_all))
            self._update_status()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al analizar"),
                                 f"{exc}\n\n{traceback.format_exc()}")

    def _maybe_refresh(self):
        """Auto-refresco solo si la replica cambio (mantiene la UI fluida). Los
        conteos comparados provienen de las mismas tablas que captura el worker."""
        try:
            counts = (self._db.row_count("equipment"),
                      self._db.row_count("change_events"),
                      self._db.row_count("movements"),
                      self._db.row_count("rfid_history"))
        except Exception:  # noqa: BLE001
            return
        if counts != self._last_counts:
            self._refresh()

    def _update_status(self):
        n_rep, n_eq = len(self._report), len(self._eq_all)
        loading = self._db.get_watermark("change_events") is None
        if loading:
            self.statusBar().showMessage(
                t("Cargando historial de cambios desde el endpoint…"))
        else:
            self.statusBar().showMessage(
                f"{n_rep:,} {t('cambios de RFID en el rango')} · "
                f"{n_eq:,} {t('equipos en el maestro')} · {datetime.now():%H:%M:%S}")

    def _update_charts(self, report: pd.DataFrame):
        periods, series = _events_over_time(report)
        if periods:
            self.ch_time.set_series(periods, {
                t("Asignado"): series[config.TYPE_NEW],
                t("Cambiado"): series[config.TYPE_REPLACEMENT],
                t("Removido"): series[config.TYPE_REMOVAL]})
        else:
            self.ch_time.set_series([], {})
        bt = ri.by_type_summary(report)
        if not bt.empty:
            self.ch_type.set_data([tr_value(x) for x in bt["Tipo de operacion"].tolist()],
                                  bt["Cantidad"].tolist(), "#1F4E78")
        else:
            self.ch_type.set_data([], [])

    def _set_kpis(self, k: dict):
        while self._kpi_layout.count():
            it = self._kpi_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        n_oos = len(ri.find_out_of_service(self._report))
        n_dtag = len(ri.find_duplicate_tags(self._eq_all))
        n_inc = len(ri.find_incomplete_records(self._report))
        cards = [
            kpi_label(t("Nuevas instalaciones"), f"{k['Nuevas instalaciones']:,}", "#2E7D32"),
            kpi_label(t("Reemplazos"), f"{k['Reemplazos']:,}", "#1F4E78"),
            kpi_label(t("Remociones"), f"{k['Remociones']:,}", "#7030A0"),
            kpi_label(t("Tags distintos"), f"{k['Tags distintos']:,}"),
            kpi_label(t("Con RFID"), f"{k['Total con RFID']:,}", "#2E7D32"),
            kpi_label(t("Total equipos"), f"{k['Total equipos']:,}"),
            warn_label(t("OOS con tag"), f"{n_oos:,}", warn=n_oos > 0),
            warn_label(t("Tags duplicados"), f"{n_dtag:,}", warn=n_dtag > 0),
            warn_label(t("Sin equipo"), f"{n_inc:,}", warn=n_inc > 0),
        ]
        for c in cards:
            self._kpi_layout.addWidget(c)
        self._kpi_layout.addStretch(1)

    # --- Audit log por equipo (doble clic en el reporte) --------------------

    def _on_report_double_clicked(self, index):
        proxy = self.tbl_report.model()
        src = proxy.mapToSource(index) if proxy is not None else index
        df = self.m_report.dataframe()
        if df is None or df.empty or src.row() >= len(df):
            return
        eq_id = df.iloc[src.row()].get("ID")
        if not ri._present(eq_id):
            QMessageBox.information(
                self, t("Sin equipo"),
                t("Esta fila no tiene un equipo identificado (remoción o tag no "
                  "encontrado en el maestro), así que no hay Audit Log que mostrar."))
            return
        internal, desc = None, ""
        if not self._eq_all.empty:
            match = self._eq_all[self._eq_all["equipment_id"].astype("string") == str(eq_id)]
            if not match.empty:
                internal = match["internal_id"].iloc[0]
                desc = match["description"].iloc[0]
        log = ea.equipment_audit_log(self._db.get_change_events(), internal)
        AuditLogDialog(eq_id, desc, log, self).exec()

    # --- Exportar -----------------------------------------------------------

    def _on_export_weekly(self):
        d_from = self.date_from.date().toString("ddMMyyyy")
        d_to = self.date_to.date().toString("ddMMyyyy")
        path, _ = QFileDialog.getSaveFileName(
            self, t("Exportar reporte semanal"),
            f"Inventory Tag Installed {d_from}-{d_to}.xlsx", "Excel (*.xlsx)")
        if not path:
            return
        try:
            export_weekly_report(self._report, path)
            QMessageBox.information(self, t("Exportado"), f"{t('Reporte generado:')}\n{path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al exportar"),
                                 f"{exc}\n\n{traceback.format_exc()}")

    def _on_export_full(self):
        path, _ = QFileDialog.getSaveFileName(
            self, t("Exportar análisis completo"),
            t("Inventario_RFID_MSGQ.xlsx"), "Excel (*.xlsx)")
        if not path:
            return
        try:
            report = self._report
            sheets = {
                "Reporte semanal": ri.report_display(report),
                "Inventario actual": ri.current_inventory(self._eq_all),
                "Por tipo": ri.by_type_summary(report),
                "Por departamento": ri.by_department_summary(report),
                "Por cost center": ri.by_cost_center_summary(report),
                "Por categoria": ri.by_category_summary(report),
                "Resumen validacion": ri.validation_summary(report, self._eq_all),
                "Fuera de servicio": ri.find_out_of_service(report),
                "Tags duplicados": ri.find_duplicate_tags(self._eq_all),
                "IDs duplicados": ri.find_duplicate_ids(report),
                "Registros incompletos": ri.find_incomplete_records(report),
            }
            export_sheets(path, sheets)
            QMessageBox.information(self, t("Exportado"), f"{t('Análisis generado:')}\n{path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al exportar"),
                                 f"{exc}\n\n{traceback.format_exc()}")


def _events_over_time(report: pd.DataFrame):
    """(periodos, {TYPE: [conteos]}) por mes, para la grafica de serie temporal."""
    empty = {config.TYPE_NEW: [], config.TYPE_REPLACEMENT: [], config.TYPE_REMOVAL: []}
    if report is None or report.empty or "DATE" not in report.columns:
        return [], empty
    df = report.dropna(subset=["DATE"]).copy()
    if df.empty:
        return [], empty
    g = (df.set_index("DATE").groupby([pd.Grouper(freq="ME"), "TYPE"])
         .size().unstack(fill_value=0))
    for col in (config.TYPE_NEW, config.TYPE_REPLACEMENT, config.TYPE_REMOVAL):
        if col not in g.columns:
            g[col] = 0
    periods = list(g.index)
    return periods, {
        config.TYPE_NEW: g[config.TYPE_NEW].tolist(),
        config.TYPE_REPLACEMENT: g[config.TYPE_REPLACEMENT].tolist(),
        config.TYPE_REMOVAL: g[config.TYPE_REMOVAL].tolist(),
    }
