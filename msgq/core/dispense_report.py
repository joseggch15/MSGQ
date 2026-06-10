"""Reporte analitico 'Dispensas por Equipo' (Dispenses per Equipment).

Replica el reporte de auditoria de Merian: una grafica de dispersion por equipo
con todos sus despachos en el tiempo, la linea del Safe Fill Level (SFL) y la
clasificacion de cada despacho como **Normal** (volume <= SFL) u **Over SFL**
(volume > SFL, sobrellenado). Este modulo contiene SOLO la logica de negocio
(pandas, sin Qt ni matplotlib); el dibujo del PDF y el Excel viven en
`export/dispense_report.py`, y el dialogo de generacion en la interfaz.

Resolucion del SFL por equipo (en orden de prioridad):

  1. **Limite real** (`consumption_limits`, replicado de la API): el SFL del
     producto MAS despachado por ese equipo; si el equipo no tiene limite para
     ese producto, el SFL maximo entre sus productos con limite.
  2. **Mapeo por categoria** (`config.SFL_FALLBACK_BY_CATEGORY`): respaldo
     temporal por palabra clave para equipos sin limite cargado en el FMS.
  3. **Sin SFL** (NaN): el equipo se reporta con SFL "N/D" y todos sus
     despachos cuentan como Normal (no se puede exceder un limite desconocido).
"""
from __future__ import annotations

import pandas as pd

from msgq import config

CLASS_NORMAL = "Normal"
CLASS_OVER = "Over SFL"

# Fuente del SFL aplicado a cada equipo (columna `sfl_source` del dataset).
SFL_SOURCE_LIMIT = "Límite FMS"
SFL_SOURCE_FALLBACK = "Mapeo por categoría"
SFL_SOURCE_NONE = "N/D"

# Columnas del dataset clasificado (una fila por despacho).
DATASET_COLS = [
    "date", "equipment_id", "description", "category", "group", "department",
    "make", "cost_centre", "product", "volume", "sfl", "sfl_source", "clase",
    "field_user", "tank", "source_id",
]

# Dimensiones de agrupacion que ofrece el reporte (columna -> etiqueta visible).
# Mismas dimensiones que ya usa el analisis de equipos/tanques.
SCOPE_DIMENSIONS = (
    ("category", "Categoría"),
    ("group", "Grupo"),
    ("department", "Departamento"),
    ("make", "Marca"),
    ("cost_centre", "Cost Centre"),
)


def _norm(s: pd.Series) -> pd.Series:
    return s.astype("string").str.strip()


def _fallback_sfl(category) -> float | None:
    """SFL de respaldo por palabra clave de categoria (config, ajustable)."""
    if category is None or (isinstance(category, float) and pd.isna(category)):
        return None
    cat = str(category).strip().upper()
    if not cat:
        return None
    for keyword, sfl in config.SFL_FALLBACK_BY_CATEGORY:
        if keyword in cat:
            return float(sfl)
    return None


def resolve_sfl(dispenses: pd.DataFrame, limits: pd.DataFrame | None,
                equipment: pd.DataFrame | None) -> pd.DataFrame:
    """Una fila por equipo con su SFL resuelto: (equipment_id, sfl, sfl_source).

    `dispenses` decide cual es el producto MAS despachado por equipo (para
    elegir el limite pertinente cuando un equipo tiene varios productos).
    """
    eq_ids = (_norm(dispenses["equipment_id"]).dropna().unique()
              if not dispenses.empty else [])
    out = pd.DataFrame({"equipment_id": pd.Series(eq_ids, dtype="string")})
    if out.empty:
        return out.assign(sfl=pd.Series(dtype=float),
                          sfl_source=pd.Series(dtype="string"))

    # 1) Limite real por (equipo, producto), priorizando el producto dominante.
    sfl_by_eq: dict[str, float] = {}
    if limits is not None and not limits.empty and "sfl" in limits.columns:
        lim = limits.dropna(subset=["sfl"]).copy()
        lim["_eq"] = _norm(lim["equipment_id"])
        lim["_prod"] = _norm(lim["product"]).str.upper()
        lim["sfl"] = pd.to_numeric(lim["sfl"], errors="coerce")
        lim = lim.dropna(subset=["sfl"]).drop_duplicates(["_eq", "_prod"])

        disp = dispenses.copy()
        disp["_eq"] = _norm(disp["equipment_id"])
        disp["_prod"] = _norm(disp["product"]).str.upper()
        # Producto dominante = el mas frecuente en los despachos del equipo
        # (desempate alfabetico para que el resultado sea determinista).
        dominant = (disp.groupby(["_eq", "_prod"]).size().reset_index(name="n")
                    .sort_values(["n", "_prod"], ascending=[False, True])
                    .drop_duplicates("_eq").set_index("_eq")["_prod"])

        by_pair = lim.set_index(["_eq", "_prod"])["sfl"]
        by_max = lim.groupby("_eq")["sfl"].max()
        for eq in out["equipment_id"]:
            prod = dominant.get(eq)
            has_prod = prod is not None and not pd.isna(prod)
            sfl = by_pair.get((eq, prod)) if has_prod else None
            if sfl is None or pd.isna(sfl):
                sfl = by_max.get(eq)
            if sfl is not None and not pd.isna(sfl):
                sfl_by_eq[eq] = float(sfl)

    # 2) Respaldo por categoria para los equipos sin limite.
    cat_by_eq: dict[str, object] = {}
    if equipment is not None and not equipment.empty and "category" in equipment.columns:
        e = equipment.copy()
        e["_eq"] = _norm(e["equipment_id"])
        cat_by_eq = e.drop_duplicates("_eq").set_index("_eq")["category"].to_dict()

    sfl_vals, sources = [], []
    for eq in out["equipment_id"]:
        if eq in sfl_by_eq:
            sfl_vals.append(sfl_by_eq[eq])
            sources.append(SFL_SOURCE_LIMIT)
            continue
        fb = _fallback_sfl(cat_by_eq.get(eq))
        if fb is not None:
            sfl_vals.append(fb)
            sources.append(SFL_SOURCE_FALLBACK)
        else:
            sfl_vals.append(float("nan"))
            sources.append(SFL_SOURCE_NONE)
    out["sfl"] = sfl_vals
    out["sfl_source"] = pd.Series(sources, dtype="string")
    return out


