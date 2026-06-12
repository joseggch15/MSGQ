"""Auditoria de Actividad: equipos fantasma y coherencia actividad<->combustible.

Tres detectores que cruzan el maestro de equipos con los despachos replicados
(y el SMU por-movimiento) para encontrar inconsistencias entre lo que el FMS
dice que opera y el combustible que realmente fluye:

  1. **Equipos fantasma** (`idle_assets`). Equipos 'In Service' sin despachos
     en >= N dias (o que NUNCA han despachado en todo el historico replicado).
     Figuran operativos pero no consumen -> distorsionan los KPIs de
     disponibilidad del Data Center.

  2. **Trabaja sin repostar** (`unfueled_activity`). Entre dos despachos
     consecutivos del mismo equipo, el avance del SMU multiplicado por su burn
     rate tipico estima el combustible quemado en el intervalo. Si ese consumo
     esperado supera el SFL (capacidad segura del tanque) por encima del
     factor de margen, el equipo NO pudo operar todo el intervalo con un solo
     tanque: recibio combustible por fuera del FMS (canecas, proveedor externo
     o despachos sin registrar) — combustible sin trazabilidad.

  3. **Repostado sin operar** (`fueling_without_activity`). Rachas de
     despachos consecutivos cuyo SMU no avanza (motor apagado / equipo
     detenido) pero que siguen recibiendo combustible con frecuencia. Si los
     litros acumulados de la racha superan el SFL, el tanque fisicamente no
     pudo absorberlos sin operar -> posible desvio usando la identidad del
     equipo. (La firma se solapa con un sensor SMU danado — que ya audita
     `hardware_health` como orden de trabajo —; aqui se agrega el angulo de
     VOLUMEN, que es lo que convierte la falla tecnica en riesgo de fraude.)

Los detectores 2 y 3 requieren `smu_value` por despacho (en este tenant lo
reporta la flota pesada: haul trucks, dozers, excavadoras...); los equipos sin
SMU solo participan del detector 1. El SFL se resuelve igual que el reporte
'Dispensas por Equipo' (limite real del FMS -> mapeo por categoria -> N/D).
"""
from __future__ import annotations

import pandas as pd

from msgq import config
from msgq.core import burn_rate
from msgq.core.dispense_report import resolve_sfl

CLASS_NEVER = "Nunca despachó"
CLASS_IDLE = "Inactivo"

IDLE_COLS = [
    "equipment_id", "description", "category", "group", "department", "status",
    "ultimo_despacho", "dias_sin_despachar", "despachos_historicos", "clase",
]
UNFUELED_COLS = [
    "equipment_id", "equipment_description", "category", "desde", "hasta",
    "dias", "smu_delta", "smu_type", "burn_rate_tipico", "consumo_esperado",
    "despachado", "sfl", "no_registrado", "source_id",
]
FROZEN_COLS = [
    "equipment_id", "equipment_description", "category", "desde", "hasta",
    "dias", "despachos", "litros", "sfl", "sobre_sfl", "smu_estancado",
    "smu_type",
]


def _norm(s: pd.Series) -> pd.Series:
    return s.astype("string").str.strip()


def _dispenses(movements: pd.DataFrame | None) -> pd.DataFrame:
    if movements is None or movements.empty:
        return pd.DataFrame()
    mv = movements
    if "kind" in mv.columns:
        mv = mv[mv["kind"] == config.KIND_DISPENSE]
    if mv.empty or "equipment_id" not in mv.columns:
        return pd.DataFrame()
    mv = mv.copy()
    date_col = ("record_collected_at" if "record_collected_at" in mv.columns
                else "updated_at")
    mv["_date"] = pd.to_datetime(mv.get(date_col), errors="coerce")
    mv["_eid"] = _norm(mv["equipment_id"])
    return mv[mv["_date"].notna() & mv["_eid"].notna() & (mv["_eid"] != "")]


# ===========================================================================
# 1. Equipos fantasma (In Service sin despachos)
# ===========================================================================

