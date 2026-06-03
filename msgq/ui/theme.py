"""Tema visual (claro / oscuro) del monitor MSGQ.

Un único punto que define las hojas de estilo (QSS) y los colores derivados que
NO se pueden expresar con QSS (tarjetas KPI con estilo en línea y los lienzos de
pyqtgraph). El QSS se aplica a nivel de `QApplication`, así cubre ambas ventanas
y los diálogos por igual.

El tema se elige con el selector de la barra superior y se recuerda entre
sesiones (QSettings). Cambiarlo reconstruye la interfaz para que las tarjetas KPI
y las gráficas tomen los colores nuevos (ver `main_window`/`equipment_window`).

No importa pyqtgraph (se mantiene la carga perezosa del análisis): las gráficas
leen `chart_colors()` al construirse.
"""
from __future__ import annotations

from PySide6.QtWidgets import QApplication

# Códigos y etiquetas (las etiquetas se traducen con i18n.t en el selector).
THEMES: tuple[tuple[str, str], ...] = (("light", "Claro"), ("dark", "Oscuro"))
_VALID = {code for code, _ in THEMES}
_DEFAULT = "light"
_theme = _DEFAULT

# --- Colores derivados por tema (para lo que no es QSS) --------------------
_CARD_BG = {"light": "#FFFFFF", "dark": "#2D2D30"}
_PANEL_BG = {"light": "#EDF1F6", "dark": "#2A2A2C"}
_CHART_BG = {"light": "w", "dark": "#252526"}
_CHART_FG = {"light": "k", "dark": "#D4D4D4"}

# Acentos semánticos: en oscuro se aclaran para que contrasten sobre fondo dark.
_DARK_ACCENTS = {
    "#1F4E78": "#5B9BD5",   # primario (azul)
    "#2E7D32": "#66BB6A",   # ok (verde)
    "#C62828": "#EF5350",   # peligro (rojo)
    "#9467bd": "#B388DD",   # morado
    "#E0A000": "#F0C040",   # ámbar
}

# Fondo de fila por severidad de alerta. En claro: pasteles suaves (texto oscuro).
# En oscuro: tintes oscuros para que el texto claro del tema siga legible.
_SEVERITY_BG = {
    "light": {"CRITICAL": "#FDE7E9", "WARNING": "#FFF4E5", "INFO": "#E8F0FE"},
    "dark":  {"CRITICAL": "#5C2B2B", "WARNING": "#5A4A28", "INFO": "#2A3F52"},
}


def set_theme(name) -> None:
    global _theme
    _theme = name if name in _VALID else _DEFAULT


def current_theme() -> str:
    return _theme


def card_bg() -> str:
    return _CARD_BG[_theme]


def panel_bg() -> str:
    return _PANEL_BG[_theme]


def chart_colors() -> tuple[str, str]:
    """(fondo, primer plano) para los lienzos de pyqtgraph."""
    return _CHART_BG[_theme], _CHART_FG[_theme]


def accent(color: str) -> str:
    """Ajusta un color semántico al tema actual (lo aclara en modo oscuro)."""
    return _DARK_ACCENTS.get(color, color) if _theme == "dark" else color


def severity_bg(severity) -> str | None:
    """Color de fondo de fila para una severidad de alerta, según el tema.
    Devuelve None si la severidad no es conocida (sin coloreado)."""
    return _SEVERITY_BG[_theme].get(severity)


def apply_theme(app: QApplication | None = None) -> None:
    """Aplica el QSS del tema actual a toda la aplicación."""
    app = app or QApplication.instance()
    if app is not None:
        app.setStyleSheet(_DARK_QSS if _theme == "dark" else _LIGHT_QSS)


# ===========================================================================
# Hojas de estilo
# ===========================================================================

