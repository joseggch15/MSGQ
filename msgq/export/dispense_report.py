"""Exportadores del reporte 'Dispensas por Equipo': PDF (grilla de graficas,
identico al reporte de muestra de Merian) y Excel (tablas analiticas).

PDF — una grafica de dispersion por equipo, en grilla de 3x3 por pagina:
puntos azules = despachos Normales, rojos = Over SFL (sobrellenado), linea
discontinua naranja = SFL del equipo, caja con el conteo "Normal: N  Over: M",
titulo "ID | descripcion" y pie "Dispensas por Equipo — <alcance> | Pag. N".

Se dibuja con la API orientada a objetos de matplotlib (Figure + canvas Agg,
sin pyplot): no toca Qt ni estado global, asi que puede correr con seguridad
en el hilo de trabajo que lanza el dialogo de la interfaz.

Excel — resumen por equipo, detalle de despachos Over SFL, agregados por
categoria/grupo/departamento y el detalle clasificado completo si el volumen
del alcance lo permite. Reusa `export_sheets` (mismo estilo del ecosistema).
"""
from __future__ import annotations

from typing import Callable

import pandas as pd

from msgq.core import dispense_report as dr
from msgq.export.equipment_excel import export_sheets
from msgq.i18n import t

# Paleta del reporte de muestra.
COLOR_NORMAL = "#1f77b4"     # azul matplotlib (puntos normales)
COLOR_OVER = "#d62728"       # rojo (sobrellenados)
COLOR_SFL = "#ff7f0e"        # naranja (linea discontinua del SFL)
_BOX_FC, _BOX_EC = "#FFFDE7", "#999999"   # caja del conteo Normal/Over

_ROWS, _COLS = 3, 3                       # graficas por pagina (como la muestra)
_FIGSIZE = (14.9, 13.7)                   # ~1070x984 pt, tamano de la muestra
_TITLE_MAX = 46                           # truncado del titulo "ID | descripcion"

# Limite de filas para incluir la hoja "Despachos clasificados" completa en el
# Excel (mas alla, el workbook se vuelve lento e inmanejable; queda el detalle
# de los Over SFL, que es lo auditable).
_EXCEL_DETAIL_MAX_ROWS = 50_000


def _chart_title(eq_id: str, description) -> str:
    desc = "" if description is None or pd.isna(description) else str(description)
    title = f"{eq_id}  |  {desc}" if desc else str(eq_id)
    return title[:_TITLE_MAX]


def _draw_equipment_chart(ax, sub: pd.DataFrame, mdates) -> None:
    """Dibuja la grafica de UN equipo (mismo lenguaje visual de la muestra)."""
    eq_id = str(sub["equipment_id"].iloc[0])
    sfl = sub["sfl"].iloc[0]
    normal = sub[sub["clase"] == dr.CLASS_NORMAL]
    over = sub[sub["clase"] == dr.CLASS_OVER]

    if not normal.empty:
        ax.scatter(normal["date"], normal["volume"], s=9, color=COLOR_NORMAL,
                   label=t("Normal"), zorder=2)
    if not over.empty:
        ax.scatter(over["date"], over["volume"], s=14, color=COLOR_OVER,
                   label=t("Over SFL"), zorder=3)
    if sfl is not None and not pd.isna(sfl):
        ax.axhline(float(sfl), linestyle="--", linewidth=1.2, color=COLOR_SFL,
                   label=f"SFL {float(sfl):,.0f} L", zorder=1)

    ax.set_title(_chart_title(eq_id, sub["description"].iloc[0]),
                 fontsize=8, fontweight="bold")
    ax.set_ylabel(t("Litros"), fontsize=7)
    ax.tick_params(axis="both", labelsize=6)
    locator = mdates.AutoDateLocator(minticks=3, maxticks=6)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b'%y"))
    for lbl in ax.get_xticklabels():
        lbl.set_rotation(30)
        lbl.set_horizontalalignment("right")
    ax.legend(fontsize=6, loc="upper right", framealpha=0.9)
    ax.text(0.02, 0.97, f"{t('Normal')}: {len(normal)}  {t('Over')}: {len(over)}",
            transform=ax.transAxes, va="top", ha="left", fontsize=7,
            bbox=dict(boxstyle="round,pad=0.3", fc=_BOX_FC, ec=_BOX_EC, lw=0.6),
            zorder=4)


