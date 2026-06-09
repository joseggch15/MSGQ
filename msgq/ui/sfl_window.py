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
from PySide6.QtCore import QDate, QSettings, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QDateEdit, QFileDialog, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from msgq import config
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


class _LoadWorker(QThread):
    """Lee la replica y recalcula TODOS los excesos/conflictos en un hilo aparte,
    para no congelar la interfaz cuando el historial de movimientos es grande (tras
    el backfill puede haber decenas de miles de filas). Emite los DataFrames ya
    calculados; la GUI solo los proyecta."""

    done = Signal(object, object, object, object)   # movements, limits, exc_all, conf_all
    failed = Signal(str)

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self._db = db

    def run(self) -> None:  # noqa: D401 - QThread entrypoint
        try:
            # Conexion de LECTURA propia (create=False): leer ~cientos de miles de
            # movimientos no debe disputar el lock de escritura del poller (con WAL
            # esta lectura ve una instantanea consistente sin bloquear la sync).
            rdb = Database(self._db.path, create=False)
            try:
                movements = rdb.read("movements")
                limits = rdb.get_consumption_limits()
            finally:
                rdb.close()
            exc_all = sa.exceedances(movements, limits)
            conf_all = sa.unattributed_conflicts(movements, limits)
            self.done.emit(movements, limits, exc_all, conf_all)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")

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
        self._last_backfilled = None
        self._loading_range = False
        self._worker: _LoadWorker | None = None
        self._pending_refresh = False
        self._busy: BusyOverlay | None = None
        # Debounce de la búsqueda: re-filtrar en cada tecla sobre decenas de miles
        # de filas trababa el tipeo; se difiere 250 ms tras la última pulsación.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._apply_filters)
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
        lay.addWidget(self._build_progress())
        lay.addWidget(self._build_kpis())
        lay.addWidget(self._build_tabs(), stretch=1)
        self.setCentralWidget(root)
        # Indicador de carga (cubre el area central durante la lectura/recalculo).
        self._busy = BusyOverlay(root, t("Cargando datos…"))

    def _build_progress(self) -> QFrame:
        """Barra de progreso de la carga histórica para el rango elegido. Da certeza
        de cuándo terminó de cargarse TODA la data con conflictos de SFL del rango."""
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet(f"QFrame{{background:{theme.panel_bg()}; border-radius:6px;}}")
        row = QHBoxLayout(frame)
        row.setContentsMargins(10, 4, 10, 4)
        row.setSpacing(10)
        self._prog_caption = QLabel(t("Carga histórica del rango:"))
        self._prog_bar = QProgressBar()
        self._prog_bar.setRange(0, 100)
        self._prog_bar.setValue(0)
        self._prog_bar.setTextVisible(True)
        self._prog_bar.setFixedWidth(260)
        self._prog_text = QLabel("")
        self._prog_text.setStyleSheet("color:#5A6B7B;")
        row.addWidget(self._prog_caption)
        row.addWidget(self._prog_bar)
        row.addWidget(self._prog_text, stretch=1)
        return frame

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
        self.txt_search.textChanged.connect(lambda _t: self._search_timer.start())

        btn_refresh = QPushButton(t("Actualizar"))
        btn_refresh.clicked.connect(lambda: self._refresh(manual=True))
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
        # Excesos y Conflictos pueden tener decenas de miles de filas tras el
        # backfill: van paginados (solo se materializa una página -> sin congelar).
        self.tbl_exc = PaginatedTableView()
        self.tabs.addTab(self.tbl_exc, t("Excesos"))
        self.tbl_prod, self.m_prod = make_table()
        self.tabs.addTab(self.tbl_prod, t("Por producto"))
        self.tbl_eq, self.m_eq = make_table()
        self.tabs.addTab(self.tbl_eq, t("Por equipo"))
        self.tbl_user, self.m_user = make_table()
        self.tabs.addTab(self.tbl_user, t("Por usuario"))
        self.tbl_conf = PaginatedTableView()
        self.tabs.addTab(self.tbl_conf, t("Conflictos"))
        self.tabs.addTab(self._build_charts_tab(), t("Gráficas"))
        return self.tabs

    def _build_charts_tab(self) -> QWidget:
        c = QWidget(); grid = QGridLayout(c)
        self.ch_time = TimeSeriesChart(t("Excesos de SFL por mes"), t("Excesos"))
        self.ch_prod = BarChart(t("Excesos por producto"), t("Excesos"))
        self.ch_user = BarChart(t("Excesos por usuario de campo"), t("Excesos"))
        grid.addWidget(self.ch_time, 0, 0)
        grid.addWidget(self.ch_prod, 0, 1)
        grid.addWidget(self.ch_user, 1, 0, 1, 2)
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

    def _refresh(self, manual: bool = False):
        """Relee la réplica y recalcula TODOS los excesos (cache) en SEGUNDO PLANO,
        luego filtra. El trabajo pesado va en `_LoadWorker` para no congelar la GUI;
        mientras tanto se muestra el indicador de carga (en la primera carga o en un
        refresco manual se cubre la ventana; en el auto-refresco solo se informa en
        la barra de estado para no interrumpir)."""
        if self._worker is not None and self._worker.isRunning():
            self._pending_refresh = True   # se relanza al terminar el actual
            return
        if manual or self._exc_all.empty:
            self._show_busy(t("Cargando datos…"))
        else:
            self.statusBar().showMessage(f"{t('Actualizando…')} {datetime.now():%H:%M:%S}")
        # parent=self + finished->deleteLater: Qt es dueño del hilo y lo destruye
        # de forma segura cuando termina (evita el "destroyed while running" si la
        # GUI suelta la referencia en el slot `done`).
        self._worker = _LoadWorker(self._db, self)
        self._worker.done.connect(self._on_loaded)
        self._worker.failed.connect(self._on_load_failed)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_loaded(self, movements, limits, exc_all, conf_all):
        self._movements = movements
        self._limits = limits
        self._exc_all = exc_all
        self._conf_all = conf_all
        self._last_counts = (len(movements), len(limits))
        self._worker = None
        try:
            self._refresh_product_combo()
            self._apply_filters()
        finally:
            self._hide_busy()
        if self._pending_refresh:        # llegaron datos nuevos mientras cargaba
            self._pending_refresh = False
            self._refresh()

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

            self._exc = _filt(self._exc_all, ("equipment_id", "equipment_description",
                                              "product", "field_user"))
            self._conf = _filt(self._conf_all, ("equipment_id", "product", "type",
                                                "status", "field_user"))

            exc_view = (self._exc[[c for c in _DISPLAY_COLS if c in self._exc.columns]]
                        if not self._exc.empty else self._exc)
            self.tbl_exc.set_full_dataframe(exc_view)
            self.m_prod.set_dataframe(sa.by_product(self._exc))
            self.m_eq.set_dataframe(sa.by_equipment(self._exc))
            self.m_user.set_dataframe(sa.by_field_user(self._exc))
            self.tbl_conf.set_full_dataframe(self._conf)
            self._update_charts(self._exc)
            self._set_kpis(sa.summary_kpis(self._exc, self._movements), sa.conflict_kpis(self._conf))
            self._update_progress()
            self._update_status()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al filtrar"),
                                 f"{exc}\n\n{traceback.format_exc()}")

    def _maybe_refresh(self):
        try:
            counts = (self._db.row_count("movements"), self._db.row_count("consumption_limits"))
            backfilled = self._db.get_flag("movements_backfill_done") == "1"
        except Exception:  # noqa: BLE001
            return
        # Refresca si llegaron datos nuevos O si el backfill acaba de terminar (para
        # que el indicador de progreso salte a 100% / "completo").
        if counts != self._last_counts or backfilled != self._last_backfilled:
            self._last_backfilled = backfilled
            self._refresh()

    def _update_progress(self):
        """Refleja cuánta de la data histórica del rango elegido ya está cargada."""
        try:
            backfilled = self._db.get_flag("movements_backfill_done") == "1"
        except Exception:  # noqa: BLE001
            backfilled = False
        lo = self._ts(self.date_from.date())
        hi = self._ts(self.date_to.date()).normalize() + pd.Timedelta(days=1)
        pct, done = sa.load_progress(self._movements, lo, hi, backfilled)
        self._prog_bar.setValue(int(round(pct)))
        n_mov, n_exc = len(self._movements), len(self._exc_all)
        chunk = "#2E7D32" if done else "#1F4E78"   # verde al completar, azul cargando
        self._prog_bar.setStyleSheet(
            f"QProgressBar{{border:1px solid {theme.accent('#1F4E78')}; border-radius:4px; "
            f"text-align:center;}} QProgressBar::chunk{{background:{chunk};}}")
        if done:
            self._prog_text.setText(
                f"✓ {t('Datos completos para el rango')} · "
                f"{n_mov:,} {t('movimientos')} · {n_exc:,} {t('excesos')}")
        else:
            oldest = self._oldest_movement_date()
            self._prog_text.setText(
                f"{t('Cargando histórico…')} {n_mov:,} {t('movimientos')} · "
                f"{n_exc:,} {t('excesos')} · {t('más antiguo:')} {oldest}")

    def _oldest_movement_date(self) -> str:
        if self._movements is None or self._movements.empty:
            return "—"
        col = ("record_collected_at" if "record_collected_at" in self._movements.columns
               else "updated_at")
        d = pd.to_datetime(self._movements[col], errors="coerce").dropna()
        return "—" if d.empty else f"{d.min():%d/%m/%Y}"

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
        # Excesos por usuario de campo (top 15): auditoría de operadores con más
        # sobrellenados — apoya la detección de posible sustracción de combustible.
        bu = sa.by_field_user(exc).head(15)
        if not bu.empty:
            self.ch_user.set_data(bu["field_user"].astype(str).tolist(),
                                  bu["Excesos"].tolist(), "#833C00")
        else:
            self.ch_user.set_data([], [])

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
                "Por usuario": sa.by_field_user(self._exc),
                "Conflictos": self._conf,
            })
            QMessageBox.information(self, t("Exportado"), f"{t('Análisis generado:')}\n{path}")
        except PermissionError as exc:
            # Archivo abierto en Excel: mensaje claro y accionable (sin traceback).
            QMessageBox.warning(self, t("Archivo en uso"), str(exc))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al exportar"),
                                 f"{exc}\n\n{traceback.format_exc()}")
