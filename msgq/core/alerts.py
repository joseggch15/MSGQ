"""Deteccion de anomalias y calculo de KPIs sobre los datos replicados.

Opera sobre DataFrames ya aplanados (ver `core/transform.py`). Cada detector
devuelve filas homogeneas que se consolidan en una tabla unica de alertas con
columnas:

    timestamp | severity | category | equipment_id | equipment_description |
    type | volume | detail | source_id

Reglas implementadas (derivadas del valor de negocio del FMS):

  1. Modo de transaccion anomalo  — KEY_BYPASS, SUP_OVERRIDE, SPILLAGE, UNAUTH.
  2. Despacho a equipo no operativo — target en 'Out of Service'/'Decommissioned'.
  3. Contaminacion de combustible alta — avg_contamination_{4,6,14} sobre umbral.
  4. Service truck en bypass con volumen acumulado atipico (> ~24.000 L).
  5. Salud de consolas AdaptMAC — offline, comunicacion stale, o key_bypass.
"""
from __future__ import annotations

import pandas as pd

from msgq import config
from msgq.core import (
    activity_audit, burn_rate, hardware_health, product_audit, sfl_audit,
    tag_hopping, volume_deviation,
)
from msgq.i18n import tr_fmt

# --- Severidades -----------------------------------------------------------
SEV_CRITICAL = "CRITICAL"
SEV_WARNING  = "WARNING"
SEV_INFO     = "INFO"

ALERT_COLS = [
    "timestamp", "severity", "category", "equipment_id",
    "equipment_description", "type", "volume", "detail", "source_id",
]


def _empty_alerts() -> pd.DataFrame:
    return pd.DataFrame(columns=ALERT_COLS)


# ===========================================================================
# Detectores sobre movimientos
# ===========================================================================

def detect_movement_alerts(mv: pd.DataFrame) -> pd.DataFrame:
    """Devuelve todas las alertas derivadas de un DataFrame de movimientos."""
    if mv is None or mv.empty:
        return _empty_alerts()

    rows: list[dict] = []
    rows += _anomalous_type(mv)
    rows += _dispense_to_non_operational(mv)
    rows += _high_contamination(mv)
    rows += _service_truck_bypass_volume(mv)

    if not rows:
        return _empty_alerts()
    out = pd.DataFrame(rows, columns=ALERT_COLS)
    out = out.sort_values(["severity", "timestamp"], ascending=[True, False])
    return out.reset_index(drop=True)


def _anomalous_type(mv: pd.DataFrame) -> list[dict]:
    mask = mv["type"].isin(config.ANOMALOUS_TYPES)
    rows = []
    for _, r in mv[mask].iterrows():
        sev = SEV_CRITICAL if r["type"] in (
            config.TYPE_KEY_BYPASS, config.TYPE_UNAUTHORISED
        ) else SEV_WARNING
        rows.append(_row(
            r, sev, "Modo de transaccion anomalo",
            tr_fmt("alert.anomalous_type", type=r["type"], kind=r.get("kind", "")),
        ))
    return rows


def _dispense_to_non_operational(mv: pd.DataFrame) -> list[dict]:
    bad = {config.STATUS_OUT, config.STATUS_DECOM}
    mask = (mv["kind"] == config.KIND_DISPENSE) & (mv["equipment_status"].isin(bad))
    rows = []
    for _, r in mv[mask].iterrows():
        rows.append(_row(
            r, SEV_CRITICAL, "Despacho a equipo no operativo",
            tr_fmt("alert.dispense_non_op", status=r.get("equipment_status")),
        ))
    return rows


def _high_contamination(mv: pd.DataFrame) -> list[dict]:
    rows = []
    channels = [
        ("avg_contamination_4", "4um"),
        ("avg_contamination_6", "6um"),
        ("avg_contamination_14", "14um"),
    ]
    for _, r in mv.iterrows():
        breached = []
        for col, label in channels:
            val = r.get(col)
            thr = config.CONTAMINATION_WARN[label]
            if pd.notna(val) and val >= thr:
                breached.append(f"{label}={int(val)}≥{thr}")
        if breached:
            rows.append(_row(
                r, SEV_WARNING, "Contaminacion de combustible alta",
                tr_fmt("alert.contamination", breaches=", ".join(breached)),
            ))
    return rows


