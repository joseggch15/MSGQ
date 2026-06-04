# -*- coding: utf-8 -*-
"""Pruebas del modulo 'Despachos sobre Safe Fill Level (SFL)'.

METODOLOGIA (igual que el resto del ecosistema): sin mocks. Las deterministas
construyen DataFrames con el esquema real (`config.*_COLS`); la smoke ejercita el
pipeline simulador -> transform -> SQLite -> deteccion.

Verifica el valor de negocio: detectar despachos cuyo volumen excede el SFL del
equipo para ese producto (sobrellenado), que dispara la alerta/alarma.

Ejecutar:   pytest tests/test_sfl_audit.py -v
o:          python tests/test_sfl_audit.py
"""
import asyncio
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msgq import config
from msgq.api import make_source
from msgq.core import alerts as al
from msgq.core import sfl_audit as sa
from msgq.core import transform
from msgq.storage import Database


# ---------------------------------------------------------------------------
# Constructores de datos (esquema real)
# ---------------------------------------------------------------------------

def _mv_df(rows: list[dict]) -> pd.DataFrame:
    base = [{**{c: pd.NA for c in config.MOVEMENT_COLS}, **r} for r in rows]
    df = pd.DataFrame(base, columns=config.MOVEMENT_COLS)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    return df


def _lim_df(rows: list[dict]) -> pd.DataFrame:
    base = [{**{c: pd.NA for c in config.CONSUMPTION_LIMIT_COLS}, **r} for r in rows]
    df = pd.DataFrame(base, columns=config.CONSUMPTION_LIMIT_COLS)
    df["sfl"] = pd.to_numeric(df["sfl"], errors="coerce")
    return df


def _disp(id_, eq, product, volume, when="2026-06-01"):
    return {"id": id_, "kind": config.KIND_DISPENSE, "equipment_id": eq,
            "equipment_description": f"Equipo {eq}", "equipment_status": config.STATUS_IN,
            "product": product, "volume": volume, "tank": "LFO Lane 3",
            "field_user": "Mitchell Godet", "record_collected_at": pd.Timestamp(when)}


# ===========================================================================
# 1. Deteccion de excesos
# ===========================================================================

def test_exceedances_basic():
    mv = _mv_df([
        _disp("d1", "EQ1", "Diesel", 2000, "2026-06-01"),     # 2000 > 1893 -> exceso
        _disp("d2", "EQ1", "Diesel", 1500, "2026-06-02"),     # bajo el SFL
        {"id": "t1", "kind": config.KIND_TRANSFER, "equipment_id": "EQ1",
         "product": "Diesel", "volume": 5000},                # no es despacho
        _disp("d3", "EQ2", "Coolant", 500, "2026-06-03"),     # EQ2/Coolant sin SFL
    ])
    lim = _lim_df([
        {"id": "1", "equipment_id": "EQ1", "product": "Diesel", "sfl": 1893},
        {"id": "2", "equipment_id": "EQ2", "product": "Diesel", "sfl": 1893},
    ])
    exc = sa.exceedances(mv, lim)
    assert len(exc) == 1
    r = exc.iloc[0]
    assert r["source_id"] == "d1" and r["equipment_id"] == "EQ1"
    assert r["volume"] == 2000 and r["sfl"] == 1893
    assert round(r["excess"], 1) == 107.0
    assert r["dispensing_point"] == "LFO Lane 3"
    print("OK  test_exceedances_basic")


def test_exceedances_product_case_insensitive():
    mv = _mv_df([_disp("d", "EQ1", "diesel", 2000)])
    lim = _lim_df([{"id": "1", "equipment_id": "EQ1", "product": "DIESEL", "sfl": 1893}])
    assert len(sa.exceedances(mv, lim)) == 1
    print("OK  test_exceedances_product_case_insensitive")


def test_kpis_and_groupings():
    mv = _mv_df([
        _disp("d1", "EQ1", "Diesel", 2000, "2026-06-01"),
        _disp("d2", "EQ2", "Diesel", 2200, "2026-06-02"),
        _disp("d3", "EQ1", "Diesel", 1000, "2026-06-03"),     # bajo el SFL
    ])
    lim = _lim_df([
        {"id": "1", "equipment_id": "EQ1", "product": "Diesel", "sfl": 1893},
        {"id": "2", "equipment_id": "EQ2", "product": "Diesel", "sfl": 1893},
    ])
    exc = sa.exceedances(mv, lim)
    assert len(exc) == 2
    k = sa.summary_kpis(exc, mv)
    assert k["Excesos"] == 2
    assert k["Equipos afectados"] == 2
    assert round(k["% de despachos"], 1) == 66.7    # 2 de 3 despachos
    assert sa.by_product(exc).iloc[0]["Excesos"] == 2
    assert set(sa.by_equipment(exc)["equipment_id"]) == {"EQ1", "EQ2"}
    print("OK  test_kpis_and_groupings")


# ===========================================================================
# 2. Alerta (alimenta el panel + KPI critico + toast)
# ===========================================================================

