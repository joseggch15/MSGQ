"""Ventana del módulo 'Auditoría de Actividad — Equipos fantasma'.

Tres listas accionables (ver `core/activity_audit.py`):

  • **Equipos fantasma**: 'In Service' sin despachos en ≥ N días (umbral
    elegible) o que nunca despacharon — distorsionan los KPIs de disponibilidad.
  • **Trabaja sin repostar**: el avance de SMU implica un consumo mayor que el
    tanque (SFL) sin despacho de por medio → combustible no registrado en el FMS.
  • **Repostado sin operar**: rachas de despachos con el SMU congelado; si los
    litros acumulados superan el SFL, el tanque no pudo absorberlos sin operar.

Lee de la réplica en un `_LoadWorker` (QThread) y solo proyecta en la GUI; el
umbral de inactividad filtra el cache local sin relanzar la lectura. Exporta
todo a Excel con el estilo del ecosistema.
"""
from __future__ import annotations

import traceback
from datetime import datetime

import pandas as pd
from PySide6.QtCore import QSettings, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QMainWindow, QMessageBox, QPushButton, QTabWidget, QVBoxLayout,
    QWidget,
)

from msgq import config
from msgq.core import activity_audit as aa
from msgq.export import export_sheets
from msgq.i18n import current_language, set_language, t
from msgq.storage import Database
from msgq.ui import theme
from msgq.ui.charts import BarChart
from msgq.ui.common import (
    BusyOverlay, kpi_label, language_selector, make_table, theme_selector,
    warn_label, wrap_with_search,
)

_IDLE_THRESHOLDS = (15, 30, 60, 90)


class _LoadWorker(QThread):
    """Lee la réplica y calcula los tres detectores en un hilo aparte."""

    done = Signal(object)   # dict: idle_all, unfueled, frozen, counts, smu_eq
    failed = Signal(str)

    # Columnas de `movements` que la auditoría consume (12 de 46).
    _MOVEMENT_COLS = ["id", "kind", "record_collected_at", "updated_at",
                      "equipment_id", "equipment_description", "product",
                      "volume", "smu_value", "smu_type", "field_user", "tank"]

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self._db = db

    def run(self) -> None:  # noqa: D401 - QThread entrypoint
        try:
            rdb = Database(self._db.path, create=False)
            try:
                movements = rdb.read("movements", columns=self._MOVEMENT_COLS)
                equipment = rdb.get_equipment()
                limits = rdb.get_consumption_limits()
                counts = (rdb.row_count("movements"), rdb.row_count("equipment"))
            finally:
                rdb.close()
            # min_days=0: la lista completa con sus días; la vista aplica el
            # umbral localmente (cambiarlo no relee la réplica).
            idle_all = aa.idle_assets(equipment, movements, min_days=0)
            unfueled = aa.unfueled_activity(movements, equipment, limits)
            frozen = aa.fueling_without_activity(movements, equipment, limits)
            disp = movements[movements["kind"] == config.KIND_DISPENSE]
            smu_eq = int(disp.loc[pd.to_numeric(disp["smu_value"],
                                                errors="coerce").notna(),
                                  "equipment_id"].nunique())
            self.done.emit({"idle_all": idle_all, "unfueled": unfueled,
                            "frozen": frozen, "equipment": equipment,
                            "counts": counts, "smu_eq": smu_eq})
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")


