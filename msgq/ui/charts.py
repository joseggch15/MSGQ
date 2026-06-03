"""Gráficos estadísticos reutilizables (pyqtgraph).

Dos widgets simples que el análisis de equipos alimenta con DataFrames:
  • `BarChart`        — barras por categoría (con etiquetas en el eje X).
  • `TimeSeriesChart` — una o varias series en el tiempo (eje X de fechas).

Ambos muestran el **valor exacto** en un tooltip flotante al pasar el cursor
(pyqtgraph no lo trae de fábrica): una línea de referencia vertical se ancla a la
barra / punto más cercano y una etiqueta indica su valor numérico junto con la
categoría o el periodo. Resuelve además la lectura cuando las etiquetas del eje
X se solapan, porque el tooltip muestra el nombre completo.

Mismo enfoque robusto que TLS (`fms_analyzer/ui/chart_widget.py`): fondo claro,
sin manipular la escena de pyqtgraph.
"""
from __future__ import annotations

import pandas as pd
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QVBoxLayout, QWidget

from msgq.i18n import current_language

pg.setConfigOptions(antialias=True, background="w", foreground="k")

_COLORS = [
    "#1F4E78", "#2E7D32", "#C62828", "#ff7f0e",
    "#9467bd", "#17becf", "#8c564b", "#e377c2",
]

# Los periodos llegan agrupados por mes; abreviaturas según el idioma activo.
_MESES = {
    "es": ("ene", "feb", "mar", "abr", "may", "jun",
           "jul", "ago", "sep", "oct", "nov", "dic"),
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
}


def _month_abbr(month: int) -> str:
    return _MESES.get(current_language(), _MESES["es"])[month - 1]


