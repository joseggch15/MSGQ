"""Analitica de tanques y consumo — lado TRANSACCIONES (paso 1 del modulo).

Reproduce la 'mitad de transacciones' del FMS Tank Analyzer (proyecto TLS) pero
sobre los DataFrames que MSGQ ya replica en tiempo real desde el endpoint
(movimientos + equipos), sin depender de nuevas consultas:

  • Consumo / despachos por producto, tanque, cost centre y por dimension del
    equipo (grupo / categoria / departamento, via join al inventario).
  • Burn rate (volumen despachado por periodo) y top consumidores.
  • Flujo por tanque y por periodo: inflow (entregas) vs outflow
    (despachos + transferencias) — el lado 'movimientos' de la reconciliacion.

Separacion por circuito (Diesel / Gasolina), igual que TLS: nunca mezcla
productos. El PRODUCTO viaja por-movimiento (en el inventario queda NA), por eso
la clasificacion de circuito se hace sobre `movements["product"]`.

La 'mitad de stock medido' (opening/closing, niveles) la expone el endpoint via
las conexiones `tanks` y `reconciliations` (confirmado por introspeccion) y se
integrara en un paso posterior; aqui no se calcula.
"""
from __future__ import annotations

import pandas as pd

from msgq import config

DISPENSE = config.KIND_DISPENSE
DELIVERY = config.KIND_DELIVERY
TRANSFER = config.KIND_TRANSFER

_NO_DATA = "(sin dato)"


# ===========================================================================
# Circuitos (Diesel / Gasolina) — alineado con TLS
# ===========================================================================

def classify_circuit(product) -> str | None:
    """Clasifica un texto de producto en circuito ('Diesel' / 'Gasolina').

    Devuelve None para productos que no son combustible de circuito (lubricantes,
    refrigerante, etc.) o vacios.
    """
    if product is None:
        return None
    v = str(product).strip().upper()
    if not v:
        return None
    if "DIESEL" in v:
        return "Diesel"
    if any(k in v for k in ("UNLEAD", "GASOL", "PETROL", "ULP")):
        return "Gasolina"
    return None


def filter_circuit(movements: pd.DataFrame, circuit: str | None) -> pd.DataFrame:
    """Filtra los movimientos al circuito indicado (None/''/'Todos' = sin filtro)."""
    if (circuit in (None, "", "Todos")
            or movements is None or movements.empty
            or "product" not in movements.columns):
        return movements if movements is not None else pd.DataFrame()
    return movements[movements["product"].map(classify_circuit) == circuit]


# ===========================================================================
# Helpers
# ===========================================================================

def _by_kind(movements: pd.DataFrame, kind: str) -> pd.DataFrame:
    if movements is None or movements.empty or "kind" not in movements.columns:
        return pd.DataFrame()
    return movements[movements["kind"] == kind]


def _key(series: pd.Series) -> pd.Series:
    """Normaliza una columna categorica: vacio / NA -> '(sin dato)'."""
    return (series.astype("string").str.strip()
            .replace({"": _NO_DATA}).fillna(_NO_DATA))


def _group_volume(df: pd.DataFrame, key_col: str, label: str) -> pd.DataFrame:
    """Agrupa por `key_col` -> conteo de despachos + suma de volumen."""
    cols = [label, "Despachos", "Volumen (L)"]
    if df is None or df.empty or "volume" not in df.columns:
        return pd.DataFrame(columns=cols)
    work = df.copy()
    work["_k"] = _key(work[key_col])
    g = (work.groupby("_k")
         .agg(Despachos=("volume", "size"), **{"Volumen (L)": ("volume", "sum")})
         .reset_index().rename(columns={"_k": label}))
    g["Volumen (L)"] = g["Volumen (L)"].astype(float).round(1)
    return g.sort_values("Volumen (L)", ascending=False).reset_index(drop=True)


