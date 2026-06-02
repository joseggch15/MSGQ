"""Gráficos estadísticos reutilizables (pyqtgraph).

Dos widgets simples que el análisis de equipos alimenta con DataFrames:
  • `BarChart`        — barras por categoría (con etiquetas en el eje X).
  • `TimeSeriesChart` — una o varias series en el tiempo (eje X de fechas).

Mismo enfoque robusto que TLS (`fms_analyzer/ui/chart_widget.py`): fondo claro,
sin manipular la escena de pyqtgraph.
"""
from __future__ import annotations

import pandas as pd
import pyqtgraph as pg
from PySide6.QtWidgets import QVBoxLayout, QWidget

pg.setConfigOptions(antialias=True, background="w", foreground="k")

_COLORS = [
    "#1F4E78", "#2E7D32", "#C62828", "#ff7f0e",
    "#9467bd", "#17becf", "#8c564b", "#e377c2",
]


class BarChart(QWidget):
    """Gráfico de barras verticales con etiquetas de categoría en el eje X."""

    def __init__(self, title: str = "", y_label: str = ""):
        super().__init__()
        self._plot = pg.PlotWidget()
        self._plot.setTitle(title)
        self._plot.showGrid(x=False, y=True, alpha=0.3)
        if y_label:
            self._plot.setLabel("left", y_label)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._plot)

    def set_data(self, labels: list[str], values: list[float], color: str = "#1F4E78"):
        self._plot.clear()
        labels = [str(x) for x in (labels or [])]
        values = [float(v) if v is not None else 0.0 for v in (values or [])]
        if not labels:
            return
        xs = list(range(len(labels)))
        self._plot.addItem(pg.BarGraphItem(x=xs, height=values, width=0.6, brush=color))
        axis = self._plot.getAxis("bottom")
        axis.setTicks([list(zip(xs, labels))])
        self._plot.enableAutoRange()


class TimeSeriesChart(QWidget):
    """Una o varias series en el tiempo (eje X de fechas)."""

    def __init__(self, title: str = "", y_label: str = ""):
        super().__init__()
        self._plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        self._plot.setTitle(title)
        self._plot.showGrid(x=True, y=True, alpha=0.3)
        if y_label:
            self._plot.setLabel("left", y_label)
        self._plot.addLegend(offset=(10, 10))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._plot)

    def set_series(self, periods, series: dict[str, list]):
        """`periods`: lista de Timestamps; `series`: {nombre: valores}."""
        self._plot.clear()
        if periods is None or len(periods) == 0:
            return
        x = [pd.Timestamp(p).timestamp() for p in periods]
        for i, (name, ys) in enumerate(series.items()):
            color = _COLORS[i % len(_COLORS)]
            self._plot.plot(x, [float(v or 0) for v in ys],
                            pen=pg.mkPen(color, width=2), name=name,
                            symbol="o", symbolSize=5, symbolBrush=color)
        self._plot.enableAutoRange()