def _service_truck_bypass_volume(mv: pd.DataFrame) -> list[dict]:
    """Suma el volumen de transferencias en bypass por service truck y alerta
    si el acumulado supera el umbral critico de trazabilidad."""
    mask = (
        (mv["type"] == config.TYPE_KEY_BYPASS)
        & (mv["service_truck"].notna())
    )
    sub = mv[mask]
    if sub.empty:
        return []
    rows = []
    grouped = sub.groupby("service_truck")["volume"].sum()
    for truck, total in grouped.items():
        if total >= config.SERVICE_TRUCK_BYPASS_VOLUME_L:
            last = sub[sub["service_truck"] == truck].iloc[-1]
            rows.append({
                "timestamp": last.get("updated_at"),
                "severity": SEV_CRITICAL,
                "category": "Service truck en bypass (volumen acumulado)",
                "equipment_id": truck,
                "equipment_description": last.get("equipment_description"),
                "type": config.TYPE_KEY_BYPASS,
                "volume": round(float(total), 1),
                "detail": tr_fmt("alert.bypass_volume", total=total,
                                 threshold=config.SERVICE_TRUCK_BYPASS_VOLUME_L),
                "source_id": last.get("id"),
            })
    return rows


def _row(r: pd.Series, severity: str, category: str, detail: str) -> dict:
    """Construye una fila de alerta a partir de una fila de movimiento."""
    return {
        "timestamp": r.get("updated_at"),
        "severity": severity,
        "category": category,
        "equipment_id": r.get("equipment_id"),
        "equipment_description": r.get("equipment_description"),
        "type": r.get("type"),
        "volume": r.get("volume"),
        "detail": detail,
        "source_id": r.get("id"),
    }


# ===========================================================================
# Detector sobre el Safe Fill Level (SFL)
# ===========================================================================

def detect_sfl_alerts(mv: pd.DataFrame, limits: pd.DataFrame) -> pd.DataFrame:
    """Alertas CRITICAS por despachos cuyo volumen excede el Safe Fill Level del
    equipo para ese producto (sobrellenado). Reutiliza `sfl_audit.exceedances`."""
    exc = sfl_audit.exceedances(mv, limits)
    if exc is None or exc.empty:
        return _empty_alerts()
    rows = []
    for _, r in exc.iterrows():
        rows.append({
            "timestamp": r.get("date"),
            "severity": SEV_CRITICAL,
            "category": config.ALERT_SFL_EXCEEDED,
            "equipment_id": r.get("equipment_id"),
            "equipment_description": r.get("equipment_description"),
            "type": config.KIND_DISPENSE,
            "volume": r.get("volume"),
            "detail": tr_fmt("alert.sfl_exceedance",
                             volume=float(r.get("volume") or 0.0),
                             sfl=float(r.get("sfl") or 0.0),
                             product=r.get("product") or "",
                             excess=float(r.get("excess") or 0.0)),
            "source_id": r.get("source_id"),
        })
    return pd.DataFrame(rows, columns=ALERT_COLS).reset_index(drop=True)