def test_detect_sfl_alerts():
    mv = _mv_df([_disp("d1", "EQ1", "Diesel", 2000)])
    lim = _lim_df([{"id": "1", "equipment_id": "EQ1", "product": "Diesel", "sfl": 1893}])
    alerts = al.detect_sfl_alerts(mv, lim)
    assert len(alerts) == 1
    a = alerts.iloc[0]
    assert a["severity"] == al.SEV_CRITICAL
    assert a["category"] == config.ALERT_SFL_EXCEEDED
    assert a["source_id"] == "d1"
    assert isinstance(a["detail"], str) and a["detail"]
    # vacio cuando no hay limites
    assert al.detect_sfl_alerts(mv, _lim_df([])).empty
    print("OK  test_detect_sfl_alerts")


# ===========================================================================
# 3. Transform + almacenamiento de los limites (SFL)
# ===========================================================================

def test_consumption_limits_transform_and_roundtrip():
    nodes = [{"equipmentId": "HTK0819", "id": "21", "consumptionTanks": [
        {"id": "31", "sfl": "1893", "product": {"code": "DIESEL", "description": "Diesel"}},
        {"id": "2528", "sfl": None,  # sin SFL -> se descarta
         "product": {"code": "Hydraulic Fluid 10W", "description": "Spirax S4CX10W"}},
    ]}]
    df = transform.consumption_limits_to_df(nodes)
    assert len(df) == 1
    assert df.iloc[0]["product"] == "Diesel" and df.iloc[0]["sfl"] == 1893
    assert df.iloc[0]["equipment_id"] == "HTK0819"

    db = Database(":memory:")
    db.upsert("consumption_limits", df)
    back = db.get_consumption_limits()
    db.close()
    assert len(back) == 1 and back.iloc[0]["sfl"] == 1893
    print("OK  test_consumption_limits_transform_and_roundtrip")


# ===========================================================================
# 3b. Conflictos: despachos sin equipo / Unauthorised
# ===========================================================================

def _conf_disp(id_, eq, product, volume, status, typ, when="2026-04-05"):
    return {"id": id_, "kind": config.KIND_DISPENSE, "equipment_id": eq,
            "product": product, "volume": volume, "status": status, "type": typ,
            "tank": "LFO Lane 3", "field_user": "A. Singh",
            "record_collected_at": pd.Timestamp(when)}


def test_unattributed_conflicts():
    lim = _lim_df([
        {"id": "1", "equipment_id": "EQ1", "product": "Diesel", "sfl": 1893},
        {"id": "2", "equipment_id": "EQ2", "product": "Diesel", "sfl": 560},
    ])  # SFL maximo de flota para Diesel = 1893
    mv = _mv_df([
        _conf_disp("c1", None, "Diesel", 2759.4, "no_equip", "Unauthorised"),     # over_max
        _conf_disp("c2", "Unauthorised", "Diesel", 150.0, "no_equip", "Unauthorised"),  # conflicto, no over
        _disp("d1", "EQ1", "Diesel", 100.0, "2026-04-07"),                        # con equipo -> NO conflicto
    ])
    conf = sa.unattributed_conflicts(mv, lim)
    assert len(conf) == 2
    assert list(conf.columns) == sa.CONFLICT_COLS
    big = conf[conf["source_id"] == "c1"].iloc[0]
    assert bool(big["over_max"]) is True and big["fleet_max_sfl"] == 1893
    small = conf[conf["source_id"] == "c2"].iloc[0]
    assert bool(small["over_max"]) is False
    assert sa.fleet_sfl_by_product(lim)["DIESEL"] == 1893
    k = sa.conflict_kpis(conf)
    assert k["Conflictos"] == 2 and k["Sobre SFL flota"] == 1
    print("OK  test_unattributed_conflicts")


def test_detect_sfl_conflict_alerts():
    lim = _lim_df([{"id": "1", "equipment_id": "EQ1", "product": "Diesel", "sfl": 1893}])
    mv = _mv_df([
        _conf_disp("c1", None, "Diesel", 2759.4, "no_equip", "Unauthorised"),  # over_max -> CRITICAL
        _conf_disp("c2", None, "Diesel", 150.0, "no_equip", "Unauthorised"),   # no over -> sin alerta SFL
    ])
    alerts = al.detect_sfl_conflict_alerts(mv, lim)
    assert len(alerts) == 1
    a = alerts.iloc[0]
    assert a["severity"] == al.SEV_CRITICAL
    assert a["category"] == config.ALERT_SFL_CONFLICT
    assert a["source_id"] == "c1" and a["detail"]
    print("OK  test_detect_sfl_conflict_alerts")


def test_sfl_tolerance_filters_meter_noise():
    """Con tolerancia del 2%, un exceso marginal (ruido de medidor) NO se marca,
    pero un sobrellenado real sí."""
    lim = _lim_df([{"id": "1", "equipment_id": "FT001", "product": "Diesel", "sfl": 2000}])
    mv = _mv_df([
        _disp("m1", "FT001", "Diesel", 2004.6, "2026-06-03"),   # +0.23% -> ruido, NO
        _disp("m2", "FT001", "Diesel", 2100.0, "2026-06-03"),   # +5%    -> real, SI
    ])
    assert abs(config.SFL_TOLERANCE_PCT - 0.02) < 1e-9
    exc = sa.exceedances(mv, lim)
    assert len(exc) == 1 and exc.iloc[0]["source_id"] == "m2"
    print("OK  test_sfl_tolerance_filters_meter_noise")


