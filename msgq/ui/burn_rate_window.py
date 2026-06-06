"""Ventana del módulo 'Auditoría de Burn Rate (consumo L/h)'.

Software auditor: reconstruye el burn rate de cada equipo desde los despachos
(método tanque-a-tanque, ver `core/burn_rate.py`), lo compara contra la línea
base de su categoría y marca a los equipos —e intervalos puntuales— con un
comportamiento anómalo. Permite ver el comportamiento PROMEDIO (línea base de la
categoría) vs el REAL por equipo, con gráficas por categoría e individuales por
ID, para facilitar la detección de anomalías.

Lee de la réplica SQLite (`movements` + `equipment`); todo el cálculo pesado va
en un hilo aparte para no congelar la interfaz. El rango de fechas RE-AUDITA la
ventana elegida (la línea base y las medianas reflejan ese periodo); el filtro de
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

from msgq.core import burn_rate as br
from msgq.core import sfl_audit as sa   # reutiliza el indicador de carga histórica
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
_DEFAULT_RANGE_DAYS = 365   # 12 meses: suficientes intervalos por equipo para una base estable

# Columnas visibles de la tabla de muestras (sin smu_prev/smu_curr, para no saturar).
_SAMPLE_DISPLAY = [
    "date", "equipment_id", "equipment_description", "category", "product",
    "litres", "smu_delta", "burn_rate", "smu_type", "field_user", "source_id",
]


class _LoadWorker(QThread):
    """Lee la réplica (solo la primera vez) y RE-AUDITA el burn rate de la ventana
    de fechas elegida en un hilo aparte. Cachea los movimientos crudos en la
    ventana para que cambiar el rango sea un re-cálculo en memoria (rápido) y no
    otra lectura de disco."""

    done = Signal(object, object, object, object, object)  # movements, equipment, result, lo, hi
    failed = Signal(str)

    def __init__(self, db: Database, movements, equipment, lo, hi, parent=None):
        super().__init__(parent)
        self._db = db
        self._movements = movements
        self._equipment = equipment
        self._lo = lo
        self._hi = hi

    def run(self) -> None:  # noqa: D401 - QThread entrypoint
        try:
            mv = self._movements
            eq = self._equipment
            if mv is None:
                mv = self._db.read("movements")
                eq = self._db.get_equipment()
            win = mv
            if mv is not None and not mv.empty:
                col = ("record_collected_at" if "record_collected_at" in mv.columns
                       else "updated_at")
                d = pd.to_datetime(mv[col], errors="coerce")
                win = mv[(d >= self._lo) & (d < self._hi)]
            result = br.audit(win, eq)
            self.done.emit(mv, eq, result, self._lo, self._hi)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")


class BurnRateWindow(QMainWindow):
    """Auditoría del burn rate: anomalías de equipo/intervalo y comparación
    promedio (categoría) vs real (equipo)."""

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self._db = db
        self._main = parent
        self._qsettings = QSettings("NewmontMerian", "MSGQ")
        self._movements = None          # cache de movimientos crudos (todos)
        self._equipment = pd.DataFrame()
        self._result: br.BurnRateResult | None = None
        # Vistas filtradas (por categoría/búsqueda) del resultado auditado.
        self._f_samples = pd.DataFrame()
        self._f_equipment = pd.DataFrame()
        self._last_counts = None
        self._loading_range = False
        self._worker: _LoadWorker | None = None
        self._pending_reread = False
        self._busy: BusyOverlay | None = None
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._apply_filters)

        self.setWindowTitle(t("MSGQ — Auditoría de Burn Rate  ·  Newmont Merian"))
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

    # --- Idioma / tema (la ventana principal es la fuente única) ------------

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
        self.setWindowTitle(t("MSGQ — Auditoría de Burn Rate  ·  Newmont Merian"))
        self._build_central()
        if dfrom is not None:
            self._loading_range = True
            self.date_from.setDate(dfrom)
            self.date_to.setDate(dto)
            self._loading_range = False
        # El resultado ya está en memoria: re-proyecta sin re-leer disco.
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
        box = QGroupBox(t("Auditoría de Burn Rate (consumo L/h)"))
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

        self.cmb_equipment = QComboBox()
        self.cmb_equipment.addItem(t("(automático)"), None)
        self.cmb_equipment.currentIndexChanged.connect(self._update_individual_chart)

        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText(t("Buscar por ID, descripción, categoría..."))
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
        row.addWidget(QLabel(t("Equipo (gráfica):")))
        row.addWidget(self.cmb_equipment)
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
        self._prog_caption = QLabel(t("Carga histórica del rango:"))
        self._prog_bar = QProgressBar()
        self._prog_bar.setRange(0, 100)
        self._prog_bar.setValue(0)
        self._prog_bar.setTextVisible(True)
        self._prog_bar.setFixedWidth(260)
        self._prog_text = QLabel("")
        self._prog_text.setStyleSheet("color:#5A6B7B;")
        r.addWidget(self._prog_caption)
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
        self.tbl_eq_anom, self.m_eq_anom = make_table()
        self.tabs.addTab(self.tbl_eq_anom, t("Anomalías de equipo"))
        self.tbl_interval = PaginatedTableView()
        self.tabs.addTab(self.tbl_interval, t("Intervalos atípicos"))
        self.tbl_equip, self.m_equip = make_table()
        self.tabs.addTab(self.tbl_equip, t("Por equipo"))
        self.tbl_cat, self.m_cat = make_table()
        self.tabs.addTab(self.tbl_cat, t("Por categoría"))
        self.tbl_samples = PaginatedTableView()
        self.tabs.addTab(self.tbl_samples, t("Muestras"))
        self.tabs.addTab(self._build_charts_tab(), t("Gráficas"))
        return self.tabs

    def _build_charts_tab(self) -> QWidget:
        c = QWidget()
        grid = QGridLayout(c)
        self.ch_cat = BarChart(t("Burn rate base por categoría (L/h)"), t("L/h"))
        self.ch_dev = BarChart(t("Mayor desviación del burn rate (%)"), "%", value_suffix="%")
        self.ch_eq = TimeSeriesChart(t("Burn rate real vs promedio — equipo"), t("L/h"))
        grid.addWidget(self.ch_cat, 0, 0)
        grid.addWidget(self.ch_dev, 0, 1)
        grid.addWidget(self.ch_eq, 1, 0, 1, 2)
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
        self._refresh(reread=False)   # re-audita la ventana (en memoria)

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
        """Re-audita la ventana de fechas en SEGUNDO PLANO. `reread` re-lee la
        réplica (datos nuevos); si no, re-calcula sobre los movimientos cacheados."""
        if self._worker is not None and self._worker.isRunning():
            if reread:
                self._pending_reread = True
            return
        if manual or self._result is None:
            self._show_busy(t("Cargando datos…"))
        else:
            self.statusBar().showMessage(f"{t('Actualizando…')} {datetime.now():%H:%M:%S}")
        lo, hi = self._range()
        movements = None if reread else self._movements
        equipment = None if reread else self._equipment
        self._worker = _LoadWorker(self._db, movements, equipment, lo, hi, self)
        self._worker.done.connect(self._on_loaded)
        self._worker.failed.connect(self._on_load_failed)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_loaded(self, movements, equipment, result, lo, hi):
        self._movements = movements
        self._equipment = equipment
        self._result = result
        if movements is not None:
            self._last_counts = (len(movements), 0 if equipment is None else len(equipment))
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
            counts = (self._db.row_count("movements"), self._db.row_count("equipment"))
        except Exception:  # noqa: BLE001
            return
        if counts != self._last_counts:
            self._refresh(reread=True)

    # --- Combos -------------------------------------------------------------

    def _refresh_combos(self):
        if self._result is None:
            return
        cats = sorted(self._result.samples["category"].dropna().astype(str).unique()) \
            if not self._result.samples.empty else []
        cur = self.cmb_category.currentData()
        self.cmb_category.blockSignals(True)
        self.cmb_category.clear()
        self.cmb_category.addItem(t("Todas"), None)
        for c in cats:
            self.cmb_category.addItem(c, c)
        ix = self.cmb_category.findData(cur)
        self.cmb_category.setCurrentIndex(ix if ix >= 0 else 0)
        self.cmb_category.blockSignals(False)

    def _refresh_equipment_combo(self, eq_table: pd.DataFrame):
        """Lista los equipos de la vista actual (anómalos primero) para la gráfica
        individual. Conserva la selección si sigue presente."""
        cur = self.cmb_equipment.currentData()
        self.cmb_equipment.blockSignals(True)
        self.cmb_equipment.clear()
        self.cmb_equipment.addItem(t("(automático)"), None)
        if eq_table is not None and not eq_table.empty:
            for _, r in eq_table.iterrows():
                eid = str(r.get("equipment_id"))
                desc = r.get("equipment_description")
                mark = "⚠ " if bool(r.get("Anómalo")) else ""
                label = f"{mark}{eid}" + (f" — {desc}" if desc and str(desc) != eid else "")
                self.cmb_equipment.addItem(label, eid)
        ix = self.cmb_equipment.findData(cur)
        self.cmb_equipment.setCurrentIndex(ix if ix >= 0 else 0)
        self.cmb_equipment.blockSignals(False)

    # --- Filtrado y proyección ---------------------------------------------

    def _apply_filters(self):
        """Filtra el resultado auditado por categoría/búsqueda y refresca todo."""
        if self._result is None:
            return
        try:
            cat = self.cmb_category.currentData()
            txt = self.txt_search.text().strip().lower()

            def _filt(df, search_cols):
                if df is None or df.empty:
                    return df if df is not None else pd.DataFrame()
                out = df
                if cat is not None and "category" in out.columns:
                    out = out[out["category"].astype("string") == str(cat)]
                if txt:
                    cols = [c for c in search_cols if c in out.columns]
                    mask = pd.Series(False, index=out.index)
                    for c in cols:
                        mask |= out[c].astype("string").str.lower().str.contains(txt, na=False)
                    out = out[mask]
                return out

            id_cols = ("equipment_id", "equipment_description", "category", "product")
            self._f_samples = _filt(self._result.samples, id_cols)
            self._f_equipment = _filt(self._result.equipment, id_cols)
            f_eq_anom = _filt(self._result.equipment_anomalies, id_cols)
            f_interval = _filt(self._result.interval_anomalies, id_cols)
            f_cats = self._result.categories
            if cat is not None and not f_cats.empty:
                f_cats = f_cats[f_cats["category"].astype("string") == str(cat)]

            self.m_eq_anom.set_dataframe(f_eq_anom)
            self.tbl_interval.set_full_dataframe(f_interval)
            self.m_equip.set_dataframe(self._f_equipment)
            self.m_cat.set_dataframe(f_cats)
            sample_view = (self._f_samples[[c for c in _SAMPLE_DISPLAY
                                            if c in self._f_samples.columns]]
                           if not self._f_samples.empty else self._f_samples)
            self.tbl_samples.set_full_dataframe(sample_view)

            self._refresh_equipment_combo(self._f_equipment)
            self._update_category_charts(f_cats, self._f_equipment)
            self._update_individual_chart()
            self._set_kpis(br.summary_kpis(self._f_equipment, self._f_samples, f_interval))
            self._update_progress()
            self._update_status(len(f_eq_anom), len(f_interval))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al filtrar"),
                                 f"{exc}\n\n{traceback.format_exc()}")

    # --- Gráficas -----------------------------------------------------------

    def _update_category_charts(self, cats: pd.DataFrame, eq_table: pd.DataFrame):
        if cats is not None and not cats.empty:
            self.ch_cat.set_data(cats["category"].astype(str).tolist(),
                                 cats["Burn rate base (L/h)"].tolist(), "#1F4E78")
        else:
            self.ch_cat.set_data([], [])
        # Mayor desviación: equipos confiables ordenados por |Desviación %| (top 15).
        if eq_table is not None and not eq_table.empty and "Desviación %" in eq_table.columns:
            dev = eq_table.dropna(subset=["Desviación %"]).copy()
            dev["_abs"] = dev["Desviación %"].abs()
            dev = dev.sort_values("_abs", ascending=False).head(15)
            labels = dev["equipment_id"].astype(str).tolist()
            vals = dev["Desviación %"].tolist()
            self.ch_dev.set_data(labels, vals, "#C62828")
        else:
            self.ch_dev.set_data([], [])

    def _selected_equipment(self) -> str | None:
        """Equipo para la gráfica individual: el elegido, o el más desviado de la
        vista (anómalos primero) si está en '(automático)'."""
        sel = self.cmb_equipment.currentData()
        if sel:
            return str(sel)
        eq = self._f_equipment
        if eq is None or eq.empty:
            return None
        cand = eq.dropna(subset=["Desviación %"]) if "Desviación %" in eq.columns else eq
        if cand.empty:
            cand = eq
        cand = cand.copy()
        if "Desviación %" in cand.columns:
            cand["_abs"] = cand["Desviación %"].abs()
            cand = cand.sort_values("_abs", ascending=False)
        return str(cand.iloc[0]["equipment_id"])

    def _update_individual_chart(self):
        if self._result is None:
            return
        eid = self._selected_equipment()
        if not eid:
            self.ch_eq.set_series([], {})
            self.ch_eq._plot.setTitle(t("Burn rate real vs promedio — equipo"))
            return
        series = br.equipment_series(self._result.samples, eid)
        if series.empty:
            self.ch_eq.set_series([], {})
            return
        periods = series["date"].tolist()
        real = series["burn_rate"].tolist()
        data = {t("Real"): real}
        # Líneas de referencia: mediana del equipo y base de su categoría.
        row = self._f_equipment[self._f_equipment["equipment_id"].astype("string") == eid] \
            if not self._f_equipment.empty else pd.DataFrame()
        if not row.empty:
            r = row.iloc[0]
            eq_med = r.get("Burn rate (L/h)")
            base = r.get("Baseline categoría (L/h)")
            if pd.notna(eq_med):
                data[t("Equipo (mediana)")] = [float(eq_med)] * len(periods)
            if pd.notna(base):
                data[t("Promedio categoría")] = [float(base)] * len(periods)
        self.ch_eq.set_series(periods, data)
        self.ch_eq._plot.setTitle(f"{t('Burn rate real vs promedio')} — {eid}")

    # --- KPIs / progreso / estado ------------------------------------------

    def _set_kpis(self, k: dict):
        while self._kpi_layout.count():
            it = self._kpi_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        n_anom = k.get("Equipos anómalos", 0)
        n_int = k.get("Intervalos atípicos", 0)
        cards = [
            warn_label(t("Equipos anómalos"), f"{n_anom:,}", warn=n_anom > 0),
            kpi_label(t("Equipos analizados"), f"{k.get('Equipos analizados', 0):,}"),
            kpi_label(t("Burn rate flota (L/h)"), f"{k.get('Burn rate flota (L/h)', 0):,.1f}", "#1F4E78"),
            warn_label(t("Intervalos atípicos"), f"{n_int:,}", warn=n_int > 0),
            kpi_label(t("Intervalos analizados"), f"{k.get('Intervalos analizados', 0):,}"),
            kpi_label(t("Peor desviación %"), f"{k.get('Peor desviación %', 0):,.1f}%", "#833C00"),
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
        n_s = 0 if self._result is None else len(self._result.samples)
        chunk = "#2E7D32" if done else "#1F4E78"
        self._prog_bar.setStyleSheet(
            f"QProgressBar{{border:1px solid {theme.accent('#1F4E78')}; border-radius:4px; "
            f"text-align:center;}} QProgressBar::chunk{{background:{chunk};}}")
        if done:
            self._prog_text.setText(
                f"✓ {t('Datos completos para el rango')} · "
                f"{n_mov:,} {t('movimientos')} · {n_s:,} {t('intervalos')}")
        else:
            self._prog_text.setText(
                f"{t('Cargando histórico…')} {n_mov:,} {t('movimientos')} · "
                f"{n_s:,} {t('intervalos')}")

    def _update_status(self, n_anom: int, n_int: int):
        self.statusBar().showMessage(
            f"{n_anom:,} {t('equipos con burn rate anómalo')} · "
            f"{n_int:,} {t('intervalos atípicos')} · {datetime.now():%H:%M:%S}")

    # --- Exportar -----------------------------------------------------------

    def _on_export(self):
        if self._result is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, t("Exportar auditoría de Burn Rate"),
            t("BurnRate_Auditoria_MSGQ.xlsx"), "Excel (*.xlsx)")
        if not path:
            return
        try:
            sample_view = (self._f_samples[[c for c in _SAMPLE_DISPLAY
                                            if c in self._f_samples.columns]]
                           if not self._f_samples.empty else self._f_samples)
            cat = self.cmb_category.currentData()
            f_cats = self._result.categories
            if cat is not None and not f_cats.empty:
                f_cats = f_cats[f_cats["category"].astype("string") == str(cat)]
            export_sheets(path, {
                "Anomalías equipo": self.m_eq_anom.dataframe(),
                "Intervalos atípicos": self._result.interval_anomalies,
                "Por equipo": self._f_equipment,
                "Por categoría": f_cats,
                "Muestras": sample_view,
            })
            QMessageBox.information(self, t("Exportado"), f"{t('Análisis generado:')}\n{path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al exportar"),
                                 f"{exc}\n\n{traceback.format_exc()}")