# ===========================================================================
# Consumo / despachos (solo movimientos DISPENSE)
# ===========================================================================

def consumption_by_product(movements: pd.DataFrame) -> pd.DataFrame:
    return _group_volume(_by_kind(movements, DISPENSE), "product", "Producto")


def consumption_by_tank(movements: pd.DataFrame) -> pd.DataFrame:
    return _group_volume(_by_kind(movements, DISPENSE), "tank", "Tanque")


def consumption_by_cost_centre(movements: pd.DataFrame) -> pd.DataFrame:
    return _group_volume(_by_kind(movements, DISPENSE), "cost_centre", "Cost Centre")


def consumption_by_dimension(movements: pd.DataFrame, equipment: pd.DataFrame,
                             dim_col: str, label: str) -> pd.DataFrame:
    """Consumo despachado agrupado por una dimension del equipo (grupo /
    categoria / departamento), uniendo los despachos al inventario por
    `equipment_id` (el grupo/categoria no viaja por-movimiento)."""
    cols = [label, "Despachos", "Volumen (L)"]
    disp = _by_kind(movements, DISPENSE)
    if (disp.empty or equipment is None or equipment.empty
            or dim_col not in equipment.columns
            or "equipment_id" not in disp.columns):
        return pd.DataFrame(columns=cols)
    lut = (equipment[["equipment_id", dim_col]]
           .dropna(subset=["equipment_id"]).copy())
    lut["equipment_id"] = lut["equipment_id"].astype("string")
    disp = disp.copy()
    disp["equipment_id"] = disp["equipment_id"].astype("string")
    merged = disp.merge(lut, on="equipment_id", how="left")
    return _group_volume(merged, dim_col, label)


def top_consumers(movements: pd.DataFrame, n: int = 25) -> pd.DataFrame:
    """Equipos que mas combustible consumen (por volumen despachado)."""
    cols = ["equipment_id", "equipment_description", "Despachos", "Volumen (L)"]
    disp = _by_kind(movements, DISPENSE)
    if disp.empty or "equipment_id" not in disp.columns:
        return pd.DataFrame(columns=cols)
    g = (disp.groupby("equipment_id")
         .agg(equipment_description=("equipment_description", "first"),
              Despachos=("volume", "size"),
              **{"Volumen (L)": ("volume", "sum")})
         .reset_index())
    g["Volumen (L)"] = g["Volumen (L)"].astype(float).round(1)
    return g.sort_values("Volumen (L)", ascending=False).head(n).reset_index(drop=True)


def burn_rate(movements: pd.DataFrame, freq: str = "D") -> pd.DataFrame:
    """Volumen despachado por periodo (consumo en el tiempo)."""
    cols = ["Periodo", "Despachos", "Volumen (L)"]
    disp = _by_kind(movements, DISPENSE)
    if disp.empty or "updated_at" not in disp.columns:
        return pd.DataFrame(columns=cols)
    disp = disp.dropna(subset=["updated_at"])
    if disp.empty:
        return pd.DataFrame(columns=cols)
    g = (disp.set_index("updated_at")
         .groupby(pd.Grouper(freq=freq))
         .agg(Despachos=("volume", "size"), **{"Volumen (L)": ("volume", "sum")})
         .reset_index().rename(columns={"updated_at": "Periodo"}))
    g = g[g["Despachos"] > 0]
    g["Volumen (L)"] = g["Volumen (L)"].astype(float).round(1)
    return g.reset_index(drop=True)


# ===========================================================================
# Flujo (lado movimientos de la reconciliacion)
# ===========================================================================