def build_dataset(movements: pd.DataFrame | None,
                  equipment: pd.DataFrame | None,
                  limits: pd.DataFrame | None,
                  date_from: pd.Timestamp | None = None,
                  date_to: pd.Timestamp | None = None) -> pd.DataFrame:
    """Dataset clasificado del reporte: una fila por despacho con su SFL y clase.

    Cruza los despachos (`kind=DISPENSE`, con equipo y volumen validos) con el
    maestro de equipos (descripcion y dimensiones) y el SFL resuelto, y evalua
    `volume > sfl` -> Over SFL. `date_to` es exclusivo si trae hora 00:00 del
    dia siguiente (el dialogo normaliza el rango igual que la auditoria SFL).
    """
    if movements is None or movements.empty:
        return pd.DataFrame(columns=DATASET_COLS)
    mv = movements
    if "kind" in mv.columns:
        mv = mv[mv["kind"] == config.KIND_DISPENSE]
    if mv.empty or "equipment_id" not in mv.columns or "volume" not in mv.columns:
        return pd.DataFrame(columns=DATASET_COLS)

    mv = mv.copy()
    date_col = ("record_collected_at" if "record_collected_at" in mv.columns
                else "updated_at")
    mv["date"] = pd.to_datetime(mv[date_col], errors="coerce")
    mv["volume"] = pd.to_numeric(mv["volume"], errors="coerce")
    mv["_eq"] = _norm(mv["equipment_id"])
    mv = mv[mv["date"].notna() & mv["volume"].notna()
            & mv["_eq"].notna() & (mv["_eq"] != "")]
    if date_from is not None:
        mv = mv[mv["date"] >= pd.Timestamp(date_from)]
    if date_to is not None:
        mv = mv[mv["date"] < pd.Timestamp(date_to)]
    if mv.empty:
        return pd.DataFrame(columns=DATASET_COLS)

    # Maestro: descripcion completa y dimensiones de agrupacion.
    dims = ["description", "category", "group", "department", "make", "cost_centre"]
    if equipment is not None and not equipment.empty:
        e = equipment.copy()
        e["_eq"] = _norm(e["equipment_id"])
        cols = ["_eq"] + [c for c in dims if c in e.columns]
        mv = mv.merge(e[cols].drop_duplicates("_eq"), on="_eq", how="left",
                      suffixes=("", "_master"))
        # La descripcion del maestro manda; si falta, queda la del movimiento.
        if "description_master" in mv.columns:
            mv["description"] = mv["description_master"]
    if "description" not in mv.columns or mv["description"].isna().all():
        src = (mv["equipment_description"] if "equipment_description" in mv.columns
               else pd.Series(pd.NA, index=mv.index))
        mv["description"] = mv.get("description", pd.Series(pd.NA, index=mv.index))
        mv["description"] = mv["description"].fillna(src)
    elif "equipment_description" in mv.columns:
        mv["description"] = mv["description"].fillna(mv["equipment_description"])
    for c in dims:
        if c not in mv.columns:
            mv[c] = pd.NA

    # SFL por equipo + clasificacion (volume > sfl -> Over SFL).
    sfl_map = resolve_sfl(pd.DataFrame({"equipment_id": mv["_eq"],
                                        "product": mv.get("product")}),
                          limits, equipment)
    mv = mv.merge(sfl_map, left_on="_eq", right_on="equipment_id", how="left",
                  suffixes=("", "_sfl"))
    mv["sfl_source"] = mv["sfl_source"].fillna(SFL_SOURCE_NONE)
    over = mv["sfl"].notna() & (mv["volume"] > mv["sfl"])
    mv["clase"] = over.map({True: CLASS_OVER, False: CLASS_NORMAL})

    out = pd.DataFrame({
        "date": mv["date"],
        "equipment_id": mv["_eq"],
        "description": mv["description"],
        "category": mv["category"],
        "group": mv["group"],
        "department": mv["department"],
        "make": mv["make"],
        "cost_centre": mv["cost_centre"],
        "product": mv.get("product"),
        "volume": mv["volume"],
        "sfl": mv["sfl"],
        "sfl_source": mv["sfl_source"],
        "clase": mv["clase"],
        "field_user": mv.get("field_user"),
        "tank": mv.get("tank"),
        "source_id": mv.get("id"),
    }, columns=DATASET_COLS)
    return out.sort_values(["equipment_id", "date"]).reset_index(drop=True)


