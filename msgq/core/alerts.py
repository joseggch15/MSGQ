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
from msgq.core import sfl_audit
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
