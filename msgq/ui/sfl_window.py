"""Ventana del módulo 'Despachos sobre Safe Fill Level (SFL)'.

Software auditor: lista los despachos cuyo volumen excedió el Safe Fill Level
del equipo para ese producto (sobrellenado — no debería ocurrir) y permite
filtrar por rango de fechas, producto y equipo. La detección vive en
`core/sfl_audit.py`; aquí solo se presenta y se exporta.

Lee de la réplica SQLite: `movements` (despachos) y `consumption_limits` (SFL por
equipo/producto, que el poller sincroniza de `EquipmentItem.consumptionTanks`).
La alarma en vivo (panel de Alertas + notificación de escritorio) la dispara la
ventana principal; este módulo es la vista de análisis.
"""
from __future__ import annotations

import traceback
from datetime import datetime

import pandas as pd
from PySide6.QtCore import QDate, QSettings, QTimer
from PySide6.QtWidgets import (
    QComboBox, QDateEdit, QFileDialog, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QTabWidget, QVBoxLayout, QWidget,
)

from msgq import config
from msgq.core import sfl_audit as sa
from msgq.export import export_sheets
from msgq.i18n import current_language, set_language, t
from msgq.storage import Database
from msgq.ui import theme
from msgq.ui.charts import BarChart, TimeSeriesChart
from msgq.ui.common import (
    kpi_label, language_selector, make_table, theme_selector, warn_label, wrap_with_search,
)

_RANGES = (
    ("Últimos 7 días", 7),
    ("Últimos 30 días", 30),
    ("Últimos 90 días", 90),
    ("Últimos 12 meses", 365),
    ("Todo el rango", None),
)
_DEFAULT_RANGE_DAYS = 90
_HISTORY_START = pd.Timestamp("2022-01-01")

# Columnas visibles de la tabla de excesos (orden legible).
_DISPLAY_COLS = [
    "date", "equipment_id", "equipment_description", "product",
    "volume", "sfl", "excess", "excess_pct", "field_user",
    "dispensing_point", "equipment_status", "source_id",
]