def filter_scope(dataset: pd.DataFrame,
                 equipment_ids: list[str] | None = None,
                 dimension: str | None = None,
                 value: str | None = None) -> pd.DataFrame:
    """Recorta el dataset al alcance elegido: equipos especificos, o una
    dimension del maestro (categoria / grupo / departamento / marca / cost
    centre) con un valor concreto. Sin argumentos = todos los equipos."""
    if dataset is None or dataset.empty:
        return dataset if dataset is not None else pd.DataFrame(columns=DATASET_COLS)
    out = dataset
    if equipment_ids:
        wanted = {str(x).strip() for x in equipment_ids}
        out = out[out["equipment_id"].astype("string").str.strip().isin(wanted)]
    if dimension and value is not None and dimension in out.columns:
        out = out[_norm(out[dimension]) == str(value).strip()]
    return out


def equipment_summary(dataset: pd.DataFrame) -> pd.DataFrame:
    """Resumen por equipo: conteos Normal/Over, SFL aplicado y volumenes."""
    cols = ["equipment_id", "description", "category", "group", "department",
            "SFL (L)", "Fuente SFL", "Despachos", "Normal", "Over SFL",
            "% Over", "Volumen total (L)", "Volumen máx (L)",
            "Primer despacho", "Último despacho"]
    if dataset is None or dataset.empty:
        return pd.DataFrame(columns=cols)
    g = dataset.groupby("equipment_id", dropna=False)
    out = pd.DataFrame({
        "equipment_id": g.size().index,
        "description": g["description"].first().values,
        "category": g["category"].first().values,
        "group": g["group"].first().values,
        "department": g["department"].first().values,
        "SFL (L)": g["sfl"].first().values,
        "Fuente SFL": g["sfl_source"].first().values,
        "Despachos": g.size().values,
        "Normal": g["clase"].apply(lambda s: int((s == CLASS_NORMAL).sum())).values,
        "Over SFL": g["clase"].apply(lambda s: int((s == CLASS_OVER).sum())).values,
        "Volumen total (L)": g["volume"].sum().round(1).values,
        "Volumen máx (L)": g["volume"].max().round(1).values,
        "Primer despacho": g["date"].min().values,
        "Último despacho": g["date"].max().values,
    })
    out["% Over"] = (out["Over SFL"] / out["Despachos"] * 100).round(2)
    return (out[cols].sort_values("Over SFL", ascending=False)
            .reset_index(drop=True))


def dimension_summary(dataset: pd.DataFrame, dim_col: str, label: str) -> pd.DataFrame:
    """Agregado por una dimension del maestro (categoria, grupo, etc.)."""
    cols = [label, "Equipos", "Despachos", "Normal", "Over SFL", "% Over",
            "Volumen total (L)"]
    if dataset is None or dataset.empty or dim_col not in dataset.columns:
        return pd.DataFrame(columns=cols)
    work = dataset.copy()
    work["_k"] = (_norm(work[dim_col]).replace({"": pd.NA}).fillna("(sin dato)"))
    g = work.groupby("_k")
    out = pd.DataFrame({
        label: g.size().index,
        "Equipos": g["equipment_id"].nunique().values,
        "Despachos": g.size().values,
        "Normal": g["clase"].apply(lambda s: int((s == CLASS_NORMAL).sum())).values,
        "Over SFL": g["clase"].apply(lambda s: int((s == CLASS_OVER).sum())).values,
        "Volumen total (L)": g["volume"].sum().round(1).values,
    })
    out["% Over"] = (out["Over SFL"] / out["Despachos"] * 100).round(2)
    return (out[cols].sort_values("Over SFL", ascending=False)
            .reset_index(drop=True))


def overall_kpis(dataset: pd.DataFrame) -> dict:
    """KPIs globales del alcance: totales de equipos, despachos y clases."""
    if dataset is None or dataset.empty:
        return {"Equipos": 0, "Despachos": 0, "Normal": 0, "Over SFL": 0,
                "% Over": 0.0, "Volumen total (L)": 0.0}
    n = len(dataset)
    over = int((dataset["clase"] == CLASS_OVER).sum())
    return {
        "Equipos": int(dataset["equipment_id"].nunique()),
        "Despachos": n,
        "Normal": n - over,
        "Over SFL": over,
        "% Over": round(over / n * 100, 2) if n else 0.0,
        "Volumen total (L)": round(float(dataset["volume"].sum()), 1),
    }