class ActivityWindow(QMainWindow):
    """Auditoría de actividad: fantasmas y coherencia actividad↔combustible."""

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self._db = db
        self._main = parent
        self._qsettings = QSettings("NewmontMerian", "MSGQ")
        self._idle_all = pd.DataFrame()
        self._unfueled = pd.DataFrame()
        self._frozen = pd.DataFrame()
        self._equipment = pd.DataFrame()
        self._smu_eq = 0
        self._last_counts = None
        self._worker: _LoadWorker | None = None
        self._pending_refresh = False
        self._busy: BusyOverlay | None = None
        self.setWindowTitle(t("MSGQ — Auditoría de Actividad  ·  Newmont Merian"))
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
        self.setWindowTitle(t("MSGQ — Auditoría de Actividad  ·  Newmont Merian"))
        self._build_central()
        self._refresh()

    # --- Construcción ---------------------------------------------------------

    def _build_central(self) -> None:
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setSpacing(6)
        lay.addWidget(self._build_controls())
        lay.addWidget(self._build_kpis())
        lay.addWidget(self._build_tabs(), stretch=1)
        self.setCentralWidget(root)
        self._busy = BusyOverlay(root, t("Cargando datos…"))

    def _build_controls(self) -> QGroupBox:
        box = QGroupBox(t("Auditoría de actividad (equipos fantasma y coherencia SMU↔combustible)"))
        row = QHBoxLayout(box)
        self.cmb_idle = QComboBox()
        for d in _IDLE_THRESHOLDS:
            self.cmb_idle.addItem(f"≥ {d} {t('días')}", d)
        # Umbral por defecto: el crítico de los KPIs (30 días).
        ix = (_IDLE_THRESHOLDS.index(config.IDLE_ASSET_DAYS_CRITICAL)
              if config.IDLE_ASSET_DAYS_CRITICAL in _IDLE_THRESHOLDS else 1)
        self.cmb_idle.setCurrentIndex(ix)
        self.cmb_idle.currentIndexChanged.connect(self._project)

        btn_refresh = QPushButton(t("Actualizar"))
        btn_refresh.clicked.connect(self._refresh)
        btn_export = QPushButton(t("Exportar a Excel…"))
        btn_export.clicked.connect(self._on_export)

        row.addWidget(QLabel(t("Umbral de inactividad:")))
        row.addWidget(self.cmb_idle)
        row.addStretch(1)
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
        self.tbl_idle, self.m_idle = make_table()
        self.tabs.addTab(wrap_with_search(self.tbl_idle), t("Equipos fantasma"))
        self.tbl_unf, self.m_unf = make_table()
        self.tabs.addTab(wrap_with_search(self.tbl_unf), t("Trabaja sin repostar"))
        self.tbl_frz, self.m_frz = make_table()
        self.tabs.addTab(wrap_with_search(self.tbl_frz), t("Repostado sin operar"))
        self.tabs.addTab(self._build_charts_tab(), t("Gráficas"))
        return self.tabs

    def _build_charts_tab(self) -> QWidget:
        c = QWidget(); grid = QGridLayout(c)
        self.ch_cat = BarChart(t("Equipos fantasma por categoría"), t("Equipos"))
        self.ch_grp = BarChart(t("Equipos fantasma por grupo"), t("Equipos"))
        grid.addWidget(self.ch_cat, 0, 0)
        grid.addWidget(self.ch_grp, 0, 1)
        return c

    # --- Datos -----------------------------------------------------------------

    def _refresh(self):
        if self._worker is not None and self._worker.isRunning():
            self._pending_refresh = True
            return
        if self._idle_all.empty and self._busy is not None:
            self._busy.start(t("Cargando datos…"))
        self._worker = _LoadWorker(self._db, self)
        self._worker.done.connect(self._on_loaded)
        self._worker.failed.connect(self._on_load_failed)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_loaded(self, data: dict):
        self._worker = None
        self._idle_all = data["idle_all"]
        self._unfueled = data["unfueled"]
        self._frozen = data["frozen"]
        self._equipment = data["equipment"]
        self._smu_eq = data["smu_eq"]
        self._last_counts = data["counts"]
        try:
            self._project()
        finally:
            if self._busy is not None:
                self._busy.stop()
        if self._pending_refresh:
            self._pending_refresh = False
            self._refresh()

    def _on_load_failed(self, message: str):
        self._worker = None
        if self._busy is not None:
            self._busy.stop()
        QMessageBox.critical(self, t("Error al analizar"), message)

    def _maybe_refresh(self):
        try:
            counts = (self._db.row_count("movements"), self._db.row_count("equipment"))
        except Exception:  # noqa: BLE001
            return
        if counts != self._last_counts:
            self._refresh()

    # --- Proyección -------------------------------------------------------------

    def _idle_filtered(self) -> pd.DataFrame:
        days = float(self.cmb_idle.currentData() or config.IDLE_ASSET_DAYS_CRITICAL)
        if self._idle_all.empty:
            return self._idle_all
        d = self._idle_all
        return d[d["ultimo_despacho"].isna() | (d["dias_sin_despachar"] >= days)]

    def _project(self):
        try:
            idle = self._idle_filtered()
            self.m_idle.set_dataframe(idle)
            self.m_unf.set_dataframe(self._unfueled)
            self.m_frz.set_dataframe(self._frozen)
            self._update_charts(idle)
            days = float(self.cmb_idle.currentData() or 0)
            self._set_kpis(aa.kpis(idle, self._unfueled, self._frozen,
                                   self._equipment, days))
            self._update_status()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al filtrar"),
                                 f"{exc}\n\n{traceback.format_exc()}")

    def _update_charts(self, idle: pd.DataFrame):
        for chart, col in ((self.ch_cat, "category"), (self.ch_grp, "group")):
            if idle is None or idle.empty or col not in idle.columns:
                chart.set_data([], [])
                continue
            g = (idle[col].astype("string").str.strip().replace({"": pd.NA})
                 .fillna(t("(sin dato)")).value_counts().head(12))
            chart.set_data([str(x) for x in g.index], g.tolist(), "#7030A0")

    def _set_kpis(self, k: dict):
        while self._kpi_layout.count():
            it = self._kpi_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        n_idle = next((v for kk, v in k.items() if kk.startswith("Fantasmas")), 0)
        cards = [
            kpi_label(t("Equipos In Service"), f"{k.get('Equipos In Service', 0):,}"),
            warn_label(t("Equipos fantasma"), f"{n_idle:,}", warn=n_idle > 0),
            warn_label(t("Nunca despacharon"), f"{k.get('Nunca despacharon', 0):,}",
                       warn=k.get("Nunca despacharon", 0) > 0),
            kpi_label(t("% de la flota IN"), f"{k.get('% de la flota IN', 0):.1f}%", "#7030A0"),
            warn_label(t("Trabaja sin repostar"), f"{k.get('Trabaja sin repostar', 0):,}",
                       warn=k.get("Trabaja sin repostar", 0) > 0),
            kpi_label(t("Combustible no registrado (L)"),
                      f"{k.get('Combustible no registrado (L)', 0):,.0f}", "#C62828"),
            warn_label(t("Repostado sin operar"), f"{k.get('Repostado sin operar', 0):,}",
                       warn=k.get("Repostado sin operar", 0) > 0),
            warn_label(t("Rachas sobre SFL"), f"{k.get('Rachas sobre SFL', 0):,}",
                       warn=k.get("Rachas sobre SFL", 0) > 0),
        ]
        for c in cards:
            self._kpi_layout.addWidget(c)
        self._kpi_layout.addStretch(1)

    def _update_status(self):
        self.statusBar().showMessage(
            f"{t('Equipos con SMU por despacho (detectores 2 y 3):')} "
            f"{self._smu_eq:,} · {datetime.now():%H:%M:%S}")

    # --- Exportar ----------------------------------------------------------------

    def _on_export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, t("Exportar auditoría de actividad"),
            t("Auditoria_Actividad_MSGQ.xlsx"), "Excel (*.xlsx)")
        if not path:
            return
        try:
            days = float(self.cmb_idle.currentData() or 0)
            k = aa.kpis(self._idle_filtered(), self._unfueled, self._frozen,
                        self._equipment, days)
            resumen = pd.DataFrame({"Indicador": list(k.keys()),
                                    "Valor": [k[x] for x in k]})
            export_sheets(path, {
                "Resumen": resumen,
                "Equipos fantasma": self._idle_filtered(),
                "Trabaja sin repostar": self._unfueled,
                "Repostado sin operar": self._frozen,
            })
            QMessageBox.information(self, t("Exportado"), f"{t('Análisis generado:')}\n{path}")
        except PermissionError as exc:
            QMessageBox.warning(self, t("Archivo en uso"), str(exc))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al exportar"),
                                 f"{exc}\n\n{traceback.format_exc()}")