def detect_sfl_conflict_alerts(mv: pd.DataFrame, limits: pd.DataFrame) -> pd.DataFrame:
    """Alertas CRITICAS por despachos SIN equipo valido (no_equip / Unauthorised)
    cuyo volumen supera el SFL maximo de la flota para ese producto: combustible
    sin trazabilidad y por encima de lo seguro para cualquier equipo. Los demas
    no_equip/Unauthorised ya los marca `_anomalous_type`, no se duplican."""
    conf = sfl_audit.unattributed_conflicts(mv, limits)
    if conf is None or conf.empty:
        return _empty_alerts()
    conf = conf[conf["over_max"].map(bool)]
    if conf.empty:
        return _empty_alerts()
    rows = []
    for _, r in conf.iterrows():
        rows.append({
            "timestamp": r.get("date"),
            "severity": SEV_CRITICAL,
            "category": config.ALERT_SFL_CONFLICT,
            "equipment_id": r.get("equipment_id"),
            "equipment_description": None,
            "type": r.get("type") or config.KIND_DISPENSE,
            "volume": r.get("volume"),
            "detail": tr_fmt("alert.sfl_conflict",
                             volume=float(r.get("volume") or 0.0),
                             product=r.get("product") or "",
                             fleet_max=float(r.get("fleet_max_sfl") or 0.0)),
            "source_id": r.get("source_id"),
        })
    return pd.DataFrame(rows, columns=ALERT_COLS).reset_index(drop=True)


# ===========================================================================
# Detector de coherencia Producto <-> Equipo (posible tag clonado)
# ===========================================================================

def detect_product_mismatch_alerts(mv: pd.DataFrame, limits: pd.DataFrame,
                                   product_history: pd.DataFrame | None = None) -> pd.DataFrame:
    """Alertas por despachos cuyo producto es AJENO al equipo (ver
    `core/product_audit`). Producto de OTRA clase que la del equipo (combustible
    vs fluido) -> CRITICO (posible tag clonado); mismo-clase fuera del conjunto
    conocido -> ADVERTENCIA (probable equipo mal configurado en el maestro).

    Requiere TODO el historico de movimientos: la legitimidad de un producto se
    juzga, entre otras cosas, por su huella de uso en el propio equipo (asi no se
    marcan productos que estuvieron habilitados y luego se deshabilitaron)."""
    mm = product_audit.mismatches(mv, limits, product_history)
    if mm is None or mm.empty:
        return _empty_alerts()
    rows = []
    for _, r in mm.iterrows():
        if bool(r.get("cross_class")):
            rows.append({
                "timestamp": r.get("date"), "severity": SEV_CRITICAL,
                "category": config.ALERT_PRODUCT_FOREIGN,
                "equipment_id": r.get("equipment_id"),
                "equipment_description": r.get("equipment_description"),
                "type": config.KIND_DISPENSE, "volume": r.get("volume"),
                "detail": tr_fmt("alert.product_foreign",
                                 product=r.get("product") or "",
                                 pclass=r.get("product_class") or "",
                                 expected=r.get("expected_classes") or "?"),
                "source_id": r.get("source_id"),
            })
        else:
            rows.append({
                "timestamp": r.get("date"), "severity": SEV_WARNING,
                "category": config.ALERT_PRODUCT_OFF_MASTER,
                "equipment_id": r.get("equipment_id"),
                "equipment_description": r.get("equipment_description"),
                "type": config.KIND_DISPENSE, "volume": r.get("volume"),
                "detail": tr_fmt("alert.product_off_master",
                                 product=r.get("product") or "",
                                 expected=r.get("expected_products") or "?"),
                "source_id": r.get("source_id"),
            })
    return pd.DataFrame(rows, columns=ALERT_COLS).reset_index(drop=True)


# ===========================================================================
# Detector de desviacion de volumen en entregas (medidor vs guia)
# ===========================================================================

def detect_volume_deviation_alerts(mv: pd.DataFrame) -> pd.DataFrame:
    """Alertas por entregas cuya desviacion entre el volumen MEDIDO y el DIGITADO
    en campo (de la guia del camion) supera el umbral. >= umbral critico ->
    CRITICO (sobre-facturacion grave / medidor muy descalibrado); >= umbral base
    -> ADVERTENCIA. Reutiliza `core/volume_deviation` (ver su docstring)."""
    fl = volume_deviation.flagged(volume_deviation.deviations(mv))
    if fl is None or fl.empty:
        return _empty_alerts()
    rows = []
    for _, r in fl.iterrows():
        dev_pct = float(r.get("deviation_pct") or 0.0)
        sev = (SEV_CRITICAL if abs(dev_pct) >= config.DELIVERY_VOLUME_DEVIATION_CRITICAL_PCT
               else SEV_WARNING)
        rows.append({
            "timestamp": r.get("date"),
            "severity": sev,
            "category": config.ALERT_VOLUME_DEVIATION,
            "equipment_id": r.get("tank"),
            "equipment_description": None,
            "type": r.get("transaction_type") or config.KIND_DELIVERY,
            "volume": r.get("measured_volume"),
            "detail": tr_fmt("alert.volume_deviation",
                             measured=float(r.get("measured_volume") or 0.0),
                             field=float(r.get("field_volume") or 0.0),
                             dev=dev_pct,
                             diff=float(r.get("deviation_l") or 0.0)),
            "source_id": r.get("source_id"),
        })
    return pd.DataFrame(rows, columns=ALERT_COLS).reset_index(drop=True)