def test_demo_data_isolation_and_purge():
    """El replica demo va en archivo aparte; y se pueden purgar movimientos del
    simulador que hubieran quedado en un replica de produccion (la causa de los
    falsos positivos de SFL: despachos demo cruzados con SFL reales)."""
    from msgq.config import demo_db_path
    assert demo_db_path("/x/msgq.sqlite3") == "/x/msgq_demo.sqlite3"
    assert demo_db_path("").endswith("_demo.sqlite3")

    db = Database(":memory:")
    db.upsert("movements", _mv_df([
        _disp("251025", "TFL0847", "Diesel", 367.4, "2026-06-02"),          # real
        {"id": "SIM-00000066", "kind": config.KIND_DISPENSE, "equipment_id": "TFL0847",
         "product": "Diesel", "volume": 1276.8},                            # simulador
    ]))
    lim = _lim_df([{"id": "1", "equipment_id": "TFL0847", "product": "Diesel", "sfl": 560}])
    # Con el dato del simulador se reporta un FALSO exceso (1276.8 > 560):
    assert len(sa.exceedances(db.read("movements"), lim)) == 1
    # Tras purgar el dato demo, no queda exceso (el real 367.4 < 560):
    assert db.purge_simulator_movements() == 1
    assert db.row_count("movements") == 1
    assert sa.exceedances(db.read("movements"), lim).empty
    db.close()
    print("OK  test_demo_data_isolation_and_purge")


def test_fetch_movements_paged_parity_and_backfill():
    from PySide6.QtCore import QCoreApplication
    from msgq.ingest.poller import Poller

    # Paridad: fetch_movements_paged entrega lotes con `kind`.
    async def _paged():
        src = make_source(config.Settings(demo_mode=True, token="", db_path=":memory:"))
        out = []
        await src.fetch_movements_paged(None, out.extend)
        await src.aclose()
        return out
    paged = asyncio.run(_paged())
    assert len(paged) >= 1 and all("kind" in n for n in paged)

    # Backfill del primer arranque: sin watermark -> usa paged y fija watermark.
    QCoreApplication.instance() or QCoreApplication([])
    db = Database(":memory:")
    src = make_source(config.Settings(demo_mode=True, token="", db_path=":memory:"))
    poller = Poller(config.Settings(demo_mode=True, token="", db_path=":memory:"), db)
    n = asyncio.run(poller._sync_movements(src))
    asyncio.run(src.aclose())
    assert n >= 1 and db.row_count("movements") >= 1
    assert db.get_watermark("movements") is not None
    db.close()
    print("OK  test_fetch_movements_paged_parity_and_backfill")


# ===========================================================================
# 4. Smoke E2E con el simulador
# ===========================================================================

def test_e2e_sfl_via_simulator():
    async def _collect():
        src = make_source(config.Settings(demo_mode=True, token="", db_path=":memory:"))
        mv_nodes: list[dict] = []
        for _ in range(60):                       # acumula despachos (~6% exceden)
            mv_nodes += await src.fetch_movements(None)
        eq_nodes = await src.fetch_equipment(None)
        await src.aclose()
        return mv_nodes, eq_nodes

    mv_nodes, eq_nodes = asyncio.run(_collect())
    assert any(n.get("consumptionTanks") for n in eq_nodes), \
        "el simulador debe exponer consumptionTanks (SFL) por equipo"

    db = Database(":memory:")
    db.upsert("movements", transform.movements_to_df(mv_nodes))
    db.upsert("consumption_limits", transform.consumption_limits_to_df(eq_nodes))
    exc = sa.exceedances(db.read("movements"), db.get_consumption_limits())
    alerts = al.detect_sfl_alerts(db.read("movements"), db.get_consumption_limits())
    db.close()

    assert not exc.empty, "el simulador genera ~6% de despachos sobre SFL"
    assert (exc["volume"] > exc["sfl"]).all()
    assert list(exc.columns) == sa.EXCEEDANCE_COLS
    assert (alerts["severity"] == al.SEV_CRITICAL).all()
    print(f"OK  test_e2e_sfl_via_simulator ({len(exc)} excesos detectados)")


if __name__ == "__main__":
    tests = [
        test_exceedances_basic,
        test_exceedances_product_case_insensitive,
        test_kpis_and_groupings,
        test_detect_sfl_alerts,
        test_consumption_limits_transform_and_roundtrip,
        test_unattributed_conflicts,
        test_detect_sfl_conflict_alerts,
        test_sfl_tolerance_filters_meter_noise,
        test_demo_data_isolation_and_purge,
        test_fetch_movements_paged_parity_and_backfill,
        test_e2e_sfl_via_simulator,
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
