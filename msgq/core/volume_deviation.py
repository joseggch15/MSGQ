"""Auditoria de Desviacion de Volumen en Entregas (medidor vs guia).

En cada entrega (delivery) el FMS guarda DOS volumenes:

  • el MEDIDO (`volume`): lo que conto el medidor digital de la linea, o el gauge
    del tanque en las entregas GAUGED (alli `volume` = lectura del gauge); y
  • el DIGITADO en campo (`secondary_volume`): lo que el operador tecleo a partir
    de la guia/albaran del camion de combustible.

Una diferencia sostenida entre ambos significa que el proveedor factura litros que
nunca entraron al tanque (sobre-facturacion) o que el medidor esta descalibrado.
Este modulo calcula la desviacion relativa entre los dos volumenes (sobre el
MEDIDO, la referencia fisica) y marca las entregas que superan el umbral del
negocio (`DELIVERY_VOLUME_DEVIATION_PCT`, 1%).

Verificado contra los CSV reales de Merian: las entregas MANUAL traen `volume`
(=Metered) y `secondary_volume` (=Field Entered) por separado (p. ej. 39.810,5 L
medidos vs 40.000 de guia = 0,48%, dentro de tolerancia), y las GAUGED comparan el
gauge contra la guia (8-11% en la muestra: si se marcan).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from msgq import config

# --- Una fila por entrega con ambos volumenes y su desviacion ----------------
DEVIATION_COLS = [
    "date", "tank", "product", "transaction_type",
    "measured_volume", "field_volume", "deviation_l", "deviation_pct",
    "direction", "measured_source", "field_source", "source_id", "flagged",
]

# --- Resumen por tanque de destino -------------------------------------------
BY_TANK_COLS = [
    "tank", "Entregas", "Marcadas", "Volumen medido (L)", "Volumen guia (L)",
    "Sobre-facturación neta (L)", "Peor desviación %",
]


def _empty(cols: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=cols)


def deviations(movements: pd.DataFrame | None) -> pd.DataFrame:
    """Una fila por entrega que trae AMBOS volumenes (medido y de guia), con la
    desviacion relativa y la marca `flagged` si supera el umbral.

    `deviation_l` y `deviation_pct` van con signo: POSITIVO = la guia reclama mas
    de lo medido (sobre-facturacion, el caso de fraude); negativo = la guia esta
    por debajo de lo medido (medidor leyo de mas / sub-registro). Se descartan las
    entregas por debajo de `DELIVERY_MIN_VOLUME_L` (un % enorme sobre pocos litros
    no es relevante) y las que no traen los dos volumenes."""
    if movements is None or movements.empty:
        return _empty(DEVIATION_COLS)
    mv = movements
    if "kind" in mv.columns:
        mv = mv[mv["kind"] == config.KIND_DELIVERY]
    if mv.empty or not {"volume", "secondary_volume"}.issubset(mv.columns):
        return _empty(DEVIATION_COLS)

    d = mv.copy()
    measured = pd.to_numeric(d["volume"], errors="coerce")
    field = pd.to_numeric(d["secondary_volume"], errors="coerce")
    valid = measured.notna() & field.notna() & (measured > 0) & (field > 0)
    d = d[valid].copy()
    if d.empty:
        return _empty(DEVIATION_COLS)
    measured = measured[valid]
    field = field[valid]

    dev_l = field - measured                       # + = guia cobra de mas
    dev_pct = (dev_l / measured * 100.0)
    direction = pd.Series("", index=d.index, dtype="object")
    direction[dev_l > 0] = config.DELIVERY_DIR_OVERBILL
    direction[dev_l < 0] = config.DELIVERY_DIR_UNDERBILL
    flagged = ((measured >= config.DELIVERY_MIN_VOLUME_L)
               & (dev_pct.abs() >= config.DELIVERY_VOLUME_DEVIATION_PCT))

    date_col = "record_collected_at" if "record_collected_at" in d.columns else "updated_at"
    out = pd.DataFrame({
        "date":             pd.to_datetime(d.get(date_col), errors="coerce"),
        "tank":             d.get("tank"),
        "product":          d.get("product"),
        "transaction_type": d.get("type"),
        "measured_volume":  measured.round(1),
        "field_volume":     field.round(1),
        "deviation_l":      dev_l.round(1),
        "deviation_pct":    dev_pct.round(2),
        "direction":        direction,
        "measured_source":  d.get("primary_volume_source"),
        "field_source":     d.get("secondary_volume_source"),
        "source_id":        d.get("id"),
        "flagged":          flagged.fillna(False).astype(bool),
    }, columns=DEVIATION_COLS)
    # Marcadas primero, luego por magnitud de la desviacion.
    out["_abs"] = out["deviation_pct"].abs()
    out = out.sort_values(["flagged", "_abs"], ascending=[False, False])
    return out.drop(columns="_abs").reset_index(drop=True)


def flagged(dev: pd.DataFrame | None) -> pd.DataFrame:
    """Solo las entregas marcadas (desviacion >= umbral)."""
    if dev is None or dev.empty or "flagged" not in dev.columns:
        return _empty(DEVIATION_COLS)
    return dev[dev["flagged"].map(bool)].reset_index(drop=True)


def by_tank(dev: pd.DataFrame | None) -> pd.DataFrame:
    """Resumen por tanque de destino: entregas, marcadas, volumenes y peor
    desviacion. `Sobre-facturación neta` = suma de los litros que la guia reclama
    de mas (positivos) menos los que reclama de menos: el saldo a favor/contra."""
    if dev is None or dev.empty:
        return _empty(BY_TANK_COLS)
    rows = []
    for tank, chunk in dev.groupby(dev["tank"].astype("string"), dropna=False):
        rows.append({
            "tank": tank if tank is not None else "(sin dato)",
            "Entregas": int(len(chunk)),
            "Marcadas": int(chunk["flagged"].map(bool).sum()),
            "Volumen medido (L)": round(float(chunk["measured_volume"].sum()), 1),
            "Volumen guia (L)": round(float(chunk["field_volume"].sum()), 1),
            "Sobre-facturación neta (L)": round(float(chunk["deviation_l"].sum()), 1),
            "Peor desviación %": round(float(chunk["deviation_pct"].abs().max()), 2),
        })
    return (pd.DataFrame(rows, columns=BY_TANK_COLS)
            .sort_values("Peor desviación %", ascending=False)
            .reset_index(drop=True))


def summary_kpis(dev: pd.DataFrame | None) -> dict:
    """KPIs de la franja superior de la ventana."""
    if dev is None or dev.empty:
        return {
            "Entregas analizadas": 0, "Entregas marcadas": 0,
            "Peor desviación %": 0.0, "Volumen en disputa (L)": 0.0,
            "Sobre-facturación neta (L)": 0.0,
        }
    fl = dev[dev["flagged"].map(bool)]
    worst = float(dev["deviation_pct"].abs().max()) if not dev.empty else 0.0
    disputed = float(fl["deviation_l"].abs().sum()) if not fl.empty else 0.0
    net_over = float(fl["deviation_l"].sum()) if not fl.empty else 0.0
    return {
        "Entregas analizadas": int(len(dev)),
        "Entregas marcadas": int(len(fl)),
        "Peor desviación %": round(worst, 2),
        "Volumen en disputa (L)": round(disputed, 1),
        "Sobre-facturación neta (L)": round(net_over, 1),
    }


@dataclass
class VolumeDeviationResult:
    """Resultado de la auditoria de desviacion de volumen, en una sola pasada:
    detalle por entrega, subconjunto marcado, resumen por tanque y KPIs."""
    deviations: pd.DataFrame
    flagged: pd.DataFrame
    by_tank: pd.DataFrame
    kpis: dict


def audit(movements: pd.DataFrame | None) -> VolumeDeviationResult:
    """Calcula TODA la auditoria de desviacion de volumen de una vez (la GUI la usa
    asi para no recomputar el detalle varias veces)."""
    dev = deviations(movements)
    return VolumeDeviationResult(dev, flagged(dev), by_tank(dev), summary_kpis(dev))
