"""Auditoría de Salud de Hardware y Sensores.

Tres detectores que vigilan el hardware del FMS desde los datos replicados
(despachos + log de cambios), sin depender del CSV:

  1. **SMU en regresión / estancado** (`smu_anomalies`). El SMU (horómetro/
     odómetro) SIEMPRE debe avanzar.
       • Regresión: el SMU da un paso atrás respecto a la lectura anterior y NO
         se recupera en la siguiente → el sensor se rompió, reinició o lo
         manipularon. Se reporta una vez por evento. Usa el SMU *calculado*.
       • Estancamiento: el MISMO SMU *crudo* en ≥K despachos consecutivos de un
         equipo In Service abarcando ≥D días → el sensor no envía pulsos al
         AdaptMAC (orden de mantenimiento).

  2. **Re-tagueo sospechoso** (`retag_alerts`). Si un equipo sufre > N cambios de
     RFID en una ventana móvil de D días, el operador podría estar destruyendo
     los tags para forzar despachos manuales / bypass.

  3. **Degradación del medidor** (`meter_health`). Por manguera (`meter_id`), si
     el caudal promedio reciente cae ≥ PCT % respecto a su línea base histórica,
     los filtros están obstruidos o la bomba falla (mantenimiento preventivo).

Todo lo marcado se consolida en una lista accionable de **órdenes de trabajo**
(`work_orders`). Estadística simple y robusta; tolera columnas/insumos ausentes.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from msgq import config
from msgq.core.equipment_analytics import rfid_changes

STATUS_IN = config.STATUS_IN

# Tipos (tokens de celda, traducibles).
TYPE_REGRESSION = "Regresión"
TYPE_STAGNATION = "Estancamiento"

SMU_COLS = [
    "date", "equipment_id", "equipment_description", "category", "equipment_status",
    "tipo", "smu_type", "valor_smu", "valor_referencia", "caida",
    "repeticiones", "dias", "source_id",
]
RETAG_COLS = [
    "equipment_id", "internal_id", "equipment_description", "category",
    "equipment_status", "cambios_30d", "primer_cambio", "ultimo_cambio", "ultimo_tag",
]
METER_COLS = [
    "meter_id", "meter_description", "metrica", "muestras_base", "muestras_reciente",
    "caudal_base", "caudal_reciente", "caida_pct", "degradado",
]
METER_SERIES_COLS = ["date", "meter_id", "caudal"]
WORK_ORDER_COLS = ["tipo", "activo", "severidad", "detalle", "fecha", "accion"]


# ===========================================================================
# Helpers
# ===========================================================================

def _empty(cols: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=cols)


def _blank(s: pd.Series) -> pd.Series:
    txt = s.astype("string").str.strip()
    return s.isna() | txt.eq("") | txt.str.upper().isin(["<NA>", "NAN", "NONE", "UNAUTHORISED"])


def _dispenses(movements: pd.DataFrame | None) -> pd.DataFrame:
    if movements is None or movements.empty:
        return pd.DataFrame()
    mv = movements
    if "kind" in mv.columns:
        mv = mv[mv["kind"] == config.KIND_DISPENSE]
    return mv


def _date_col(mv: pd.DataFrame) -> str:
    return "record_collected_at" if "record_collected_at" in mv.columns else "updated_at"


def _smu_pref(mv: pd.DataFrame, primary: str) -> pd.Series:
    """SMU preferido `primary` (crudo o calculado) con respaldo en `smu_value`."""
    base = pd.to_numeric(mv["smu_value"], errors="coerce") if "smu_value" in mv.columns \
        else pd.Series(np.nan, index=mv.index)
    if primary in mv.columns:
        p = pd.to_numeric(mv[primary], errors="coerce")
        return p.where(p.notna(), base)
    return base


def _equipment_maps(equipment: pd.DataFrame | None) -> tuple[dict, dict, dict]:
    """{id: categoría}, {id: descripción}, {id: estado} desde el maestro."""
    cat, desc, status = {}, {}, {}
    if equipment is None or equipment.empty or "equipment_id" not in equipment.columns:
        return cat, desc, status
    ids = equipment["equipment_id"].astype("string").str.strip()
    for col, out in (("category", cat), ("description", desc), ("status", status)):
        if col in equipment.columns:
            out.update({k: v for k, v in zip(ids, equipment[col]) if k and not pd.isna(v)})
    return cat, desc, status


# ===========================================================================
# 1. SMU: regresión y estancamiento
# ===========================================================================

def smu_anomalies(movements: pd.DataFrame | None,
                  equipment: pd.DataFrame | None = None) -> pd.DataFrame:
    mv = _dispenses(movements)
    if mv.empty or "equipment_id" not in mv.columns or "smu_value" not in mv.columns:
        return _empty(SMU_COLS)
    mv = mv.copy()
    mv["_eid"] = mv["equipment_id"].astype("string").str.strip()
    mv = mv[~_blank(mv["_eid"])]
    mv["_calc"] = _smu_pref(mv, "calculated_smu_value")
    mv["_raw"] = _smu_pref(mv, "raw_smu_value")
    mv["_date"] = pd.to_datetime(mv[_date_col(mv)], errors="coerce")
    mv = mv.dropna(subset=["_date"])
    if mv.empty:
        return _empty(SMU_COLS)

    cat_map, desc_map, status_map = _equipment_maps(equipment)
    mv["_cat"] = mv["_eid"].map(cat_map)
    desc = mv.get("equipment_description")
    if desc is None:
        desc = pd.Series(pd.NA, index=mv.index)
    mv["_desc"] = desc.where(~_blank(desc), mv["_eid"].map(desc_map)).where(
        lambda s: ~_blank(s), mv["_eid"])
    # Estado: el del maestro (vigente); si no, el del movimiento.
    mv["_status"] = mv["_eid"].map(status_map)
    if "equipment_status" in mv.columns:
        mv["_status"] = mv["_status"].where(~_blank(mv["_status"]), mv["equipment_status"])
    mv = mv.sort_values(["_eid", "_date"], kind="mergesort")

    rows = _regression_rows(mv) + _stagnation_rows(mv)
    if not rows:
        return _empty(SMU_COLS)
    return (pd.DataFrame(rows, columns=SMU_COLS)
            .sort_values("date", ascending=False).reset_index(drop=True))


def _regression_rows(mv: pd.DataFrame) -> list[dict]:
    """Cada paso ATRÁS del SMU calculado (cae respecto a la lectura anterior) que
    NO se recupera en la lectura siguiente: el evento de reset/manipulación. Se
    reporta una vez por ocurrencia (no cada despacho posterior)."""
    w = mv[mv["_calc"].notna()].copy()
    if w.empty:
        return []
    eid = w["_eid"]
    w["_prev"] = w["_calc"].groupby(eid).shift(1)
    w["_prev_date"] = w["_date"].groupby(eid).shift(1)
    w["_next"] = w["_calc"].groupby(eid).shift(-1)
    drop = w["_prev"] - w["_calc"]
    step_back = w["_prev"].notna() & (drop >= config.SMU_REGRESSION_MIN_DROP)
    # Recuperado si la SIGUIENTE lectura vuelve al nivel previo (o no hay siguiente
    # -> persiste hasta el final). Un reset real no recupera; un bache, sí.
    recovered = w["_next"].notna() & (w["_next"] >= w["_prev"])
    hit = w[(step_back & ~recovered).fillna(False)]
    rows = []
    for _, r in hit.iterrows():
        days = (r["_date"] - r["_prev_date"]).days if pd.notna(r["_prev_date"]) else None
        rows.append({
            "date": r["_date"], "equipment_id": r["equipment_id"],
            "equipment_description": r["_desc"], "category": r["_cat"],
            "equipment_status": r["_status"], "tipo": TYPE_REGRESSION,
            "smu_type": r.get("smu_type"),
            "valor_smu": round(float(r["_calc"]), 1),
            "valor_referencia": round(float(r["_prev"]), 1),
            "caida": round(float(r["_prev"] - r["_calc"]), 1),
            "repeticiones": pd.NA,
            "dias": int(days) if days is not None else pd.NA,
            "source_id": r.get("id"),
        })
    return rows


def _stagnation_rows(mv: pd.DataFrame) -> list[dict]:
    """Corridas de SMU crudo idéntico en ≥K despachos consecutivos (≥D días) de un
    equipo In Service: el sensor no reporta."""
    w = mv[mv["_raw"].notna()].copy()
    if "_status" in w.columns:
        w = w[w["_status"].astype("string").str.strip() == STATUS_IN]
    if w.empty:
        return []
    boundary = (w["_eid"] != w["_eid"].shift()) | (w["_raw"] != w["_raw"].shift())
    run_id = boundary.cumsum()
    g = w.groupby(run_id)
    runs = g.agg(
        equipment_id=("equipment_id", "first"), _desc=("_desc", "first"),
        _cat=("_cat", "first"), _status=("_status", "first"),
        smu=("_raw", "first"), smu_type=("smu_type", "first"),
        n=("_raw", "size"), first=("_date", "min"), last=("_date", "max"),
        src=("id", "last"),
    )
    runs["dias"] = (runs["last"] - runs["first"]).dt.days
    runs = runs[(runs["n"] >= config.SMU_STAGNATION_MIN_REPEATS)
                & (runs["dias"] >= config.SMU_STAGNATION_MIN_DAYS)]
    rows = []
    for _, r in runs.iterrows():
        rows.append({
            "date": r["last"], "equipment_id": r["equipment_id"],
            "equipment_description": r["_desc"], "category": r["_cat"],
            "equipment_status": r["_status"], "tipo": TYPE_STAGNATION,
            "smu_type": r.get("smu_type"),
            "valor_smu": round(float(r["smu"]), 1), "valor_referencia": pd.NA,
            "caida": pd.NA, "repeticiones": int(r["n"]), "dias": int(r["dias"]),
            "source_id": r.get("src"),
        })
    return rows


# ===========================================================================
# 2. Re-tagueo sospechoso (RFID)
# ===========================================================================

def _max_in_window(ts_ns: np.ndarray, window_ns: int):
    """(máximo de eventos en cualquier ventana móvil, ts inicio, ts fin)."""
    best, lo, hi = 0, None, None
    for j in range(len(ts_ns)):
        i = int(np.searchsorted(ts_ns, ts_ns[j] - window_ns, side="left"))
        c = j - i + 1
        if c > best:
            best, lo, hi = c, ts_ns[i], ts_ns[j]
    return best, lo, hi


def retag_alerts(changes: pd.DataFrame | None,
                 equipment: pd.DataFrame | None = None) -> pd.DataFrame:
    rc = rfid_changes(changes)
    if rc is None or rc.empty:
        return _empty(RETAG_COLS)
    rc = rc[rc["tipo"] == "Cambiado"].copy()
    if rc.empty:
        return _empty(RETAG_COLS)
    rc["changed_at"] = pd.to_datetime(rc["changed_at"], errors="coerce")
    rc = rc.dropna(subset=["changed_at", "record_id"])
    if rc.empty:
        return _empty(RETAG_COLS)
    window_ns = config.RETAG_WINDOW_DAYS * 86_400 * 1_000_000_000

    # Maestro: internal_id -> equipo.
    eq_id, eq_desc, eq_cat, eq_status = {}, {}, {}, {}
    if equipment is not None and not equipment.empty and "internal_id" in equipment.columns:
        iid = equipment["internal_id"].astype("string").str.strip()
        eq_id = dict(zip(iid, equipment.get("equipment_id", pd.Series(index=equipment.index))))
        eq_desc = dict(zip(iid, equipment.get("description", pd.Series(index=equipment.index))))
        eq_cat = dict(zip(iid, equipment.get("category", pd.Series(index=equipment.index))))
        eq_status = dict(zip(iid, equipment.get("status", pd.Series(index=equipment.index))))

    rows = []
    for rid, chunk in rc.groupby("record_id"):
        chunk = chunk.sort_values("changed_at")
        ts = chunk["changed_at"].astype("int64").to_numpy()
        best, lo, hi = _max_in_window(ts, window_ns)
        if best <= config.RETAG_MAX_CHANGES:
            continue
        key = str(rid)
        rows.append({
            "equipment_id": eq_id.get(key, config.UNIDENTIFIED),
            "internal_id": key,
            "equipment_description": eq_desc.get(key),
            "category": eq_cat.get(key),
            "equipment_status": eq_status.get(key),
            "cambios_30d": int(best),
            "primer_cambio": pd.Timestamp(lo),
            "ultimo_cambio": pd.Timestamp(hi),
            "ultimo_tag": chunk.iloc[-1].get("after"),
        })
    if not rows:
        return _empty(RETAG_COLS)
    return (pd.DataFrame(rows, columns=RETAG_COLS)
            .sort_values("cambios_30d", ascending=False).reset_index(drop=True))


# ===========================================================================
# 3. Degradación del medidor (caudal por manguera)
# ===========================================================================

def _flow_setup(movements: pd.DataFrame | None):
    """(despachos con medidor, columna de caudal, etiqueta) o None si no hay datos."""
    mv = _dispenses(movements)
    if mv.empty or "meter_id" not in mv.columns:
        return None
    mv = mv[~_blank(mv["meter_id"])].copy()
    if mv.empty:
        return None
    col, label = None, ""
    for c, lbl in (("average_flow_rate", "Caudal promedio"), ("peak_flow_rate", "Caudal pico")):
        if c in mv.columns:
            vals = pd.to_numeric(mv[c], errors="coerce")
            if vals.notna().any():
                col, label = c, lbl
                mv["_flow"] = vals
                break
    if col is None:
        return None
    mv["_date"] = pd.to_datetime(mv[_date_col(mv)], errors="coerce")
    mv = mv.dropna(subset=["_date", "_flow"])
    mv = mv[mv["_flow"] > 0]
    return None if mv.empty else (mv, col, label)


def meter_health(movements: pd.DataFrame | None) -> pd.DataFrame:
    setup = _flow_setup(movements)
    if setup is None:
        return _empty(METER_COLS)
    mv, _col, label = setup
    now = mv["_date"].max()
    recent_start = now - pd.Timedelta(days=config.METER_RECENT_DAYS)
    rows = []
    for mid, chunk in mv.groupby("meter_id"):
        base = chunk[chunk["_date"] < recent_start]["_flow"]
        recent = chunk[chunk["_date"] >= recent_start]["_flow"]
        if len(base) < config.METER_MIN_SAMPLES or len(recent) < config.METER_MIN_SAMPLES:
            continue
        b = float(base.median())
        r = float(recent.median())
        drop_pct = (b - r) / b * 100 if b > 0 else 0.0
        rows.append({
            "meter_id": mid,
            "meter_description": chunk["meter_description"].dropna().iloc[0]
            if "meter_description" in chunk.columns and chunk["meter_description"].notna().any() else pd.NA,
            "metrica": label,
            "muestras_base": int(len(base)), "muestras_reciente": int(len(recent)),
            "caudal_base": round(b, 1), "caudal_reciente": round(r, 1),
            "caida_pct": round(drop_pct, 1),
            "degradado": bool(b > 0 and drop_pct >= config.METER_DROP_PCT),
        })
    if not rows:
        return _empty(METER_COLS)
    return (pd.DataFrame(rows, columns=METER_COLS)
            .sort_values(["degradado", "caida_pct"], ascending=[False, False])
            .reset_index(drop=True))


def meter_series(movements: pd.DataFrame | None, freq: str = "D") -> pd.DataFrame:
    """Caudal mediano por medidor y día (para la gráfica de tendencia)."""
    setup = _flow_setup(movements)
    if setup is None:
        return _empty(METER_SERIES_COLS)
    mv, _col, _label = setup
    g = (mv.set_index("_date").groupby(["meter_id", pd.Grouper(freq=freq)])["_flow"]
         .median().reset_index())
    g.columns = ["meter_id", "date", "caudal"]
    g["caudal"] = g["caudal"].round(1)
    return g[METER_SERIES_COLS].sort_values(["meter_id", "date"]).reset_index(drop=True)


def meter_available(movements: pd.DataFrame | None) -> bool:
    """¿La réplica tiene datos de medidor? (si no, el endpoint no los expone aún)."""
    mv = _dispenses(movements)
    return (not mv.empty and "meter_id" in mv.columns and not _blank(mv["meter_id"]).all())


# ===========================================================================
# 4. Órdenes de trabajo (consolidado accionable) + KPIs
# ===========================================================================

_ACTION_SMU = "Revisar / reemplazar sensor SMU (horómetro/odómetro)"
_ACTION_RETAG = "Auditar tags y operador; revisar despachos manuales/bypass"
_ACTION_METER = "Revisar filtros / bomba de la manguera"


def work_orders(smu: pd.DataFrame, retag: pd.DataFrame,
                meters: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if smu is not None and not smu.empty:
        for _, r in smu.iterrows():
            reg = r["tipo"] == TYPE_REGRESSION
            if reg:
                detail = (f"SMU cayó {r['caida']} (de {r['valor_referencia']} a "
                          f"{r['valor_smu']}) tras {r['dias']} días")
            else:
                detail = (f"Mismo SMU {r['valor_smu']} en {r['repeticiones']} despachos "
                          f"({r['dias']} días)")
            rows.append({
                "tipo": config.ALERT_SMU_REGRESSION if reg else config.ALERT_SMU_STAGNATION,
                "activo": r["equipment_id"], "severidad": "CRITICAL",
                "detalle": detail, "fecha": r["date"], "accion": _ACTION_SMU,
            })
    if retag is not None and not retag.empty:
        for _, r in retag.iterrows():
            rows.append({
                "tipo": config.ALERT_RETAG, "activo": r["equipment_id"],
                "severidad": "CRITICAL",
                "detalle": f"{r['cambios_30d']} cambios de RFID en {config.RETAG_WINDOW_DAYS} días",
                "fecha": r["ultimo_cambio"], "accion": _ACTION_RETAG,
            })
    if meters is not None and not meters.empty:
        for _, r in meters[meters["degradado"].map(bool)].iterrows():
            rows.append({
                "tipo": config.ALERT_METER_DEGRADED, "activo": r["meter_id"],
                "severidad": "WARNING",
                "detalle": (f"{r['metrica']} cayó {r['caida_pct']}% "
                            f"({r['caudal_base']} → {r['caudal_reciente']} L/min)"),
                "fecha": pd.NaT, "accion": _ACTION_METER,
            })
    if not rows:
        return _empty(WORK_ORDER_COLS)
    out = pd.DataFrame(rows, columns=WORK_ORDER_COLS)
    # Una orden por activo+problema (el ticket accionable), con el evento más
    # reciente. El detalle completo de cada ocurrencia vive en las tablas SMU.
    out = (out.sort_values("fecha", ascending=False, na_position="last")
           .drop_duplicates(subset=["tipo", "activo"], keep="first"))
    return out.sort_values("severidad").reset_index(drop=True)


def summary_kpis(smu: pd.DataFrame, retag: pd.DataFrame, meters: pd.DataFrame,
                 orders: pd.DataFrame) -> dict:
    n_reg = n_stag = 0
    if smu is not None and not smu.empty:
        n_reg = int((smu["tipo"] == TYPE_REGRESSION).sum())
        n_stag = int((smu["tipo"] == TYPE_STAGNATION).sum())
    n_meter = 0 if meters is None or meters.empty else int(meters["degradado"].map(bool).sum())
    return {
        "SMU en regresión": n_reg,
        "SMU sin pulsos": n_stag,
        "Re-tagueo sospechoso": 0 if retag is None else int(len(retag)),
        "Medidores degradados": n_meter,
        "Órdenes de trabajo": 0 if orders is None else int(len(orders)),
    }


# ===========================================================================
# Auditoría completa (una pasada)
# ===========================================================================

@dataclass
class HardwareResult:
    smu: pd.DataFrame
    retag: pd.DataFrame
    meters: pd.DataFrame
    meter_series: pd.DataFrame
    work_orders: pd.DataFrame
    meter_available: bool
    kpis: dict


def audit(movements: pd.DataFrame | None, equipment: pd.DataFrame | None = None,
          changes: pd.DataFrame | None = None) -> HardwareResult:
    smu = smu_anomalies(movements, equipment)
    retag = retag_alerts(changes, equipment)
    meters = meter_health(movements)
    series = meter_series(movements)
    orders = work_orders(smu, retag, meters)
    kpis = summary_kpis(smu, retag, meters, orders)
    return HardwareResult(smu, retag, meters, series, orders,
                          meter_available(movements), kpis)
