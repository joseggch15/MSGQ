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
"""
from __future__ import annotations

import traceback

import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from msgq import config
from msgq.core import equipment_analytics as ea
from msgq.export import export_sheets
from msgq.storage import Database
from msgq.ui.charts import BarChart, TimeSeriesChart
from msgq.ui.common import kpi_label, make_table, warn_label, wrap_with_search

_STATUS_OPTIONS = ["Todos", config.STATUS_IN, config.STATUS_OUT, config.STATUS_DECOM]
_TYPE_OPTIONS = ["Todos", "Propios", "Contratistas"]
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
        msg = (f"{len(log_df):,} cambios registrados en la réplica."
               if log_df is not None and not log_df.empty
               else "Sin cambios de este equipo en la réplica todavía "
                    "(el log se llena al sincronizar).")
        lay.addWidget(QLabel(msg))


class EquipmentWindow(QMainWindow):
    """Análisis de inventario + auditoría de la flota."""

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self._db = db
        self._eq_all = pd.DataFrame()
        self._changes = pd.DataFrame()
        self._trans = pd.DataFrame()
        self.setWindowTitle("MSGQ — Análisis de Equipos  ·  Newmont Merian")
        self.resize(1420, 880)

        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setSpacing(6)
        lay.addWidget(self._build_controls())
        lay.addWidget(self._build_kpis())
        lay.addWidget(self._build_tabs(), stretch=1)
        self._refresh()

    # --- Controles ----------------------------------------------------------

    def _build_controls(self) -> QGroupBox:
        box = QGroupBox("Filtros")
        row = QHBoxLayout(box)
        self.cmb_status = QComboBox(); self.cmb_status.addItems(_STATUS_OPTIONS)
        self.cmb_status.currentIndexChanged.connect(self._refresh)
        self.cmb_type = QComboBox(); self.cmb_type.addItems(_TYPE_OPTIONS)
        self.cmb_type.currentIndexChanged.connect(self._refresh)
        eq = self._db.get_equipment()
        self.cmb_category = QComboBox(); self.cmb_category.addItem("Todas")
        self.cmb_category.addItems(_distinct(eq, "category"))
        self.cmb_category.currentIndexChanged.connect(self._refresh)
        self.cmb_group = QComboBox(); self.cmb_group.addItem("Todos")
        self.cmb_group.addItems(_distinct(eq, "group"))
        self.cmb_group.currentIndexChanged.connect(self._refresh)
        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("Buscar por ID, descripción, marca, modelo...")
        self.txt_search.textChanged.connect(self._refresh)
        btn_refresh = QPushButton("Actualizar"); btn_refresh.clicked.connect(self._refresh)
        btn_export = QPushButton("Exportar a Excel…"); btn_export.clicked.connect(self._on_export)

        for label, w in (("Estado:", self.cmb_status), ("Tipo:", self.cmb_type),
                         ("Categoría:", self.cmb_category), ("Grupo:", self.cmb_group)):
            row.addWidget(QLabel(label)); row.addWidget(w)
        row.addWidget(QLabel("Buscar:")); row.addWidget(self.txt_search, stretch=1)
        row.addWidget(btn_refresh); row.addWidget(btn_export)
        return box

    def _build_kpis(self) -> QFrame:
        frame = QFrame(); frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet("QFrame{background:#EDF1F6; border-radius:6px;}")
        self._kpi_layout = QHBoxLayout(frame)
        self._kpi_layout.setContentsMargins(8, 6, 8, 6)
        return frame

    def _build_tabs(self) -> QTabWidget:
        self.tabs = QTabWidget()

        # Inventario (con doble clic -> audit log)
        inv = QWidget(); ilay = QVBoxLayout(inv); ilay.setContentsMargins(2, 2, 2, 2)
        ilay.addWidget(QLabel("Doble clic en un equipo para ver su <b>Audit Log</b> completo."))
        self.tbl_inv, self.m_inv = make_table()
        self.tbl_inv.doubleClicked.connect(self._on_inv_double_clicked)
        ilay.addWidget(self.tbl_inv)
        self.tabs.addTab(inv, "Inventario")

        # Agrupaciones
        agg = QTabWidget()
        self.tbl_cat, self.m_cat = make_table(); agg.addTab(self.tbl_cat, "Categoría")
        self.tbl_grp, self.m_grp = make_table(); agg.addTab(self.tbl_grp, "Grupo")
        self.tbl_dep, self.m_dep = make_table(); agg.addTab(self.tbl_dep, "Departamento")
        self.tbl_mk, self.m_mk = make_table(); agg.addTab(self.tbl_mk, "Marca")
        self.tabs.addTab(agg, "Agrupaciones")

        self.tabs.addTab(self._build_rfid_tab(), "Cambios de RFID")
        self.tabs.addTab(self._build_status_tab(), "Transiciones de estado")
        self.tabs.addTab(self._build_costcentre_tab(), "Cost center")

        self.tbl_attr, self.m_attr = make_table()
        self.tabs.addTab(self.tbl_attr, "Atributos")
        self.tbl_audit, self.m_audit = make_table()
        self.tabs.addTab(self.tbl_audit, "Auditoría (quién)")
        self.tbl_comp, self.m_comp = make_table()
        self.tabs.addTab(self.tbl_comp, "Calidad de datos")
        self.tabs.addTab(self._build_charts_tab(), "Gráficas")
        return self.tabs

    def _build_rfid_tab(self) -> QWidget:
        c = QWidget(); lay = QVBoxLayout(c)
        self.lbl_rfid = QLabel(); self.lbl_rfid.setStyleSheet("font-weight:bold; color:#1F4E78;")
        lay.addWidget(self.lbl_rfid)
        split = QSplitter(Qt.Vertical)
        for title, attr in (("Eventos de RFID por mes (asignado / cambiado / removido)", "tbl_rfid_time"),
                            ("Tags con más cambios (re-tagueo)", "tbl_rfid_churn")):
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
        inner.addTab(wrap_with_search(self.tbl_trans), "Transiciones")
        self.tbl_trans_sum, self.m_trans_sum = make_table()
        inner.addTab(self.tbl_trans_sum, "Resumen")
        self.tbl_top_oi, self.m_top_oi = make_table()
        inner.addTab(wrap_with_search(self.tbl_top_oi), "Top Out→In")
        self.tbl_top_io, self.m_top_io = make_table()
        inner.addTab(wrap_with_search(self.tbl_top_io), "Top In→Out")
        self.tbl_by_grp, self.m_by_grp = make_table()
        inner.addTab(self.tbl_by_grp, "Por grupo")
        self.tbl_by_cc, self.m_by_cc = make_table()
        inner.addTab(self.tbl_by_cc, "Por cost centre")
        self.tbl_tis, self.m_tis = make_table()
        inner.addTab(wrap_with_search(self.tbl_tis), "Tiempo en servicio")
        lay.addWidget(inner)
        return c

    def _build_costcentre_tab(self) -> QWidget:
        c = QWidget(); lay = QVBoxLayout(c)
        split = QSplitter(Qt.Vertical)
        w1 = QWidget(); l1 = QVBoxLayout(w1); l1.setContentsMargins(0, 0, 0, 0)
        l1.addWidget(QLabel("Equipos que más cambian de cost centre"))
        self.tbl_cc_eq, self.m_cc_eq = make_table(); l1.addWidget(self.tbl_cc_eq)
        split.addWidget(w1)
        w2 = QWidget(); l2 = QVBoxLayout(w2); l2.setContentsMargins(0, 0, 0, 0)
        l2.addWidget(QLabel("Cost centres con más actividad de reasignación (por CC actual del equipo)"))
        self.tbl_cc_by, self.m_cc_by = make_table(); l2.addWidget(self.tbl_cc_by)
        split.addWidget(w2)
        lay.addWidget(split)
        return c

    def _build_charts_tab(self) -> QWidget:
        c = QWidget(); grid = QGridLayout(c)
        self.ch_status = BarChart("Equipos por estado", "Equipos")
        self.ch_avail = BarChart("Disponibilidad por categoría (%)", "%")
        self.ch_rfid = TimeSeriesChart("Cambios de RFID por mes", "eventos")
        self.ch_inout = TimeSeriesChart("Transiciones In→Out por mes", "transiciones")
        self.ch_top_oi = BarChart("Top equipos Out→In", "veces")
        self.ch_grp = BarChart("Transiciones In→Out por grupo", "veces")
        grid.addWidget(self.ch_status, 0, 0); grid.addWidget(self.ch_avail, 0, 1)
        grid.addWidget(self.ch_rfid, 1, 0); grid.addWidget(self.ch_inout, 1, 1)
        grid.addWidget(self.ch_top_oi, 2, 0); grid.addWidget(self.ch_grp, 2, 1)
        return c

    # --- Filtrado -----------------------------------------------------------

    def _filtered(self, eq: pd.DataFrame) -> pd.DataFrame:
        if eq is None or eq.empty:
            return pd.DataFrame()
        out = eq
        st = self.cmb_status.currentText()
        if st != "Todos":
            out = out[out["status"].astype("string").str.strip() == st]
        tp = self.cmb_type.currentText()
        if tp != "Todos":
            contr = ea._truthy(out.get("is_contractor_vehicle"))
            out = out[contr] if tp == "Contratistas" else out[~contr]
        cat = self.cmb_category.currentText()
        if cat != "Todas":
            out = out[out["category"].astype("string").str.strip() == cat]
        gr = self.cmb_group.currentText()
        if gr != "Todos":
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
        try:
            self._eq_all = self._db.get_equipment()
            self._changes = self._db.get_change_events()
            eq = self._filtered(self._eq_all)
            ch, eq_all = self._changes, self._eq_all

            self.m_inv.set_dataframe(eq[[c for c in _INVENTORY_COLS if c in eq.columns]] if not eq.empty else eq)
            self.m_cat.set_dataframe(ea.group_summary(eq, "category", "Categoría"))
            self.m_grp.set_dataframe(ea.group_summary(eq, "group", "Grupo"))
            self.m_dep.set_dataframe(ea.group_summary(eq, "department", "Departamento"))
            self.m_mk.set_dataframe(ea.group_summary(eq, "make", "Marca"))

            self.m_rfid_time.set_dataframe(ea.rfid_changes_over_time(ch))
            self.m_rfid_churn.set_dataframe(ea.rfid_churn_by_tag(ch))
            rs = ea.rfid_change_summary(ch)
            self.lbl_rfid.setText(
                f"Eventos RFID: {rs['Eventos RFID']:,}   ·   Asignados: {rs['Asignados']:,}"
                f"   ·   Cambiados: {rs['Cambiados']:,}   ·   Removidos: {rs['Removidos']:,}"
                f"   ·   Tags: {rs['Tags (registros)']:,}")

            trans = ea.status_transitions(ch, eq_all)
            self._trans = trans
            self.m_trans.set_dataframe(trans)
            self.m_trans_sum.set_dataframe(ea.status_transition_summary(trans))
            self.m_top_oi.set_dataframe(ea.top_equipment_by_transition(trans, OUT, IN))
            self.m_top_io.set_dataframe(ea.top_equipment_by_transition(trans, IN, OUT))
            self.m_by_grp.set_dataframe(ea.transitions_by_dimension(trans, "group", "Grupo"))
            self.m_by_cc.set_dataframe(ea.transitions_by_dimension(trans, "cost_centre", "Cost Centre"))
            self.m_tis.set_dataframe(ea.time_in_service(trans))

            self.m_cc_eq.set_dataframe(ea.top_equipment_by_attribute(
                ch, config.ATTR_COST_CENTRE, eq_all, label="Cambios CC"))
            self.m_cc_by.set_dataframe(ea.attribute_change_by_dimension(
                ch, config.ATTR_COST_CENTRE, eq_all, "cost_centre", "Cost Centre"))

            self.m_attr.set_dataframe(ea.attribute_change_summary(ch))
            self.m_audit.set_dataframe(ea.audit_by_user(ch))
            self.m_comp.set_dataframe(ea.data_completeness(eq_all))

            self._update_charts(eq_all, ch, trans)
            self._set_kpis(ea.fleet_kpis(eq), rs, trans)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Error al analizar",
                                 f"{exc}\n\n{traceback.format_exc()}")

    def _update_charts(self, eq_all, ch, trans):
        sb = ea.status_breakdown(eq_all)
        self.ch_status.set_data(sb["Estado"].tolist(), sb["Equipos"].tolist(), "#1F4E78")
        cat = ea.group_summary(eq_all, "category", "Categoría").head(12)
        self.ch_avail.set_data(cat["Categoría"].tolist() if not cat.empty else [],
                               cat["Disponibilidad %"].tolist() if not cat.empty else [], "#2E7D32")
        rt = ea.rfid_changes_over_time(ch)
        if not rt.empty:
            self.ch_rfid.set_series(rt["Periodo"].tolist(), {
                "Asignado": rt["Asignado"].tolist(), "Cambiado": rt["Cambiado"].tolist(),
                "Removido": rt["Removido"].tolist()})
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
            self._kpi_layout.addWidget(QLabel("Sin equipos para los filtros."))
            self._kpi_layout.addStretch(1)
            return
        avail = kpis["Disponibilidad %"]
        color = "#2E7D32" if avail >= 70 else "#E0A000" if avail >= 40 else "#C62828"
        n_io = 0 if trans is None or trans.empty else int(((trans["De"] == IN) & (trans["A"] == OUT)).sum())
        n_oi = 0 if trans is None or trans.empty else int(((trans["De"] == OUT) & (trans["A"] == IN)).sum())
        cards = [
            kpi_label("Total equipos", f"{kpis['Total equipos']:,}"),
            kpi_label("En servicio", f"{kpis['En servicio']:,}", "#2E7D32"),
            warn_label("Fuera de servicio", f"{kpis['Fuera de servicio']:,}", warn=kpis['Fuera de servicio'] > 0),
            kpi_label("Disponibilidad", f"{avail:.1f}%", color),
            kpi_label("Eventos RFID", f"{rfid['Eventos RFID']:,}", "#9467bd"),
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
            self, "Exportar análisis de equipos", "Analisis_Equipos_MSGQ.xlsx", "Excel (*.xlsx)")
        if not path:
            return
        try:
            eq = self._filtered(self._eq_all)
            ch, eq_all, trans = self._changes, self._eq_all, self._trans
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
            }
            export_sheets(path, sheets)
            QMessageBox.information(self, "Exportado", f"Análisis generado:\n{path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Error al exportar",
                                 f"{exc}\n\n{traceback.format_exc()}")


def _distinct(eq: pd.DataFrame, col: str) -> list[str]:
    if eq is None or eq.empty or col not in eq.columns:
        return []
    vals = (eq[col].dropna().astype(str).str.strip().replace({"": None}).dropna().unique())
    return sorted(vals)
