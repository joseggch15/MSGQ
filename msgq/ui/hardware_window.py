"""Ventana del módulo 'Salud de Hardware y Sensores'.

Software auditor del hardware del FMS (ver `core/hardware_health.py`): SMU en
regresión/estancado (sensores rotos o sin pulsos), re-tagueo RFID sospechoso
(operador forzando manual/bypass) y degradación del caudal por medidor (filtros
obstruidos / bomba fallando). Todo lo marcado se consolida en una lista de
**órdenes de trabajo** exportable.

Lee de la réplica SQLite (`movements` + `equipment` + `change_events`); el rango
de fechas RE-AUDITA la ventana elegida en un hilo aparte; el filtro de
categoría/búsqueda solo proyecta el resultado ya calculado.
"""
from __future__ import annotations

import traceback
from datetime import datetime

import pandas as pd
from PySide6.QtCore import QDate, QSettings, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QDateEdit, QFileDialog, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from msgq.core import hardware_health as hw
from msgq.core import sfl_audit as sa
from msgq.export import export_sheets
from msgq.i18n import current_language, set_language, t
from msgq.storage import Database
from msgq.ui import theme
from msgq.ui.charts import BarChart, TimeSeriesChart
from msgq.ui.common import (
    BusyOverlay, PaginatedTableView, kpi_label, language_selector, make_table,
    theme_selector, warn_label,
)

_RANGES = (
    ("Últimos 30 días", 30),
    ("Últimos 90 días", 90),
    ("Últimos 12 meses", 365),
    ("Todo el rango", None),
)
_HISTORY_START = pd.Timestamp("2022-01-01")
_DEFAULT_RANGE_DAYS = 365


class _LoadWorker(QThread):
    """Lee la réplica (solo la primera vez) y RE-AUDITA la salud de hardware de la
    ventana de fechas elegida en un hilo aparte. Cachea los crudos en la ventana
    para que cambiar el rango sea un re-cálculo en memoria."""

    done = Signal(object, object, object, object, object, object)  # mv, eq, chg, result, lo, hi
    failed = Signal(str)

    def __init__(self, db: Database, movements, equipment, changes, lo, hi, parent=None):
        super().__init__(parent)
        self._db = db
        self._movements = movements
        self._equipment = equipment
        self._changes = changes
        self._lo = lo
        self._hi = hi

    def run(self) -> None:  # noqa: D401 - QThread entrypoint
        try:
            mv, eq, chg = self._movements, self._equipment, self._changes
            if mv is None:
                mv = self._db.read("movements")
                eq = self._db.get_equipment()
                chg = self._db.get_change_events()
            win_mv = self._window(mv, "record_collected_at", "updated_at")
            win_chg = self._window(chg, "changed_at")
            result = hw.audit(win_mv, eq, win_chg)
            self.done.emit(mv, eq, chg, result, self._lo, self._hi)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")

    def _window(self, df, *date_cols):
        if df is None or df.empty:
            return df
        col = next((c for c in date_cols if c in df.columns), None)
        if col is None:
            return df
        d = pd.to_datetime(df[col], errors="coerce")
        return df[(d >= self._lo) & (d < self._hi)]


