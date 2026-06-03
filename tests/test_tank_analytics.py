# -*- coding: utf-8 -*-
"""Pruebas E2E de la analitica de tanques (lado transacciones).

Misma metodologia que test_e2e.py: pipeline REAL (nodos crudos -> transform ->
DataFrame -> analitica), sin mocks. Datos deterministas construidos a mano para
asertar montos exactos, mas un smoke test con la fuente simulada.

Ejecutar:   pytest tests/test_tank_analytics.py -v
o:          python tests/test_tank_analytics.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msgq import config
from msgq.api import make_source
from msgq.core import tank_analytics as ta
from msgq.core import transform
from msgq.storage import Database

DISP, DELI, TRAN = config.KIND_DISPENSE, config.KIND_DELIVERY, config.KIND_TRANSFER
_WHEN = datetime(2026, 1, 15, 8, 0, 0)


# ---------------------------------------------------------------------------
# Constructores de nodos crudos (forma camelCase de GraphQL)
# ---------------------------------------------------------------------------

def _mv(kind, volume, product, tank=None, equipment_id=None, mtype="AUTO"):
    node = {
        "kind": kind, "id": f"M-{kind}-{volume}", "type": mtype, "status": "all_ok",
        "volume": str(volume),
        "recordUpdatedAt": _WHEN.isoformat(),
        "product": {"code": product[:4].upper(), "description": product},
        "costCentre": {"description": "CC-1"},
        "site": {"code": "MERIAN"},
    }
    if kind == DISP:
        node["source"] = {"name": tank}
        node["target"] = {"equipmentId": equipment_id,
                          "description": f"Equipo {equipment_id}", "status": "In Service"}
    elif kind == DELI:
        node["target"] = {"name": tank}
    else:  # TRANSFER
        node["source"] = {"name": tank}
        node["target"] = {"name": "Service Tank"}
    return node


def _eq(eid, group, category, department):
    return {
        "equipmentId": eid, "description": f"Equipo {eid}", "status": "In Service",
        "equipmentGroup": {"description": group},
        "equipmentCategory": {"description": category},
        "department": {"description": department},
    }


def _fixture():
    mv = transform.movements_to_df([
        _mv(DISP, 100, "Diesel", "Diesel Main", "785"),
        _mv(DISP, 200, "Diesel", "Diesel Main", "785"),
        _mv(DISP, 30, "Unleaded Gasoline", "Gasoline Tank", "LV01"),
        _mv(DELI, 10000, "Diesel", "Diesel Main"),
        _mv(TRAN, 500, "Diesel", "Diesel Main"),
    ])
    eq = transform.equipment_to_df([
        _eq("785", "Haul Trucks", "N-HT", "Mining"),
        _eq("LV01", "Light Vehicles", "N-LV", "Logistics"),
    ])
    return mv, eq


def _val(df, key_col, key, value_col):
    return float(df.loc[df[key_col] == key, value_col].iloc[0])


# ===========================================================================
# 1. Clasificacion de circuito
# ===========================================================================

def test_classify_circuit():
    assert ta.classify_circuit("Diesel") == "Diesel"
    assert ta.classify_circuit("DIESEL ULSD") == "Diesel"
    assert ta.classify_circuit("Unleaded Gasoline") == "Gasolina"
    assert ta.classify_circuit("ULP 95") == "Gasolina"
    assert ta.classify_circuit("Coolant") is None
    assert ta.classify_circuit("") is None and ta.classify_circuit(None) is None
    print("OK  test_classify_circuit")


# ===========================================================================
# 2. Consumo por producto / tanque (solo despachos)
# ===========================================================================

def test_consumption_by_product_and_tank():
    mv, _ = _fixture()
    prod = ta.consumption_by_product(mv)
    assert _val(prod, "Producto", "Diesel", "Volumen (L)") == 300.0
    assert _val(prod, "Producto", "Unleaded Gasoline", "Volumen (L)") == 30.0
    assert int(_val(prod, "Producto", "Diesel", "Despachos")) == 2
    # Entregas/transferencias NO cuentan como consumo.
    assert prod["Volumen (L)"].sum() == 330.0

    tank = ta.consumption_by_tank(mv)
    assert _val(tank, "Tanque", "Diesel Main", "Volumen (L)") == 300.0
    assert _val(tank, "Tanque", "Gasoline Tank", "Volumen (L)") == 30.0
    print("OK  test_consumption_by_product_and_tank")


# ===========================================================================
# 3. Consumo por dimension del equipo (join al inventario)
# ===========================================================================

def test_consumption_by_dimension():
    mv, eq = _fixture()
    grp = ta.consumption_by_dimension(mv, eq, "group", "Grupo")
    assert _val(grp, "Grupo", "Haul Trucks", "Volumen (L)") == 300.0
    assert _val(grp, "Grupo", "Light Vehicles", "Volumen (L)") == 30.0

    dep = ta.consumption_by_dimension(mv, eq, "department", "Departamento")
    assert _val(dep, "Departamento", "Mining", "Volumen (L)") == 300.0
    assert _val(dep, "Departamento", "Logistics", "Volumen (L)") == 30.0
    print("OK  test_consumption_by_dimension")


# ===========================================================================
# 4. Top consumidores y burn rate
# ===========================================================================

def test_top_consumers_and_burn_rate():
    mv, _ = _fixture()
    top = ta.top_consumers(mv)
    assert top.iloc[0]["equipment_id"] == "785"
    assert float(top.iloc[0]["Volumen (L)"]) == 300.0

    br = ta.burn_rate(mv, freq="D")
    assert len(br) == 1                      # todo el mismo dia
    assert int(br.iloc[0]["Despachos"]) == 3 and float(br.iloc[0]["Volumen (L)"]) == 330.0
    print("OK  test_top_consumers_and_burn_rate")


# ===========================================================================
# 5. Flujo por tanque y por periodo (lado movimientos de la reconciliacion)
# ===========================================================================

def test_flow_by_tank_and_over_time():
    mv, _ = _fixture()
    flow = ta.flow_by_tank(mv)
    dm = flow[flow["Tanque"] == "Diesel Main"].iloc[0]
    assert float(dm["Entregas (L)"]) == 10000.0
    assert float(dm["Despachos (L)"]) == 300.0
    assert float(dm["Transferencias salida (L)"]) == 500.0
    assert float(dm["Neto transacciones (L)"]) == 9200.0   # 10000 - 300 - 500

    ot = ta.flow_over_time(mv, freq="D")
    assert len(ot) == 1
    assert float(ot.iloc[0]["Inflow (L)"]) == 10000.0
    assert float(ot.iloc[0]["Outflow (L)"]) == 830.0       # 330 despachos + 500 transfer
    assert float(ot.iloc[0]["Neto (L)"]) == 9170.0
    print("OK  test_flow_by_tank_and_over_time")


# ===========================================================================
# 6. Resumen por circuito y filtro de circuito
# ===========================================================================

def test_circuit_summary_and_filter():
    mv, _ = _fixture()
    summ = ta.circuit_summary(mv)
    diesel = summ[summ["Circuito"] == "Diesel"].iloc[0]
    assert int(diesel["Despachos"]) == 2
    assert float(diesel["Volumen despachado (L)"]) == 300.0
    assert float(diesel["Entregas (L)"]) == 10000.0
    gas = summ[summ["Circuito"] == "Gasolina"].iloc[0]
    assert float(gas["Volumen despachado (L)"]) == 30.0

    only_diesel = ta.filter_circuit(mv, "Diesel")
    prod = ta.consumption_by_product(only_diesel)
    assert len(prod) == 1 and prod.iloc[0]["Producto"] == "Diesel"
    print("OK  test_circuit_summary_and_filter")


# ===========================================================================
# 7. Smoke test con la fuente simulada (pipeline completo, sin mocks)
# ===========================================================================

def test_tank_analytics_via_simulator():
    import asyncio
    src = make_source(config.Settings(demo_mode=True, token="", db_path=":memory:"))
    mv_nodes = asyncio.run(src.fetch_movements(None))
    eq_nodes = asyncio.run(src.fetch_equipment(None))
    asyncio.run(src.aclose())
    mv = transform.movements_to_df(mv_nodes)
    eq = transform.equipment_to_df(eq_nodes)

    prod = ta.consumption_by_product(mv)
    disp_total = float(mv[mv["kind"] == DISP]["volume"].fillna(0).sum())
    # El consumo por producto debe conservar el volumen total despachado.
    assert abs(prod["Volumen (L)"].sum() - round(disp_total, 1)) < 1.0
    # Ninguna funcion debe romper con datos reales del simulador.
    assert list(ta.flow_by_tank(mv).columns)[0] == "Tanque"
    assert list(ta.flow_over_time(mv).columns) == ["Periodo", "Inflow (L)", "Outflow (L)", "Neto (L)"]
    assert not ta.consumption_by_dimension(mv, eq, "group", "Grupo").empty
    assert not ta.circuit_summary(mv).empty
    print("OK  test_tank_analytics_via_simulator")


# ===========================================================================
# 8. Pipeline de TANQUES (endpoint -> transform): forma y simulador
# ===========================================================================

def test_tanks_pipeline():
    import asyncio
    src = make_source(config.Settings(demo_mode=True, token="", db_path=":memory:"))
    nodes = asyncio.run(src.fetch_tanks(None))
    asyncio.run(src.aclose())
    df = transform.tanks_to_df(nodes)
    assert list(df.columns) == config.TANK_COLS
    codes = set(df["code"])
    assert {"LFO - Main Tank", "LFO - Virtual Tank", "LFO - 174-TK-01"} <= codes
    # El Virtual Tank esta marcado como virtual; los satelites apuntan a el.
    assert bool(df.loc[df["code"] == "LFO - Virtual Tank", "virtual"].iloc[0]) is True
    sat = df[df["code"] == "LFO - 171-TK-03"].iloc[0]
    assert sat["parent_tank"] == "LFO - Virtual Tank"
    print("OK  test_tanks_pipeline")


# ===========================================================================
# 9. Pipeline de RECONCILIACIONES: forma + aritmetica del error
# ===========================================================================

def test_reconciliations_pipeline():
    import asyncio
    src = make_source(config.Settings(demo_mode=True, token="", db_path=":memory:"))
    nodes = asyncio.run(src.fetch_reconciliations(None))
    asyncio.run(src.aclose())
    df = transform.reconciliations_to_df(nodes)
    assert list(df.columns) == config.RECONCILIATION_COLS
    assert not df.empty and "LFO - Main Tank" in set(df["tank"])
    # error == (closing - opening) - (inflow - outflow), tal cual el API.
    r = df.iloc[0]
    recomputed = (r["closing_stock"] - r["opening_stock"]) - (r["inflow"] - r["outflow"])
    assert abs(float(r["error"]) - recomputed) < 0.1
    assert set(df["status"].dropna()) <= {"all_ok", "unconfirmed", "pending"}
    print("OK  test_reconciliations_pipeline")


# ===========================================================================
# 10. Ida y vuelta por SQLite (upsert idempotente + dtypes restaurados)
# ===========================================================================

def test_tanks_recons_storage_roundtrip():
    import asyncio
    import tempfile
    import shutil
    work = tempfile.mkdtemp(prefix="msgq_tank_")
    try:
        db = Database(os.path.join(work, "r.sqlite3"))
        src = make_source(config.Settings(demo_mode=True, token="", db_path=":memory:"))
        tanks = transform.tanks_to_df(asyncio.run(src.fetch_tanks(None)))
        recons = transform.reconciliations_to_df(asyncio.run(src.fetch_reconciliations(None)))
        asyncio.run(src.aclose())

        db.upsert("tanks", tanks); db.upsert("tanks", tanks)         # idempotente
        db.upsert("reconciliations", recons); db.upsert("reconciliations", recons)
        assert db.row_count("tanks") == len(tanks)
        assert db.row_count("reconciliations") == len(recons)

        back_t = db.get_tanks()
        assert list(back_t.columns) == config.TANK_COLS
        import pandas as pd
        assert pd.api.types.is_numeric_dtype(back_t["capacity"])
        back_r = db.get_reconciliations()
        assert pd.api.types.is_numeric_dtype(back_r["opening_stock"])
        assert pd.api.types.is_datetime64_any_dtype(back_r["period_end"])
        db.close()
        print("OK  test_tanks_recons_storage_roundtrip")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    tests = [
        test_classify_circuit,
        test_consumption_by_product_and_tank,
        test_consumption_by_dimension,
        test_top_consumers_and_burn_rate,
        test_flow_by_tank_and_over_time,
        test_circuit_summary_and_filter,
        test_tank_analytics_via_simulator,
        test_tanks_pipeline,
        test_reconciliations_pipeline,
        test_tanks_recons_storage_roundtrip,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FALLO  {t.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} pruebas de analitica de tanques superadas.")
    raise SystemExit(1 if failed else 0)