def idle_assets(equipment: pd.DataFrame | None,
                movements: pd.DataFrame | None,
                now: pd.Timestamp | None = None,
                min_days: float = 0.0) -> pd.DataFrame:
    """Equipos 'In Service' con `min_days` o mas dias sin despachar (0 = todos,
    con sus dias de inactividad; la vista filtra por umbral sin recalcular)."""
    if equipment is None or equipment.empty or "status" not in equipment.columns:
        return pd.DataFrame(columns=IDLE_COLS)
    now = pd.Timestamp.now() if now is None else pd.Timestamp(now)

    eq = equipment.copy()
    eq["_eid"] = _norm(eq["equipment_id"])
    eq = eq[eq["status"] == config.STATUS_IN].drop_duplicates("_eid")
    if eq.empty:
        return pd.DataFrame(columns=IDLE_COLS)

    mv = _dispenses(movements)
    if not mv.empty:
        last = mv.groupby("_eid")["_date"].max()
        count = mv.groupby("_eid").size()
    else:
        last = pd.Series(dtype="datetime64[ns]")
        count = pd.Series(dtype=int)

    eq["ultimo_despacho"] = eq["_eid"].map(last)
    eq["despachos_historicos"] = eq["_eid"].map(count).fillna(0).astype(int)
    eq["dias_sin_despachar"] = ((now - eq["ultimo_despacho"]).dt.total_seconds()
                                / 86400.0).round(1)
    eq["clase"] = (eq["ultimo_despacho"].isna()
                   .map({True: CLASS_NEVER, False: CLASS_IDLE}))
    if min_days > 0:
        eq = eq[eq["ultimo_despacho"].isna()
                | (eq["dias_sin_despachar"] >= float(min_days))]

    out = pd.DataFrame({
        "equipment_id": eq["_eid"],
        "description": eq.get("description"),
        "category": eq.get("category"),
        "group": eq.get("group"),
        "department": eq.get("department"),
        "status": eq.get("status"),
        "ultimo_despacho": eq["ultimo_despacho"],
        "dias_sin_despachar": eq["dias_sin_despachar"],
        "despachos_historicos": eq["despachos_historicos"],
        "clase": eq["clase"],
    }, columns=IDLE_COLS)
    # Nunca-despacho primero (inactividad infinita), luego por dias desc.
    out["_orden"] = out["dias_sin_despachar"].fillna(float("inf"))
    out = out.sort_values(["_orden", "equipment_id"], ascending=[False, True])
    return out.drop(columns="_orden").reset_index(drop=True)


# ===========================================================================
# Pares consecutivos con SMU (insumo de los detectores 2 y 3)
# ===========================================================================

def _smu_pairs(movements: pd.DataFrame | None) -> pd.DataFrame:
    """Pares (lectura de SMU anterior -> actual) del mismo equipo, INCLUYENDO
    los de avance cero (a diferencia de las muestras de burn rate, que los
    descartan: aqui el SMU congelado ES la senal).

    Entre dos lecturas de SMU puede haber despachos SIN SMU: `_litres_window`
    acumula TODO el combustible registrado al equipo en la ventana
    (date_prev, date] — sin esto, los despachos intermedios sin SMU contarian
    como 'combustible no registrado', un falso positivo masivo."""
    mv = _dispenses(movements)
    if mv.empty or not {"volume", "smu_value"}.issubset(mv.columns):
        return pd.DataFrame()
    mv = mv.copy()
    mv["_litres"] = pd.to_numeric(mv["volume"], errors="coerce")
    mv["_smu"] = pd.to_numeric(mv["smu_value"], errors="coerce")
    mv = mv[mv["_litres"] > 0]
    if mv.empty:
        return pd.DataFrame()
    # Acumulado de litros por equipo sobre TODOS sus despachos (con o sin SMU),
    # antes de quedarnos solo con las filas que traen lectura de SMU.
    mv = mv.sort_values(["_eid", "_date", "_smu"], kind="mergesort")
    mv["_cum"] = mv.groupby("_eid", sort=False)["_litres"].cumsum()
    mv = mv[mv["_smu"].notna()]
    if mv.empty:
        return pd.DataFrame()
    g = mv.groupby("_eid", sort=False)
    mv["_smu_prev"] = g["_smu"].shift(1)
    mv["_date_prev"] = g["_date"].shift(1)
    mv["_cum_prev"] = g["_cum"].shift(1)
    mv = mv[mv["_smu_prev"].notna()].copy()
    mv["_smu_delta"] = mv["_smu"] - mv["_smu_prev"]
    mv["_days"] = (mv["_date"] - mv["_date_prev"]).dt.total_seconds() / 86400.0
    mv["_litres_window"] = mv["_cum"] - mv["_cum_prev"]
    return mv