class SFLWindow(QMainWindow):
    """Auditoría de despachos que exceden el Safe Fill Level."""

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self._db = db
        self._main = parent
        self._qsettings = QSettings("NewmontMerian", "MSGQ")
        self._movements = pd.DataFrame()
        self._limits = pd.DataFrame()
        self._exc_all = pd.DataFrame()
        self._exc = pd.DataFrame()
        self._conf_all = pd.DataFrame()
        self._conf = pd.DataFrame()
        self._last_counts = None
        self._loading_range = False
        self.setWindowTitle(t("MSGQ — Despachos sobre SFL  ·  Newmont Merian"))
        self.resize(1420, 880)

        self._build_central()
        self._refresh()

        self._timer = QTimer(self)
        self._timer.setInterval(10000)
        self._timer.timeout.connect(self._maybe_refresh)
        self._timer.start()

    def closeEvent(self, event):  # noqa: N802 - override Qt
        self._timer.stop()
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
        self.setWindowTitle(t("MSGQ — Despachos sobre SFL  ·  Newmont Merian"))
        self._build_central()
        if dfrom is not None:
            self._loading_range = True
            self.date_from.setDate(dfrom)
            self.date_to.setDate(dto)
            self._loading_range = False
        self._refresh()

    # --- Construcción -------------------------------------------------------

    def _build_central(self) -> None:
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setSpacing(6)
        lay.addWidget(self._build_controls())
        lay.addWidget(self._build_kpis())
        lay.addWidget(self._build_tabs(), stretch=1)
        self.setCentralWidget(root)

    def _build_controls(self) -> QGroupBox:
        box = QGroupBox(t("Auditoría de Safe Fill Level (SFL)"))
        row = QHBoxLayout(box)

        self.cmb_range = QComboBox()
        for label, days in _RANGES:
            self.cmb_range.addItem(t(label), days)
        self.cmb_range.setCurrentIndex(2)   # Últimos 90 días
        self.cmb_range.currentIndexChanged.connect(self._apply_quick_range)

        today = QDate.currentDate()
        self.date_from = QDateEdit(today.addDays(-_DEFAULT_RANGE_DAYS))
        self.date_to = QDateEdit(today)
        for de in (self.date_from, self.date_to):
            de.setDisplayFormat("dd/MM/yyyy")
            de.setCalendarPopup(True)
            de.dateChanged.connect(self._on_date_edited)

        self.cmb_product = QComboBox()
        self.cmb_product.addItem(t("Todos"), None)
        self.cmb_product.currentIndexChanged.connect(self._apply_filters)

        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText(t("Buscar por ID, descripción, marca, modelo..."))
        self.txt_search.textChanged.connect(self._apply_filters)

        btn_refresh = QPushButton(t("Actualizar"))
        btn_refresh.clicked.connect(self._refresh)
        btn_export = QPushButton(t("Exportar a Excel…"))
        btn_export.clicked.connect(self._on_export)

        row.addWidget(QLabel(t("Rango:")))
        row.addWidget(self.cmb_range)
        row.addWidget(QLabel(t("Desde:")))
        row.addWidget(self.date_from)
        row.addWidget(QLabel(t("Hasta:")))
        row.addWidget(self.date_to)
        row.addWidget(QLabel(t("Producto:")))
        row.addWidget(self.cmb_product)
        row.addWidget(QLabel(t("Buscar:")))
        row.addWidget(self.txt_search, stretch=1)
        row.addWidget(btn_refresh)
        row.addWidget(btn_export)
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
        self.tbl_exc, self.m_exc = make_table()
        self.tabs.addTab(wrap_with_search(self.tbl_exc), t("Excesos"))
        self.tbl_prod, self.m_prod = make_table()
        self.tabs.addTab(self.tbl_prod, t("Por producto"))
        self.tbl_eq, self.m_eq = make_table()
        self.tabs.addTab(self.tbl_eq, t("Por equipo"))
        self.tbl_conf, self.m_conf = make_table()
        self.tabs.addTab(wrap_with_search(self.tbl_conf), t("Conflictos"))
        self.tabs.addTab(self._build_charts_tab(), t("Gráficas"))
        return self.tabs

    def _build_charts_tab(self) -> QWidget:
        c = QWidget(); grid = QGridLayout(c)
        self.ch_time = TimeSeriesChart(t("Excesos de SFL por mes"), t("Excesos"))
        self.ch_prod = BarChart(t("Excesos por producto"), t("Excesos"))
        grid.addWidget(self.ch_time, 0, 0)
        grid.addWidget(self.ch_prod, 0, 1)
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
        self._apply_filters()

    def _on_date_edited(self, _date):
        if not self._loading_range:
            self._apply_filters()

    @staticmethod
    def _ts(qdate: QDate) -> pd.Timestamp:
        return pd.Timestamp(qdate.toPython())

    # --- Refresco -----------------------------------------------------------

    def _refresh(self):
        """Relee la réplica y recalcula TODOS los excesos (cache); luego filtra."""
        try:
            self._movements = self._db.read("movements")
            self._limits = self._db.get_consumption_limits()
            self._last_counts = (len(self._movements), len(self._limits))
            self._exc_all = sa.exceedances(self._movements, self._limits)
            self._conf_all = sa.unattributed_conflicts(self._movements, self._limits)
            self._refresh_product_combo()
            self._apply_filters()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al analizar"),
                                 f"{exc}\n\n{traceback.format_exc()}")

    def _refresh_product_combo(self):
        current = self.cmb_product.currentData()
        self.cmb_product.blockSignals(True)
        self.cmb_product.clear()
        self.cmb_product.addItem(t("Todos"), None)
        if not self._exc_all.empty:
            for p in sorted(self._exc_all["product"].dropna().astype(str).unique()):
                self.cmb_product.addItem(p, p)
        ix = self.cmb_product.findData(current)
        self.cmb_product.setCurrentIndex(ix if ix >= 0 else 0)
        self.cmb_product.blockSignals(False)

    def _apply_filters(self):
        """Filtra los caches (excesos y conflictos) por rango/producto/búsqueda."""
        try:
            lo = self._ts(self.date_from.date())
            hi = self._ts(self.date_to.date()).normalize() + pd.Timedelta(days=1)
            prod = self.cmb_product.currentData()
            txt = self.txt_search.text().strip().lower()

            def _filt(df, search_cols):
                if df is None or df.empty:
                    return pd.DataFrame() if df is None else df
                d = pd.to_datetime(df["date"], errors="coerce")
                out = df[(d >= lo) & (d < hi)]
                if prod is not None and "product" in out.columns:
                    out = out[out["product"].astype("string") == str(prod)]
                if txt:
                    cols = [c for c in search_cols if c in out.columns]
                    mask = pd.Series(False, index=out.index)
                    for c in cols:
                        mask |= out[c].astype("string").str.lower().str.contains(txt, na=False)
                    out = out[mask]
                return out

            self._exc = _filt(self._exc_all, ("equipment_id", "equipment_description", "product"))
            self._conf = _filt(self._conf_all, ("equipment_id", "product", "type", "status"))

            self.m_exc.set_dataframe(self._exc[[c for c in _DISPLAY_COLS if c in self._exc.columns]]
                                     if not self._exc.empty else self._exc)
            self.m_prod.set_dataframe(sa.by_product(self._exc))
            self.m_eq.set_dataframe(sa.by_equipment(self._exc))
            self.m_conf.set_dataframe(self._conf)
            self._update_charts(self._exc)
            self._set_kpis(sa.summary_kpis(self._exc, self._movements), sa.conflict_kpis(self._conf))
            self._update_status()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al filtrar"),
                                 f"{exc}\n\n{traceback.format_exc()}")

    def _maybe_refresh(self):
        try:
            counts = (self._db.row_count("movements"), self._db.row_count("consumption_limits"))
        except Exception:  # noqa: BLE001
            return
        if counts != self._last_counts:
            self._refresh()

    def _update_status(self):
        pct = config.SFL_TOLERANCE_PCT * 100
        self.statusBar().showMessage(
            f"{len(self._exc):,} {t('despachos sobre SFL en el rango')} · "
            f"{t('tolerancia')} {pct:g}% · {datetime.now():%H:%M:%S}")

    def _update_charts(self, exc: pd.DataFrame):
        ot = sa.over_time(exc)
        if not ot.empty:
            self.ch_time.set_series(ot["Periodo"].tolist(), {t("Excesos"): ot["Excesos"].tolist()})
        else:
            self.ch_time.set_series([], {})
        bp = sa.by_product(exc)
        if not bp.empty:
            self.ch_prod.set_data(bp["Producto"].astype(str).tolist(), bp["Excesos"].tolist(), "#C62828")
        else:
            self.ch_prod.set_data([], [])

    def _set_kpis(self, k: dict, ck: dict):
        while self._kpi_layout.count():
            it = self._kpi_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        n = k.get("Excesos", 0)
        nconf = ck.get("Conflictos", 0)
        nover = ck.get("Sobre SFL flota", 0)
        cards = [
            warn_label(t("Excesos"), f"{n:,}", warn=n > 0),
            kpi_label(t("Exceso total (L)"), f"{k.get('Exceso total (L)', 0):,.1f}", "#C62828"),
            kpi_label(t("Peor exceso (L)"), f"{k.get('Peor exceso (L)', 0):,.1f}", "#833C00"),
            kpi_label(t("Equipos afectados"), f"{k.get('Equipos afectados', 0):,}"),
            kpi_label(t("% de despachos"), f"{k.get('% de despachos', 0):.2f}%"),
            warn_label(t("Conflictos"), f"{nconf:,}", warn=nconf > 0),
            warn_label(t("Sobre SFL flota"), f"{nover:,}", warn=nover > 0),
        ]
        for c in cards:
            self._kpi_layout.addWidget(c)
        self._kpi_layout.addStretch(1)

    # --- Exportar -----------------------------------------------------------

    def _on_export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, t("Exportar auditoría SFL"), t("SFL_Auditoria_MSGQ.xlsx"), "Excel (*.xlsx)")
        if not path:
            return
        try:
            export_sheets(path, {
                "Excesos": self._exc[[c for c in _DISPLAY_COLS if c in self._exc.columns]]
                           if not self._exc.empty else self._exc,
                "Por producto": sa.by_product(self._exc),
                "Por equipo": sa.by_equipment(self._exc),
                "Conflictos": self._conf,
            })
            QMessageBox.information(self, t("Exportado"), f"{t('Análisis generado:')}\n{path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al exportar"),
                                 f"{exc}\n\n{traceback.format_exc()}")
