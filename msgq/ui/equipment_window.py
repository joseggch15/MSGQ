"""Ventana de análisis de la flota de equipos.

Lee de la réplica SQLite (sin tocar el poller) y ofrece:
  • Inventario filtrable por estado / tipo / categoría / grupo / texto.
  • KPIs de flota (disponibilidad, contratistas, ligeros).
  • Agrupaciones por categoría / grupo / departamento / marca.
  • Analítica temporal del log de auditoría: frecuencia de cambio de RFID,
    transiciones de estado (foco In→Out) con tiempo en servicio, y auditoría
    de quién hizo cada cambio.
  • Gráficas (pyqtgraph).
"""
from __future__ import annotations

import traceback

import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QSplitter, QTabWidget, QVBoxLayout,
    QWidget,
)

from msgq import config
from msgq.core import equipment_analytics as ea
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


class EquipmentWindow(QMainWindow):
    """Análisis de inventario + auditoría de la flota."""

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self._db = db
        self.setWindowTitle("MSGQ — Análisis de Equipos  ·  Newmont Merian")
        self.resize(1380, 860)

        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setSpacing(6)
        lay.addWidget(self._build_controls())
        lay.addWidget(self._build_kpis())
        lay.addWidget(self._build_tabs(), stretch=1)
        self._refresh()

    # --- Controles / filtros ------------------------------------------------

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

        btn = QPushButton("Actualizar")
        btn.clicked.connect(self._refresh)

        for label, w in (("Estado:", self.cmb_status), ("Tipo:", self.cmb_type),
                         ("Categoría:", self.cmb_category), ("Grupo:", self.cmb_group)):
            row.addWidget(QLabel(label)); row.addWidget(w)
        row.addWidget(QLabel("Buscar:")); row.addWidget(self.txt_search, stretch=1)
        row.addWidget(btn)
        return box

    def _build_kpis(self) -> QFrame:
        frame = QFrame(); frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet("QFrame{background:#EDF1F6; border-radius:6px;}")
        self._kpi_layout = QHBoxLayout(frame)
        self._kpi_layout.setContentsMargins(8, 6, 8, 6)
        return frame

    def _build_tabs(self) -> QTabWidget:
        self.tabs = QTabWidget()
        self.tbl_inv, self.m_inv = make_table()
        self.tabs.addTab(wrap_with_search(self.tbl_inv), "Inventario")
        self.tbl_cat, self.m_cat = make_table()
        self.tabs.addTab(self.tbl_cat, "Por categoría")
        self.tbl_grp, self.m_grp = make_table()
        self.tabs.addTab(self.tbl_grp, "Por grupo")
        self.tbl_dep, self.m_dep = make_table()
        self.tabs.addTab(self.tbl_dep, "Por departamento")
        self.tbl_mk, self.m_mk = make_table()
        self.tabs.addTab(self.tbl_mk, "Por marca")
        self.tabs.addTab(self._build_rfid_tab(), "Cambios de RFID")
        self.tabs.addTab(self._build_status_tab(), "Transiciones de estado")
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
        w1 = QWidget(); l1 = QVBoxLayout(w1); l1.setContentsMargins(0, 0, 0, 0)
        l1.addWidget(QLabel("Eventos de RFID por mes (asignado / cambiado / removido)"))
        self.tbl_rfid_time, self.m_rfid_time = make_table(); l1.addWidget(self.tbl_rfid_time)
        split.addWidget(w1)
        w2 = QWidget(); l2 = QVBoxLayout(w2); l2.setContentsMargins(0, 0, 0, 0)
        l2.addWidget(QLabel("Tags con más cambios (re-tagueo)"))
        self.tbl_rfid_churn, self.m_rfid_churn = make_table(); l2.addWidget(self.tbl_rfid_churn)
        split.addWidget(w2)
        lay.addWidget(split)
        return c

    def _build_status_tab(self) -> QWidget:
        c = QWidget(); lay = QVBoxLayout(c)
        inner = QTabWidget()
        self.tbl_trans, self.m_trans = make_table()
        inner.addTab(wrap_with_search(self.tbl_trans), "Transiciones")
        self.tbl_trans_sum, self.m_trans_sum = make_table()
        inner.addTab(self.tbl_trans_sum, "Resumen")
        self.tbl_tis, self.m_tis = make_table()
        inner.addTab(wrap_with_search(self.tbl_tis), "Tiempo en servicio")
        lay.addWidget(inner)
        return c

    def _build_charts_tab(self) -> QWidget:
        c = QWidget(); grid = QGridLayout(c)
        self.ch_status = BarChart("Equipos por estado", "Equipos")
        self.ch_avail = BarChart("Disponibilidad por categoría (%)", "%")
        self.ch_rfid = TimeSeriesChart("Cambios de RFID por mes", "eventos")
        self.ch_inout = TimeSeriesChart("Transiciones In→Out por mes", "transiciones")
        grid.addWidget(self.ch_status, 0, 0)
        grid.addWidget(self.ch_avail, 0, 1)
        grid.addWidget(self.ch_rfid, 1, 0)
        grid.addWidget(self.ch_inout, 1, 1)
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
            eq_all = self._db.get_equipment()
            changes = self._db.get_change_events()
            eq = self._filtered(eq_all)

            self.m_inv.set_dataframe(eq[[c for c in _INVENTORY_COLS if c in eq.columns]] if not eq.empty else eq)
            self.m_cat.set_dataframe(ea.group_summary(eq, "category", "Categoría"))
            self.m_grp.set_dataframe(ea.group_summary(eq, "group", "Grupo"))
            self.m_dep.set_dataframe(ea.group_summary(eq, "department", "Departamento"))
            self.m_mk.set_dataframe(ea.group_summary(eq, "make", "Marca"))

            # RFID
            self.m_rfid_time.set_dataframe(ea.rfid_changes_over_time(changes))
            self.m_rfid_churn.set_dataframe(ea.rfid_churn_by_tag(changes))
            rs = ea.rfid_change_summary(changes)
            self.lbl_rfid.setText(
                f"Eventos RFID: {rs['Eventos RFID']:,}   ·   Asignados: {rs['Asignados']:,}"
                f"   ·   Cambiados: {rs['Cambiados']:,}   ·   Removidos: {rs['Removidos']:,}"
                f"   ·   Tags: {rs['Tags (registros)']:,}")

            # Transiciones de estado (enlazadas al inventario completo)
            trans = ea.status_transitions(changes, eq_all)
            self.m_trans.set_dataframe(trans)
            self.m_trans_sum.set_dataframe(ea.status_transition_summary(trans))
            self.m_tis.set_dataframe(ea.time_in_service(trans))

            # Auditoría + calidad de datos
            self.m_audit.set_dataframe(ea.audit_by_user(changes))
            self.m_comp.set_dataframe(ea.data_completeness(eq_all))

            self._update_charts(eq_all, changes, trans)
            self._set_kpis(ea.fleet_kpis(eq), rs, trans)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Error al analizar",
                                 f"{exc}\n\n{traceback.format_exc()}")

    def _update_charts(self, eq_all, changes, trans):
        sb = ea.status_breakdown(eq_all)
        self.ch_status.set_data(sb["Estado"].tolist(), sb["Equipos"].tolist(), "#1F4E78")
        cat = ea.group_summary(eq_all, "category", "Categoría").head(12)
        self.ch_avail.set_data(cat["Categoría"].tolist() if not cat.empty else [],
                               cat["Disponibilidad %"].tolist() if not cat.empty else [], "#2E7D32")
        rt = ea.rfid_changes_over_time(changes)
        if not rt.empty:
            self.ch_rfid.set_series(rt["Periodo"].tolist(), {
                "Asignado": rt["Asignado"].tolist(),
                "Cambiado": rt["Cambiado"].tolist(),
                "Removido": rt["Removido"].tolist()})
        io = ea.in_to_out_over_time(trans)
        if not io.empty:
            self.ch_inout.set_series(io["Periodo"].tolist(), {"In→Out": io["In->Out"].tolist()})

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
        n_in_out = 0 if trans is None or trans.empty else int(
            ((trans["De"] == config.STATUS_IN) & (trans["A"] == config.STATUS_OUT)).sum())
        cards = [
            kpi_label("Total equipos", f"{kpis['Total equipos']:,}"),
            kpi_label("En servicio", f"{kpis['En servicio']:,}", "#2E7D32"),
            warn_label("Fuera de servicio", f"{kpis['Fuera de servicio']:,}", warn=kpis['Fuera de servicio'] > 0),
            kpi_label("Disponibilidad", f"{avail:.1f}%", color),
            kpi_label("De contratista", f"{kpis['De contratista']:,} ({kpis['% contratista']:.0f}%)"),
            kpi_label("Eventos RFID", f"{rfid['Eventos RFID']:,}", "#9467bd"),
            warn_label("Transiciones In→Out", f"{n_in_out:,}", warn=n_in_out > 0),
        ]
        for c in cards:
            self._kpi_layout.addWidget(c)
        self._kpi_layout.addStretch(1)


def _distinct(eq: pd.DataFrame, col: str) -> list[str]:
    if eq is None or eq.empty or col not in eq.columns:
        return []
    vals = (eq[col].dropna().astype(str).str.strip().replace({"": None}).dropna().unique())
    return sorted(vals)
