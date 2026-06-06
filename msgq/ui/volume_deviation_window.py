"""Ventana del módulo 'Desviación de Volumen en Entregas (medidor vs guía)'.

Software auditor de las ENTREGAS de combustible: compara el volumen MEDIDO
(`volume`, del medidor digital o del gauge del tanque) contra el DIGITADO en campo
desde la guía del camión (`secondary_volume`), y marca las entregas cuya
diferencia supera el umbral del negocio (1%) — posible sobre-facturación del
proveedor o medidor descalibrado. Ver `core/volume_deviation.py`.

Lee de la réplica SQLite (`movements`); todo el cálculo va en un hilo aparte. El
rango de fechas RE-AUDITA la ventana elegida; el buscador solo proyecta el
resultado ya calculado.
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

from msgq.core import sfl_audit as sa   # reutiliza el indicador de carga histórica
from msgq.core import volume_deviation as vd
from msgq.export import export_sheets
from msgq.i18n import current_language, set_language, t
from msgq.storage import Database
from msgq.ui import theme
from msgq.ui.charts import BarChart
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
    """Lee la réplica (solo la primera vez) y RE-AUDITA la desviación de volumen de
    la ventana de fechas en un hilo aparte. Cachea los movimientos crudos para que
    cambiar el rango sea un re-cálculo en memoria."""

    done = Signal(object, object, object, object)   # movements, result, lo, hi
    failed = Signal(str)

    def __init__(self, db: Database, movements, lo, hi, parent=None):
        super().__init__(parent)
        self._db = db
        self._movements = movements
        self._lo = lo
        self._hi = hi

    def run(self) -> None:  # noqa: D401 - QThread entrypoint
        try:
            mv = self._movements
            if mv is None:
                mv = self._db.read("movements")
            win = mv
            if mv is not None and not mv.empty:
                col = ("record_collected_at" if "record_collected_at" in mv.columns
                       else "updated_at")
                d = pd.to_datetime(mv[col], errors="coerce")
                win = mv[(d >= self._lo) & (d < self._hi)]
            result = vd.audit(win)
            self.done.emit(mv, result, self._lo, self._hi)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")


class VolumeDeviationWindow(QMainWindow):
    """Auditoría de desviación de volumen en entregas (medidor vs guía)."""

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self._db = db
        self._main = parent
        self._qsettings = QSettings("NewmontMerian", "MSGQ")
        self._movements = None
        self._result: vd.VolumeDeviationResult | None = None
        self._f_dev = pd.DataFrame()
        self._last_count = None
        self._loading_range = False
        self._worker: _LoadWorker | None = None
        self._pending_reread = False
        self._busy: BusyOverlay | None = None
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._apply_filters)

        self.setWindowTitle(t("MSGQ — Desviación de Volumen (Entregas)  ·  Newmont Merian"))
        self.resize(1380, 860)
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
        self.setWindowTitle(t("MSGQ — Desviación de Volumen (Entregas)  ·  Newmont Merian"))
        self._build_central()
        if dfrom is not None:
            self._loading_range = True
            self.date_from.setDate(dfrom)
            self.date_to.setDate(dto)
            self._loading_range = False
        if self._result is not None:
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
        box = QGroupBox(t("Auditoría de desviación de volumen (medidor vs guía)"))
        row = QHBoxLayout(box)

        self.cmb_range = QComboBox()
        for label, days in _RANGES:
            self.cmb_range.addItem(t(label), days)
        self.cmb_range.setCurrentIndex(2)
        self.cmb_range.currentIndexChanged.connect(self._apply_quick_range)

        today = QDate.currentDate()
        self.date_from = QDateEdit(today.addDays(-_DEFAULT_RANGE_DAYS))
        self.date_to = QDateEdit(today)
        for de in (self.date_from, self.date_to):
            de.setDisplayFormat("dd/MM/yyyy")
            de.setCalendarPopup(True)
            de.dateChanged.connect(self._on_date_edited)

        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText(t("Buscar por tanque, producto..."))
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
        self.tbl_flagged = PaginatedTableView()
        self.tabs.addTab(self.tbl_flagged, t("Marcadas"))
        self.tbl_all = PaginatedTableView()
        self.tabs.addTab(self.tbl_all, t("Todas las entregas"))
        self.tbl_tank, self.m_tank = make_table()
        self.tabs.addTab(self.tbl_tank, t("Por tanque"))
        self.tabs.addTab(self._build_charts_tab(), t("Gráficas"))
        return self.tabs

    def _build_charts_tab(self) -> QWidget:
        c = QWidget()
        grid = QGridLayout(c)
        self.ch_dev = BarChart(
            t("Mayor desviación de volumen por entrega (%)"), "%", value_suffix="%")
        grid.addWidget(self.ch_dev, 0, 0)
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
        movements = None if reread else self._movements
        self._worker = _LoadWorker(self._db, movements, lo, hi, self)
        self._worker.done.connect(self._on_loaded)
        self._worker.failed.connect(self._on_load_failed)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_loaded(self, movements, result, lo, hi):
        self._movements = movements
        self._result = result
        if movements is not None:
            self._last_count = len(movements)
        self._worker = None
        try:
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
            count = self._db.row_count("movements")
        except Exception:  # noqa: BLE001
            return
        if count != self._last_count:
            self._refresh(reread=True)

    # --- Filtrado y proyección ---------------------------------------------

    def _apply_filters(self):
        if self._result is None:
            return
        try:
            txt = self.txt_search.text().strip().lower()

            def _filt(df, cols):
                if df is None or df.empty or not txt:
                    return df if df is not None else pd.DataFrame()
                present = [c for c in cols if c in df.columns]
                mask = pd.Series(False, index=df.index)
                for c in present:
                    mask |= df[c].astype("string").str.lower().str.contains(txt, na=False)
                return df[mask]

            search_cols = ("tank", "product", "transaction_type", "direction", "source_id")
            dev = _filt(self._result.deviations, search_cols)
            self._f_dev = dev
            fl = dev[dev["flagged"].map(bool)] if not dev.empty else dev

            self.tbl_flagged.set_full_dataframe(fl.reset_index(drop=True) if not fl.empty else fl)
            self.tbl_all.set_full_dataframe(dev.reset_index(drop=True) if not dev.empty else dev)
            self.m_tank.set_dataframe(vd.by_tank(dev))
            self._update_chart(dev)
            self._set_kpis(vd.summary_kpis(dev))
            self._update_progress()
            self._update_status(len(fl) if fl is not None else 0)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al filtrar"),
                                 f"{exc}\n\n{traceback.format_exc()}")

    # --- Gráficas -----------------------------------------------------------

    def _update_chart(self, dev: pd.DataFrame):
        if dev is not None and not dev.empty:
            top = dev.copy()
            top["_abs"] = top["deviation_pct"].abs()
            top = top.sort_values("_abs", ascending=False).head(15)
            labels = [str(x) for x in top["source_id"].tolist()]
            self.ch_dev.set_data(labels, top["deviation_pct"].tolist(), "#C62828")
        else:
            self.ch_dev.set_data([], [])

    # --- KPIs / progreso / estado ------------------------------------------

    def _set_kpis(self, k: dict):
        while self._kpi_layout.count():
            it = self._kpi_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        n_flag = k.get("Entregas marcadas", 0)
        cards = [
            warn_label(t("Entregas marcadas"), f"{n_flag:,}", warn=n_flag > 0),
            kpi_label(t("Entregas analizadas"), f"{k.get('Entregas analizadas', 0):,}"),
            kpi_label(t("Peor desviación %"), f"{k.get('Peor desviación %', 0):,.2f}%", "#833C00"),
            kpi_label(t("Volumen en disputa (L)"), f"{k.get('Volumen en disputa (L)', 0):,.0f}", "#1F4E78"),
            kpi_label(t("Sobre-facturación neta (L)"),
                      f"{k.get('Sobre-facturación neta (L)', 0):,.0f}", "#C62828"),
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

    def _update_status(self, n_flagged: int):
        self.statusBar().showMessage(
            f"{n_flagged:,} {t('entregas marcadas (medidor vs guía)')} · "
            f"{datetime.now():%H:%M:%S}")

    # --- Exportar -----------------------------------------------------------

    def _on_export(self):
        if self._result is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, t("Exportar desviaciones de volumen"),
            t("DesviacionVolumen_MSGQ.xlsx"), "Excel (*.xlsx)")
        if not path:
            return
        try:
            dev = self._f_dev if self._f_dev is not None else self._result.deviations
            fl = dev[dev["flagged"].map(bool)] if not dev.empty else dev
            export_sheets(path, {
                "Marcadas": fl,
                "Detalle de entregas": dev,
                "Por tanque": vd.by_tank(dev),
            })
            QMessageBox.information(self, t("Exportado"), f"{t('Análisis generado:')}\n{path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al exportar"),
                                 f"{exc}\n\n{traceback.format_exc()}")
