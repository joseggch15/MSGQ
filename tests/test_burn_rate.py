# -*- coding: utf-8 -*-
"""Pruebas de la auditoría de Burn Rate (consumo L/h).

Sin mocks: se arman DataFrames de movimientos (despachos) con el esquema real y
se verifica el valor de negocio — reconstruir el burn rate por el método
tanque-a-tanque (litros ÷ ΔSMU) y marcar equipos/intervalos anómalos con
estadística robusta.

Ejecutar:   pytest tests/test_burn_rate.py -v
o:          python tests/test_burn_rate.py
"""
import os
import sys
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msgq import config
from msgq.core import burn_rate as br


# ===========================================================================
# Constructores de datos de prueba
# ===========================================================================

_T0 = datetime(2026, 1, 1, 8, 0, 0)


def _dispenses(equipment_id: str, fills: list[tuple[float, float]],
               product: str = "Diesel", smu_type: str = "hrs",
               start: datetime = _T0) -> list[dict]:
    """Despachos consecutivos de un equipo. `fills` = lista de (smu, litros)."""
    rows = []
    for i, (smu, litres) in enumerate(fills):
        rows.append({
            "id": f"{equipment_id}-{i}",
            "kind": config.KIND_DISPENSE,
            "equipment_id": equipment_id,
            "equipment_description": f"Truck {equipment_id}",
            "volume": litres,
            "smu_value": smu,
            "smu_type": smu_type,
            "product": product,
            "field_user": "op1",
            "record_collected_at": (start + timedelta(days=i)).isoformat(),
        })
    return rows


def _mv_df(rows: list[dict]) -> pd.DataFrame:
    base = [{**{c: pd.NA for c in config.MOVEMENT_COLS}, **r} for r in rows]
    df = pd.DataFrame(base, columns=config.MOVEMENT_COLS)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df["smu_value"] = pd.to_numeric(df["smu_value"], errors="coerce")
    df["record_collected_at"] = pd.to_datetime(df["record_collected_at"], errors="coerce")
    return df


def _eq_master(mapping: dict[str, str]) -> pd.DataFrame:
    """Maestro mínimo {equipment_id: categoría}."""
    rows = [{**{c: pd.NA for c in config.EQUIPMENT_COLS},
             "equipment_id": eid, "category": cat, "description": f"Truck {eid}"}
            for eid, cat in mapping.items()]
    return pd.DataFrame(rows, columns=config.EQUIPMENT_COLS)


# A 4 despachos -> 3 intervalos; con burn rate ~`rate` y ΔSMU=10 cada intervalo.
def _steady(equipment_id: str, rate: float, n: int = 4, smu0: float = 1000.0):
    fills = [(smu0, 0.0)]   # primer fill (sin intervalo); litros del primero no cuentan
    smu = smu0
    for _ in range(n - 1):
        smu += 10.0
        fills.append((smu, rate * 10.0))
    # el primer fill necesita litros > 0 para no ser filtrado (su volumen no se usa
    # como intervalo, pero la fila debe sobrevivir el filtro volume>0).
    fills[0] = (smu0, rate * 10.0)
    return _dispenses(equipment_id, fills)


# ===========================================================================
# 1. Cálculo del burn rate por intervalo (litros / ΔSMU)
# ===========================================================================

def test_interval_burn_rate_litres_over_smu_delta():
    # 3 fills: 1000, 1010 (+1500 L), 1025 (+1500 L) -> burn 150 y 100.
    mv = _mv_df(_dispenses("HT1", [(1000, 500.0), (1010, 1500.0), (1025, 1500.0)]))
    s = br.interval_samples(mv, _eq_master({"HT1": "Haul Trucks"}))
    assert len(s) == 2                                   # 3 despachos -> 2 intervalos
    rates = sorted(s["burn_rate"].tolist())
    assert rates == [100.0, 150.0]                       # 1500/15 y 1500/10
    assert set(s["category"]) == {"Haul Trucks"}
    print("OK  test_interval_burn_rate_litres_over_smu_delta")


def test_non_increasing_smu_is_skipped():
    # El SMU retrocede (1010 -> 1005): ese intervalo no se puede calcular.
    mv = _mv_df(_dispenses("HT1", [(1000, 800.0), (1010, 1000.0), (1005, 1000.0),
                                   (1030, 2000.0)]))
    s = br.interval_samples(mv, _eq_master({"HT1": "Haul Trucks"}))
    # Intervalos válidos: 1000->1010 (Δ10) y 1010->1030 (Δ20, tras saltar el 1005).
    assert len(s) == 2
    assert (s["smu_delta"] > 0).all()
    print("OK  test_non_increasing_smu_is_skipped")


def test_implausible_burn_rate_is_dropped():
    # ΔSMU=1 con 5000 L -> 5000 L/h, por encima del techo de plausibilidad.
    mv = _mv_df(_dispenses("TK1", [(1, 100.0), (2, 5000.0), (12, 2000.0)]))
    s = br.interval_samples(mv, _eq_master({"TK1": "Tanks"}))
    # El intervalo 1->2 (5000 L/h) se descarta; queda 2->12 (2000/10=200).
    assert len(s) == 1
    assert s.iloc[0]["burn_rate"] == 200.0
    print("OK  test_implausible_burn_rate_is_dropped")


