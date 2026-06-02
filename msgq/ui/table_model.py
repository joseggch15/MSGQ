"""Modelo de tabla que adapta un DataFrame de pandas a un QTableView.

Mismo patron que `Inventory_Equipment`/TLS, con ordenamiento numerico correcto
via `SORT_ROLE` y coloreado opcional de filas por severidad para la vista de
alertas (BackgroundRole sobre una columna 'severity' si esta presente).
"""
from __future__ import annotations

import pandas as pd
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

SORT_ROLE = Qt.UserRole + 1

# Colores de fondo por severidad (suaves, para no saturar la tabla).
_SEVERITY_BG = {
    "CRITICAL": QColor("#FDE7E9"),
    "WARNING":  QColor("#FFF4E5"),
    "INFO":     QColor("#E8F0FE"),
}


class DataFrameModel(QAbstractTableModel):
    """Expone un DataFrame de pandas como modelo de solo lectura para Qt."""

    def __init__(self, df: pd.DataFrame | None = None):
        super().__init__()
        self._df = df if df is not None else pd.DataFrame()

    def set_dataframe(self, df: pd.DataFrame):
        self.beginResetModel()
        self._df = df if df is not None else pd.DataFrame()
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._df)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._df.columns)

    def data(self, index, role=Qt.DisplayRole):
        try:
            if not index.isValid():
                return None
            value = self._df.iat[index.row(), index.column()]
            if role == Qt.DisplayRole:
                return self._format(value)
            if role == SORT_ROLE:
                if value is None or (isinstance(value, float) and pd.isna(value)):
                    return None
                if isinstance(value, pd.Timestamp):
                    return value.to_pydatetime()
                return value
            if role == Qt.TextAlignmentRole:
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return int(Qt.AlignRight | Qt.AlignVCenter)
            if role == Qt.BackgroundRole and "severity" in self._df.columns:
                sev = self._df.iloc[index.row()].get("severity")
                if sev in _SEVERITY_BG:
                    return _SEVERITY_BG[sev]
        except Exception:  # noqa: BLE001
            return None
        return None

    @staticmethod
    def _format(value):
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return ""
        if isinstance(value, pd.Timestamp):
            return value.strftime("%d/%m/%Y %H:%M:%S")
        if isinstance(value, float):
            return f"{value:,.2f}"
        if isinstance(value, bool):
            return "Si" if value else "No"
        return str(value)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return str(self._df.columns[section])
        return str(self._df.index[section])
