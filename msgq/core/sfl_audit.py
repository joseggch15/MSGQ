"""Auditoria de despachos contra el Safe Fill Level (SFL).

El SFL (de `EquipmentItem.consumptionTanks`, replicado en la tabla
`consumption_limits`) es el volumen maximo seguro a despachar a un equipo en UN
repostaje, por producto. Dispensar mas que el SFL en un solo despacho es un
**sobrellenado**: riesgo de derrame / seguridad y bandera para el auditor — no
deberia ocurrir.

Este modulo cruza los despachos (movimientos `kind=DISPENSE`) con el SFL del
equipo para ese producto y lista los que lo exceden. El cruce es por
(equipment_id, producto), usando la misma etiqueta de producto (la `description`,
via `transform._label`) que viaja tanto en el movimiento como en el limite.
"""
from __future__ import annotations

import pandas as pd

from msgq import config

# Esquema de la tabla de excesos (una fila por despacho que supera el SFL).
EXCEEDANCE_COLS = [
    "date", "equipment_id", "equipment_description", "equipment_status",
    "product", "volume", "sfl", "excess", "excess_pct",
    "field_user", "dispensing_point", "source_id",
]


def _norm(s: pd.Series) -> pd.Series:
    """Etiqueta de producto / id normalizada para cruce (texto, strip, upper)."""
    return s.astype("string").str.strip().str.upper()


def sfl_map(limits: pd.DataFrame | None) -> dict:
    """Mapa {(equipment_id, PRODUCTO_MAYUS): sfl} desde `consumption_limits`."""
    out: dict = {}
    if limits is None or limits.empty:
        return out
    for _, r in limits.iterrows():
        sfl = r.get("sfl")
        if sfl is None or (isinstance(sfl, float) and pd.isna(sfl)):
            continue
        eid = r.get("equipment_id")
        prod = r.get("product")
        if eid is None or prod is None:
            continue
        out[(str(eid).strip(), str(prod).strip().upper())] = float(sfl)
    return out


def exceedances(movements: pd.DataFrame | None,
                limits: pd.DataFrame | None) -> pd.DataFrame:
    """Despachos cuyo volumen excede el SFL del equipo para ese producto.

    Solo considera `kind=DISPENSE` con SFL conocido para (equipo, producto).
    `excess` = volume - sfl; `excess_pct` = excess / sfl * 100.
    """
    if (movements is None or movements.empty or limits is None or limits.empty
            or "sfl" not in limits.columns):
        return pd.DataFrame(columns=EXCEEDANCE_COLS)

    mv = movements
    if "kind" in mv.columns:
        mv = mv[mv["kind"] == config.KIND_DISPENSE]
    needed = {"equipment_id", "product", "volume"}
    if mv.empty or not needed.issubset(mv.columns):
        return pd.DataFrame(columns=EXCEEDANCE_COLS)

    mv = mv.copy()
    mv["_pk"] = mv["equipment_id"].astype("string").str.strip()   # id exacto (mismo origen)
    mv["_prod"] = _norm(mv["product"])                            # producto por etiqueta, case-insensitive
    mv["volume"] = pd.to_numeric(mv["volume"], errors="coerce")

    lim = limits.dropna(subset=["sfl"]).copy()
    lim["_pk"] = lim["equipment_id"].astype("string").str.strip()
    lim["_prod"] = _norm(lim["product"])
    lim["sfl"] = pd.to_numeric(lim["sfl"], errors="coerce")
    lim = lim.dropna(subset=["sfl"]).drop_duplicates(["_pk", "_prod"])

    merged = mv.merge(lim[["_pk", "_prod", "sfl"]], on=["_pk", "_prod"], how="inner")
    merged = merged[merged["volume"].notna() & (merged["volume"] > merged["sfl"])]
    if merged.empty:
        return pd.DataFrame(columns=EXCEEDANCE_COLS)

    date_col = "record_collected_at" if "record_collected_at" in merged.columns else "updated_at"
    out = pd.DataFrame({
        "date":                  merged.get(date_col),
        "equipment_id":          merged.get("equipment_id"),
        "equipment_description": merged.get("equipment_description"),
        "equipment_status":      merged.get("equipment_status"),
        "product":               merged.get("product"),
        "volume":                merged["volume"],
        "sfl":                   merged["sfl"],
        "excess":                (merged["volume"] - merged["sfl"]).round(2),
        "excess_pct":            ((merged["volume"] - merged["sfl"]) / merged["sfl"] * 100).round(1),
        "field_user":            merged.get("field_user"),
        "dispensing_point":      merged.get("tank"),
        "source_id":             merged.get("id"),
    }, columns=EXCEEDANCE_COLS)
    return out.sort_values("date", ascending=False).reset_index(drop=True)