class HardwareWindow(QMainWindow):
    """Auditoría de salud de hardware y sensores."""

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self._db = db
        self._main = parent
        self._qsettings = QSettings("NewmontMerian", "MSGQ")
        self._movements = None
        self._equipment = pd.DataFrame()
        self._changes = pd.DataFrame()
        self._result: hw.HardwareResult | None = None
        # Vistas filtradas (por categoría/búsqueda) del resultado auditado.
        self._f_smu = pd.DataFrame()
        self._f_retag = pd.DataFrame()
        self._f_meters = pd.DataFrame()
        self._f_orders = pd.DataFrame()
        self._last_counts = None
        self._loading_range = False
        self._worker: _LoadWorker | None = None
        self._pending_reread = False
        self._busy: BusyOverlay | None = None
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._apply_filters)

        self.setWindowTitle(t("MSGQ — Salud de Hardware y Sensores  ·  Newmont Merian"))
        self.resize(1460, 900)
        self._build_central()
        self._refresh(reread=True)

        self._timer = QTimer(self)
        self._timer.setInterval(10000)
        self._timer.timeout.connect(self._maybe_refresh)
        self._timer.start()

    def closeEvent(self, event):  # noqa: N802 - override Qt
        self._timer.stop()
        self._search_timer.stop()
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
        prev = getattr(self, "date_from", None)
        dfrom = self.date_from.date() if prev is not None else None
        dto = self.date_to.date() if prev is not None else None
        self.setWindowTitle(t("MSGQ — Salud de Hardware y Sensores  ·  Newmont Merian"))
        self._build_central()
        if dfrom is not None:
            self._loading_range = True
            self.date_from.setDate(dfrom)
            self.date_to.setDate(dto)
            self._loading_range = False
        if self._result is not None:
            self._refresh_combos()
            self._apply_filters()
        else:
            self._refresh(reread=True)

    # --- Construcción -------------------------------------------------------

    def _build_central(self) -> None:
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setSpacing(6)
        lay.addWidget(self._build_controls())
        lay.addWidget(self._build_progress())
        lay.addWidget(self._build_kpis())
        lay.addWidget(self._build_tabs(), stretch=1)
        self.setCentralWidget(root)
        self._busy = BusyOverlay(root, t("Cargando datos…"))

    def _build_controls(self) -> QGroupBox:
        box = QGroupBox(t("Auditoría de Salud de Hardware y Sensores"))
        row = QHBoxLayout(box)

        self.cmb_range = QComboBox()
        for label, days in _RANGES:
            self.cmb_range.addItem(t(label), days)
        self.cmb_range.setCurrentIndex(2)   # Últimos 12 meses
        self.cmb_range.currentIndexChanged.connect(self._apply_quick_range)

        today = QDate.currentDate()
        self.date_from = QDateEdit(today.addDays(-_DEFAULT_RANGE_DAYS))
        self.date_to = QDateEdit(today)
        for de in (self.date_from, self.date_to):
            de.setDisplayFormat("dd/MM/yyyy")
            de.setCalendarPopup(True)
            de.dateChanged.connect(self._on_date_edited)

        self.cmb_category = QComboBox()
        self.cmb_category.addItem(t("Todas"), None)
        self.cmb_category.currentIndexChanged.connect(self._apply_filters)

        self.cmb_meter = QComboBox()
        self.cmb_meter.addItem(t("(automático)"), None)
        self.cmb_meter.currentIndexChanged.connect(self._update_meter_chart)

        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText(t("Buscar por ID, descripción, medidor..."))
        self.txt_search.textChanged.connect(lambda _t: self._search_timer.start())

        btn_refresh = QPushButton(t("Actualizar"))
        btn_refresh.clicked.connect(lambda: self._refresh(reread=True, manual=True))
        btn_export = QPushButton(t("Exportar a Excel…"))
        btn_export.clicked.connect(self._on_export)

        row.addWidget(QLabel(t("Rango:")))
        row.addWidget(self.cmb_range)
        row.addWidget(QLabel(t("Desde:")))
        row.addWidget(self.date_from)
        row.addWidget(QLabel(t("Hasta:")))
        row.addWidget(self.date_to)
        row.addWidget(QLabel(t("Categoría:")))
        row.addWidget(self.cmb_category)
        row.addWidget(QLabel(t("Medidor (gráfica):")))
        row.addWidget(self.cmb_meter)
        row.addWidget(QLabel(t("Buscar:")))
        row.addWidget(self.txt_search, stretch=1)
        row.addWidget(btn_refresh)
        row.addWidget(btn_export)
        row.addWidget(language_selector(self._on_language_changed))
        row.addWidget(theme_selector(self._on_theme_changed))
        return box

    def _build_progress(self) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet(f"QFrame{{background:{theme.panel_bg()}; border-radius:6px;}}")
        r = QHBoxLayout(frame)
        r.setContentsMargins(10, 4, 10, 4)
        r.setSpacing(10)
        r.addWidget(QLabel(t("Carga histórica del rango:")))
        self._prog_bar = QProgressBar()
        self._prog_bar.setRange(0, 100)
        self._prog_bar.setValue(0)
        self._prog_bar.setTextVisible(True)
        self._prog_bar.setFixedWidth(260)
        self._prog_text = QLabel("")
        self._prog_text.setStyleSheet("color:#5A6B7B;")
        r.addWidget(self._prog_bar)
        r.addWidget(self._prog_text, stretch=1)
        return frame

    def _build_kpis(self) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet(f"QFrame{{background:{theme.panel_bg()}; border-radius:6px;}}")
        self._kpi_layout = QHBoxLayout(frame)
        self._kpi_layout.setContentsMargins(8, 6, 8, 6)
        return frame

    def _build_tabs(self) -> QTabWidget:
        self.tabs = QTabWidget()
        self.tbl_smu = PaginatedTableView()
        self.tabs.addTab(self.tbl_smu, t("Salud de SMU"))
        self.tbl_retag, self.m_retag = make_table()
        self.tabs.addTab(self.tbl_retag, t("Re-tagueo sospechoso"))
        self.tabs.addTab(self._build_meters_tab(), t("Salud de medidores"))
        self.tbl_orders = PaginatedTableView()
        self.tabs.addTab(self.tbl_orders, t("Órdenes de trabajo"))
        self.tabs.addTab(self._build_charts_tab(), t("Gráficas"))
        return self.tabs

    def _build_meters_tab(self) -> QWidget:
        c = QWidget()
        lay = QVBoxLayout(c)
        lay.setContentsMargins(2, 2, 2, 2)
        self._meter_note = QLabel("")
        self._meter_note.setStyleSheet("color:#833C00;")
        self._meter_note.setVisible(False)
        lay.addWidget(self._meter_note)
        self.tbl_meter, self.m_meter = make_table()
        lay.addWidget(self.tbl_meter)
        return c

    def _build_charts_tab(self) -> QWidget:
        c = QWidget()
        grid = QGridLayout(c)
        self.ch_meter = TimeSeriesChart(t("Caudal del medidor en el tiempo (L/min)"), t("L/min"))
        self.ch_smu = BarChart(t("Eventos de SMU por equipo"), t("Eventos"))
        self.ch_retag = BarChart(t("Cambios de RFID por equipo (re-tagueo)"), t("Cambios"))
        grid.addWidget(self.ch_meter, 0, 0, 1, 2)
        grid.addWidget(self.ch_smu, 1, 0)
        grid.addWidget(self.ch_retag, 1, 1)
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
        self._refresh(reread=False)

    def _on_date_edited(self, _date):
        if not self._loading_range:
            self._refresh(reread=False)

    @staticmethod
    def _ts(qdate: QDate) -> pd.Timestamp:
        return pd.Timestamp(qdate.toPython())

    def _range(self) -> tuple[pd.Timestamp, pd.Timestamp]:
        lo = self._ts(self.date_from.date())
        hi = self._ts(self.date_to.date()).normalize() + pd.Timedelta(days=1)
        return lo, hi

    # --- Carga / re-auditoría ----------------------------------------------

    def _refresh(self, reread: bool = False, manual: bool = False):
        if self._worker is not None and self._worker.isRunning():
            if reread:
                self._pending_reread = True
            return
        if manual or self._result is None:
            self._show_busy(t("Cargando datos…"))
        else:
            self.statusBar().showMessage(f"{t('Actualizando…')} {datetime.now():%H:%M:%S}")
        lo, hi = self._range()
        mv = None if reread else self._movements
        eq = None if reread else self._equipment
        chg = None if reread else self._changes
        self._worker = _LoadWorker(self._db, mv, eq, chg, lo, hi, self)
        self._worker.done.connect(self._on_loaded)
        self._worker.failed.connect(self._on_load_failed)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_loaded(self, movements, equipment, changes, result, lo, hi):
        self._movements = movements
        self._equipment = equipment
        self._changes = changes
        self._result = result
        if movements is not None:
            self._last_counts = (len(movements),
                                 0 if changes is None else len(changes))
        self._worker = None
        try:
            self._refresh_combos()
            self._apply_filters()
        finally:
            self._hide_busy()
        if self._pending_reread:
            self._pending_reread = False
            self._refresh(reread=True)

    def _on_load_failed(self, message: str):
        self._worker = None
        self._hide_busy()
        QMessageBox.critical(self, t("Error al analizar"), message)

    def _show_busy(self, text: str):
        if self._busy is not None:
            self._busy.start(text)

    def _hide_busy(self):
        if self._busy is not None:
            self._busy.stop()

    def _maybe_refresh(self):
        try:
            counts = (self._db.row_count("movements"), self._db.row_count("change_events"))
        except Exception:  # noqa: BLE001
            return
        if counts != self._last_counts:
            self._refresh(reread=True)

    # --- Combos -------------------------------------------------------------

    def _refresh_combos(self):
        if self._result is None:
            return
        cats = set()
        for df in (self._result.smu, self._result.retag):
            if df is not None and not df.empty and "category" in df.columns:
                cats |= set(df["category"].dropna().astype(str))
        cur = self.cmb_category.currentData()
        self.cmb_category.blockSignals(True)
        self.cmb_category.clear()
        self.cmb_category.addItem(t("Todas"), None)
        for c in sorted(cats):
            self.cmb_category.addItem(c, c)
        ix = self.cmb_category.findData(cur)
        self.cmb_category.setCurrentIndex(ix if ix >= 0 else 0)
        self.cmb_category.blockSignals(False)

    def _refresh_meter_combo(self, meters: pd.DataFrame):
        cur = self.cmb_meter.currentData()
        self.cmb_meter.blockSignals(True)
        self.cmb_meter.clear()
        self.cmb_meter.addItem(t("(automático)"), None)
        if meters is not None and not meters.empty:
            for _, r in meters.iterrows():
                mid = str(r.get("meter_id"))
                mark = "⚠ " if bool(r.get("degradado")) else ""
                self.cmb_meter.addItem(f"{mark}{mid}", mid)
        ix = self.cmb_meter.findData(cur)
        self.cmb_meter.setCurrentIndex(ix if ix >= 0 else 0)
        self.cmb_meter.blockSignals(False)

    # --- Filtrado y proyección ---------------------------------------------

    def _apply_filters(self):
        if self._result is None:
            return
        try:
            cat = self.cmb_category.currentData()
            txt = self.txt_search.text().strip().lower()

            def _filt(df, search_cols, by_category=True):
                if df is None or df.empty:
                    return df if df is not None else pd.DataFrame()
                out = df
                if by_category and cat is not None and "category" in out.columns:
                    out = out[out["category"].astype("string") == str(cat)]
                if txt:
                    cols = [c for c in search_cols if c in out.columns]
                    mask = pd.Series(False, index=out.index)
                    for c in cols:
                        mask |= out[c].astype("string").str.lower().str.contains(txt, na=False)
                    out = out[mask]
                return out

            eq_cols = ("equipment_id", "equipment_description", "category")
            f_smu = _filt(self._result.smu, eq_cols)
            f_retag = _filt(self._result.retag, eq_cols + ("internal_id",))
            # Los medidores no tienen categoría: solo se filtran por búsqueda.
            f_meters = _filt(self._result.meters, ("meter_id", "meter_description"),
                             by_category=False)
            f_orders = hw.work_orders(f_smu, f_retag, f_meters)
            self._f_smu, self._f_retag = f_smu, f_retag
            self._f_meters, self._f_orders = f_meters, f_orders

            self.tbl_smu.set_full_dataframe(f_smu)
            self.m_retag.set_dataframe(f_retag)
            self._set_meters(f_meters)
            self.tbl_orders.set_full_dataframe(f_orders)

            self._refresh_meter_combo(f_meters)
            self._update_charts(f_smu, f_retag, f_meters)
            self._set_kpis(hw.summary_kpis(f_smu, f_retag, f_meters, f_orders))
            self._update_progress()
            self._update_status(f_orders)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al filtrar"),
                                 f"{exc}\n\n{traceback.format_exc()}")

    def _set_meters(self, meters: pd.DataFrame):
        self.m_meter.set_dataframe(meters)
        available = self._result is not None and self._result.meter_available
        if not available:
            self._meter_note.setText(t(
                "El endpoint aún no expone Meter ID / caudal por manguera. La "
                "auditoría de medidores se activará cuando esos campos lleguen al "
                "re-sincronizar (las demás auditorías ya funcionan)."))
        self._meter_note.setVisible(not available)

    # --- Gráficas -----------------------------------------------------------

    def _update_charts(self, smu, retag, meters):
        # SMU: eventos por equipo (top 15).
        if smu is not None and not smu.empty:
            by_eq = (smu.groupby("equipment_id").size().sort_values(ascending=False)
                     .head(15))
            self.ch_smu.set_data(by_eq.index.astype(str).tolist(),
                                 by_eq.values.tolist(), "#C62828")
        else:
            self.ch_smu.set_data([], [])
        # Re-tagueo por equipo.
        if retag is not None and not retag.empty:
            r = retag.head(15)
            self.ch_retag.set_data(r["equipment_id"].astype(str).tolist(),
                                   r["cambios_30d"].tolist(), "#833C00")
        else:
            self.ch_retag.set_data([], [])
        self._update_meter_chart()

    def _selected_meter(self, meters: pd.DataFrame | None = None) -> str | None:
        sel = self.cmb_meter.currentData()
        if sel:
            return str(sel)
        m = meters if meters is not None else (
            self._result.meters if self._result is not None else None)
        if m is None or m.empty:
            return None
        deg = m[m["degradado"].map(bool)] if "degradado" in m.columns else m
        pick = deg if not deg.empty else m
        return str(pick.iloc[0]["meter_id"])

    def _update_meter_chart(self):
        if self._result is None:
            self.ch_meter.set_series([], {})
            return
        series = self._result.meter_series
        mid = self._selected_meter()
        if series is None or series.empty or not mid:
            self.ch_meter.set_series([], {})
            return
        sub = series[series["meter_id"].astype("string") == mid].sort_values("date")
        if sub.empty:
            self.ch_meter.set_series([], {})
            return
        periods = sub["date"].tolist()
        data = {t("Caudal"): sub["caudal"].tolist()}
        # Línea base del medidor (si está degradado o no, la referencia ayuda a leer).
        if not self._result.meters.empty:
            row = self._result.meters[self._result.meters["meter_id"].astype("string") == mid]
            if not row.empty and pd.notna(row.iloc[0].get("caudal_base")):
                data[t("Base")] = [float(row.iloc[0]["caudal_base"])] * len(periods)
        self.ch_meter.set_series(periods, data)
        self.ch_meter._plot.setTitle(f"{t('Caudal del medidor')} — {mid}")

    # --- KPIs / progreso / estado ------------------------------------------

    def _set_kpis(self, k: dict):
        while self._kpi_layout.count():
            it = self._kpi_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        reg = k.get("SMU en regresión", 0)
        stag = k.get("SMU sin pulsos", 0)
        retag = k.get("Re-tagueo sospechoso", 0)
        meters = k.get("Medidores degradados", 0)
        orders = k.get("Órdenes de trabajo", 0)
        cards = [
            warn_label(t("SMU en regresión"), f"{reg:,}", warn=reg > 0),
            warn_label(t("SMU sin pulsos"), f"{stag:,}", warn=stag > 0),
            warn_label(t("Re-tagueo sospechoso"), f"{retag:,}", warn=retag > 0),
            warn_label(t("Medidores degradados"), f"{meters:,}", warn=meters > 0),
            kpi_label(t("Órdenes de trabajo"), f"{orders:,}", "#833C00"),
        ]
        for c in cards:
            self._kpi_layout.addWidget(c)
        self._kpi_layout.addStretch(1)

    def _update_progress(self):
        try:
            backfilled = self._db.get_flag("movements_backfill_done") == "1"
        except Exception:  # noqa: BLE001
            backfilled = False
        lo, hi = self._range()
        pct, done = sa.load_progress(self._movements, lo, hi, backfilled)
        self._prog_bar.setValue(int(round(pct)))
        n_mov = 0 if self._movements is None else len(self._movements)
        chunk = "#2E7D32" if done else "#1F4E78"
        self._prog_bar.setStyleSheet(
            f"QProgressBar{{border:1px solid {theme.accent('#1F4E78')}; border-radius:4px; "
            f"text-align:center;}} QProgressBar::chunk{{background:{chunk};}}")
        msg = (f"✓ {t('Datos completos para el rango')}" if done
               else f"{t('Cargando histórico…')}")
        self._prog_text.setText(f"{msg} · {n_mov:,} {t('movimientos')}")

    def _update_status(self, orders: pd.DataFrame):
        n = 0 if orders is None else len(orders)
        self.statusBar().showMessage(
            f"{n:,} {t('órdenes de trabajo de hardware')} · {datetime.now():%H:%M:%S}")

    # --- Exportar -----------------------------------------------------------

    def _on_export(self):
        if self._result is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, t("Exportar auditoría de hardware"),
            t("SaludHardware_MSGQ.xlsx"), "Excel (*.xlsx)")
        if not path:
            return
        try:
            export_sheets(path, {
                "Órdenes de trabajo": self._f_orders,
                "Salud de SMU": self._f_smu,
                "Re-tagueo": self._f_retag,
                "Medidores": self._f_meters,
            })
            QMessageBox.information(self, t("Exportado"), f"{t('Análisis generado:')}\n{path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al exportar"),
                                 f"{exc}\n\n{traceback.format_exc()}")