_LIGHT_QSS = """
QMainWindow, QWidget { background: #F4F6F9; color: #1A1A1A; }
QGroupBox {
    font-weight: bold; color: #1F4E78;
    border: 1px solid #C9D3DF; border-radius: 8px;
    margin-top: 14px; padding: 10px;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; }
QPushButton {
    background: #1F4E78; color: white; border: none;
    border-radius: 6px; padding: 7px 14px; font-weight: bold;
}
QPushButton:hover { background: #2A5F92; }
QPushButton:disabled { background: #9AA8B8; color: white; }
QPushButton#accent { background: #2E7D32; }
QPushButton#danger { background: #C62828; }
QTabWidget::pane { border: 1px solid #C9D3DF; border-radius: 6px; background: white; }
QTabBar::tab {
    background: #E3E9F0; color: #1A1A1A; padding: 8px 16px; margin-right: 2px;
    border-top-left-radius: 6px; border-top-right-radius: 6px;
}
QTabBar::tab:selected { background: white; color: #1F4E78; font-weight: bold; }
QTableView {
    background: white; alternate-background-color: #EAF1F8;
    gridline-color: #DCE3EB; color: #1A1A1A;
    selection-background-color: #D0E4F7; selection-color: #1A1A1A;
}
QHeaderView::section {
    background: #1F4E78; color: white; padding: 6px; border: none; font-weight: bold;
}
QLineEdit, QSpinBox, QComboBox {
    background: white; color: #1A1A1A; border: 1px solid #C9D3DF;
    border-radius: 5px; padding: 4px;
}
QComboBox QAbstractItemView {
    background: white; color: #1A1A1A; selection-background-color: #D0E4F7;
}
QCheckBox { color: #1A1A1A; }
QMenu { background: white; color: #1A1A1A; }
QMenu::item:selected { background: #D0E4F7; }
"""

_DARK_QSS = """
QMainWindow, QWidget { background: #1E1E1E; color: #E0E0E0; }
QGroupBox {
    font-weight: bold; color: #5B9BD5;
    border: 1px solid #3A3A3A; border-radius: 8px;
    margin-top: 14px; padding: 10px;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; }
QPushButton {
    background: #2A5F92; color: white; border: none;
    border-radius: 6px; padding: 7px 14px; font-weight: bold;
}
QPushButton:hover { background: #3A6FA2; }
QPushButton:disabled { background: #3A3A3A; color: #8A8A8A; }
QPushButton#accent { background: #2E7D32; }
QPushButton#danger { background: #C62828; }
QTabWidget::pane { border: 1px solid #3A3A3A; border-radius: 6px; background: #252526; }
QTabBar::tab {
    background: #2D2D30; color: #CCCCCC; padding: 8px 16px; margin-right: 2px;
    border-top-left-radius: 6px; border-top-right-radius: 6px;
}
QTabBar::tab:selected { background: #252526; color: #5B9BD5; font-weight: bold; }
QTableView {
    background: #252526; alternate-background-color: #2D2D30;
    gridline-color: #3A3A3A; color: #E0E0E0;
    selection-background-color: #2A4A6A; selection-color: #FFFFFF;
}
QHeaderView::section {
    background: #2A5F92; color: white; padding: 6px; border: none; font-weight: bold;
}
QLineEdit, QSpinBox, QComboBox {
    background: #2D2D30; color: #E0E0E0; border: 1px solid #3A3A3A;
    border-radius: 5px; padding: 4px;
}
QComboBox QAbstractItemView {
    background: #2D2D30; color: #E0E0E0; selection-background-color: #2A4A6A;
}
QCheckBox { color: #E0E0E0; }
QStatusBar { color: #AAAAAA; }
QMenu { background: #2D2D30; color: #E0E0E0; }
QMenu::item:selected { background: #2A4A6A; }
QScrollBar:vertical { background: #252526; width: 12px; }
QScrollBar::handle:vertical { background: #3A3A3A; border-radius: 6px; min-height: 20px; }
QScrollBar:horizontal { background: #252526; height: 12px; }
QScrollBar::handle:horizontal { background: #3A3A3A; border-radius: 6px; min-width: 20px; }
"""
