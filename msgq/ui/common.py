"""Utilidades compartidas de la interfaz grafica (mismo patron del ecosistema)."""
from __future__ import annotations

from PySide6.QtCore import Qt, QSortFilterProxyModel
from PySide6.QtWidgets import QLabel, QTableView, QVBoxLayout, QLineEdit, QWidget

from msgq.ui.table_model import SORT_ROLE, DataFrameModel


def make_table() -> tuple[QTableView, DataFrameModel]:
    """Crea un QTableView ordenable y filtrable con su modelo subyacente."""
    view = QTableView()
    model = DataFrameModel()
    proxy = QSortFilterProxyModel()
    proxy.setSortRole(SORT_ROLE)
    proxy.setSourceModel(model)
    view.setModel(proxy)
    view.setAlternatingRowColors(True)
    view.setSortingEnabled(True)
    view.horizontalHeader().setStretchLastSection(True)
    view.setSelectionBehavior(QTableView.SelectRows)
    return view, model


def wrap_with_search(table: QTableView) -> QWidget:
    """Envuelve una tabla con una caja de filtro de texto sobre todas las columnas."""
    container = QWidget()
    lay = QVBoxLayout(container)
    lay.setContentsMargins(2, 2, 2, 2)
    box = QLineEdit()
    box.setPlaceholderText("Filtrar por cualquier texto...")

    def _filter(text: str):
        proxy = table.model()
        proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        proxy.setFilterKeyColumn(-1)
        proxy.setFilterFixedString(text)

    box.textChanged.connect(_filter)
    lay.addWidget(box)
    lay.addWidget(table)
    return container


def kpi_label(title: str, value: str, color: str = "#1F4E78") -> QLabel:
    """Tarjeta KPI compacta con titulo y valor coloreado."""
    lbl = QLabel(f"<b>{title}</b><br><span style='font-size:15px'>{value}</span>")
    lbl.setTextFormat(Qt.RichText)
    lbl.setStyleSheet(
        f"QLabel {{ border: 1px solid {color}; border-radius: 6px; "
        f"padding: 6px 14px; color: {color}; background: white; }}"
    )
    lbl.setMinimumWidth(130)
    return lbl


def warn_label(title: str, value: str, warn: bool = False) -> QLabel:
    """Tarjeta KPI con color condicional: rojo si hay anomalias, verde si no."""
    color = "#C62828" if warn else "#2E7D32"
    return kpi_label(title, value, color)
