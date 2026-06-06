"""Auditoría del Burn Rate (consumo de combustible, L/h) por equipo.

AdaptIQ publica un 'Burn Rate' por equipo/día (Litres Consumed ÷ SMU Increase).
Este módulo lo **reconstruye en vivo desde el endpoint** (no del CSV) a partir de
los despachos replicados, para poder auditarlo, graficarlo y marcar los equipos
con un comportamiento anómalo.

Método «tanque-a-tanque» (el estándar del dominio y el que usa AdaptIQ):

  • Para CADA equipo se ordenan sus despachos (`kind=DISPENSE`) por fecha.
  • Entre dos despachos consecutivos, los litros del despacho POSTERIOR reponen el
    combustible quemado desde el anterior; el avance del SMU (horas-motor, u
    odómetro en vehículos ligeros) es el 'uso' del intervalo. Entonces:

        burn_rate_intervalo = litros(despacho_n) / (SMU_n − SMU_{n-1})

    Asunción: el repostaje llena el tanque (fill-to-fill). Los repostajes
    parciales meten ruido — por eso TODO se resume con estadística ROBUSTA
    (mediana + MAD), inmune a los outliers que justamente queremos detectar.

Detección de anomalías en dos granularidades:

  1. **Equipo vs su categoría** (`equipment_table` / `equipment_anomalies`): el
     burn rate típico del equipo (mediana de sus intervalos) se compara contra la
     línea base de su categoría (mediana de los equipos de la categoría). Se marca
     si se desvía con un |z robusto| ≥ umbral Y una desviación relativa ≥ mínimo.
     Dirección 'Alto' = sobre-consumo (posible fuga/robo/falla); 'Bajo' =
     sub-consumo (posible medidor mal o despachos sin registrar).

  2. **Intervalo atípico** (`interval_anomalies`): un despacho puntual cuyo burn
     rate se aparta del propio historial del equipo (pico o caída a investigar).

Las unidades del SMU difieren por tipo de equipo (horas en maquinaria, km en
vehículos ligeros). La línea base se calcula POR categoría; en este tenant cada
categoría es homogénea en su `smu_type` (no mezcla horas con km), así que la
comparación equipo↔categoría es consistente. El `smu_type` se conserva por
equipo para que el auditor detecte cualquier excepción.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from msgq import config

# --- Muestra: un intervalo entre dos despachos consecutivos (un burn rate) ---
SAMPLE_COLS = [
    "date", "equipment_id", "equipment_description", "category", "product",
    "litres", "smu_delta", "burn_rate", "smu_prev", "smu_curr", "smu_type",
    "field_user", "source_id",
]

# --- Tabla por equipo: burn rate típico + comparación con su categoría -------
EQUIPMENT_COLS = [
    "equipment_id", "equipment_description", "category", "product", "smu_type",
    "Muestras", "Burn rate (L/h)", "Baseline categoría (L/h)", "Desviación %",
    "Z robusto", "Dirección", "Litros total", "Anómalo",
]

# --- Tabla por categoría: la línea base y su dispersión ----------------------
CATEGORY_COLS = [
    "category", "Equipos", "Muestras", "Burn rate base (L/h)", "Dispersión (L/h)",
    "Mín equipo (L/h)", "Máx equipo (L/h)", "Anómalos",
]

# --- Intervalo atípico (un despacho puntual fuera del historial del equipo) --
INTERVAL_COLS = [
    "date", "equipment_id", "equipment_description", "category", "product",
    "litres", "smu_delta", "burn_rate", "Burn rate típico (L/h)", "Desviación %",
    "Z robusto", "Dirección", "field_user", "source_id",
]

_MAD_TO_SIGMA = 1.4826   # MAD -> sigma robusto (consistente bajo normalidad)
_NO_CATEGORY = "(sin dato)"


def _empty(cols: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=cols)


def _blank(s: pd.Series) -> pd.Series:
    txt = s.astype("string").str.strip()
    return s.isna() | txt.eq("") | txt.str.upper().isin(["<NA>", "NAN", "NONE", "UNAUTHORISED"])


def _direction(delta: pd.Series) -> pd.Series:
    """'Alto' si el valor supera la referencia, 'Bajo' si está por debajo, '' si NA."""
    out = pd.Series("", index=delta.index, dtype="object")
    out[delta > 0] = "Alto"
    out[delta < 0] = "Bajo"
    out[delta.isna()] = ""
    return out


# ===========================================================================
# 1. Muestras por intervalo (litros / ΔSMU entre despachos consecutivos)
# ===========================================================================

def _equipment_maps(equipment: pd.DataFrame | None) -> tuple[dict, dict]:
    """Mapas {equipment_id: categoría} y {equipment_id: descripción} del maestro.

    Los movimientos no llevan la categoría del equipo (solo el id y la
    descripción), así que la categoría se resuelve del maestro de equipos."""
    cat: dict = {}
    desc: dict = {}
    if equipment is None or equipment.empty or "equipment_id" not in equipment.columns:
        return cat, desc
    ids = equipment["equipment_id"].astype("string").str.strip()
    if "category" in equipment.columns:
        cat = {k: v for k, v in zip(ids, equipment["category"])
               if k and not pd.isna(v)}
    if "description" in equipment.columns:
        desc = {k: v for k, v in zip(ids, equipment["description"])
                if k and not pd.isna(v)}
    return cat, desc


def interval_samples(movements: pd.DataFrame | None,
                     equipment: pd.DataFrame | None = None) -> pd.DataFrame:
    """Un burn rate por intervalo (par de despachos consecutivos del mismo equipo).

    Filtra a `kind=DISPENSE` con `equipment_id`, `volume>0` y `smu_value`; ordena
    por equipo y fecha; descarta intervalos con avance de SMU insuficiente
    (`BURN_RATE_MIN_SMU_DELTA`) o un burn rate no plausible (artefacto de dato,
    `BURN_RATE_MAX_PLAUSIBLE`)."""
    if movements is None or movements.empty:
        return _empty(SAMPLE_COLS)
    mv = movements
    if "kind" in mv.columns:
        mv = mv[mv["kind"] == config.KIND_DISPENSE]
    if mv.empty or not {"equipment_id", "volume", "smu_value"}.issubset(mv.columns):
        return _empty(SAMPLE_COLS)

    mv = mv.copy()
    mv["_eid"] = mv["equipment_id"].astype("string").str.strip()
    mv = mv[~_blank(mv["_eid"])]
    mv["_litres"] = pd.to_numeric(mv["volume"], errors="coerce")
    mv["_smu"] = pd.to_numeric(mv["smu_value"], errors="coerce")
    mv = mv[(mv["_litres"] > 0) & mv["_smu"].notna()]
    if mv.empty:
        return _empty(SAMPLE_COLS)

    date_col = "record_collected_at" if "record_collected_at" in mv.columns else "updated_at"
    mv["_date"] = pd.to_datetime(mv.get(date_col), errors="coerce")
    # Orden temporal dentro de cada equipo (estable: ante fechas iguales, por SMU).
    mv = mv.sort_values(["_eid", "_date", "_smu"], kind="mergesort")

    g = mv.groupby("_eid", sort=False)
    mv["_smu_prev"] = g["_smu"].shift(1)
    mv["_smu_delta"] = mv["_smu"] - mv["_smu_prev"]
    s = mv[mv["_smu_delta"] >= config.BURN_RATE_MIN_SMU_DELTA].copy()
    if s.empty:
        return _empty(SAMPLE_COLS)
    s["_burn"] = s["_litres"] / s["_smu_delta"]
    s = s[(s["_burn"] > 0) & (s["_burn"] <= config.BURN_RATE_MAX_PLAUSIBLE)]
    if s.empty:
        return _empty(SAMPLE_COLS)

    cat_map, desc_map = _equipment_maps(equipment)
    category = s["_eid"].map(cat_map)
    category = category.where(~_blank(category), _NO_CATEGORY)
    # Descripción: la del movimiento; si falta, la del maestro; si no, el id.
    desc = s.get("equipment_description")
    if desc is None:
        desc = pd.Series(pd.NA, index=s.index)
    desc = desc.where(~_blank(desc), s["_eid"].map(desc_map))
    desc = desc.where(~_blank(desc), s["_eid"])

    out = pd.DataFrame({
        "date":                  s["_date"],
        "equipment_id":          s["equipment_id"],
        "equipment_description": desc,
        "category":              category,
        "product":               s.get("product"),
        "litres":                s["_litres"].round(1),
        "smu_delta":             s["_smu_delta"].round(2),
        "burn_rate":             s["_burn"].round(2),
        "smu_prev":              s["_smu_prev"].round(1),
        "smu_curr":              s["_smu"].round(1),
        "smu_type":              s.get("smu_type"),
        "field_user":            s.get("field_user"),
        "source_id":             s.get("id"),
    }, columns=SAMPLE_COLS)
    return out.sort_values("date", ascending=False).reset_index(drop=True)


# ===========================================================================
# 2. Estadística por equipo y línea base por categoría
# ===========================================================================

def _equipment_stats(samples: pd.DataFrame) -> pd.DataFrame:
    """Una fila por equipo: burn rate típico (mediana), nº de muestras, litros."""
    if samples is None or samples.empty:
        return pd.DataFrame(columns=[
            "equipment_id", "equipment_description", "category", "product",
            "smu_type", "samples", "burn_rate", "total_litres"])
    g = samples.groupby("equipment_id", sort=False)
    eq = g.agg(
        equipment_description=("equipment_description", "first"),
        category=("category", "first"),
        product=("product", "first"),
        smu_type=("smu_type", "first"),
        samples=("burn_rate", "size"),
        burn_rate=("burn_rate", "median"),
        total_litres=("litres", "sum"),
    ).reset_index()
    return eq


def category_baselines(eq_stats: pd.DataFrame) -> pd.DataFrame:
    """Línea base por categoría sobre los equipos CONFIABLES (≥ muestras mínimas):
    centro robusto (mediana de los burn rates de equipo) y su dispersión (MAD)."""
    cols = ["category", "baseline", "sigma", "n_equipment", "min", "max"]
    if eq_stats is None or eq_stats.empty:
        return pd.DataFrame(columns=cols)
    reliable = eq_stats[eq_stats["samples"] >= config.BURN_RATE_MIN_SAMPLES]
    rows = []
    for cat, chunk in reliable.groupby("category"):
        vals = chunk["burn_rate"].dropna()
        if len(vals) < config.BURN_RATE_MIN_CAT_EQUIPMENT:
            continue
        med = float(vals.median())
        mad = float((vals - med).abs().median())
        rows.append({
            "category": cat, "baseline": med, "sigma": _MAD_TO_SIGMA * mad,
            "n_equipment": int(len(vals)), "min": float(vals.min()),
            "max": float(vals.max()),
        })
    return pd.DataFrame(rows, columns=cols)


def equipment_table(samples: pd.DataFrame,
                    baselines: pd.DataFrame | None = None) -> pd.DataFrame:
    """Tabla por equipo con su burn rate, la línea base de su categoría, la
    desviación (% y z robusto) y la marca de anómalo. Incluye TODOS los equipos
    con al menos una muestra (la marca solo aplica a los confiables)."""
    eq = _equipment_stats(samples)
    if eq.empty:
        return _empty(EQUIPMENT_COLS)
    if baselines is None:
        baselines = category_baselines(eq)

    base_map = dict(zip(baselines["category"], baselines["baseline"])) if not baselines.empty else {}
    sigma_map = dict(zip(baselines["category"], baselines["sigma"])) if not baselines.empty else {}
    eq["baseline"] = eq["category"].map(base_map)
    eq["sigma"] = eq["category"].map(sigma_map)

    reliable = eq["samples"] >= config.BURN_RATE_MIN_SAMPLES
    has_base = eq["baseline"].notna()
    delta = eq["burn_rate"] - eq["baseline"]
    eq["dev_pct"] = (delta / eq["baseline"] * 100).where(has_base & (eq["baseline"] != 0))
    sig_ok = eq["sigma"].notna() & (eq["sigma"] > 0)
    eq["z"] = (delta / eq["sigma"]).where(sig_ok)
    eq["direction"] = _direction(delta.where(reliable & has_base))

    # Anómalo: confiable, con línea base, desviación operativamente relevante Y
    # estadísticamente significativa (|z|≥umbral). Si la categoría es degenerada
    # (todos sus equipos casi idénticos, sigma=0), basta la desviación relativa.
    stat = (sig_ok & (eq["z"].abs() >= config.BURN_RATE_Z_THRESHOLD)) | (~sig_ok & has_base)
    eq["anomalo"] = (reliable & has_base
                     & (eq["dev_pct"].abs() >= config.BURN_RATE_MIN_DEV_PCT)
                     & stat).fillna(False)

    out = pd.DataFrame({
        "equipment_id":               eq["equipment_id"],
        "equipment_description":      eq["equipment_description"],
        "category":                   eq["category"],
        "product":                    eq["product"],
        "smu_type":                   eq["smu_type"],
        "Muestras":                   eq["samples"].astype(int),
        "Burn rate (L/h)":            eq["burn_rate"].round(2),
        "Baseline categoría (L/h)":   eq["baseline"].round(2),
        "Desviación %":               eq["dev_pct"].round(1),
        "Z robusto":                  eq["z"].round(2),
        "Dirección":                  eq["direction"],
        "Litros total":               eq["total_litres"].round(1),
        "Anómalo":                    eq["anomalo"].astype(bool),
    }, columns=EQUIPMENT_COLS)
    # Anómalos primero, luego por magnitud de la desviación.
    out["_abs"] = out["Desviación %"].abs()
    out = out.sort_values(["Anómalo", "_abs"], ascending=[False, False])
    return out.drop(columns="_abs").reset_index(drop=True)


def equipment_anomalies(eq_table: pd.DataFrame) -> pd.DataFrame:
    """Solo los equipos marcados como anómalos (subconjunto de `equipment_table`)."""
    if eq_table is None or eq_table.empty or "Anómalo" not in eq_table.columns:
        return _empty(EQUIPMENT_COLS)
    return eq_table[eq_table["Anómalo"].map(bool)].reset_index(drop=True)


def categories_table(eq_table: pd.DataFrame,
                     baselines: pd.DataFrame) -> pd.DataFrame:
    """Resumen por categoría: línea base, dispersión, rango de equipos y nº de
    equipos anómalos. Solo categorías con línea base confiable."""
    if baselines is None or baselines.empty:
        return _empty(CATEGORY_COLS)
    anom = {}
    samp = {}
    if eq_table is not None and not eq_table.empty:
        anom = eq_table.groupby("category")["Anómalo"].apply(lambda s: int(s.map(bool).sum())).to_dict()
        samp = eq_table.groupby("category")["Muestras"].sum().to_dict()
    rows = []
    for _, b in baselines.iterrows():
        cat = b["category"]
        rows.append({
            "category": cat,
            "Equipos": int(b["n_equipment"]),
            "Muestras": int(samp.get(cat, 0)),
            "Burn rate base (L/h)": round(float(b["baseline"]), 2),
            "Dispersión (L/h)": round(float(b["sigma"]), 2),
            "Mín equipo (L/h)": round(float(b["min"]), 2),
            "Máx equipo (L/h)": round(float(b["max"]), 2),
            "Anómalos": int(anom.get(cat, 0)),
        })
    return (pd.DataFrame(rows, columns=CATEGORY_COLS)
            .sort_values("Burn rate base (L/h)", ascending=False)
            .reset_index(drop=True))


# ===========================================================================
# 3. Intervalos atípicos (un despacho fuera del historial del propio equipo)
# ===========================================================================

def interval_anomalies(samples: pd.DataFrame) -> pd.DataFrame:
    """Despachos cuyo burn rate se aparta del historial del propio equipo.

    Para cada equipo con suficientes muestras compara cada intervalo contra el
    centro robusto (mediana) y la dispersión (MAD) de ESE equipo; marca los de
    |z robusto| ≥ `BURN_RATE_INTERVAL_Z` y desviación relativa ≥ mínimo."""
    if samples is None or samples.empty:
        return _empty(INTERVAL_COLS)
    s = samples.copy()
    eid = s["equipment_id"]
    grp = s.groupby(eid)["burn_rate"]
    center = grp.transform("median")
    count = grp.transform("size")
    absdev = (s["burn_rate"] - center).abs()
    mad = absdev.groupby(eid).transform("median")
    sigma = _MAD_TO_SIGMA * mad
    delta = s["burn_rate"] - center
    z = (delta / sigma).where(sigma > 0)
    dev_pct = (delta / center * 100).where(center != 0)

    flag = ((count >= config.BURN_RATE_MIN_SAMPLES) & (sigma > 0)
            & (z.abs() >= config.BURN_RATE_INTERVAL_Z)
            & (dev_pct.abs() >= config.BURN_RATE_MIN_DEV_PCT)).fillna(False)
    hit = s[flag]
    if hit.empty:
        return _empty(INTERVAL_COLS)

    out = pd.DataFrame({
        "date":                    hit["date"],
        "equipment_id":            hit["equipment_id"],
        "equipment_description":   hit["equipment_description"],
        "category":                hit["category"],
        "product":                 hit["product"],
        "litres":                  hit["litres"],
        "smu_delta":               hit["smu_delta"],
        "burn_rate":               hit["burn_rate"],
        "Burn rate típico (L/h)":  center[flag].round(2),
        "Desviación %":            dev_pct[flag].round(1),
        "Z robusto":               z[flag].round(2),
        "Dirección":               _direction(delta[flag]),
        "field_user":              hit["field_user"],
        "source_id":               hit["source_id"],
    }, columns=INTERVAL_COLS)
    out["_abs"] = out["Z robusto"].abs()
    out = out.sort_values("_abs", ascending=False)
    return out.drop(columns="_abs").reset_index(drop=True)


# ===========================================================================
# 4. Series para gráficas y KPIs
# ===========================================================================

def equipment_series(samples: pd.DataFrame, equipment_id: str) -> pd.DataFrame:
    """Burn rate en el tiempo de UN equipo (para la gráfica individual)."""
    cols = ["date", "burn_rate"]
    if samples is None or samples.empty or not equipment_id:
        return pd.DataFrame(columns=cols)
    sub = samples[samples["equipment_id"].astype("string") == str(equipment_id)]
    if sub.empty:
        return pd.DataFrame(columns=cols)
    sub = sub.dropna(subset=["date"]).sort_values("date")
    return sub[cols].reset_index(drop=True)


def summary_kpis(eq_table: pd.DataFrame, samples: pd.DataFrame,
                 interval_anom: pd.DataFrame) -> dict:
    """KPIs de la franja superior de la ventana."""
    reliable = pd.DataFrame()
    if eq_table is not None and not eq_table.empty:
        reliable = eq_table[eq_table["Muestras"] >= config.BURN_RATE_MIN_SAMPLES]
    n_eq = int(len(reliable))
    anom = equipment_anomalies(eq_table)
    n_anom = int(len(anom))
    fleet = (round(float(reliable["Burn rate (L/h)"].median()), 2)
             if not reliable.empty else 0.0)
    worst = (round(float(anom["Desviación %"].abs().max()), 1)
             if not anom.empty else 0.0)
    return {
        "Equipos analizados": n_eq,
        "Equipos anómalos": n_anom,
        "Intervalos analizados": 0 if samples is None or samples.empty else int(len(samples)),
        "Intervalos atípicos": 0 if interval_anom is None or interval_anom.empty else int(len(interval_anom)),
        "Burn rate flota (L/h)": fleet,
        "Peor desviación %": worst,
    }


# ===========================================================================
# Auditoría completa (una pasada; lo que consume la ventana en su worker)
# ===========================================================================

@dataclass
class BurnRateResult:
    """Resultado de la auditoría de burn rate, calculado en una sola pasada:
    muestras por intervalo, tabla por equipo, resumen por categoría, anomalías de
    equipo e intervalo, y los KPIs."""
    samples: pd.DataFrame
    equipment: pd.DataFrame
    categories: pd.DataFrame
    equipment_anomalies: pd.DataFrame
    interval_anomalies: pd.DataFrame
    kpis: dict


def audit(movements: pd.DataFrame | None,
          equipment: pd.DataFrame | None = None) -> BurnRateResult:
    """Calcula TODA la auditoría de burn rate de una vez (la GUI la usa así para
    no recomputar las muestras varias veces)."""
    samples = interval_samples(movements, equipment)
    eq_stats = _equipment_stats(samples)
    baselines = category_baselines(eq_stats)
    eq_table = equipment_table(samples, baselines)
    cats = categories_table(eq_table, baselines)
    eq_anom = equipment_anomalies(eq_table)
    int_anom = interval_anomalies(samples)
    kpis = summary_kpis(eq_table, samples, int_anom)
    return BurnRateResult(samples, eq_table, cats, eq_anom, int_anom, kpis)