def _fmt(value, suffix: str = "") -> str:
    """Valor para el tooltip: entero con separador de miles, o con 1 decimal si
    no es entero. `suffix` agrega unidades (p. ej. '%')."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "—"
    txt = f"{f:,.0f}" if abs(f - round(f)) < 1e-9 else f"{f:,.1f}"
    return f"{txt}{suffix}"


class _HoverChart(QWidget):
    """Base con tooltip flotante de valor exacto + línea de referencia vertical.

    Las subclases implementan `_value_at(x_view)` -> `(x_anchor, y_anchor, texto)`
    para el dato más cercano al cursor, o `None` si no hay nada bajo el cursor.
    """

    def __init__(self, title: str = "", y_label: str = "",
                 value_suffix: str = "", axis_items: dict | None = None):
        super().__init__()
        self._value_suffix = value_suffix
        self._plot = pg.PlotWidget(axisItems=axis_items or {})
        self._plot.setTitle(title)
        if y_label:
            self._plot.setLabel("left", y_label)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._plot)

        # Overlays de hover: hay que re-agregarlos tras cada PlotWidget.clear().
        self._vline = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen("#9AA7B5", width=1, style=Qt.DashLine))
        self._tip = pg.TextItem(
            color="#102A43", anchor=(0.5, 1),
            fill=pg.mkBrush(255, 255, 255, 235), border=pg.mkPen("#9AA7B5"))
        self._vline.setZValue(900)
        self._tip.setZValue(1000)
        self._add_overlays()

        # sigMouseMoved se dispara al pasar el cursor (GraphicsView ya rastrea el
        # mouse); SignalProxy limita la frecuencia para no recalcular de más.
        self._proxy = pg.SignalProxy(
            self._plot.scene().sigMouseMoved, rateLimit=60, slot=self._on_mouse_moved)

    def _add_overlays(self):
        self._plot.addItem(self._vline, ignoreBounds=True)
        self._plot.addItem(self._tip, ignoreBounds=True)
        self._hide_hover()

    def _hide_hover(self):
        self._vline.setVisible(False)
        self._tip.setVisible(False)

    def _on_mouse_moved(self, evt):
        pos = evt[0]
        if not self._plot.sceneBoundingRect().contains(pos):
            self._hide_hover()
            return
        vb = self._plot.getViewBox()
        x_view = vb.mapSceneToView(pos).x()
        hit = self._value_at(x_view)
        if hit is None:
            self._hide_hover()
            return
        x_anchor, y_anchor, text = hit
        # Etiqueta encima del dato; debajo si está pegado al borde superior.
        (_, _), (ymin, ymax) = vb.viewRange()
        span = (ymax - ymin) or 1.0
        self._tip.setAnchor((0.5, 0) if (y_anchor - ymin) / span > 0.82 else (0.5, 1))
        self._tip.setText(text)
        self._tip.setPos(x_anchor, y_anchor)
        self._vline.setPos(x_anchor)
        self._tip.setVisible(True)
        self._vline.setVisible(True)

    def _value_at(self, x_view: float):  # pragma: no cover - lo implementan subclases
        raise NotImplementedError


class BarChart(_HoverChart):
    """Gráfico de barras verticales con etiquetas de categoría en el eje X."""

    def __init__(self, title: str = "", y_label: str = "", value_suffix: str = ""):
        super().__init__(title, y_label, value_suffix)
        self._plot.showGrid(x=False, y=True, alpha=0.3)
        self._labels: list[str] = []
        self._values: list[float] = []
        self._width = 0.6

    def set_data(self, labels: list[str], values: list[float], color: str = "#1F4E78"):
        self._plot.clear()
        self._add_overlays()
        self._labels = [str(x) for x in (labels or [])]
        self._values = [float(v) if v is not None else 0.0 for v in (values or [])]
        if not self._labels:
            return
        xs = list(range(len(self._labels)))
        self._plot.addItem(pg.BarGraphItem(
            x=xs, height=self._values, width=self._width, brush=color))
        self._plot.getAxis("bottom").setTicks([list(zip(xs, self._labels))])
        self._plot.enableAutoRange()

    def _value_at(self, x_view: float):
        if not self._values:
            return None
        idx = int(round(x_view))
        if idx < 0 or idx >= len(self._values) or abs(x_view - idx) > self._width / 2:
            return None
        val = self._values[idx]
        return idx, val, f"{self._labels[idx]}\n{_fmt(val, self._value_suffix)}"


class TimeSeriesChart(_HoverChart):
    """Una o varias series en el tiempo (eje X de fechas)."""

    def __init__(self, title: str = "", y_label: str = "", value_suffix: str = ""):
        super().__init__(title, y_label, value_suffix,
                         axis_items={"bottom": pg.DateAxisItem()})
        self._plot.showGrid(x=True, y=True, alpha=0.3)
        self._plot.addLegend(offset=(10, 10))
        self._periods: list[pd.Timestamp] = []
        self._x: list[float] = []
        self._series: dict[str, list[float]] = {}

    def set_series(self, periods, series: dict[str, list]):
        """`periods`: lista de Timestamps; `series`: {nombre: valores}."""
        self._plot.clear()
        self._add_overlays()
        self._periods, self._x, self._series = [], [], {}
        if periods is None or len(periods) == 0:
            return
        self._periods = [pd.Timestamp(p) for p in periods]
        self._x = [ts.timestamp() for ts in self._periods]
        for i, (name, ys) in enumerate(series.items()):
            color = _COLORS[i % len(_COLORS)]
            yvals = [float(v or 0) for v in ys]
            self._series[name] = yvals
            self._plot.plot(self._x, yvals, pen=pg.mkPen(color, width=2), name=name,
                            symbol="o", symbolSize=5, symbolBrush=color)
        self._plot.enableAutoRange()

    def _value_at(self, x_view: float):
        if not self._x:
            return None
        idx = min(range(len(self._x)), key=lambda i: abs(self._x[i] - x_view))
        period = self._periods[idx]
        lines = [f"{_month_abbr(period.month)} {period.year}"]
        top = 0.0
        for name, yvals in self._series.items():
            v = yvals[idx] if idx < len(yvals) else 0.0
            lines.append(f"{name}: {_fmt(v, self._value_suffix)}")
            top = max(top, v)
        return self._x[idx], top, "\n".join(lines)