def _sfl_by_equipment(movements: pd.DataFrame | None,
                      limits: pd.DataFrame | None,
                      equipment: pd.DataFrame | None) -> pd.Series:
    """{equipment_id: SFL} con la misma cascada del reporte de dispensas."""
    mv = _dispenses(movements)
    if mv.empty:
        return pd.Series(dtype=float)
    res = resolve_sfl(pd.DataFrame({"equipment_id": mv["_eid"],
                                    "product": mv.get("product")}),
                      limits, equipment)
    if res.empty:
        return pd.Series(dtype=float)
    return res.set_index("equipment_id")["sfl"]


# ===========================================================================
# 2. Trabaja sin repostar (consumo esperado > SFL sin despacho de por medio)
# ===========================================================================

def unfueled_activity(movements: pd.DataFrame | None,
                      equipment: pd.DataFrame | None,
                      limits: pd.DataFrame | None,
                      sfl_factor: float | None = None) -> pd.DataFrame:
    """Intervalos en los que el equipo trabajo mas de lo que su tanque permite
    sin repostar dentro del FMS -> combustible NO registrado (estimado)."""
    factor = config.ACTIVITY_UNFUELED_SFL_FACTOR if sfl_factor is None else sfl_factor
    pairs = _smu_pairs(movements)
    if pairs.empty:
        return pd.DataFrame(columns=UNFUELED_COLS)

    # Burn rate tipico por equipo (mediana de sus intervalos validos); respaldo:
    # linea base de su categoria. Mismas muestras robustas del modulo burn_rate.
    samples = burn_rate.interval_samples(movements, equipment)
    if samples.empty:
        return pd.DataFrame(columns=UNFUELED_COLS)
    samples = samples.copy()
    samples["_eid"] = _norm(samples["equipment_id"])
    own = samples.groupby("_eid")["burn_rate"].agg(["median", "size"])
    own_rate = own[own["size"] >= 3]["median"]
    cat_rate = samples.groupby("category")["burn_rate"].median()

    valid = pairs[pairs["_smu_delta"] >= config.BURN_RATE_MIN_SMU_DELTA].copy()
    if valid.empty:
        return pd.DataFrame(columns=UNFUELED_COLS)
    # Plausibilidad fisica: el SMU no puede avanzar mas que el tiempo de pared
    # transcurrido (horometro <= ~1 h/h; odometro <= ~120 km/h sostenidos). Los
    # saltos que lo exceden son lecturas corruptas del sensor (las audita
    # hardware_health), no actividad real: estimar consumo con ellas inventaria
    # millones de litros fantasma.
    elapsed_h = (valid["_days"] * 24.0).clip(lower=0)
    smu_type = valid.get("smu_type")
    if smu_type is None:
        smu_type = pd.Series(pd.NA, index=valid.index)
    is_hours = (smu_type.astype("string").str.strip().str.lower()
                .str.startswith("h").fillna(False))
    max_rate = is_hours.map({True: config.ACTIVITY_MAX_SMU_PER_HOUR_HRS,
                             False: config.ACTIVITY_MAX_SMU_PER_HOUR_KM})
    valid = valid[(elapsed_h > 0) & (valid["_smu_delta"] <= elapsed_h * max_rate)]
    if valid.empty:
        return pd.DataFrame(columns=UNFUELED_COLS)
    cat_map, _desc = burn_rate._equipment_maps(equipment)
    valid["_rate"] = valid["_eid"].map(own_rate)
    fb = valid["_eid"].map(cat_map).map(cat_rate)
    valid["_rate"] = valid["_rate"].fillna(fb)
    valid = valid[valid["_rate"].notna() & (valid["_rate"] > 0)]
    if valid.empty:
        return pd.DataFrame(columns=UNFUELED_COLS)

    # Ventanas acotadas: mas alla del maximo, el burn rate tipico multiplicado
    # por miles de horas amplifica el error y las brechas de cobertura de SMU
    # del endpoint producirian falsos positivos sistematicos.
    valid = valid[valid["_days"] <= config.ACTIVITY_UNFUELED_MAX_GAP_DAYS]
    if valid.empty:
        return pd.DataFrame(columns=UNFUELED_COLS)

    sfl = _sfl_by_equipment(movements, limits, equipment)
    valid["_sfl"] = valid["_eid"].map(sfl)
    valid["_expected"] = valid["_smu_delta"] * valid["_rate"]
    # Faltante = consumo esperado - TODO el combustible registrado en la
    # ventana (incluidos despachos sin SMU). Anomalia si el faltante excede
    # mas de un tanque (SFL) con margen: ese combustible entro por fuera del FMS.
    valid["_missing"] = (valid["_expected"] - valid["_litres_window"]).clip(lower=0)
    hit = valid[valid["_sfl"].notna()
                & (valid["_missing"] > valid["_sfl"] * float(factor))].copy()
    if hit.empty:
        return pd.DataFrame(columns=UNFUELED_COLS)

    out = pd.DataFrame({
        "equipment_id": hit["_eid"],
        "equipment_description": hit.get("equipment_description"),
        "category": hit["_eid"].map(cat_map),
        "desde": hit["_date_prev"],
        "hasta": hit["_date"],
        "dias": hit["_days"].round(1),
        "smu_delta": hit["_smu_delta"].round(1),
        "smu_type": hit.get("smu_type"),
        "burn_rate_tipico": hit["_rate"].round(2),
        "consumo_esperado": hit["_expected"].round(0),
        "despachado": hit["_litres_window"].round(1),
        "sfl": hit["_sfl"],
        "no_registrado": hit["_missing"].round(0),
        "source_id": hit.get("id"),
    }, columns=UNFUELED_COLS)
    return (out.sort_values("no_registrado", ascending=False)
            .reset_index(drop=True))