# ===========================================================================
# Detector de tag hopping ("el tag en el bolsillo")
# ===========================================================================

def detect_tag_hopping_alerts(mv: pd.DataFrame, equipment: pd.DataFrame | None = None,
                              point_coords: dict | None = None) -> pd.DataFrame:
    """Alertas por el MISMO tag (equipo) despachando en dos lugares en un lapso
    imposible. Solapamiento temporal o teletransporte -> CRITICO; velocidad
    implicita implausible -> ADVERTENCIA. Reutiliza `core/tag_hopping` (que mira
    TODO el historico de despachos para ordenar cada equipo en el tiempo)."""
    res = tag_hopping.audit(mv, equipment, point_coords)
    ev = res.events
    if ev is None or ev.empty:
        return _empty_alerts()
    rows = []
    for _, r in ev.iterrows():
        by_speed = r.get("reason") == config.TAG_HOP_REASON_SPEED
        if by_speed:
            spd = r.get("speed_kmh")
            metric = (tr_fmt("alert.tag_hop_speed", speed=float(spd),
                             dist=float(r.get("distance_km") or 0.0))
                      if spd is not None
                      else tr_fmt("alert.tag_hop_teleport", dist=float(r.get("distance_km") or 0.0)))
        else:
            metric = tr_fmt("alert.tag_hop_overlap")
        rows.append({
            "timestamp": r.get("date"),
            "severity": r.get("severity") or SEV_CRITICAL,
            "category": config.ALERT_TAG_HOPPING,
            "equipment_id": r.get("equipment_id"),
            "equipment_description": r.get("equipment_description"),
            "type": "TAG",
            "volume": None,
            "detail": tr_fmt("alert.tag_hopping",
                             loc_prev=r.get("location_prev") or "?",
                             loc=r.get("location") or "?",
                             gap=float(r.get("gap_min") or 0.0),
                             metric=metric),
            "source_id": r.get("source_id"),
        })
    return pd.DataFrame(rows, columns=ALERT_COLS).reset_index(drop=True)


# ===========================================================================
# Detector sobre el Burn Rate (consumo L/h)
# ===========================================================================

def detect_burn_rate_alerts(mv: pd.DataFrame,
                            equipment: pd.DataFrame | None = None) -> pd.DataFrame:
    """Alertas por equipos cuyo burn rate se desvía de su categoría. 'Alto'
    (sobre-consumo: posible fuga/robo/falla) es CRÍTICO; 'Bajo' (sub-consumo:
    posible medidor mal o despachos sin registrar) es ADVERTENCIA. El burn rate y
    su línea base se reconstruyen de los despachos (ver `core/burn_rate`)."""
    res = burn_rate.audit(mv, equipment)
    eq_anom = res.equipment_anomalies
    if eq_anom is None or eq_anom.empty:
        return _empty_alerts()
    # Fecha del último intervalo por equipo (para datar la alerta).
    last_date = {}
    if res.samples is not None and not res.samples.empty:
        last_date = res.samples.groupby("equipment_id")["date"].max().to_dict()
    rows = []
    for _, r in eq_anom.iterrows():
        eid = r.get("equipment_id")
        sev = SEV_CRITICAL if r.get("Dirección") == "Alto" else SEV_WARNING
        rows.append({
            "timestamp": last_date.get(eid),
            "severity": sev,
            "category": config.ALERT_BURN_RATE_ANOMALY,
            "equipment_id": eid,
            "equipment_description": r.get("equipment_description"),
            "type": "BURN_RATE",
            "volume": None,
            "detail": tr_fmt("alert.burn_rate",
                             rate=float(r.get("Burn rate (L/h)") or 0.0),
                             baseline=float(r.get("Baseline categoría (L/h)") or 0.0),
                             dev=float(r.get("Desviación %") or 0.0)),
            "source_id": eid,
        })
    return pd.DataFrame(rows, columns=ALERT_COLS).reset_index(drop=True)


