# -*- coding: utf-8 -*-
"""Pruebas del modulo 'Desviacion de Volumen en Entregas' (medidor vs guia).

METODOLOGIA (igual que el resto del ecosistema): sin mocks. Las deterministas
construyen DataFrames con el esquema real (`config.MOVEMENT_COLS`); la smoke
ejercita el pipeline simulador -> transform -> SQLite -> deteccion.

Valor de negocio: marcar las entregas cuya diferencia entre el volumen MEDIDO
(`volume`) y el DIGITADO de la guia (`secondary_volume`) supera el 1% — el
proveedor podria estar facturando litros que no entraron, o el medidor esta
descalibrado. Verificado contra el export real: MANUAL ~0,5% (no marca), GAUGED
8-11% (marca).

Ejecutar:   pytest tests/test_volume_deviation.py -v
o:          python tests/test_volume_deviation.py
"""
import asyncio
import os
import sys
from datetime import timedelta

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msgq import config
from msgq.api import make_source
from msgq.core import alerts as al
from msgq.core import transform
from msgq.core import volume_deviation as vd
from msgq.storage import Database

_T0 = pd.Timestamp("2026-05-30 07:30:00")


def _mv_df(rows: list[dict]) -> pd.DataFrame:
    base = [{**{c: pd.NA for c in config.MOVEMENT_COLS}, **r} for r in rows]
    df = pd.DataFrame(base, columns=config.MOVEMENT_COLS)
    for c in ("volume", "secondary_volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["record_collected_at"] = pd.to_datetime(df["record_collected_at"], errors="coerce")
    return df


def _deliv(id_, measured, field, tank="LFO - Main Tank", ttype="MANUAL", day=0,
           kind=config.KIND_DELIVERY):
    return {"id": id_, "kind": kind, "type": ttype, "volume": measured,
            "secondary_volume": field, "tank": tank, "product": "DIESEL",
            "primary_volume_source": "METER", "secondary_volume_source": "DOCKET",
            "record_collected_at": _T0 + timedelta(days=day)}


# ===========================================================================
# 1. Dentro de tolerancia (MANUAL real): NO se marca
# ===========================================================================

def test_within_tolerance_not_flagged():
    """39.810,5 medidos vs 40.000 de guia = 0,48% < 1% -> no se marca (es el
    desfase normal de un camion nominal de 40.000 L)."""
    mv = _mv_df([_deliv("D1", 39810.5, 40000.0)])
    dev = vd.deviations(mv)
    assert len(dev) == 1
    r = dev.iloc[0]
    assert abs(float(r["deviation_pct"])) < 1.0
    assert bool(r["flagged"]) is False
    assert vd.flagged(dev).empty
    assert al.detect_volume_deviation_alerts(mv).empty


# ===========================================================================
# 2. Por encima del umbral (GAUGED real): se marca, direccion correcta
# ===========================================================================

def test_over_threshold_flagged_underbill():
    """5.742,3 medidos (gauge) vs 5.300 de guia = -7,7% -> marca; la guia esta por
    DEBAJO de lo medido (el medidor leyo de mas)."""
    mv = _mv_df([_deliv("D2", 5742.3, 5300.0, ttype="GAUGED")])
    dev = vd.deviations(mv)
    r = dev.iloc[0]
    assert bool(r["flagged"]) is True
    assert round(float(r["deviation_pct"]), 1) == -7.7
    assert r["direction"] == config.DELIVERY_DIR_UNDERBILL


def test_overbill_direction_and_critical():
    """Guia 1.050 vs medido 1.000 = +5% -> sobre-facturacion y, al alcanzar el
    umbral critico (5%), la alerta es CRITICA."""
    mv = _mv_df([_deliv("D3", 1000.0, 1050.0)])
    dev = vd.deviations(mv)
    r = dev.iloc[0]
    assert bool(r["flagged"]) is True
    assert r["direction"] == config.DELIVERY_DIR_OVERBILL
    assert round(float(r["deviation_pct"]), 1) == 5.0

    alerts = al.detect_volume_deviation_alerts(mv)
    assert len(alerts) == 1
    assert alerts.iloc[0]["severity"] == al.SEV_CRITICAL
    assert alerts.iloc[0]["category"] == config.ALERT_VOLUME_DEVIATION


# ===========================================================================
# 3. Volumen minusculo: se ignora (un % enorme sobre pocos litros no importa)
# ===========================================================================

def test_tiny_volume_ignored():
    mv = _mv_df([_deliv("D4", 50.0, 70.0)])    # 40% pero solo 50 L medidos
    dev = vd.deviations(mv)
    assert len(dev) == 1
    assert bool(dev.iloc[0]["flagged"]) is False


# ===========================================================================
# 4. Sin volumen de guia: la entrega no entra al analisis
# ===========================================================================

def test_missing_field_volume_excluded():
    mv = _mv_df([_deliv("D5", 40000.0, None)])
    assert vd.deviations(mv).empty


# ===========================================================================
# 5. Solo entregas: los despachos no se analizan aqui
# ===========================================================================

def test_dispenses_not_analyzed():
    mv = _mv_df([_deliv("X1", 1000.0, 1100.0, kind=config.KIND_DISPENSE)])
    assert vd.deviations(mv).empty


# ===========================================================================
# 6. Severidad: 1% <= dev < 5% -> ADVERTENCIA
# ===========================================================================

def test_warning_severity_below_critical():
    mv = _mv_df([_deliv("D6", 1000.0, 1020.0)])   # +2% -> WARNING
    alerts = al.detect_volume_deviation_alerts(mv)
    assert len(alerts) == 1
    assert alerts.iloc[0]["severity"] == al.SEV_WARNING


# ===========================================================================
# 7. Resumen por tanque y KPIs
# ===========================================================================

def test_by_tank_and_kpis():
    mv = _mv_df([
        _deliv("A1", 1000.0, 1050.0, tank="Tank A"),    # +5% marcada
        _deliv("A2", 2000.0, 2005.0, tank="Tank A"),    # 0.25% no
        _deliv("B1", 3000.0, 3300.0, tank="Tank B"),    # +10% marcada
    ])
    dev = vd.deviations(mv)
    bt = vd.by_tank(dev)
    assert set(bt["tank"]) == {"Tank A", "Tank B"}
    k = vd.summary_kpis(dev)
    assert k["Entregas analizadas"] == 3
    assert k["Entregas marcadas"] == 2
    assert round(k["Peor desviación %"], 1) == 10.0


# ===========================================================================
# 8. Smoke: pipeline real con el simulador (el demo emite secondaryVolume)
# ===========================================================================

def test_smoke_simulator_pipeline():
    import tempfile
    db_path = os.path.join(tempfile.mkdtemp(), "smoke.sqlite3")
    db = Database(db_path)
    try:
        src = make_source(config.Settings(demo_mode=True, token="", db_path=db_path))

        async def pull():
            nodes = []
            for _ in range(30):
                nodes += await src.fetch_movements(None)
            return nodes

        mv_nodes = asyncio.run(pull())
        db.upsert("movements", transform.movements_to_df(mv_nodes))

        mv = db.read("movements")
        # El simulador pone secondary_volume = volume*0.99 en cada entrega.
        deliveries = mv[mv["kind"] == config.KIND_DELIVERY]
        assert deliveries["secondary_volume"].notna().any(), "secondaryVolume debe replicarse"

        dev = vd.deviations(mv)
        assert list(dev.columns) == vd.DEVIATION_COLS
        alerts = al.detect_volume_deviation_alerts(mv)
        assert list(alerts.columns) == al.ALERT_COLS
    finally:
        db.close()


if __name__ == "__main__":
    tests = [
        test_within_tolerance_not_flagged,
        test_over_threshold_flagged_underbill,
        test_overbill_direction_and_critical,
        test_tiny_volume_ignored,
        test_missing_field_volume_excluded,
        test_dispenses_not_analyzed,
        test_warning_severity_below_critical,
        test_by_tank_and_kpis,
        test_smoke_simulator_pipeline,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"OK     {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FALLO  {fn.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} pruebas de volume_deviation superadas.")
    raise SystemExit(1 if failed else 0)
