"""Ventana de análisis de la flota de equipos.

Lee de la réplica SQLite (sin tocar el poller) y ofrece:
  • Inventario filtrable (doble clic en un equipo → su Audit Log completo).
  • KPIs de flota y agrupaciones por categoría / grupo / departamento / marca.
  • Analítica temporal del log de auditoría GraphQL:
      - Frecuencia de cambio de RFID.
      - Transiciones de estado (foco In→Out / Out→In): top equipos, por grupo,
        por cost centre, y tiempo en servicio.
      - Cambios de cost centre: equipos que más se reasignan y cost centres con
        más actividad.
      - Atributos que más se modifican y auditoría por usuario.
  • Gráficas (pyqtgraph) y exportación a Excel.

El idioma (ES/EN) se comparte con la ventana principal; el selector de la barra
de filtros lo cambia y reconstruye la interfaz (chrome, tablas, gráficas, export).
"""
from __future__ import annotations

import traceback
from datetime import datetime

import pandas as pd
from PySide6.QtCore import Qt, QSettings, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from msgq import config
from msgq.core import data_quality as dq
from msgq.core import equipment_analytics as ea
from msgq.export import export_sheets
from msgq.i18n import current_language, set_language, t, tr_fmt, tr_value
from msgq.storage import Database
from msgq.ui import theme
from msgq.ui.charts import BarChart, TimeSeriesChart
from msgq.ui.common import (
    BusyOverlay, kpi_label, language_selector, make_table, theme_selector,
    warn_label, wrap_with_search,
)

_INVENTORY_COLS = [
    "equipment_id", "description", "status", "group", "category", "make",
    "model", "department", "cost_centre", "is_light_vehicle",
    "is_contractor_vehicle", "rfid", "dispense_limited", "service_interval",
    "service_interval_type",
]
IN, OUT = config.STATUS_IN, config.STATUS_OUT