# ===========================================================================
# Conflictos: despachos SIN equipo valido (no_equip / Unauthorised)
# ===========================================================================
# Un despacho sin equipo no se puede contrastar contra el SFL de un equipo
# concreto; si ademas su volumen supera el SFL MAXIMO de la flota para ese
# producto, es un sobrellenado que no es seguro para NINGUN equipo -> conflicto
# critico. Cubre el caso del usuario: combustible despachado como 'no_equip' /
# 'Unauthorised' (y luego, quiza, reasignado al equipo equivocado).

CONFLICT_COLS = [
    "date", "equipment_id", "product", "volume", "type", "status",
    "fleet_max_sfl", "over_max", "field_user", "dispensing_point", "source_id",
]


def fleet_sfl_by_product(limits: pd.DataFrame | None) -> dict:
    """Mapa {PRODUCTO_MAYUS: SFL maximo de la flota} desde `consumption_limits`."""
    out: dict = {}
    if limits is None or limits.empty or "sfl" not in limits.columns:
        return out
    df = limits.copy()
    df["sfl"] = pd.to_numeric(df["sfl"], errors="coerce")
    df = df.dropna(subset=["sfl"])
    if df.empty:
        return out
    df["_p"] = _norm(df["product"])
    for p, chunk in df.groupby("_p"):
        out[p] = float(chunk["sfl"].max())
    return out


def _blank_id(s: pd.Series) -> pd.Series:
    txt = s.astype("string").str.strip()
    return s.isna() | txt.eq("") | txt.str.upper().isin(["<NA>", "NAN", "UNAUTHORISED"])


def unattributed_conflicts(movements: pd.DataFrame | None,
                           limits: pd.DataFrame | None) -> pd.DataFrame:
    """Despachos sin equipo valido (status 'no_equip', tipo 'Unauthorised', o
    equipment_id vacio/'Unauthorised'). `over_max` = el volumen supera el SFL
    maximo de la flota para ese producto (sobrellenado para cualquier equipo)."""
    if movements is None or movements.empty:
        return pd.DataFrame(columns=CONFLICT_COLS)
    mv = movements
    if "kind" in mv.columns:
        mv = mv[mv["kind"] == config.KIND_DISPENSE]
    if mv.empty:
        return pd.DataFrame(columns=CONFLICT_COLS)
    mv = mv.copy()
    idx = mv.index
    status = (mv["status"].astype("string").str.strip().str.lower()
              if "status" in mv.columns else pd.Series("", index=idx))
    typ = (mv["type"].astype("string").str.strip()
           if "type" in mv.columns else pd.Series("", index=idx))
    no_eq = (_blank_id(mv["equipment_id"]) if "equipment_id" in mv.columns
             else pd.Series(True, index=idx))
    mask = (status.eq("no_equip") | typ.eq(config.TYPE_UNAUTHORISED) | no_eq)
    mask = mask.fillna(False).astype(bool)   # NA-safe para indexar
    conf = mv[mask].copy()
    if conf.empty:
        return pd.DataFrame(columns=CONFLICT_COLS)

    fleet = fleet_sfl_by_product(limits)
    conf["volume"] = pd.to_numeric(conf["volume"], errors="coerce")
    conf["_p"] = _norm(conf["product"]) if "product" in conf.columns else ""
    conf["fleet_max_sfl"] = conf["_p"].map(fleet)
    conf["over_max"] = conf["fleet_max_sfl"].notna() & (conf["volume"] > conf["fleet_max_sfl"])
    date_col = "record_collected_at" if "record_collected_at" in conf.columns else "updated_at"
    out = pd.DataFrame({
        "date":             conf.get(date_col),
        "equipment_id":     conf.get("equipment_id"),
        "product":          conf.get("product"),
        "volume":           conf["volume"],
        "type":             conf.get("type"),
        "status":           conf.get("status"),
        "fleet_max_sfl":    conf["fleet_max_sfl"],
        "over_max":         conf["over_max"].map(lambda b: bool(b)).astype(object),
        "field_user":       conf.get("field_user"),
        "dispensing_point": conf.get("tank"),
        "source_id":        conf.get("id"),
    }, columns=CONFLICT_COLS)
    return out.sort_values(["over_max", "volume"], ascending=[False, False]).reset_index(drop=True)