# ===========================================================================
# 3. Repostado sin operar (rachas de despachos con SMU congelado)
# ===========================================================================

def fueling_without_activity(movements: pd.DataFrame | None,
                             equipment: pd.DataFrame | None,
                             limits: pd.DataFrame | None,
                             epsilon: float | None = None,
                             min_dispenses: int | None = None,
                             min_days: float | None = None) -> pd.DataFrame:
    """Rachas de despachos consecutivos sin avance de SMU: el equipo no opera
    pero sigue recibiendo combustible. `sobre_sfl` marca las rachas cuyos
    litros acumulados exceden el SFL (fisicamente imposibles sin operar)."""
    eps = config.ACTIVITY_FROZEN_SMU_EPSILON if epsilon is None else epsilon
    min_n = (config.ACTIVITY_FROZEN_MIN_DISPENSES if min_dispenses is None
             else min_dispenses)
    min_d = config.ACTIVITY_FROZEN_MIN_DAYS if min_days is None else min_days

    pairs = _smu_pairs(movements)
    if pairs.empty:
        return pd.DataFrame(columns=FROZEN_COLS)
    pairs = pairs.copy()
    pairs["_frozen"] = pairs["_smu_delta"].abs() <= float(eps)

    sfl = _sfl_by_equipment(movements, limits, equipment)
    cat_map, desc_map = burn_rate._equipment_maps(equipment)

    # Rachas de pares congelados CONSECUTIVOS por equipo, vectorizadas: cada
    # cambio de equipo o de estado congelado/no-congelado abre un bloque nuevo
    # (cumsum); los bloques congelados son las rachas. `pairs` ya viene ordenado
    # por (equipo, fecha) desde `_smu_pairs`. Una racha de K pares abarca K+1
    # lecturas; los litros que entraron SIN operar son TODOS los despachados
    # despues de la primera lectura (incluidos los despachos intermedios sin
    # SMU, via `_litres_window`; la primera lectura pudo reponer consumo
    # legitimo previo).
    block = ((pairs["_eid"] != pairs["_eid"].shift())
             | (pairs["_frozen"] != pairs["_frozen"].shift())).cumsum()
    fz = pairs[pairs["_frozen"]]
    if fz.empty:
        return pd.DataFrame(columns=FROZEN_COLS)
    keys = block[pairs["_frozen"]]

    def _first(col: str) -> pd.Series:
        s = (fz[col] if col in fz.columns else pd.Series(pd.NA, index=fz.index))
        return s.groupby(keys, sort=False).first()

    runs = pd.DataFrame({
        "eid": fz["_eid"].groupby(keys, sort=False).first(),
        "desde": fz["_date_prev"].groupby(keys, sort=False).first(),
        "hasta": fz["_date"].groupby(keys, sort=False).last(),
        "litros": fz["_litres_window"].groupby(keys, sort=False).sum(),
        "n_pares": fz["_eid"].groupby(keys, sort=False).size(),
        "smu_prev": fz["_smu_prev"].groupby(keys, sort=False).first(),
        "desc": _first("equipment_description"),
        "smu_type": _first("smu_type"),
    })
    runs["despachos"] = runs["n_pares"] + 1
    runs["dias"] = (runs["hasta"] - runs["desde"]).dt.total_seconds() / 86400.0
    runs = runs[(runs["despachos"] >= int(min_n)) & (runs["dias"] >= float(min_d))]
    if runs.empty:
        return pd.DataFrame(columns=FROZEN_COLS)

    limit = runs["eid"].map(sfl)
    desc_fb = runs["eid"].map(desc_map)
    desc = runs["desc"].where(runs["desc"].notna(),
                              desc_fb.where(desc_fb.notna(), runs["eid"]))
    out = pd.DataFrame({
        "equipment_id": runs["eid"],
        "equipment_description": desc,
        "category": runs["eid"].map(cat_map),
        "desde": runs["desde"],
        "hasta": runs["hasta"],
        "dias": runs["dias"].round(1),
        "despachos": runs["despachos"].astype(int),
        "litros": runs["litros"].astype(float).round(1),
        "sfl": limit.astype(float),
        "sobre_sfl": (limit.notna() & (runs["litros"] > limit)).astype(bool),
        "smu_estancado": runs["smu_prev"].astype(float).round(1),
        "smu_type": runs["smu_type"],
    }, columns=FROZEN_COLS)
    return (out.sort_values(["sobre_sfl", "litros"], ascending=[False, False])
            .reset_index(drop=True))


