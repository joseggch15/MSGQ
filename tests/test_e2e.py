# -*- coding: utf-8 -*-
"""Pruebas End-to-End del monitor FMS (MSGQ).

METODOLOGIA (igual que el resto del ecosistema): E2E con el pipeline REAL y la
fuente simulada — sin mocks. Cada prueba ejercita el flujo completo:

    simulador -> transform (JSON->DataFrame) -> SQLite real -> lectura ->
    deteccion de alertas / KPIs.

Ejecutar:   pytest tests/test_e2e.py -v
o:          python tests/test_e2e.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

import pandas as pd

# Permite `import msgq` al correr el archivo directamente.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msgq import config
from msgq.api import make_source
from msgq.core import alerts as al
from msgq.core import equipment_analytics as ea
from msgq.core import transform
from msgq.storage import Database


def _settings(db_path: str) -> config.Settings:
    return config.Settings(demo_mode=True, token="", db_path=db_path, poll_seconds=3)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Helpers para construir nodos de movimiento (forma cruda de GraphQL)
# ---------------------------------------------------------------------------

def _mv_node(**over) -> dict:
    """Construye un node de movimiento en la forma GraphQL real (camelCase)."""
    base = {
        "id": "M1", "kind": config.KIND_DISPENSE, "type": config.TYPE_AUTO,
        "status": "all_ok", "volume": "100.0",
        "recordUpdatedAt": datetime.now().isoformat(),
        "avgContamination4": 12, "avgContamination6": 10, "avgContamination14": 8,
        "site": {"code": "MERIAN", "description": "Merian"},
        "product": {"code": "DIESEL", "description": "Diesel"},
        "target": {"equipmentId": "001", "description": "CAT 785",
                   "status": config.STATUS_IN},
        "serviceTruck": None,
    }
    # `target` se reemplaza por completo si se provee (dispense=equipo, transfer=tanque).
    base.update(over)
    return base


# ===========================================================================
# 1. Forma y tipos del pipeline simulador -> DataFrame
# ===========================================================================

def test_e2e_simulator_pipeline_shapes():
    src = make_source(_settings(":memory:"))
    mv_nodes = _run(src.fetch_movements(None))
    eq_nodes = _run(src.fetch_equipment(None))
    mac_nodes = _run(src.fetch_adaptmacs(None))
    _run(src.aclose())

    assert mv_nodes, "el simulador debe producir movimientos"
    assert len(eq_nodes) >= 40, "el roster debe traer la flota completa"

    mv = transform.movements_to_df(mv_nodes)
    eq = transform.equipment_to_df(eq_nodes)
    mac = transform.adaptmacs_to_df(mac_nodes)

    assert list(mv.columns) == config.MOVEMENT_COLS
    assert list(eq.columns) == config.EQUIPMENT_COLS
    assert list(mac.columns) == config.ADAPTMAC_COLS

    # Tipos coercidos correctamente (volume llega como String desde GraphQL).
    assert pd.api.types.is_numeric_dtype(mv["volume"])
    assert pd.api.types.is_datetime64_any_dtype(mv["updated_at"])
    # Equipo (forma GraphQL documentada): clave e estado presentes.
    assert eq["equipment_id"].notna().all()
    assert eq["status"].isin([config.STATUS_IN, config.STATUS_OUT, config.STATUS_DECOM]).any()
    # El producto viaja por-movimiento (no en el Equipment Item).
    assert mv["product"].notna().any()
    print("OK  test_e2e_simulator_pipeline_shapes")


# ===========================================================================
# 2. Ida y vuelta por SQLite + watermark
# ===========================================================================

def test_e2e_db_roundtrip_and_watermark():
    work = tempfile.mkdtemp(prefix="msgq_e2e_")
    try:
        db = Database(os.path.join(work, "r.sqlite3"))
        src = make_source(_settings(":memory:"))
        mv = transform.movements_to_df(_run(src.fetch_movements(None)))
        eq = transform.equipment_to_df(_run(src.fetch_equipment(None)))
        _run(src.aclose())

        n_mv = db.upsert("movements", mv)
        n_eq = db.upsert("equipment", eq)
        assert n_mv == len(mv) and n_eq == len(eq)
        assert db.row_count("movements") == len(mv)
        assert db.row_count("equipment") == len(eq)

        # Lectura: columnas canonicas y dtypes restaurados.
        back = db.get_movements()
        assert list(back.columns) == config.MOVEMENT_COLS
        assert pd.api.types.is_numeric_dtype(back["volume"])

        # Watermark.
        wm_dt = mv["updated_at"].max().to_pydatetime()
        db.set_watermark("movements", wm_dt)
        got = db.get_watermark("movements")
        assert got is not None and abs((got - wm_dt).total_seconds()) < 1
        db.close()
        print("OK  test_e2e_db_roundtrip_and_watermark")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ===========================================================================
# 3. Upsert idempotente (no duplica por clave primaria)
# ===========================================================================

def test_e2e_upsert_idempotent():
    work = tempfile.mkdtemp(prefix="msgq_e2e_")
    try:
        db = Database(os.path.join(work, "r.sqlite3"))
        eq = transform.equipment_to_df([
            _eq_node("001"), _eq_node("002"),
        ])
        db.upsert("equipment", eq)
        db.upsert("equipment", eq)   # mismo lote otra vez
        assert db.row_count("equipment") == 2, "no debe duplicar por equipment_id"
        db.close()
        print("OK  test_e2e_upsert_idempotent")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _eq_node(eid: str) -> dict:
    return {
        "equipmentId": eid, "description": f"Equipo {eid}",
        "status": config.STATUS_IN, "fieldId": eid,
        "erpReference": f"SAP-{eid}", "rfidTags": [f"E280{eid}"],
    }


# ===========================================================================
# 4. Deteccion de alertas sobre movimientos
# ===========================================================================

def test_e2e_alert_detection():
    nodes = [
        _mv_node(id="A1", type=config.TYPE_KEY_BYPASS),                       # critica
        _mv_node(id="A2", kind=config.KIND_DISPENSE,
                 target={"status": config.STATUS_OUT}),                       # critica
        _mv_node(id="A3", avgContamination4=20),                             # advertencia
        _mv_node(id="A4"),                                                   # normal
    ]
    mv = transform.movements_to_df(nodes)
    alerts = al.detect_movement_alerts(mv)

    cats = set(alerts["category"])
    assert "Modo de transaccion anomalo" in cats
    assert "Despacho a equipo no operativo" in cats
    assert "Contaminacion de combustible alta" in cats

    crit = alerts[alerts["severity"] == al.SEV_CRITICAL]
    assert set(crit["source_id"]) >= {"A1", "A2"}

    summary = al.alert_summary(alerts)
    assert summary["Alertas"].sum() == len(alerts)
    print("OK  test_e2e_alert_detection")


# ===========================================================================
# 5. Service truck en bypass con volumen acumulado atipico
# ===========================================================================

def test_e2e_service_truck_bypass_volume():
    nodes = [
        _mv_node(id=f"T{i}", kind=config.KIND_TRANSFER,
                 type=config.TYPE_KEY_BYPASS, volume="9000.0",
                 serviceTruck={"equipmentId": "TFL0846", "description": "ST 846"},
                 target={"code": "T-ST", "name": "Service Tank"})
        for i in range(3)   # 3 x 9000 = 27.000 L > 24.000 L
    ]
    mv = transform.movements_to_df(nodes)
    alerts = al.detect_movement_alerts(mv)
    agg = alerts[alerts["category"] == "Service truck en bypass (volumen acumulado)"]
    assert len(agg) == 1, "debe emitir una alerta agregada por service truck"
    assert agg.iloc[0]["equipment_id"] == "TFL0846"
    assert agg.iloc[0]["volume"] >= config.SERVICE_TRUCK_BYPASS_VOLUME_L
    print("OK  test_e2e_service_truck_bypass_volume")


# ===========================================================================
# 6. Alertas de consolas AdaptMAC
# ===========================================================================

def test_e2e_adaptmac_alerts():
    now = datetime.now().isoformat()
    mac = transform.adaptmacs_to_df([
        {"code": "MAC-01", "online": False, "keyBypass": False},
        {"code": "MAC-02", "online": True, "keyBypass": True,
         "lastSuccessfulComms": now},
    ])
    alerts = al.detect_adaptmac_alerts(mac)
    cats = set(alerts["category"])
    assert "Consola offline" in cats
    assert "Consola en modo bypass" in cats
    print("OK  test_e2e_adaptmac_alerts")


# ===========================================================================
# 7. Ciclo completo con la misma logica del poller (sin QThread)
# ===========================================================================

def test_e2e_full_cycle_like_poller():
    work = tempfile.mkdtemp(prefix="msgq_e2e_")
    try:
        db = Database(os.path.join(work, "r.sqlite3"))
        src = make_source(_settings(":memory:"))

        pipeline = [
            ("movements", "fetch_movements", transform.movements_to_df),
            ("equipment", "fetch_equipment", transform.equipment_to_df),
            ("adaptmac",  "fetch_adaptmacs", transform.adaptmacs_to_df),
        ]
        for entity, method, to_df in pipeline:
            nodes = _run(getattr(src, method)(None))
            db.upsert(entity, to_df(nodes))
        _run(src.aclose())

        recent = db.recent_movements(hours=24)
        eq = db.get_equipment()
        mac = db.get_adaptmac()
        alerts = al.combine(
            al.detect_movement_alerts(recent), al.detect_adaptmac_alerts(mac),
        )
        kpis = al.compute_kpis(recent, eq, mac, alerts)

        assert kpis["movimientos"] >= 1
        assert kpis["equipos_in_service"] >= 1
        assert kpis["consolas_total"] >= 1
        db.close()
        print("OK  test_e2e_full_cycle_like_poller")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ===========================================================================
# 8. Importacion de un export CSV de equipos de AdaptIQ (formato completo)
# ===========================================================================

def _write_equipment_csv(path: str) -> None:
    import csv
    headers = [
        "Equipment ID", "Description", "Field ID", "Equipment Group Description",
        "Equipment Category Description", "Status Description", "Enabled Products",
        "RFID", "Make", "Model", "Is Light Vehicle?", "Is Pod?",
        "Is Service Truck?", "Is Contractor Vehicle?", "Cost Centre", "Department",
        "Service Interval", "Service Interval Type", "Last SMU Value",
        "Last SMU Type", "Last SMU Date", "Dispense Limit Period",
        "ERP Reference", "Site", "Last Capture Date",
    ]
    rows = [
        # Haul truck en servicio.
        ["785", "CAT 785 #785", "785", "Haul Trucks", "N-HT", "In Service",
         "DIESEL:200.0:100.0|15W40::", "E280ABC", "Caterpillar", "785D",
         "false", "false", "false", "false", "CC-1001", "Mining",
         "250", "hrs", "12345", "hrs", "2026-05-30 10:00:00", "",
         "SAP-785", "Merian", "2026-05-31 08:00:00"],
        # Service truck (limite de despacho activo).
        ["TFL0846", "Service Truck 846", "TFL0846", "Service Trucks", "N-ST",
         "In Service", "DIESEL::", "E280DEF", "Isuzu", "FVR",
         "false", "false", "true", "false", "CC-2050", "Maintenance",
         "500", "hrs", "8000", "hrs", "2026-05-29 12:00:00", "shift",
         "SAP-846", "Merian", "2026-05-31 09:00:00"],
        # Light vehicle fuera de servicio.
        ["LV01", "Toyota Hilux LV01", "LV01", "Light Vehicles", "N-LV",
         "Out of Service", "UNLEADED:80.0:", "E280GHI", "Toyota", "Hilux",
         "true", "false", "false", "false", "CC-3010", "Logistics",
         "10000", "kms", "55000", "kms", "2026-05-20 07:00:00", "weekly",
         "SAP-LV01", "Merian", "2026-05-31 07:30:00"],
        # Fila sin Equipment ID: debe descartarse.
        ["", "Sin ID", "", "X", "X", "In Service", "DIESEL::", "", "", "",
         "false", "false", "false", "false", "", "", "", "", "", "", "", "",
         "", "Merian", ""],
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        w.writerows(rows)


def test_e2e_equipment_csv_import():
    from msgq.io import load_equipment_csv
    work = tempfile.mkdtemp(prefix="msgq_e2e_")
    try:
        csv_path = os.path.join(work, "equipos.csv")
        _write_equipment_csv(csv_path)

        df = load_equipment_csv(csv_path)
        assert list(df.columns) == config.EQUIPMENT_COLS
        assert len(df) == 3, "la fila sin Equipment ID debe descartarse"

        truck = df[df["equipment_id"] == "TFL0846"].iloc[0]
        assert bool(truck["is_service_truck"]) is True
        assert truck["status"] == config.STATUS_IN
        assert truck["product"] == "Diesel"
        assert bool(truck["dispense_limited"]) is True   # tiene periodo 'shift'

        lv = df[df["equipment_id"] == "LV01"].iloc[0]
        assert bool(lv["is_light_vehicle"]) is True
        assert bool(lv["is_service_truck"]) is False
        assert lv["product"] == "Unleaded Gasoline"
        assert lv["status"] == config.STATUS_OUT

        # Ida y vuelta por SQLite + idempotencia.
        db = Database(os.path.join(work, "r.sqlite3"))
        db.upsert("equipment", df)
        db.upsert("equipment", df)
        assert db.row_count("equipment") == 3
        kpis = al.compute_kpis(None, db.get_equipment(), None, None)
        assert kpis["equipos_in_service"] == 2
        assert kpis["equipos_out_service"] == 1
        db.close()
        print("OK  test_e2e_equipment_csv_import")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ===========================================================================
# 9. Log de auditoria: transform, storage e idempotencia
# ===========================================================================

def _chg_node(rtype, rid, event, attr, before, after, when=None, who="u@x"):
    return {
        "changedAt": (when or datetime.now()).isoformat(),
        "recordType": rtype, "recordId": rid, "event": event, "whodunnit": who,
        "changes": [{"attribute": attr, "before": before, "after": after}],
    }


def test_e2e_change_events_transform_and_storage():
    work = tempfile.mkdtemp(prefix="msgq_e2e_")
    try:
        nodes = [
            _chg_node("EquipmentItem", "5", "update", "equipment_status_id", "1", "2", datetime(2026, 1, 1)),
            _chg_node("EquipmentRfid", "10", "create", "rfid", None, "AAA", datetime(2026, 1, 2)),
            _chg_node("EquipmentRfid", "10", "update", "rfid", "AAA", "BBB", datetime(2026, 1, 3)),
        ]
        df = transform.change_events_to_df(nodes)
        assert list(df.columns) == config.CHANGE_EVENT_COLS
        assert len(df) == 3
        assert df["event_key"].is_unique
        assert pd.api.types.is_datetime64_any_dtype(df["changed_at"])

        db = Database(os.path.join(work, "r.sqlite3"))
        db.upsert("change_events", df)
        db.upsert("change_events", df)   # idempotente por event_key
        assert db.row_count("change_events") == 3
        db.set_watermark("change_events", df["changed_at"].max().to_pydatetime())
        assert db.get_watermark("change_events") is not None
        db.close()
        print("OK  test_e2e_change_events_transform_and_storage")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ===========================================================================
# 10. Frecuencia de cambio de RFID
# ===========================================================================

def test_e2e_rfid_change_frequency():
    base = datetime(2026, 1, 15)
    nodes = [
        _chg_node("EquipmentRfid", "1", "create", "rfid", None, "T1", base),
        _chg_node("EquipmentRfid", "1", "update", "rfid", "T1", "T2", base + timedelta(days=40)),
        _chg_node("EquipmentRfid", "2", "create", "rfid", None, "T3", base + timedelta(days=5)),
        _chg_node("EquipmentRfid", "3", "destroy", "rfid", "T4", None, base + timedelta(days=70)),
        # ruido que NO es RFID:
        _chg_node("EquipmentItem", "9", "update", "equipment_status_id", "1", "2", base),
    ]
    changes = transform.change_events_to_df(nodes)
    summary = ea.rfid_change_summary(changes)
    assert summary["Eventos RFID"] == 4
    assert summary["Asignados"] == 2 and summary["Cambiados"] == 1 and summary["Removidos"] == 1
    over_time = ea.rfid_changes_over_time(changes)
    assert not over_time.empty and over_time["Total"].sum() == 4
    churn = ea.rfid_churn_by_tag(changes)
    assert int(churn.loc[churn["record_id"] == "1", "Eventos"].iloc[0]) == 2
    print("OK  test_e2e_rfid_change_frequency")


# ===========================================================================
# 11. Transiciones de estado In->Out + tiempo en servicio
# ===========================================================================

def test_e2e_status_transitions():
    base = datetime(2026, 1, 1)
    nodes = [
        _chg_node("EquipmentItem", "5", "create", "equipment_status_id", None, "1", base),
        _chg_node("EquipmentItem", "5", "update", "equipment_status_id", "1", "2", base + timedelta(days=30)),
        _chg_node("EquipmentItem", "5", "update", "equipment_status_id", "2", "1", base + timedelta(days=40)),
        _chg_node("EquipmentItem", "5", "update", "equipment_status_id", "1", "2", base + timedelta(days=70)),
    ]
    changes = transform.change_events_to_df(nodes)
    eq = transform.equipment_to_df([
        {"id": "5", "equipmentId": "HTK0805", "description": "Dump 805", "status": "Out of Service"},
    ])
    trans = ea.status_transitions(changes, eq)
    # 3 transiciones reales (descarta el create None->1).
    assert len(trans) == 3
    assert trans["equipment_id"].iloc[0] == "HTK0805"   # enlazado por internal_id
    summary = ea.status_transition_summary(trans)
    in_out = summary.loc[summary["Transicion"] == "In Service -> Out of Service", "Veces"]
    assert int(in_out.iloc[0]) == 2

    io = ea.in_to_out_over_time(trans)
    assert io["In->Out"].sum() == 2
    tis = ea.time_in_service(trans)
    assert int(tis["Salidas a Out"].iloc[0]) == 2
    # Tiempo en servicio: (dia30-dia0)=30 y (dia70-dia40)=30 -> promedio 30.
    assert abs(float(tis["Dias prom. en servicio"].iloc[0]) - 30.0) < 0.1
    print("OK  test_e2e_status_transitions")


# ===========================================================================
# 12. Cambios via simulador -> analitica no vacia
# ===========================================================================

def test_e2e_changes_via_simulator():
    src = make_source(_settings(":memory:"))
    eq = transform.equipment_to_df(_run(src.fetch_equipment(None)))
    rfid_nodes = _run(src.fetch_changes("EquipmentRfid", None))
    eqp_nodes = _run(src.fetch_changes("EquipmentItem", None))
    _run(src.aclose())
    changes = pd.concat([transform.change_events_to_df(rfid_nodes),
                         transform.change_events_to_df(eqp_nodes)], ignore_index=True)
    assert not changes.empty
    assert ea.rfid_change_summary(changes)["Eventos RFID"] >= 1
    trans = ea.status_transitions(changes, eq)
    assert not trans.empty
    assert not ea.audit_by_user(changes).empty
    # internal_id presente en el inventario (para enlazar transiciones).
    assert eq["internal_id"].notna().any()
    print("OK  test_e2e_changes_via_simulator")


if __name__ == "__main__":
    tests = [
        test_e2e_simulator_pipeline_shapes,
        test_e2e_db_roundtrip_and_watermark,
        test_e2e_upsert_idempotent,
        test_e2e_alert_detection,
        test_e2e_service_truck_bypass_volume,
        test_e2e_adaptmac_alerts,
        test_e2e_full_cycle_like_poller,
        test_e2e_equipment_csv_import,
        test_e2e_change_events_transform_and_storage,
        test_e2e_rfid_change_frequency,
        test_e2e_status_transitions,
        test_e2e_changes_via_simulator,
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
    print(f"\n{len(tests) - failed}/{len(tests)} pruebas E2E superadas.")
    raise SystemExit(1 if failed else 0)