class AuditLogDialog(QDialog):
    """Historial de cambios de un equipo (como la pestaña Audit Log de AdaptIQ)."""

    def __init__(self, eq_id, description, log_df: pd.DataFrame, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Audit Log — {eq_id}")
        self.resize(940, 620)
        lay = QVBoxLayout(self)
        head = QLabel(f"<b>{eq_id}</b> &nbsp; {description or ''}")
        head.setTextFormat(Qt.RichText)
        lay.addWidget(head)
        view, model = make_table()
        model.set_dataframe(log_df)
        lay.addWidget(view)
        msg = (f"{len(log_df):,} {t('cambios registrados en la réplica.')}"
               if log_df is not None and not log_df.empty
               else t("Sin cambios de este equipo en la réplica todavía "
                      "(el log se llena al sincronizar)."))
        lay.addWidget(QLabel(msg))


class _LoadWorker(QThread):
    """Lee la réplica y calcula la analítica temporal (cara) en un hilo aparte.

    Antes `_refresh` leía el log de auditoría completo y recorría las decenas de
    miles de eventos EN EL HILO DE LA GUI en cada auto-refresco, congelando la
    ventana. Aquí se lee y se calcula todo; la GUI solo proyecta resultados."""

    done = Signal(object)   # dict con los DataFrames/series ya calculados
    failed = Signal(str)

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self._db = db

    def run(self) -> None:  # noqa: D401 - QThread entrypoint
        try:
            rdb = Database(self._db.path, create=False)
            try:
                eq_all = rdb.get_equipment()
                changes = rdb.get_change_events()
            finally:
                rdb.close()
            trans = ea.status_transitions(changes, eq_all)
            out = {
                "eq_all": eq_all,
                "changes": changes,
                "trans": trans,
                "rfid_sum": ea.rfid_change_summary(changes),
                "rfid_time": ea.rfid_changes_over_time(changes),
                "rfid_churn": ea.rfid_churn_by_tag(changes),
                "trans_sum": ea.status_transition_summary(trans),
                "top_oi": ea.top_equipment_by_transition(trans, OUT, IN),
                "top_io": ea.top_equipment_by_transition(trans, IN, OUT),
                "by_grp": ea.transitions_by_dimension(trans, "group", "Grupo"),
                "by_cc": ea.transitions_by_dimension(trans, "cost_centre", "Cost Centre"),
                "tis": ea.time_in_service(trans),
                "cc_eq": ea.top_equipment_by_attribute(
                    changes, config.ATTR_COST_CENTRE, eq_all, label="Cambios CC"),
                "cc_by": ea.attribute_change_by_dimension(
                    changes, config.ATTR_COST_CENTRE, eq_all, "cost_centre", "Cost Centre"),
                "attr": ea.attribute_change_summary(changes),
                "audit": ea.audit_by_user(changes),
                "comp": ea.data_completeness(eq_all),
            }
            self.done.emit(out)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")


class _DataQualityWorker(QThread):
    """Corre la auditoría de calidad de datos (variantes + fuzzy O(n²)) en un hilo
    aparte para no congelar la GUI sobre un maestro grande. Emite el AuditResult."""

    done = Signal(object)   # data_quality.AuditResult, o None si falló

    def __init__(self, eq: pd.DataFrame, parent=None):
        super().__init__(parent)
        self._eq = eq

    def run(self) -> None:  # noqa: D401 - QThread entrypoint
        try:
            self.done.emit(dq.audit(self._eq))
        except Exception:  # noqa: BLE001
            self.done.emit(None)


class EquipmentWindow(QMainWindow):
    """Análisis de inventario + auditoría de la flota."""

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self._db = db
        self._main = parent
        self._qsettings = QSettings("NewmontMerian", "MSGQ")
        self._eq_all = pd.DataFrame()
        self._changes = pd.DataFrame()
        self._trans = pd.DataFrame()
        self._rfid_sum = ea.rfid_change_summary(pd.DataFrame())
        self._last_counts = None
        # Auditoría de calidad de datos: se calcula en segundo plano y sólo cuando
        # el maestro de equipos cambió (no depende del log de cambios).
        self._dq = None
        self._dq_worker: _DataQualityWorker | None = None
        self._dq_eq_count = None
        self._worker: _LoadWorker | None = None
        self._pending_refresh = False
        self._busy: BusyOverlay | None = None
        self.setWindowTitle(t("MSGQ — Análisis de Equipos  ·  Newmont Merian"))
        self.resize(1420, 880)

        self._build_central()
        self._refresh()

        # Auto-refresco: recoge los datos a medida que el poller los sincroniza.
        self._timer = QTimer(self)
        self._timer.setInterval(10000)
        self._timer.timeout.connect(self._maybe_refresh)
        self._timer.start()

    def closeEvent(self, event):  # noqa: N802 - override Qt
        self._timer.stop()
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(3000)
        if self._dq_worker is not None and self._dq_worker.isRunning():
            self._dq_worker.wait(3000)
        event.accept()

    # --- Idioma -------------------------------------------------------------

    def _on_language_changed(self, code: str) -> None:
        if not code or code == current_language():
            return
        # La ventana principal es la fuente única: actualiza ambas ventanas.
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
        """Reconstruye esta ventana en el idioma/tema global actual (la invoca la
        ventana principal al cambiar idioma o tema; recolorea KPIs y gráficas)."""
        self.setWindowTitle(t("MSGQ — Análisis de Equipos  ·  Newmont Merian"))
        self._build_central()
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
        # Indicador de carga (cubre el área central durante la lectura/recalculo).
        self._busy = BusyOverlay(root, t("Cargando datos…"))

    def _build_controls(self) -> QGroupBox:
        box = QGroupBox(t("Filtros"))
        row = QHBoxLayout(box)
        # Los filtros usan _apply_filters (rapido); no recalculan la analitica temporal.
        # Cada combo guarda el VALOR canónico en userData; el texto visible se
        # traduce. Así filtrar no depende del idioma de la etiqueta.
        eq = self._db.get_equipment()
        self.cmb_status = QComboBox()
        self.cmb_status.addItem(t("Todos"), None)
        for st in (config.STATUS_IN, config.STATUS_OUT, config.STATUS_DECOM):
            self.cmb_status.addItem(st, st)   # estados FMS: texto original
        self.cmb_status.currentIndexChanged.connect(self._apply_filters)

        self.cmb_type = QComboBox()
        self.cmb_type.addItem(t("Todos"), None)
        self.cmb_type.addItem(t("Propios"), "own")
        self.cmb_type.addItem(t("Contratistas"), "contractor")
        self.cmb_type.currentIndexChanged.connect(self._apply_filters)

        self.cmb_category = QComboBox()
        self.cmb_category.addItem(t("Todas"), None)
        for v in _distinct(eq, "category"):
            self.cmb_category.addItem(v, v)
        self.cmb_category.currentIndexChanged.connect(self._apply_filters)

        self.cmb_group = QComboBox()
        self.cmb_group.addItem(t("Todos"), None)
        for v in _distinct(eq, "group"):
            self.cmb_group.addItem(v, v)
        self.cmb_group.currentIndexChanged.connect(self._apply_filters)

        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText(t("Buscar por ID, descripción, marca, modelo..."))
        self.txt_search.textChanged.connect(self._apply_filters)
        btn_refresh = QPushButton(t("Actualizar")); btn_refresh.clicked.connect(self._refresh)
        btn_export = QPushButton(t("Exportar a Excel…")); btn_export.clicked.connect(self._on_export)

        for label, w in ((t("Estado:"), self.cmb_status), (t("Tipo:"), self.cmb_type),
                         (t("Categoría:"), self.cmb_category), (t("Grupo:"), self.cmb_group)):
            row.addWidget(QLabel(label)); row.addWidget(w)
        row.addWidget(QLabel(t("Buscar:"))); row.addWidget(self.txt_search, stretch=1)
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

        # Inventario (con doble clic -> audit log)
        inv = QWidget(); ilay = QVBoxLayout(inv); ilay.setContentsMargins(2, 2, 2, 2)
        ilay.addWidget(QLabel(t("Doble clic en un equipo para ver su <b>Audit Log</b> completo.")))
        self.tbl_inv, self.m_inv = make_table()
        self.tbl_inv.doubleClicked.connect(self._on_inv_double_clicked)
        ilay.addWidget(self.tbl_inv)
        self.tabs.addTab(inv, t("Inventario"))

        # Agrupaciones
        agg = QTabWidget()
        self.tbl_cat, self.m_cat = make_table(); agg.addTab(self.tbl_cat, t("Categoría"))
        self.tbl_grp, self.m_grp = make_table(); agg.addTab(self.tbl_grp, t("Grupo"))
        self.tbl_dep, self.m_dep = make_table(); agg.addTab(self.tbl_dep, t("Departamento"))
        self.tbl_mk, self.m_mk = make_table(); agg.addTab(self.tbl_mk, t("Marca"))
        self.tabs.addTab(agg, t("Agrupaciones"))

        self.tabs.addTab(self._build_rfid_tab(), t("Cambios de RFID"))
        self.tabs.addTab(self._build_status_tab(), t("Transiciones de estado"))
        self.tabs.addTab(self._build_costcentre_tab(), t("Cost center"))

        self.tbl_attr, self.m_attr = make_table()
        self.tabs.addTab(self.tbl_attr, t("Atributos"))
        self.tbl_audit, self.m_audit = make_table()
        self.tabs.addTab(self.tbl_audit, t("Auditoría (quién)"))
        self.tabs.addTab(self._build_quality_tab(), t("Calidad de datos"))
        self.tabs.addTab(self._build_charts_tab(), t("Gráficas"))
        return self.tabs

    def _build_quality_tab(self) -> QWidget:
        """Auditoría de integridad de los datos maestros: variantes de un mismo
        valor escrito distinto (Ford/ford/F0RD) y duplicados léxicos (fuzzy), que
        ensucian las agrupaciones de KPIs gerenciales."""
        c = QWidget(); lay = QVBoxLayout(c); lay.setContentsMargins(4, 4, 4, 4)
        self.lbl_dq = QLabel()
        self.lbl_dq.setTextFormat(Qt.RichText)
        self.lbl_dq.setWordWrap(True)
        lay.addWidget(self.lbl_dq)
        hint = QLabel(t("Auditoría de integridad: «Ford» vs «ford» vs «F0RD» y "
                        "duplicados por typo ensucian las agrupaciones de KPIs."))
        hint.setStyleSheet("color:#5A6B7B;")
        lay.addWidget(hint)
        inner = QTabWidget()
        self.tbl_dq_sum, self.m_dq_sum = make_table()
        inner.addTab(self.tbl_dq_sum, t("Resumen"))
        self.tbl_dq_var, self.m_dq_var = make_table()
        inner.addTab(wrap_with_search(self.tbl_dq_var), t("Variantes (mayúsc./espacios)"))
        self.tbl_dq_fuzzy, self.m_dq_fuzzy = make_table()
        inner.addTab(wrap_with_search(self.tbl_dq_fuzzy), t("Duplicados léxicos (fuzzy)"))
        self.tbl_comp, self.m_comp = make_table()
        inner.addTab(self.tbl_comp, t("Completitud"))
        lay.addWidget(inner)
        return c

    def _build_rfid_tab(self) -> QWidget:
        c = QWidget(); lay = QVBoxLayout(c)
        self.lbl_rfid = QLabel()
        self.lbl_rfid.setStyleSheet(f"font-weight:bold; color:{theme.accent('#1F4E78')};")
        lay.addWidget(self.lbl_rfid)
        split = QSplitter(Qt.Vertical)
        for title, attr in ((t("Eventos de RFID por mes (asignado / cambiado / removido)"), "tbl_rfid_time"),
                            (t("Tags con más cambios (re-tagueo)"), "tbl_rfid_churn")):
            w = QWidget(); l = QVBoxLayout(w); l.setContentsMargins(0, 0, 0, 0)
            l.addWidget(QLabel(title))
            table, model = make_table(); setattr(self, attr, table)
            setattr(self, attr.replace("tbl_", "m_"), model)
            l.addWidget(table); split.addWidget(w)
        lay.addWidget(split)
        return c

    def _build_status_tab(self) -> QWidget:
        c = QWidget(); lay = QVBoxLayout(c)
        inner = QTabWidget()
        self.tbl_trans, self.m_trans = make_table()
        inner.addTab(wrap_with_search(self.tbl_trans), t("Transiciones"))
        self.tbl_trans_sum, self.m_trans_sum = make_table()
        inner.addTab(self.tbl_trans_sum, t("Resumen"))
        self.tbl_top_oi, self.m_top_oi = make_table()
        inner.addTab(wrap_with_search(self.tbl_top_oi), t("Top Out→In"))
        self.tbl_top_io, self.m_top_io = make_table()
        inner.addTab(wrap_with_search(self.tbl_top_io), t("Top In→Out"))
        self.tbl_by_grp, self.m_by_grp = make_table()
        inner.addTab(self.tbl_by_grp, t("Por grupo"))
        self.tbl_by_cc, self.m_by_cc = make_table()
        inner.addTab(self.tbl_by_cc, t("Por cost centre"))
        self.tbl_tis, self.m_tis = make_table()
        inner.addTab(wrap_with_search(self.tbl_tis), t("Tiempo en servicio"))
        lay.addWidget(inner)
        return c

    def _build_costcentre_tab(self) -> QWidget:
        c = QWidget(); lay = QVBoxLayout(c)
        split = QSplitter(Qt.Vertical)
        w1 = QWidget(); l1 = QVBoxLayout(w1); l1.setContentsMargins(0, 0, 0, 0)
        l1.addWidget(QLabel(t("Equipos que más cambian de cost centre")))
        self.tbl_cc_eq, self.m_cc_eq = make_table(); l1.addWidget(self.tbl_cc_eq)
        split.addWidget(w1)
        w2 = QWidget(); l2 = QVBoxLayout(w2); l2.setContentsMargins(0, 0, 0, 0)
        l2.addWidget(QLabel(t("Cost centres con más actividad de reasignación (por CC actual del equipo)")))
        self.tbl_cc_by, self.m_cc_by = make_table(); l2.addWidget(self.tbl_cc_by)
        split.addWidget(w2)
        lay.addWidget(split)
        return c

    def _build_charts_tab(self) -> QWidget:
        c = QWidget(); grid = QGridLayout(c)
        self.ch_status = BarChart(t("Equipos por estado"), t("Equipos"))
        self.ch_avail = BarChart(t("Disponibilidad por categoría (%)"), "%", value_suffix="%")
        self.ch_rfid = TimeSeriesChart(t("Cambios de RFID por mes"), t("eventos"))
        self.ch_inout = TimeSeriesChart(t("Transiciones In→Out por mes"), t("transiciones"))
        self.ch_top_oi = BarChart(t("Top equipos Out→In"), t("veces"))
        self.ch_grp = BarChart(t("Transiciones In→Out por grupo"), t("veces"))
        grid.addWidget(self.ch_status, 0, 0); grid.addWidget(self.ch_avail, 0, 1)
        grid.addWidget(self.ch_rfid, 1, 0); grid.addWidget(self.ch_inout, 1, 1)
        grid.addWidget(self.ch_top_oi, 2, 0); grid.addWidget(self.ch_grp, 2, 1)
        return c

    # --- Filtrado -----------------------------------------------------------

    def _filtered(self, eq: pd.DataFrame) -> pd.DataFrame:
        if eq is None or eq.empty:
            return pd.DataFrame()
        out = eq
        st = self.cmb_status.currentData()
        if st is not None:
            out = out[out["status"].astype("string").str.strip() == st]
        tp = self.cmb_type.currentData()
        if tp is not None:
            contr = ea._truthy(out.get("is_contractor_vehicle"))
            out = out[contr] if tp == "contractor" else out[~contr]
        cat = self.cmb_category.currentData()
        if cat is not None:
            out = out[out["category"].astype("string").str.strip() == cat]
        gr = self.cmb_group.currentData()
        if gr is not None:
            out = out[out["group"].astype("string").str.strip() == gr]
        txt = self.txt_search.text().strip().lower()
        if txt:
            cols = [c for c in ("equipment_id", "description", "make", "model") if c in out.columns]
            mask = pd.Series(False, index=out.index)
            for c in cols:
                mask |= out[c].astype("string").str.lower().str.contains(txt, na=False)
            out = out[mask]
        return out

    # --- Refresco -----------------------------------------------------------

    def _refresh(self):
        """Refresco COMPLETO en segundo plano: el worker relee la replica y
        recalcula la analitica temporal (cara); `_on_loaded` solo proyecta. Antes
        todo esto corria en el hilo de la GUI y la ventana se congelaba en cada
        auto-refresco del historial."""
        if self._worker is not None and self._worker.isRunning():
            self._pending_refresh = True   # se relanza al terminar el actual
            return
        if self._eq_all.empty and self._busy is not None:
            self._busy.start(t("Cargando datos…"))
        self._worker = _LoadWorker(self._db, self)
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
        self._eq_all = data["eq_all"]
        self._changes = data["changes"]
        self._rfid_sum = data["rfid_sum"]
        self._trans = data["trans"]
        self._last_counts = (len(self._eq_all), len(self._changes))
        try:
            ch, eq_all, trans = self._changes, self._eq_all, self._trans

            rs = self._rfid_sum
            self.m_rfid_time.set_dataframe(data["rfid_time"])
            self.m_rfid_churn.set_dataframe(data["rfid_churn"])
            self.lbl_rfid.setText(
                f"{t('Eventos RFID')}: {rs['Eventos RFID']:,}   ·   {t('Asignados')}: {rs['Asignados']:,}"
                f"   ·   {t('Cambiados')}: {rs['Cambiados']:,}   ·   {t('Removidos')}: {rs['Removidos']:,}"
                f"   ·   {t('Tags')}: {rs['Tags (registros)']:,}")

            self.m_trans.set_dataframe(trans)
            self.m_trans_sum.set_dataframe(data["trans_sum"])
            self.m_top_oi.set_dataframe(data["top_oi"])
            self.m_top_io.set_dataframe(data["top_io"])
            self.m_by_grp.set_dataframe(data["by_grp"])
            self.m_by_cc.set_dataframe(data["by_cc"])
            self.m_tis.set_dataframe(data["tis"])

            self.m_cc_eq.set_dataframe(data["cc_eq"])
            self.m_cc_by.set_dataframe(data["cc_by"])

            self.m_attr.set_dataframe(data["attr"])
            self.m_audit.set_dataframe(data["audit"])

            # Calidad de datos: completitud ya viene calculada; las variantes y el
            # fuzzy O(n²) van en su propio worker y sólo si el maestro cambió.
            self.m_comp.set_dataframe(data["comp"])
            self._maybe_run_data_quality(eq_all)

            self._update_charts(eq_all, ch, trans)
            self._apply_filters()      # snapshot (inventario + agrupaciones + KPIs)
            self._update_status()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al analizar"),
                                 f"{exc}\n\n{traceback.format_exc()}")
        finally:
            if self._busy is not None:
                self._busy.stop()
        if self._pending_refresh:          # llegaron datos nuevos mientras cargaba
            self._pending_refresh = False
            self._refresh()

    def _apply_filters(self):
        """Solo lo que depende de los filtros (rapido): inventario, agrupaciones
        y KPIs. Reutiliza la analitica temporal cacheada -> filtrar es instantaneo."""
        try:
            eq = self._filtered(self._eq_all)
            self.m_inv.set_dataframe(
                eq[[c for c in _INVENTORY_COLS if c in eq.columns]] if not eq.empty else eq)
            self.m_cat.set_dataframe(ea.group_summary(eq, "category", "Categoría"))
            self.m_grp.set_dataframe(ea.group_summary(eq, "group", "Grupo"))
            self.m_dep.set_dataframe(ea.group_summary(eq, "department", "Departamento"))
            self.m_mk.set_dataframe(ea.group_summary(eq, "make", "Marca"))
            self._set_kpis(ea.fleet_kpis(eq), self._rfid_sum, self._trans)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al filtrar"),
                                 f"{exc}\n\n{traceback.format_exc()}")

    def _maybe_refresh(self):
        """Auto-refresco solo si la replica cambio (mantiene la UI fluida)."""
        try:
            counts = (self._db.row_count("equipment"), self._db.row_count("change_events"))
        except Exception:  # noqa: BLE001
            return
        if counts != self._last_counts:
            self._refresh()

    def _maybe_run_data_quality(self, eq_all: pd.DataFrame):
        """Dispara la auditoría de calidad en segundo plano, sólo si el maestro
        cambió (o es la primera vez) y no hay otra corriendo."""
        n = len(eq_all)
        if self._dq is not None and n == self._dq_eq_count:
            return                               # maestro sin cambios: reusa
        if self._dq_worker is not None and self._dq_worker.isRunning():
            return
        self._dq_eq_count = n
        self.lbl_dq.setText(f"<i>{t('Analizando calidad de datos…')}</i>")
        self._dq_worker = _DataQualityWorker(eq_all, self)
        self._dq_worker.done.connect(self._on_data_quality_done)
        self._dq_worker.finished.connect(self._dq_worker.deleteLater)
        self._dq_worker.start()

    def _on_data_quality_done(self, result):
        self._dq_worker = None
        if result is None:
            self._dq_eq_count = None             # permite reintentar tras un fallo
            return
        self._dq = result
        self.m_dq_sum.set_dataframe(result.summary)
        self.m_dq_var.set_dataframe(result.variant_detail)
        self.m_dq_fuzzy.set_dataframe(result.fuzzy)
        self._update_dq_alert(result.kpis)

    def _update_dq_alert(self, k: dict):
        """Línea de alerta de Data Quality: roja si hay maestros sucios, verde si no."""
        groups = k.get("Grupos sucios", 0)
        fuzzy = k.get("Pares similares", 0)
        aff = k.get("Equipos afectados", 0)
        fields = k.get("Campos con problemas", 0)
        if groups == 0 and fuzzy == 0:
            self.lbl_dq.setText(
                f"<span style='color:#2E7D32; font-weight:bold;'>✓ "
                f"{t('Sin problemas de calidad de datos en los maestros.')}</span>")
        else:
            self.lbl_dq.setText(
                f"<span style='color:#C62828; font-weight:bold;'>⚠ "
                f"{t('Alerta de calidad de datos')}:</span> &nbsp; "
                f"{groups:,} {t('grupos con variantes')} &nbsp;·&nbsp; "
                f"{fuzzy:,} {t('pares similares (fuzzy)')} &nbsp;·&nbsp; "
                f"{aff:,} {t('equipos afectados')} &nbsp;·&nbsp; "
                f"{fields:,} {t('campos')}")

    def _update_status(self):
        """Barra de estado con indicador de carga del historial."""
        n_eq, n_ch = len(self._eq_all), len(self._changes)
        loading = self._db.get_watermark("change_events") is None
        if loading:
            self.statusBar().showMessage(tr_fmt("eq.loading", events=n_ch, equipment=n_eq))
        else:
            self.statusBar().showMessage(
                tr_fmt("eq.status", equipment=n_eq, events=n_ch,
                       when=f"{datetime.now():%H:%M:%S}"))

    def _update_charts(self, eq_all, ch, trans):
        sb = ea.status_breakdown(eq_all)
        self.ch_status.set_data([tr_value(x) for x in sb["Estado"].tolist()],
                                sb["Equipos"].tolist(), "#1F4E78")
        cat = ea.group_summary(eq_all, "category", "Categoría").head(12)
        self.ch_avail.set_data(cat["Categoría"].tolist() if not cat.empty else [],
                               cat["Disponibilidad %"].tolist() if not cat.empty else [], "#2E7D32")
        rt = ea.rfid_changes_over_time(ch)
        if not rt.empty:
            self.ch_rfid.set_series(rt["Periodo"].tolist(), {
                t("Asignado"): rt["Asignado"].tolist(), t("Cambiado"): rt["Cambiado"].tolist(),
                t("Removido"): rt["Removido"].tolist()})
        io = ea.in_to_out_over_time(trans)
        if not io.empty:
            self.ch_inout.set_series(io["Periodo"].tolist(), {"In→Out": io["In->Out"].tolist()})
        toi = ea.top_equipment_by_transition(trans, OUT, IN, n=12)
        if not toi.empty:
            labels = toi["equipment_id"].fillna("?").astype(str).tolist()
            self.ch_top_oi.set_data(labels, toi["Veces"].tolist(), "#C62828")
        bg = ea.transitions_by_dimension(trans, "group", "Grupo").head(12)
        if not bg.empty:
            self.ch_grp.set_data(bg["Grupo"].astype(str).tolist(), bg["In->Out"].tolist(), "#833C00")

    def _set_kpis(self, kpis: dict, rfid: dict, trans: pd.DataFrame):
        while self._kpi_layout.count():
            it = self._kpi_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if not kpis:
            self._kpi_layout.addWidget(QLabel(t("Sin equipos para los filtros.")))
            self._kpi_layout.addStretch(1)
            return
        avail = kpis["Disponibilidad %"]
        color = "#2E7D32" if avail >= 70 else "#E0A000" if avail >= 40 else "#C62828"
        n_io = 0 if trans is None or trans.empty else int(((trans["De"] == IN) & (trans["A"] == OUT)).sum())
        n_oi = 0 if trans is None or trans.empty else int(((trans["De"] == OUT) & (trans["A"] == IN)).sum())
        cards = [
            kpi_label(t("Total equipos"), f"{kpis['Total equipos']:,}"),
            kpi_label(t("En servicio"), f"{kpis['En servicio']:,}", "#2E7D32"),
            warn_label(t("Fuera de servicio"), f"{kpis['Fuera de servicio']:,}", warn=kpis['Fuera de servicio'] > 0),
            kpi_label(t("Disponibilidad"), f"{avail:.1f}%", color),
            kpi_label(t("Eventos RFID"), f"{rfid['Eventos RFID']:,}", "#9467bd"),
            warn_label("In→Out", f"{n_io:,}", warn=n_io > 0),
            kpi_label("Out→In", f"{n_oi:,}", "#1F4E78"),
        ]
        for c in cards:
            self._kpi_layout.addWidget(c)
        self._kpi_layout.addStretch(1)

    # --- Audit log por equipo (doble clic) ----------------------------------

    def _on_inv_double_clicked(self, index):
        proxy = self.tbl_inv.model()
        src = proxy.mapToSource(index) if proxy is not None else index
        df = self.m_inv.dataframe()
        if df is None or df.empty or src.row() >= len(df):
            return
        eq_id = df.iloc[src.row()].get("equipment_id")
        internal, desc = None, ""
        if not self._eq_all.empty:
            match = self._eq_all[self._eq_all["equipment_id"].astype("string") == str(eq_id)]
            if not match.empty:
                internal = match["internal_id"].iloc[0]
                desc = match["description"].iloc[0]
        log = ea.equipment_audit_log(self._changes, internal)
        AuditLogDialog(eq_id, desc, log, self).exec()

    # --- Exportar -----------------------------------------------------------

    def _on_export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, t("Exportar análisis de equipos"),
            t("Analisis_Equipos_MSGQ.xlsx"), "Excel (*.xlsx)")
        if not path:
            return
        try:
            eq = self._filtered(self._eq_all)
            ch, eq_all, trans = self._changes, self._eq_all, self._trans
            # Reusa la auditoría ya calculada en segundo plano; si aún no terminó,
            # la calcula una vez para el archivo.
            dq_res = self._dq if self._dq is not None else dq.audit(eq_all)
            # Claves en español canónico: export_sheets las traduce al idioma activo.
            sheets = {
                "Inventario": eq[[c for c in _INVENTORY_COLS if c in eq.columns]] if not eq.empty else eq,
                "Por categoria": ea.group_summary(eq, "category", "Categoría"),
                "Por grupo": ea.group_summary(eq, "group", "Grupo"),
                "Por departamento": ea.group_summary(eq, "department", "Departamento"),
                "Por marca": ea.group_summary(eq, "make", "Marca"),
                "RFID por mes": ea.rfid_changes_over_time(ch),
                "RFID churn": ea.rfid_churn_by_tag(ch),
                "Transiciones": trans,
                "Transiciones resumen": ea.status_transition_summary(trans),
                "Top Out-In": ea.top_equipment_by_transition(trans, OUT, IN),
                "Top In-Out": ea.top_equipment_by_transition(trans, IN, OUT),
                "Transic por grupo": ea.transitions_by_dimension(trans, "group", "Grupo"),
                "Transic por costcentre": ea.transitions_by_dimension(trans, "cost_centre", "Cost Centre"),
                "Tiempo en servicio": ea.time_in_service(trans),
                "Equipos cambio CC": ea.top_equipment_by_attribute(ch, config.ATTR_COST_CENTRE, eq_all, label="Cambios CC"),
                "Cost centres activos": ea.attribute_change_by_dimension(ch, config.ATTR_COST_CENTRE, eq_all, "cost_centre", "Cost Centre"),
                "Atributos cambiados": ea.attribute_change_summary(ch),
                "Auditoria usuarios": ea.audit_by_user(ch),
                "Calidad de datos": ea.data_completeness(eq_all),
                "Calidad resumen": dq_res.summary,
                "Calidad variantes": dq_res.variant_detail,
                "Calidad duplicados": dq_res.fuzzy,
            }
            export_sheets(path, sheets)
            QMessageBox.information(self, t("Exportado"), f"{t('Análisis generado:')}\n{path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, t("Error al exportar"),
                                 f"{exc}\n\n{traceback.format_exc()}")


def _distinct(eq: pd.DataFrame, col: str) -> list[str]:
    if eq is None or eq.empty or col not in eq.columns:
        return []
    vals = (eq[col].dropna().astype(str).str.strip().replace({"": None}).dropna().unique())
    return sorted(vals)