# ===========================================================================
# Detector sobre la salud de hardware y sensores
# ===========================================================================

def detect_hardware_alerts(mv: pd.DataFrame, equipment: pd.DataFrame | None = None,
                           changes: pd.DataFrame | None = None) -> pd.DataFrame:
    """Alertas de salud de hardware: SMU en regresión/estancado (CRÍTICO),
    re-tagueo RFID sospechoso (CRÍTICO) y caudal de medidor degradado
    (ADVERTENCIA). Reutiliza `core/hardware_health.audit`."""
    res = hardware_health.audit(mv, equipment, changes)
    rows: list[dict] = []
    for _, r in res.smu.iterrows():
        regression = r.get("tipo") == hardware_health.TYPE_REGRESSION
        if regression:
            detail = tr_fmt("alert.smu_regression",
                            drop=float(r.get("caida") or 0.0),
                            ref=float(r.get("valor_referencia") or 0.0),
                            val=float(r.get("valor_smu") or 0.0),
                            days=int(r.get("dias") or 0))
            category = config.ALERT_SMU_REGRESSION
        else:
            detail = tr_fmt("alert.smu_stagnation",
                            val=float(r.get("valor_smu") or 0.0),
                            repeats=int(r.get("repeticiones") or 0),
                            days=int(r.get("dias") or 0))
            category = config.ALERT_SMU_STAGNATION
        rows.append({
            "timestamp": r.get("date"), "severity": SEV_CRITICAL, "category": category,
            "equipment_id": r.get("equipment_id"),
            "equipment_description": r.get("equipment_description"),
            "type": "SMU", "volume": None, "detail": detail, "source_id": r.get("source_id"),
        })
    for _, r in res.retag.iterrows():
        rows.append({
            "timestamp": r.get("ultimo_cambio"), "severity": SEV_CRITICAL,
            "category": config.ALERT_RETAG, "equipment_id": r.get("equipment_id"),
            "equipment_description": r.get("equipment_description"),
            "type": "RFID", "volume": None,
            "detail": tr_fmt("alert.retag", n=int(r.get("cambios_30d") or 0),
                             window=config.RETAG_WINDOW_DAYS),
            "source_id": r.get("internal_id"),
        })
    degraded = (res.meters[res.meters["degradado"].map(bool)]
                if not res.meters.empty else res.meters)
    for _, r in degraded.iterrows():
        rows.append({
            "timestamp": None, "severity": SEV_WARNING,
            "category": config.ALERT_METER_DEGRADED, "equipment_id": r.get("meter_id"),
            "equipment_description": r.get("meter_description"),
            "type": "METER", "volume": None,
            "detail": tr_fmt("alert.meter_degraded", drop=float(r.get("caida_pct") or 0.0),
                             base=float(r.get("caudal_base") or 0.0),
                             recent=float(r.get("caudal_reciente") or 0.0)),
            "source_id": r.get("meter_id"),
        })
    if not rows:
        return _empty_alerts()
    return pd.DataFrame(rows, columns=ALERT_COLS).reset_index(drop=True)


# ===========================================================================
# Detector de actividad (fantasmas / coherencia actividad<->combustible)
# ===========================================================================

