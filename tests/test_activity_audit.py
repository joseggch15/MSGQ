# -*- coding: utf-8 -*-
"""Pruebas de la auditoría de actividad (equipos fantasma + coherencia
actividad<->combustible). Misma metodología E2E del proyecto: DataFrames con el
esquema canónico real, sin mocks.

Ejecutar:   pytest tests/test_activity_audit.py -v
o:          python tests/test_activity_audit.py
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msgq import config
from msgq.core import activity_audit as aa
from msgq.core import alerts as al

NOW = pd.Timestamp("2026-06-10 12:00:00")


def _equipment() -> pd.DataFrame:
    return pd.DataFrame({
        "equipment_id": ["ACT1", "IDLE1", "NEVER1", "OUT1", "WORK1", "FROZEN1",
                         "GLITCH1"],
        "description": ["Activo", "Inactivo 40d", "Nunca", "Fuera de servicio",
                        "Haul truck", "LV congelado", "Sensor roto"],
        "category": ["Light Vehicle", "Light Vehicle", "Pumps", "Light Vehicle",
                     "Haul truck", "Light Vehicle", "Haul truck"],
        "group": ["Newmont"] * 7,
        "department": ["Mina"] * 7,
        "status": [config.STATUS_IN, config.STATUS_IN, config.STATUS_IN,
                   config.STATUS_OUT, config.STATUS_IN, config.STATUS_IN,
                   config.STATUS_IN],
    })


def _limits() -> pd.DataFrame:
    # WORK1/GLITCH1: tanque (SFL) de 1.000 L. FROZEN1: 80 L.
    return pd.DataFrame({
        "id": ["L1", "L2", "L3"],
        "equipment_id": ["WORK1", "FROZEN1", "GLITCH1"],
        "internal_id": ["1", "2", "3"], "product": ["Diesel"] * 3,
        "product_code": ["D"] * 3, "sfl": [1000.0, 80.0, 1000.0],
    })


def _mv(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["id", "record_collected_at", "equipment_id",
                                     "volume", "smu_value"])
    df["kind"] = config.KIND_DISPENSE
    df["updated_at"] = df["record_collected_at"]
    df["equipment_description"] = df["equipment_id"]
    df["product"] = "Diesel"
    df["smu_type"] = "hours"
    return df


def _movements() -> pd.DataFrame:
    rows = []
    # ACT1: despacho reciente (5 días) -> no fantasma con umbral 15.
    rows.append(("A1", "2026-06-05T08:00:00", "ACT1", 50.0, None))
    # IDLE1: último despacho hace 40 días -> fantasma.
    rows.append(("I1", "2026-05-01T08:00:00", "IDLE1", 50.0, None))
    # WORK1 (haul truck, SFL 1.000, burn rate típico 100 L/h):
    # 5 intervalos normales de 10 h / 1.000 L cada uno...
    smu = 1000.0
    for i in range(5):
        rows.append((f"W{i}", f"2026-03-{10 + i:02d}T08:00:00", "WORK1",
                     1000.0, smu))
        smu += 10.0
    # ...y luego un salto de 70 h sin repostar (1040 -> 1110): consumo esperado
    # ~7.000 L >> SFL 1.000 * 1.2 -> 'trabaja sin repostar' (~6.000 L sin registrar).
    rows.append(("W9", "2026-04-20T08:00:00", "WORK1", 1000.0, smu + 60.0))
    # FROZEN1 (SFL 80): 4 despachos de 40 L en 12 días con el MISMO SMU
    # -> racha 'repostado sin operar' con 120 L > SFL (sobre_sfl).
    for i, day in enumerate((1, 5, 9, 13)):
        rows.append((f"F{i}", f"2026-06-{day:02d}T08:00:00", "FROZEN1",
                     40.0, 5000.0))
    # GLITCH1: 4 intervalos diarios normales de 10 h y luego un SALTO corrupto
    # del horómetro (+500.000 h en un día, físicamente imposible). El filtro de
    # plausibilidad debe descartarlo (no es actividad real, es sensor dañado).
    smu = 100.0
    for i in range(5):
        rows.append((f"G{i}", f"2026-02-{10 + i:02d}T08:00:00", "GLITCH1",
                     1000.0, smu))
        smu += 10.0
    rows.append(("G9", "2026-02-16T08:00:00", "GLITCH1", 1000.0, 500000.0))
    return _mv(rows)


def test_idle_assets_classes_and_threshold():
    idle = aa.idle_assets(_equipment(), _movements(), now=NOW, min_days=15)
    ids = set(idle["equipment_id"])
    assert "IDLE1" in ids and "NEVER1" in ids
    assert "ACT1" not in ids            # despachó hace 5 días
    assert "OUT1" not in ids            # no está In Service
    never = idle[idle["equipment_id"] == "NEVER1"].iloc[0]
    assert never["clase"] == aa.CLASS_NEVER
    assert pd.isna(never["ultimo_despacho"])
    i1 = idle[idle["equipment_id"] == "IDLE1"].iloc[0]
    assert i1["clase"] == aa.CLASS_IDLE and 39 <= i1["dias_sin_despachar"] <= 41
    # El nunca-despachó encabeza la lista (inactividad infinita).
    assert idle.iloc[0]["equipment_id"] == "NEVER1"
    print("OK  test_idle_assets_classes_and_threshold")


def test_unfueled_activity_detects_missing_fuel():
    out = aa.unfueled_activity(_movements(), _equipment(), _limits())
    assert len(out) == 1
    r = out.iloc[0]
    assert r["equipment_id"] == "WORK1"
    assert r["smu_delta"] == 70.0                        # 1040 -> 1110
    assert abs(r["burn_rate_tipico"] - 100.0) < 1e-6     # mediana de sus intervalos
    assert abs(r["consumo_esperado"] - 7000.0) < 1.0
    assert r["sfl"] == 1000.0
    assert abs(r["no_registrado"] - 6000.0) < 1.0        # esperado - despachado
    # FROZEN1 no aparece aquí: su SMU no avanza (es del detector 3). GLITCH1
    # tampoco: su salto de +500.000 h en un día es físicamente imposible y el
    # filtro de plausibilidad lo descarta (sensor dañado, no actividad).
    assert "GLITCH1" not in set(out["equipment_id"])
    print("OK  test_unfueled_activity_detects_missing_fuel")


def test_fueling_without_activity_detects_frozen_run():
    out = aa.fueling_without_activity(_movements(), _equipment(), _limits())
    assert len(out) == 1
    r = out.iloc[0]
    assert r["equipment_id"] == "FROZEN1"
    assert r["despachos"] == 4                            # racha completa
    assert abs(r["litros"] - 120.0) < 1e-6                # 3 despachos posteriores
    assert r["sfl"] == 80.0 and bool(r["sobre_sfl"])      # 120 L > tanque de 80
    assert r["smu_estancado"] == 5000.0
    assert 11 <= r["dias"] <= 13
    # WORK1 no aparece: su SMU avanza en todos los intervalos.
    print("OK  test_fueling_without_activity_detects_frozen_run")


def test_detect_activity_alerts_schema_and_severities():
    alerts = al.detect_activity_alerts(_movements(), _equipment(), _limits(),
                                       now=NOW)
    assert list(alerts.columns) == al.ALERT_COLS
    by_cat = alerts.groupby("category").size().to_dict()
    # IDLE1 (40d) + NEVER1 + WORK1 (51d) + GLITCH1 (114d desde el 16/02).
    assert by_cat.get(config.ALERT_IDLE_ASSET, 0) == 4
    assert by_cat.get(config.ALERT_UNFUELED_ACTIVITY, 0) == 1  # WORK1
    assert by_cat.get(config.ALERT_FUELING_IDLE, 0) == 1       # FROZEN1
    unf = alerts[alerts["category"] == config.ALERT_UNFUELED_ACTIVITY].iloc[0]
    assert unf["severity"] == al.SEV_CRITICAL
    frz = alerts[alerts["category"] == config.ALERT_FUELING_IDLE].iloc[0]
    assert frz["severity"] == al.SEV_CRITICAL                  # 120 L > SFL 80
    idle = alerts[alerts["category"] == config.ALERT_IDLE_ASSET]
    assert (idle["severity"] == al.SEV_WARNING).all()
    print("OK  test_detect_activity_alerts_schema_and_severities")


if __name__ == "__main__":
    tests = [
        test_idle_assets_classes_and_threshold,
        test_unfueled_activity_detects_missing_fuel,
        test_fueling_without_activity_detects_frozen_run,
        test_detect_activity_alerts_schema_and_severities,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FALLO  {fn.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} pruebas de actividad superadas.")
    raise SystemExit(1 if failed else 0)
