"""Utilidades compartidas de la interfaz grafica (mismo patron del ecosistema)."""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QSortFilterProxyModel
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
    QPushButton, QTableView, QVBoxLayout, QWidget,
)

import pandas as pd

from msgq.i18n import LANGUAGES, current_language, t, tr_fmt
from msgq.ui import theme
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
    box.setPlaceholderText(t("Filtrar por cualquier texto..."))

    def _filter(text: str):
        proxy = table.model()
        proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        proxy.setFilterKeyColumn(-1)
        proxy.setFilterFixedString(text)

    box.textChanged.connect(_filter)
    lay.addWidget(box)
    lay.addWidget(table)
    return container


class BusyOverlay(QWidget):
    """Capa de "cargando" semitransparente superpuesta a su widget padre.

    Muestra una barra de progreso indeterminada (animada) y un texto mientras el
    software lee/recalcula datos en segundo plano, para que el usuario sepa que
    esta trabajando y no parezca congelado. Mientras esta visible intercepta los
    clics y teclas, de modo que el usuario no dispare otra accion a medias.

    Se redimensiona sola para cubrir al padre (sigue sus `Resize` via event
    filter). Reutilizable por cualquier ventana del modulo."""

    def __init__(self, parent: QWidget, text: str = ""):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("busyOverlay")
        self.setStyleSheet("#busyOverlay { background: rgba(0, 0, 0, 96); }")
        card = QFrame(self)
        card.setObjectName("busyCard")
        card.setStyleSheet(
            f"#busyCard {{ background: {theme.card_bg()}; "
            f"border: 1px solid {theme.accent('#1F4E78')}; border-radius: 10px; }}")
        self._card = card
        lay = QVBoxLayout(card)
        lay.setContentsMargins(26, 20, 26, 20)
        lay.setSpacing(10)
        self._label = QLabel(text or t("Cargando…"))
        self._label.setAlignment(Qt.AlignCenter)
        bar = QProgressBar()
        bar.setRange(0, 0)            # indeterminada: animacion continua de "ocupado"
        bar.setTextVisible(False)
        bar.setFixedWidth(240)
        lay.addWidget(self._label)
        lay.addWidget(bar)
        self.hide()
        if parent is not None:
            parent.installEventFilter(self)

    def set_text(self, text: str) -> None:
        self._label.setText(text)

    def start(self, text: str = "") -> None:
        if text:
            self._label.setText(text)
        self._fit()
        self.show()
        self.raise_()

    def stop(self) -> None:
        self.hide()

    def eventFilter(self, watched, event):  # noqa: N802 - override Qt
        if watched is self.parent() and event.type() == QEvent.Resize:
            self._fit()
        return super().eventFilter(watched, event)

    def showEvent(self, event):  # noqa: N802 - override Qt
        self._fit()
        self.raise_()
        super().showEvent(event)

    def _fit(self) -> None:
        parent = self.parent()
        if parent is None:
            return
        self.setGeometry(parent.rect())
        self._card.adjustSize()
        r = self._card.rect()
        self._card.move(max(0, (self.width() - r.width()) // 2),
                        max(0, (self.height() - r.height()) // 2))

    # Traga la interaccion mientras carga (no deja pasar clics al fondo).
    def mousePressEvent(self, event):  # noqa: N802 - override Qt
        event.accept()

    def mouseReleaseEvent(self, event):  # noqa: N802 - override Qt
        event.accept()

    def keyPressEvent(self, event):  # noqa: N802 - override Qt
        event.accept()


class PaginatedTableView(QWidget):
    """Tabla paginada que solo materializa UNA página a la vez.

    Mantiene el DataFrame completo en memoria, pero proyecta en el modelo Qt
    únicamente las `page_size` filas de la página actual. Así ordenar, filtrar o
    pintar decenas de miles de filas NO congela la interfaz (Qt nunca recorre más
    de una página). El ordenamiento por encabezado opera en pandas sobre TODO el
    conjunto y vuelve a la primera página. Reutilizable por cualquier ventana.

    Reemplaza a `make_table()` + `wrap_with_search()` cuando el volumen es grande;
    el filtro de texto global lo provee la ventana contenedora (filtra el conjunto
    antes de entregarlo con `set_full_dataframe`)."""

    _PAGE_SIZES = (100, 200, 500, 1000)

    def __init__(self, page_size: int = 200, parent=None):
        super().__init__(parent)
        self._full = pd.DataFrame()      # conjunto completo (ya filtrado por la ventana)
        self._view_df = pd.DataFrame()   # tras el ordenamiento local
        self._page = 0
        self._page_size = page_size
        self._sort_col: str | None = None
        self._sort_asc = False
        self._build()

    # -- construcción -------------------------------------------------------

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(4)

        self._view = QTableView()
        self._model = DataFrameModel()
        self._view.setModel(self._model)
        self._view.setAlternatingRowColors(True)
        self._view.setSelectionBehavior(QTableView.SelectRows)
        self._view.setSortingEnabled(False)            # ordenamos en pandas (todo el set)
        header = self._view.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self._on_header_clicked)
        lay.addWidget(self._view)

        bar = QHBoxLayout()
        self._btn_first = QPushButton("⏮")
        self._btn_prev = QPushButton("◀")
        self._btn_next = QPushButton("▶")
        self._btn_last = QPushButton("⏭")
        self._btn_first.clicked.connect(lambda: self._goto(0))
        self._btn_prev.clicked.connect(lambda: self._goto(self._page - 1))
        self._btn_next.clicked.connect(lambda: self._goto(self._page + 1))
        self._btn_last.clicked.connect(lambda: self._goto(self._page_count() - 1))
        for b in (self._btn_first, self._btn_prev, self._btn_next, self._btn_last):
            b.setFixedWidth(40)
        self._lbl = QLabel("")
        bar.addWidget(self._btn_first)
        bar.addWidget(self._btn_prev)
        bar.addWidget(self._lbl)
        bar.addWidget(self._btn_next)
        bar.addWidget(self._btn_last)
        bar.addStretch(1)
        bar.addWidget(QLabel(t("Filas por página:")))
        self._cmb_size = QComboBox()
        for s in self._PAGE_SIZES:
            self._cmb_size.addItem(f"{s:,}", s)
        self._cmb_size.setCurrentIndex(self._PAGE_SIZES.index(self._page_size)
                                       if self._page_size in self._PAGE_SIZES else 1)
        self._cmb_size.currentIndexChanged.connect(self._on_size_changed)
        bar.addWidget(self._cmb_size)
        lay.addLayout(bar)

    # -- API pública --------------------------------------------------------

    def set_full_dataframe(self, df: pd.DataFrame) -> None:
        """Fija el conjunto completo a paginar (conserva orden/página si es posible)."""
        self._full = df if df is not None else pd.DataFrame()
        self._apply_sort()
        self._render_page()           # mantiene self._page, lo re-acota _render_page

    def current_model(self) -> DataFrameModel:
        return self._model

    # -- ordenamiento (pandas, sobre todo el conjunto) ----------------------

    def _on_header_clicked(self, section: int) -> None:
        if self._full.empty or section < 0 or section >= len(self._full.columns):
            return
        col = str(self._full.columns[section])
        if col == self._sort_col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col, self._sort_asc = col, True
        self._view.horizontalHeader().setSortIndicator(
            section, Qt.AscendingOrder if self._sort_asc else Qt.DescendingOrder)
        self._apply_sort()
        self._page = 0
        self._render_page()

    def _apply_sort(self) -> None:
        df = self._full
        if self._sort_col and self._sort_col in df.columns and not df.empty:
            df = df.sort_values(self._sort_col, ascending=self._sort_asc,
                                kind="mergesort", na_position="last")
        self._view_df = df

    # -- paginación ---------------------------------------------------------

    def _page_count(self) -> int:
        n = len(self._view_df)
        return max(1, (n + self._page_size - 1) // self._page_size)

    def _goto(self, page: int) -> None:
        self._page = max(0, min(page, self._page_count() - 1))
        self._render_page()

    def _on_size_changed(self, _ix: int) -> None:
        self._page_size = int(self._cmb_size.currentData())
        self._page = 0
        self._render_page()

    def _render_page(self) -> None:
        self._page = max(0, min(self._page, self._page_count() - 1))
        n = len(self._view_df)
        start = self._page * self._page_size
        self._model.set_dataframe(self._view_df.iloc[start:start + self._page_size])
        lo = 0 if n == 0 else start + 1
        hi = min(start + self._page_size, n)
        self._lbl.setText(tr_fmt("page.label", page=self._page + 1,
                                 pages=self._page_count(), lo=lo, hi=hi, total=n))
        self._btn_first.setEnabled(self._page > 0)
        self._btn_prev.setEnabled(self._page > 0)
        self._btn_next.setEnabled(self._page < self._page_count() - 1)
        self._btn_last.setEnabled(self._page < self._page_count() - 1)


def kpi_label(title: str, value: str, color: str = "#1F4E78") -> QLabel:
    """Tarjeta KPI compacta con titulo y valor coloreado (sensible al tema)."""
    c = theme.accent(color)
    lbl = QLabel(f"<b>{title}</b><br><span style='font-size:15px'>{value}</span>")
    lbl.setTextFormat(Qt.RichText)
    lbl.setStyleSheet(
        f"QLabel {{ border: 1px solid {c}; border-radius: 6px; "
        f"padding: 6px 14px; color: {c}; background: {theme.card_bg()}; }}"
    )
    lbl.setMinimumWidth(130)
    return lbl


def warn_label(title: str, value: str, warn: bool = False) -> QLabel:
    """Tarjeta KPI con color condicional: rojo si hay anomalias, verde si no."""
    color = "#C62828" if warn else "#2E7D32"
    return kpi_label(title, value, color)


def language_selector(on_change) -> QWidget:
    """Selector de idioma (🌐 + combo) reutilizable. `on_change(code)` recibe el
    código del idioma elegido ('es'/'en'). El idioma actual queda preseleccionado
    sin disparar la señal (se conecta después de fijar el índice)."""
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    lay.addWidget(QLabel("🌐"))
    combo = QComboBox()
    for code, name in LANGUAGES:
        combo.addItem(name, code)
    ix = next((i for i, (c, _) in enumerate(LANGUAGES) if c == current_language()), 0)
    combo.setCurrentIndex(ix)
    combo.currentIndexChanged.connect(lambda i: on_change(combo.itemData(i)))
    combo.setMaximumWidth(120)
    lay.addWidget(combo)
    return w


def theme_selector(on_change) -> QWidget:
    """Selector de tema (🌓 + combo claro/oscuro) reutilizable. `on_change(code)`
    recibe 'light'/'dark'. El tema actual queda preseleccionado sin disparar la
    señal (se conecta después de fijar el índice)."""
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    lay.addWidget(QLabel("🌓"))
    combo = QComboBox()
    for code, label in theme.THEMES:
        combo.addItem(t(label), code)
    ix = next((i for i, (c, _) in enumerate(theme.THEMES) if c == theme.current_theme()), 0)
    combo.setCurrentIndex(ix)
    combo.currentIndexChanged.connect(lambda i: on_change(combo.itemData(i)))
    combo.setMaximumWidth(110)
    lay.addWidget(combo)
    return w