def conflict_kpis(conf: pd.DataFrame) -> dict:
    if conf is None or conf.empty:
        return {"Conflictos": 0, "Sobre SFL flota": 0, "Volumen conflictivo (L)": 0.0}
    return {
        "Conflictos": len(conf),
        "Sobre SFL flota": int(conf["over_max"].map(bool).sum()),
        "Volumen conflictivo (L)": round(float(pd.to_numeric(conf["volume"], errors="coerce").sum()), 1),
    }


# ===========================================================================
# KPIs y agrupaciones
# ===========================================================================

def summary_kpis(exc: pd.DataFrame, movements: pd.DataFrame | None = None) -> dict:
    n_disp = 0
    if movements is not None and not movements.empty and "kind" in movements.columns:
        n_disp = int((movements["kind"] == config.KIND_DISPENSE).sum())
    if exc is None or exc.empty:
        return {"Excesos": 0, "Exceso total (L)": 0.0, "Peor exceso (L)": 0.0,
                "Equipos afectados": 0, "% de despachos": 0.0}
    total = float(exc["excess"].sum())
    worst = float(exc["excess"].max())
    equipos = int(exc["equipment_id"].nunique())
    pct = (len(exc) / n_disp * 100) if n_disp else 0.0
    return {
        "Excesos": len(exc),
        "Exceso total (L)": round(total, 1),
        "Peor exceso (L)": round(worst, 1),
        "Equipos afectados": equipos,
        "% de despachos": round(pct, 2),
    }


def by_product(exc: pd.DataFrame) -> pd.DataFrame:
    cols = ["Producto", "Excesos", "Exceso total (L)", "Peor exceso (L)"]
    if exc is None or exc.empty:
        return pd.DataFrame(columns=cols)
    g = exc.groupby(exc["product"].fillna("(sin dato)")).agg(
        Excesos=("excess", "size"),
        **{"Exceso total (L)": ("excess", "sum"), "Peor exceso (L)": ("excess", "max")}
    ).reset_index().rename(columns={"product": "Producto"})
    g["Exceso total (L)"] = g["Exceso total (L)"].round(1)
    g["Peor exceso (L)"] = g["Peor exceso (L)"].round(1)
    return g.sort_values("Excesos", ascending=False).reset_index(drop=True)


def by_equipment(exc: pd.DataFrame) -> pd.DataFrame:
    cols = ["equipment_id", "equipment_description", "Excesos",
            "Exceso total (L)", "Peor exceso (L)"]
    if exc is None or exc.empty:
        return pd.DataFrame(columns=cols)
    g = exc.groupby("equipment_id").agg(
        equipment_description=("equipment_description", "first"),
        Excesos=("excess", "size"),
        **{"Exceso total (L)": ("excess", "sum"), "Peor exceso (L)": ("excess", "max")}
    ).reset_index()
    g["Exceso total (L)"] = g["Exceso total (L)"].round(1)
    g["Peor exceso (L)"] = g["Peor exceso (L)"].round(1)
    return g.sort_values("Excesos", ascending=False).reset_index(drop=True)[cols]


def over_time(exc: pd.DataFrame, freq: str = "ME") -> pd.DataFrame:
    cols = ["Periodo", "Excesos", "Exceso total (L)"]
    if exc is None or exc.empty:
        return pd.DataFrame(columns=cols)
    df = exc.dropna(subset=["date"]).copy()
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    g = (df.set_index("date").groupby(pd.Grouper(freq=freq))
         .agg(Excesos=("excess", "size"), **{"Exceso total (L)": ("excess", "sum")})
         .reset_index().rename(columns={"date": "Periodo"}))
    g["Exceso total (L)"] = g["Exceso total (L)"].round(1)
    return g