# ===========================================================================
# KPIs
# ===========================================================================

def kpis(idle: pd.DataFrame, unfueled: pd.DataFrame, frozen: pd.DataFrame,
         equipment: pd.DataFrame | None, idle_days: float) -> dict:
    n_in = 0
    if equipment is not None and not equipment.empty and "status" in equipment.columns:
        n_in = int((equipment["status"] == config.STATUS_IN).sum())
    n_idle = 0 if idle is None or idle.empty else len(idle)
    n_never = (0 if idle is None or idle.empty
               else int((idle["clase"] == CLASS_NEVER).sum()))
    litros_nr = (0.0 if unfueled is None or unfueled.empty
                 else float(unfueled["no_registrado"].sum()))
    n_over = (0 if frozen is None or frozen.empty
              else int(frozen["sobre_sfl"].map(bool).sum()))
    return {
        "Equipos In Service": n_in,
        f"Fantasmas (≥{idle_days:g} días)": n_idle,
        "Nunca despacharon": n_never,
        "% de la flota IN": round(n_idle / n_in * 100, 1) if n_in else 0.0,
        "Trabaja sin repostar": 0 if unfueled is None or unfueled.empty else len(unfueled),
        "Combustible no registrado (L)": round(litros_nr, 0),
        "Repostado sin operar": 0 if frozen is None or frozen.empty else len(frozen),
        "Rachas sobre SFL": n_over,
    }