# ===========================================================================
# 2. Anomalía de equipo vs su categoría
# ===========================================================================

def _fleet_with_outlier():
    rows = []
    rows += _steady("HT1", 190)
    rows += _steady("HT2", 200)
    rows += _steady("HT3", 205)
    rows += _steady("HT4", 210)
    rows += _steady("HT5", 400)        # el sobre-consumidor
    mv = _mv_df(rows)
    eq = _eq_master({f"HT{i}": "Haul Trucks" for i in range(1, 6)})
    return mv, eq


def test_equipment_anomaly_flags_outlier():
    mv, eq = _fleet_with_outlier()
    res = br.audit(mv, eq)
    anom = res.equipment_anomalies
    assert set(anom["equipment_id"]) == {"HT5"}          # solo el de 400 L/h
    row = anom.iloc[0]
    assert row["Dirección"] == "Alto"                    # sobre-consumo
    assert row["Desviación %"] > 50
    # Los demás equipos NO son anómalos.
    eqt = res.equipment
    assert not eqt[eqt["equipment_id"] == "HT2"]["Anómalo"].iloc[0]
    print("OK  test_equipment_anomaly_flags_outlier")


def test_category_baseline_summary():
    mv, eq = _fleet_with_outlier()
    res = br.audit(mv, eq)
    cats = res.categories
    assert list(cats.columns) == br.CATEGORY_COLS
    ht = cats[cats["category"] == "Haul Trucks"].iloc[0]
    assert ht["Equipos"] == 5
    assert ht["Anómalos"] == 1
    assert 195 <= ht["Burn rate base (L/h)"] <= 215      # mediana ~205
    print("OK  test_category_baseline_summary")


def test_too_few_equipment_no_baseline():
    # Una categoría con 2 equipos no alcanza el mínimo para una línea base.
    rows = _steady("EX1", 300) + _steady("EX2", 800)
    res = br.audit(_mv_df(rows), _eq_master({"EX1": "Excavators", "EX2": "Excavators"}))
    assert res.equipment_anomalies.empty                 # sin línea base, nada que marcar
    assert res.categories.empty
    print("OK  test_too_few_equipment_no_baseline")


# ===========================================================================
# 3. Intervalo atípico (un despacho fuera del historial del equipo)
# ===========================================================================

def test_interval_anomaly_flags_spike():
    # HT con burn rate estable ~200 y UN intervalo disparado a 600.
    fills = [(1000, 2000.0), (1010, 2000.0), (1020, 2040.0), (1030, 1980.0),
             (1040, 6000.0)]
    mv = _mv_df(_dispenses("HT9", fills))
    res = br.audit(mv, _eq_master({"HT9": "Haul Trucks"}))
    ia = res.interval_anomalies
    assert len(ia) == 1
    assert list(ia.columns) == br.INTERVAL_COLS
    assert ia.iloc[0]["burn_rate"] == 600.0
    assert ia.iloc[0]["Dirección"] == "Alto"
    print("OK  test_interval_anomaly_flags_spike")


# ===========================================================================
# 4. Robustez ante entradas vacías / incompletas
# ===========================================================================

def test_empty_and_missing_columns_are_safe():
    assert br.interval_samples(None).empty
    assert br.interval_samples(pd.DataFrame()).empty
    res = br.audit(pd.DataFrame(), None)
    assert res.samples.empty and res.equipment.empty and res.categories.empty
    assert res.kpis["Equipos anómalos"] == 0
    # Movimientos sin smu_value: no se puede calcular ningún intervalo.
    mv = _mv_df([{"id": "X", "kind": config.KIND_DISPENSE, "equipment_id": "E1",
                  "volume": 100.0, "record_collected_at": _T0.isoformat()}])
    mv["smu_value"] = pd.NA
    assert br.interval_samples(mv).empty
    print("OK  test_empty_and_missing_columns_are_safe")


def test_kpis_shape():
    mv, eq = _fleet_with_outlier()
    k = br.audit(mv, eq).kpis
    for key in ("Equipos analizados", "Equipos anómalos", "Intervalos analizados",
                "Intervalos atípicos", "Burn rate flota (L/h)", "Peor desviación %"):
        assert key in k
    assert k["Equipos analizados"] == 5
    assert k["Equipos anómalos"] == 1
    print("OK  test_kpis_shape")


if __name__ == "__main__":
    tests = [
        test_interval_burn_rate_litres_over_smu_delta,
        test_non_increasing_smu_is_skipped,
        test_implausible_burn_rate_is_dropped,
        test_equipment_anomaly_flags_outlier,
        test_category_baseline_summary,
        test_too_few_equipment_no_baseline,
        test_interval_anomaly_flags_spike,
        test_empty_and_missing_columns_are_safe,
        test_kpis_shape,
    ]
    failed = 0
    for tc in tests:
        try:
            tc()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FALLO  {tc.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} pruebas superadas.")
    raise SystemExit(1 if failed else 0)
