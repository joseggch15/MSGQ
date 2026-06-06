# -*- coding: utf-8 -*-
"""Pruebas de la auditoría de Salud de Hardware y Sensores.

Sin mocks: DataFrames con el esquema real para verificar el valor de negocio —
regresión/estancamiento de SMU, re-tagueo sospechoso y degradación de caudal por
medidor.

Ejecutar:   pytest tests/test_hardware_health.py -v
o:          python tests/test_hardware_health.py
"""
import os
import sys
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msgq import config
from msgq.core import hardware_health as hh

_T0 = datetime(2026, 1, 1, 8, 0, 0)


# ===========================================================================
# Constructores
# ===========================================================================

def _mv(rows: list[dict]) -> pd.DataFrame:
    base = [{**{c: pd.NA for c in config.MOVEMENT_COLS},
             "kind": config.KIND_DISPENSE, **r} for r in rows]
    df = pd.DataFrame(base, columns=config.MOVEMENT_COLS)
    for c in ("volume", "smu_value", "raw_smu_value", "calculated_smu_value",
              "average_flow_rate", "peak_flow_rate"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["record_collected_at"] = pd.to_datetime(df["record_collected_at"], errors="coerce")
    return df


def _eq_master(rows: list[dict]) -> pd.DataFrame:
    base = [{**{c: pd.NA for c in config.EQUIPMENT_COLS}, **r} for r in rows]
    return pd.DataFrame(base, columns=config.EQUIPMENT_COLS)


def _smu_disp(eid, day, smu, status=config.STATUS_IN):
    d = _T0 + timedelta(days=day)
    return {"id": f"{eid}-{day}", "equipment_id": eid, "equipment_description": f"Eq {eid}",
            "equipment_status": status, "smu_value": smu, "calculated_smu_value": smu,
            "raw_smu_value": smu, "smu_type": "hrs", "record_collected_at": d.isoformat()}


def _rfid_change(record_id, day, before="OLD", after="NEW"):
    d = _T0 + timedelta(days=day)
    return {"event_key": f"{record_id}-{day}", "changed_at": d.isoformat(),
            "record_type": config.CHANGE_RECORD_RFID, "record_id": record_id,
            "event": "update", "whodunnit": "op", "attribute": config.ATTR_RFID,
            "before": before, "after": after}


def _changes(rows):
    return pd.DataFrame(rows, columns=config.CHANGE_EVENT_COLS)


# ===========================================================================
# 1. SMU — regresión
# ===========================================================================

def test_smu_regression_flags_backward_jump():
    # Sube 1000->1010->1020 y al día 8 cae a 500 (ref máx del día 5; 3 días después).
    rows = [_smu_disp("HT1", 0, 1000), _smu_disp("HT1", 1, 1010),
            _smu_disp("HT1", 5, 1020), _smu_disp("HT1", 8, 500)]
    res = hh.smu_anomalies(_mv(rows))
    reg = res[res["tipo"] == hh.TYPE_REGRESSION]
    assert len(reg) == 1
    r = reg.iloc[0]
    assert r["equipment_id"] == "HT1"
    assert r["valor_referencia"] == 1020.0 and r["valor_smu"] == 500.0
    assert r["caida"] == 520.0 and r["dias"] >= 3
    print("OK  test_smu_regression_flags_backward_jump")


def test_smu_recent_dip_not_flagged():
    # Caída de 1 día (< min días) no es regresión; luego recupera.
    rows = [_smu_disp("HT2", 0, 2000), _smu_disp("HT2", 1, 1990),
            _smu_disp("HT2", 10, 2100)]
    res = hh.smu_anomalies(_mv(rows))
    assert res[res["tipo"] == hh.TYPE_REGRESSION].empty
    print("OK  test_smu_recent_dip_not_flagged")


# ===========================================================================
# 2. SMU — estancamiento
# ===========================================================================

def test_smu_stagnation_flags_dead_sensor():
    # 5 despachos con MISMO SMU crudo (5000) en 8 días, equipo In Service.
    rows = [_smu_disp("HT3", d, 5000) for d in (0, 2, 4, 6, 8)]
    res = hh.smu_anomalies(_mv(rows), _eq_master([{"equipment_id": "HT3", "status": config.STATUS_IN}]))
    stag = res[res["tipo"] == hh.TYPE_STAGNATION]
    assert len(stag) == 1
    assert stag.iloc[0]["repeticiones"] == 5 and stag.iloc[0]["dias"] == 8
    print("OK  test_smu_stagnation_flags_dead_sensor")


def test_smu_stagnation_ignores_out_of_service():
    rows = [_smu_disp("HT4", d, 5000, status=config.STATUS_OUT) for d in (0, 2, 4, 6, 8)]
    eq = _eq_master([{"equipment_id": "HT4", "status": config.STATUS_OUT}])
    res = hh.smu_anomalies(_mv(rows), eq)
    assert res[res["tipo"] == hh.TYPE_STAGNATION].empty
    print("OK  test_smu_stagnation_ignores_out_of_service")


# ===========================================================================
# 3. Re-tagueo sospechoso
# ===========================================================================

def test_retag_flags_excessive_changes():
    # internal_id 10: 4 cambios de RFID en <30 días; 20: solo 2.
    rows = [_rfid_change("10", d) for d in (0, 5, 10, 20)] + \
           [_rfid_change("20", d) for d in (0, 40)]
    eq = _eq_master([{"equipment_id": "DOZ1", "internal_id": "10", "description": "Dozer 1"},
                     {"equipment_id": "DOZ2", "internal_id": "20"}])
    res = hh.retag_alerts(_changes(rows), eq)
    assert len(res) == 1
    r = res.iloc[0]
    assert r["internal_id"] == "10" and r["equipment_id"] == "DOZ1"
    assert r["cambios_30d"] == 4
    print("OK  test_retag_flags_excessive_changes")


def test_retag_window_is_rolling():
    # 4 cambios pero repartidos >30 días entre sí: ninguna ventana de 30 días junta >3.
    rows = [_rfid_change("30", d) for d in (0, 40, 80, 120)]
    res = hh.retag_alerts(_changes(rows), None)
    assert res.empty
    print("OK  test_retag_window_is_rolling")


# ===========================================================================
# 4. Degradación del medidor
# ===========================================================================

def _flow_disp(meter, day, flow):
    d = _T0 + timedelta(days=day)
    return {"id": f"{meter}-{day}", "equipment_id": "X", "meter_id": meter,
            "meter_description": f"Lane {meter}", "average_flow_rate": flow,
            "record_collected_at": d.isoformat()}


def test_meter_degradation_detected():
    # M1: base ~300 (días 0..20), reciente ~150 (últimos días) -> caída 50%.
    base = [_flow_disp("M1", d, 300 + (d % 3)) for d in range(0, 21, 2)]      # ~300, ≥5 muestras
    recent = [_flow_disp("M1", d, 150) for d in (25, 26, 27, 28, 29)]        # últimos 7 días
    # M2 estable ~200 ambos periodos (muestras suficientes a cada lado).
    stable = [_flow_disp("M2", d, 200) for d in range(0, 30)]
    res = hh.meter_health(_mv(base + recent + stable))
    m1 = res[res["meter_id"] == "M1"].iloc[0]
    m2 = res[res["meter_id"] == "M2"].iloc[0]
    assert bool(m1["degradado"]) is True and m1["caida_pct"] >= 40
    assert bool(m2["degradado"]) is False
    print("OK  test_meter_degradation_detected")


def test_meter_series_and_availability():
    mv = _mv([_flow_disp("M1", d, 300) for d in range(0, 10)])
    assert hh.meter_available(mv) is True
    s = hh.meter_series(mv)
    assert list(s.columns) == hh.METER_SERIES_COLS and not s.empty
    # Sin columna meter_id -> no disponible.
    mv2 = mv.drop(columns=["meter_id"])
    assert hh.meter_available(mv2) is False
    print("OK  test_meter_series_and_availability")


# ===========================================================================
# 5. Auditoría completa + robustez
# ===========================================================================

def test_audit_and_work_orders():
    rows = ([_smu_disp("HT1", 0, 1000), _smu_disp("HT1", 5, 1020), _smu_disp("HT1", 9, 400)]
            + [_smu_disp("HT3", d, 5000) for d in (0, 2, 4, 6, 8)])
    eq = _eq_master([{"equipment_id": "HT3", "status": config.STATUS_IN}])
    chg = _changes([_rfid_change("10", d) for d in (0, 5, 10, 20)])
    res = hh.audit(_mv(rows), eq, chg)
    assert not res.smu.empty and not res.retag.empty
    assert not res.work_orders.empty
    assert list(res.work_orders.columns) == hh.WORK_ORDER_COLS
    k = res.kpis
    assert k["SMU en regresión"] >= 1 and k["SMU sin pulsos"] >= 1
    assert k["Re-tagueo sospechoso"] == 1
    assert k["Órdenes de trabajo"] == len(res.work_orders)
    print("OK  test_audit_and_work_orders")


def test_empty_and_missing_columns_are_safe():
    empty = hh.audit(pd.DataFrame(), None, None)
    assert empty.smu.empty and empty.retag.empty and empty.meters.empty
    assert empty.kpis["Órdenes de trabajo"] == 0
    assert hh.smu_anomalies(None).empty
    assert hh.retag_alerts(None).empty
    assert hh.meter_health(None).empty
    print("OK  test_empty_and_missing_columns_are_safe")


if __name__ == "__main__":
    tests = [
        test_smu_regression_flags_backward_jump,
        test_smu_recent_dip_not_flagged,
        test_smu_stagnation_flags_dead_sensor,
        test_smu_stagnation_ignores_out_of_service,
        test_retag_flags_excessive_changes,
        test_retag_window_is_rolling,
        test_meter_degradation_detected,
        test_meter_series_and_availability,
        test_audit_and_work_orders,
        test_empty_and_missing_columns_are_safe,
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
