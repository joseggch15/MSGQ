"""Modelo de tabla que adapta un DataFrame de pandas a un QTableView.

Mismo patron que `Inventory_Equipment`/TLS, con ordenamiento numerico correcto
via `SORT_ROLE` y coloreado opcional de filas por severidad para la vista de
alertas (BackgroundRole sobre una columna 'severity' si esta presente).
"""
from __future__ import annotations

import pandas as pd
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from msgq.i18n import t, tr_value
from msgq.ui import theme

SORT_ROLE = Qt.UserRole + 1

# QColor por cadena de color: el rol de fondo se consulta por CADA celda visible
# en cada repintado; construir el QColor una sola vez por color evita ese costo.
_COLOR_CACHE: dict[str, QColor] = {}


def _cached_color(color: str) -> QColor:
    qc = _COLOR_CACHE.get(color)
    if qc is None:
        qc = _COLOR_CACHE[color] = QColor(color)
    return qc


class DataFrameModel(QAbstractTableModel):
    """Expone un DataFrame de pandas como modelo de solo lectura para Qt."""

    def __init__(self, df: pd.DataFrame | None = None):
        super().__init__()
        self._df = df if df is not None else pd.DataFrame()
        self._sev_col = self._severity_pos()

    def set_dataframe(self, df: pd.DataFrame):
        self.beginResetModel()
        self._df = df if df is not None else pd.DataFrame()
        self._sev_col = self._severity_pos()
        self.endResetModel()

    def _severity_pos(self) -> int | None:
        cols = self._df.columns
        return cols.get_loc("severity") if "severity" in cols else None

    def dataframe(self) -> pd.DataFrame:
        return self._df

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
            if role == Qt.BackgroundRole and self._sev_col is not None:
                # `iat` directo (no `.iloc[row]`): materializar una Series por
                # celda pintada dominaba el repintado de la tabla de alertas.
                color = theme.severity_bg(self._df.iat[index.row(), self._sev_col])
                if color is not None:
                    return _cached_color(color)
        except Exception:  # noqa: BLE001
            return None
        return None

    @staticmethod
    def _format(value):
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return ""
        if isinstance(value, pd.Timestamp):
            return value.strftime("%d/%m/%Y %H:%M:%S")
        if pd.api.types.is_bool(value):   # incluye numpy.bool_ / pandas boolean
            return tr_value("Si" if value else "No")
        if isinstance(value, float):
            return f"{value:,.2f}"
        # Solo traduce tokens conocidos; los datos reales pasan intactos.
        return tr_value(str(value))

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return t(str(self._df.columns[section]))
        return str(self._df.index[section])