def flow_by_tank(movements: pd.DataFrame) -> pd.DataFrame:
    """Por tanque: entregas (inflow), despachos y transferencias de salida.

    Nota: la replica conserva el tanque de ORIGEN de cada transaccion
    (`source` en despachos/transferencias, `target` en entregas); el tanque
    destino de una transferencia no se retiene todavia, asi que las
    transferencias cuentan como salida del tanque origen.
    """
    cols = ["Tanque", "Entregas (L)", "Despachos (L)",
            "Transferencias salida (L)", "Neto transacciones (L)"]
    if movements is None or movements.empty or "tank" not in movements.columns:
        return pd.DataFrame(columns=cols)
    mv = movements.copy()
    mv["_tank"] = _key(mv["tank"])

    def _sum(chunk, kind):
        return float(chunk.loc[chunk["kind"] == kind, "volume"].fillna(0).sum())

    rows = []
    for tank, chunk in mv.groupby("_tank"):
        deliveries = _sum(chunk, DELIVERY)
        dispenses = _sum(chunk, DISPENSE)
        transfers = _sum(chunk, TRANSFER)
        rows.append({
            "Tanque": tank,
            "Entregas (L)": round(deliveries, 1),
            "Despachos (L)": round(dispenses, 1),
            "Transferencias salida (L)": round(transfers, 1),
            "Neto transacciones (L)": round(deliveries - dispenses - transfers, 1),
        })
    return (pd.DataFrame(rows, columns=cols)
            .sort_values("Despachos (L)", ascending=False).reset_index(drop=True))


def flow_over_time(movements: pd.DataFrame, freq: str = "D") -> pd.DataFrame:
    """Inflow (entregas) vs outflow (despachos + transferencias) por periodo."""
    cols = ["Periodo", "Inflow (L)", "Outflow (L)", "Neto (L)"]
    if (movements is None or movements.empty
            or "kind" not in movements.columns or "updated_at" not in movements.columns):
        return pd.DataFrame(columns=cols)
    mv = movements.dropna(subset=["updated_at"]).copy()
    if mv.empty:
        return pd.DataFrame(columns=cols)
    mv["_in"] = mv["volume"].where(mv["kind"] == DELIVERY, 0.0)
    mv["_out"] = mv["volume"].where(mv["kind"].isin([DISPENSE, TRANSFER]), 0.0)
    g = (mv.set_index("updated_at").groupby(pd.Grouper(freq=freq))
         .agg(**{"Inflow (L)": ("_in", "sum"), "Outflow (L)": ("_out", "sum")})
         .reset_index().rename(columns={"updated_at": "Periodo"}))
    g = g[(g["Inflow (L)"] != 0) | (g["Outflow (L)"] != 0)]
    g["Neto (L)"] = (g["Inflow (L)"] - g["Outflow (L)"]).round(1)
    g["Inflow (L)"] = g["Inflow (L)"].round(1)
    g["Outflow (L)"] = g["Outflow (L)"].round(1)
    return g[cols].reset_index(drop=True)


def circuit_summary(movements: pd.DataFrame) -> pd.DataFrame:
    """Resumen por circuito: despachos (n y volumen) y entregas."""
    cols = ["Circuito", "Despachos", "Volumen despachado (L)", "Entregas (L)"]
    if movements is None or movements.empty or "product" not in movements.columns:
        return pd.DataFrame(columns=cols)
    mv = movements.copy()
    mv["_circ"] = mv["product"].map(classify_circuit)
    mv = mv[mv["_circ"].notna()]
    if mv.empty:
        return pd.DataFrame(columns=cols)
    rows = []
    for circ, chunk in mv.groupby("_circ"):
        disp = chunk[chunk["kind"] == DISPENSE]
        deliv = chunk[chunk["kind"] == DELIVERY]
        rows.append({
            "Circuito": circ,
            "Despachos": int(len(disp)),
            "Volumen despachado (L)": round(float(disp["volume"].fillna(0).sum()), 1),
            "Entregas (L)": round(float(deliv["volume"].fillna(0).sum()), 1),
        })
    return (pd.DataFrame(rows, columns=cols)
            .sort_values("Volumen despachado (L)", ascending=False).reset_index(drop=True))