def detect_activity_alerts(mv: pd.DataFrame, equipment: pd.DataFrame | None = None,
                           limits: pd.DataFrame | None = None,
                           now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Alertas de la auditoría de actividad (`core/activity_audit`):

      • Equipo fantasma (ADVERTENCIA): 'In Service' sin despachos >= umbral
        crítico (o que nunca despachó) — distorsiona los KPIs de disponibilidad.
      • Trabaja sin repostar (CRÍTICO): consumo esperado por avance de SMU
        supera el SFL sin despacho de por medio -> combustible no registrado.
      • Repostado sin operar: racha de despachos con SMU congelado; CRÍTICO si
        los litros acumulados exceden el SFL, ADVERTENCIA en caso contrario.
    """
    rows: list[dict] = []

    idle = activity_audit.idle_assets(
        equipment, mv, now=now, min_days=config.IDLE_ASSET_DAYS_CRITICAL)
    for _, r in idle.iterrows():
        never = r.get("clase") == activity_audit.CLASS_NEVER
        last = r.get("ultimo_despacho")
        detail = (tr_fmt("alert.idle_never") if never else
                  tr_fmt("alert.idle_asset",
                         days=float(r.get("dias_sin_despachar") or 0.0),
                         last="" if pd.isna(last) else f"{last:%d/%m/%Y}"))
        rows.append({
            "timestamp": None if pd.isna(last) else last,
            "severity": SEV_WARNING, "category": config.ALERT_IDLE_ASSET,
            "equipment_id": r.get("equipment_id"),
            "equipment_description": r.get("description"),
            "type": "IDLE", "volume": None, "detail": detail,
            "source_id": f"idle:{r.get('equipment_id')}",
        })

    unfueled = activity_audit.unfueled_activity(mv, equipment, limits)
    for _, r in unfueled.iterrows():
        rows.append({
            "timestamp": r.get("hasta"), "severity": SEV_CRITICAL,
            "category": config.ALERT_UNFUELED_ACTIVITY,
            "equipment_id": r.get("equipment_id"),
            "equipment_description": r.get("equipment_description"),
            "type": "SMU", "volume": r.get("no_registrado"),
            "detail": tr_fmt("alert.unfueled_activity",
                             smu=float(r.get("smu_delta") or 0.0),
                             unit=r.get("smu_type") or "SMU",
                             expected=float(r.get("consumo_esperado") or 0.0),
                             sfl=float(r.get("sfl") or 0.0),
                             missing=float(r.get("no_registrado") or 0.0)),
            "source_id": r.get("source_id"),
        })

    frozen = activity_audit.fueling_without_activity(mv, equipment, limits)
    for _, r in frozen.iterrows():
        over = bool(r.get("sobre_sfl"))
        sfl = r.get("sfl")
        over_txt = ("" if not over or sfl is None or pd.isna(sfl)
                    else f" — > SFL {float(sfl):,.0f} L")
        rows.append({
            "timestamp": r.get("hasta"),
            "severity": SEV_CRITICAL if over else SEV_WARNING,
            "category": config.ALERT_FUELING_IDLE,
            "equipment_id": r.get("equipment_id"),
            "equipment_description": r.get("equipment_description"),
            "type": "SMU", "volume": r.get("litros"),
            "detail": tr_fmt("alert.fueling_idle",
                             n=int(r.get("despachos") or 0),
                             litres=float(r.get("litros") or 0.0),
                             days=float(r.get("dias") or 0.0),
                             smu=float(r.get("smu_estancado") or 0.0),
                             over=over_txt),
            "source_id": f"frozen:{r.get('equipment_id')}:{r.get('desde')}",
        })

    if not rows:
        return _empty_alerts()
    return pd.DataFrame(rows, columns=ALERT_COLS).reset_index(drop=True)


# ===========================================================================
# Detector sobre consolas AdaptMAC
# ===========================================================================

def detect_adaptmac_alerts(mac: pd.DataFrame, now: pd.Timestamp | None = None) -> pd.DataFrame:
    if mac is None or mac.empty:
        return _empty_alerts()
    now = now or pd.Timestamp.now()
    stale_delta = pd.Timedelta(minutes=config.ADAPTMAC_STALE_MINUTES)
    rows = []
    for _, r in mac.iterrows():
        code = r.get("code")
        if r.get("key_bypass"):
            rows.append(_mac_row(r, SEV_CRITICAL, "Consola en modo bypass",
                                  tr_fmt("alert.mac_bypass", code=code)))
        if r.get("online") is False:
            rows.append(_mac_row(r, SEV_WARNING, "Consola offline",
                                  tr_fmt("alert.mac_offline", code=code)))
        else:
            last = r.get("last_successful_comms")
            if pd.notna(last) and (now - last) > stale_delta:
                mins = int((now - last).total_seconds() // 60)
                rows.append(_mac_row(r, SEV_WARNING, "Comunicacion stale",
                                     tr_fmt("alert.mac_stale", code=code, mins=mins)))
    if not rows:
        return _empty_alerts()
    return pd.DataFrame(rows, columns=ALERT_COLS).reset_index(drop=True)


def _mac_row(r: pd.Series, severity: str, category: str, detail: str) -> dict:
    return {
        "timestamp": r.get("updated_at"),
        "severity": severity,
        "category": category,
        "equipment_id": r.get("code"),
        "equipment_description": r.get("description"),
        "type": "ADAPTMAC",
        "volume": None,
        "detail": detail,
        "source_id": r.get("code"),
    }


# ===========================================================================
# Resumen y KPIs
# ===========================================================================

def combine(*frames: pd.DataFrame) -> pd.DataFrame:
    """Une varias tablas de alertas (ignorando vacias) y ordena por severidad."""
    non_empty = []
    for f in frames:
        if f is not None and not f.empty:
            f = f.copy()
            # Evita ambiguedad de dtype al concatenar (p. ej. volume todo-NA
            # en alertas de consolas vs numerico en alertas de movimientos).
            f["volume"] = pd.to_numeric(f.get("volume"), errors="coerce")
            non_empty.append(f)
    if not non_empty:
        return _empty_alerts()
    out = pd.concat(non_empty, ignore_index=True)
    return out.sort_values(
        ["severity", "timestamp"], ascending=[True, False]
    ).reset_index(drop=True)


def alert_summary(alerts: pd.DataFrame) -> pd.DataFrame:
    """Conteo de alertas por categoria y severidad (resumen ejecutivo)."""
    if alerts is None or alerts.empty:
        return pd.DataFrame(columns=["Categoria", "Severidad", "Alertas"])
    g = (alerts.groupby(["category", "severity"]).size()
         .reset_index(name="Alertas")
         .rename(columns={"category": "Categoria", "severity": "Severidad"}))
    return g.sort_values("Alertas", ascending=False).reset_index(drop=True)


def compute_kpis(mv: pd.DataFrame, eq: pd.DataFrame,
                 mac: pd.DataFrame, alerts: pd.DataFrame) -> dict[str, int | float]:
    """KPIs para la franja superior del dashboard."""
    def _n(df):
        return 0 if df is None or df.empty else len(df)

    total_volume = float(mv["volume"].sum()) if _n(mv) else 0.0
    n_critical = 0 if _n(alerts) == 0 else int((alerts["severity"] == SEV_CRITICAL).sum())
    n_warning = 0 if _n(alerts) == 0 else int((alerts["severity"] == SEV_WARNING).sum())

    eq_in = eq_out = 0
    if _n(eq):
        eq_in = int((eq["status"] == config.STATUS_IN).sum())
        eq_out = int((eq["status"] == config.STATUS_OUT).sum())

    mac_online = mac_total = 0
    if _n(mac):
        mac_total = len(mac)
        mac_online = int((mac["online"] == True).sum())  # noqa: E712

    return {
        "movimientos": _n(mv),
        "volumen_total": total_volume,
        "criticas": n_critical,
        "advertencias": n_warning,
        "equipos_in_service": eq_in,
        "equipos_out_service": eq_out,
        "consolas_online": mac_online,
        "consolas_total": mac_total,
    }