def export_pdf(path: str, dataset: pd.DataFrame, *, scope_label: str,
               extra_equipment: list[tuple[str, str]] | None = None,
               progress: Callable[[int, int], None] | None = None,
               cancel: Callable[[], bool] | None = None) -> int:
    """Genera el PDF del reporte. Devuelve el numero de paginas escritas.

    `extra_equipment`: pares (id, descripcion) elegidos explicitamente que no
    tienen despachos en el rango; aparecen con el rotulo "Sin despachos en el
    rango" (como la muestra hace con sus equipos sin datos).
    `progress(pagina, total)` informa el avance; `cancel()` -> True interrumpe
    (el archivo queda incompleto y el llamador decide descartarlo).
    """
    # Import perezoso y backend Agg: sin ventanas, seguro fuera del hilo principal.
    import matplotlib
    matplotlib.use("Agg", force=False)
    import matplotlib.dates as mdates
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.figure import Figure

    groups: list[tuple[str, pd.DataFrame | None, str]] = []
    if dataset is not None and not dataset.empty:
        for eq_id, sub in dataset.groupby("equipment_id", sort=True):
            groups.append((str(eq_id), sub, ""))
    have = {g[0] for g in groups}
    for eq_id, desc in (extra_equipment or []):
        if str(eq_id) not in have:
            groups.append((str(eq_id), None, desc or ""))
    groups.sort(key=lambda g: g[0])

    per_page = _ROWS * _COLS
    total_pages = max(1, (len(groups) + per_page - 1) // per_page)
    title_base = f"{t('Dispensas por Equipo')} — {scope_label}"

    pages_written = 0
    with PdfPages(path) as pdf:
        for p in range(total_pages):
            if cancel is not None and cancel():
                break
            chunk = groups[p * per_page:(p + 1) * per_page]
            fig = Figure(figsize=_FIGSIZE, dpi=100)
            fig.suptitle(f"{title_base} | {t('Pág.')} {p + 1} {t('de')} {total_pages}",
                         fontsize=11, fontweight="bold")
            for i in range(per_page):
                ax = fig.add_subplot(_ROWS, _COLS, i + 1)
                if i >= len(chunk):
                    ax.set_axis_off()
                    continue
                eq_id, sub, desc = chunk[i]
                if sub is None or sub.empty:
                    ax.set_title(_chart_title(eq_id, desc),
                                 fontsize=8, fontweight="bold")
                    ax.text(0.5, 0.5, t("Sin despachos en el rango"),
                            transform=ax.transAxes, ha="center", va="center",
                            fontsize=8, color="#999999")
                    ax.set_xticks([]); ax.set_yticks([])
                    continue
                _draw_equipment_chart(ax, sub, mdates)
            fig.tight_layout(rect=(0, 0, 1, 0.965))
            pdf.savefig(fig)
            pages_written += 1
            if progress is not None:
                progress(pages_written, total_pages)
        meta = pdf.infodict()
        meta["Title"] = title_base
        meta["Author"] = "MSGQ — Newmont Merian"
        meta["Subject"] = t("Despachos clasificados contra el Safe Fill Level (SFL)")
    return pages_written


# Columnas visibles del detalle clasificado (orden legible para el Excel).
_DETAIL_COLS = ["date", "equipment_id", "description", "category", "group",
                "product", "volume", "sfl", "sfl_source", "clase",
                "field_user", "tank", "source_id"]


def export_excel(path: str, dataset: pd.DataFrame, *, scope_label: str) -> None:
    """Genera el Excel del reporte: resumen por equipo, detalle de Over SFL,
    agregados por dimension y (si el volumen lo permite) el detalle completo."""
    detail = (dataset[[c for c in _DETAIL_COLS if c in dataset.columns]]
              if dataset is not None and not dataset.empty else pd.DataFrame())
    over = (detail[detail["clase"] == dr.CLASS_OVER]
            if not detail.empty else pd.DataFrame())
    kpis = dr.overall_kpis(dataset)
    resumen_global = pd.DataFrame(
        {"Indicador": [f"{t('Alcance')}: {scope_label}"] + list(kpis.keys()),
         "Valor": [""] + [kpis[k] for k in kpis]})

    sheets: dict[str, pd.DataFrame] = {
        "Resumen": resumen_global,
        "Resumen por equipo": dr.equipment_summary(dataset),
        "Despachos Over SFL": over,
        "Por categoría": dr.dimension_summary(dataset, "category", "Categoría"),
        "Por grupo": dr.dimension_summary(dataset, "group", "Grupo"),
        "Por departamento": dr.dimension_summary(dataset, "department", "Departamento"),
    }
    if not detail.empty and len(detail) <= _EXCEL_DETAIL_MAX_ROWS:
        sheets["Despachos clasificados"] = detail
    export_sheets(path, sheets)
