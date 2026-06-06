# -*- coding: utf-8 -*-
"""Pruebas del modulo 'Tag Hopping' (el tag en el bolsillo).

METODOLOGIA (igual que el resto del ecosistema): sin mocks. Las deterministas
construyen DataFrames con el esquema real (`config.MOVEMENT_COLS` /
`config.EQUIPMENT_COLS`); la smoke ejercita el pipeline simulador -> transform ->
SQLite -> deteccion.

Valor de negocio: marcar el MISMO tag (equipo) despachando en dos lugares en un
lapso fisicamente imposible — solapamiento temporal (sin coordenadas, la senal de
cobertura) o velocidad implicita implausible (con GPS de la transaccion o con el
mapa opcional de coordenadas de islas fijas).

Ejecutar:   pytest tests/test_tag_hopping.py -v
o:          python tests/test_tag_hopping.py
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
from msgq.core import tag_hopping as th
from msgq.core import transform
from msgq.storage import Database

_T0 = pd.Timestamp("2026-01-01 08:00:00")


def _mv_df(rows: list[dict]) -> pd.DataFrame:
    base = [{**{c: pd.NA for c in config.MOVEMENT_COLS}, **r} for r in rows]
    df = pd.DataFrame(base, columns=config.MOVEMENT_COLS)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df["flow_duration_s"] = pd.to_numeric(df["flow_duration_s"], errors="coerce")
    df["record_collected_at"] = pd.to_datetime(df["record_collected_at"], errors="coerce")
    return df


def _eq_df(rows: list[dict]) -> pd.DataFrame:
    base = [{**{c: pd.NA for c in config.EQUIPMENT_COLS}, **r} for r in rows]
    return pd.DataFrame(base, columns=config.EQUIPMENT_COLS)


def _disp(id_, eq, tank, t_min, dur_s=120, gps=None, light_tank_meter=None):
    return {"id": id_, "kind": config.KIND_DISPENSE, "equipment_id": eq,
            "equipment_description": f"Equipo {eq}", "tank": tank,
            "meter_id": light_tank_meter, "product": "DIESEL", "volume": 500,
            "flow_duration_s": dur_s, "gps_coordinates": gps,
            "record_collected_at": _T0 + timedelta(minutes=t_min)}


# ===========================================================================
# 1. Solapamiento temporal en dos puntos distintos -> CRITICO
# ===========================================================================

def test_overlap_different_locations_critical():
    """Mismo equipo: tanque A a t0 (dura 10 min) y tanque B a t0+5min -> sus
    intervalos se solapan 5 min en lugares distintos = imposible."""
    mv = _mv_df([
        _disp("a", "EQ1", "Tank A", 0, dur_s=600),
        _disp("b", "EQ1", "Tank B", 5, dur_s=120),
    ])
    ev = th.tag_hops(mv)
    assert len(ev) == 1
    r = ev.iloc[0]
    assert r["reason"] == config.TAG_HOP_REASON_OVERLAP
    assert r["severity"] == "CRITICAL"
    assert r["location_prev"] == "Tank A" and r["location"] == "Tank B"

    alerts = al.detect_tag_hopping_alerts(mv)
    assert len(alerts) == 1
    assert alerts.iloc[0]["category"] == config.ALERT_TAG_HOPPING
    assert alerts.iloc[0]["severity"] == al.SEV_CRITICAL


# ===========================================================================
# 2. Mismo tanque (dos mangueras) -> NO es hopping
# ===========================================================================

def test_same_location_not_flagged():
    mv = _mv_df([
        _disp("a", "EQ1", "Tank A", 0, dur_s=600),
        _disp("b", "EQ1", "Tank A", 5, dur_s=120),   # mismo lugar
    ])
    assert th.tag_hops(mv).empty


# ===========================================================================
# 3. Secuencial sin solapamiento ni coordenadas -> NO se marca
# ===========================================================================

def test_sequential_no_overlap_not_flagged():
    mv = _mv_df([
        _disp("a", "EQ1", "Tank A", 0, dur_s=120),    # 2 min
        _disp("b", "EQ1", "Tank B", 30, dur_s=120),   # 30 min despues -> sin solape
    ])
    assert th.tag_hops(mv).empty


# ===========================================================================
# 4. Velocidad implicita imposible (GPS por transaccion) -> WARNING
# ===========================================================================

def test_gps_speed_impossible_heavy():
    """~5 km en 5 min = 60 km/h: imposible para un equipo pesado (umbral 40)."""
    mv = _mv_df([
        _disp("a", "EQ1", "Tank A", 0, dur_s=60, gps="5.000,-54.000"),
        _disp("b", "EQ1", "Tank B", 5, dur_s=60, gps="5.045,-54.000"),
    ])
    ev = th.tag_hops(mv)
    assert len(ev) == 1
    r = ev.iloc[0]
    assert r["reason"] == config.TAG_HOP_REASON_SPEED
    assert r["severity"] == "WARNING"
    assert 4.5 <= float(r["distance_km"]) <= 5.5
    assert float(r["speed_kmh"]) > config.TAG_HOP_MAX_SPEED_KMH


def test_gps_speed_ok_for_light_vehicle():
    """Mismo escenario (60 km/h) pero el equipo es vehiculo ligero (umbral 100):
    se desplaza rapido legitimamente -> NO se marca."""
    mv = _mv_df([
        _disp("a", "LV1", "Tank A", 0, dur_s=60, gps="5.000,-54.000"),
        _disp("b", "LV1", "Tank B", 5, dur_s=60, gps="5.045,-54.000"),
    ])
    eq = _eq_df([{"equipment_id": "LV1", "is_light_vehicle": True,
                  "description": "Hilux"}])
    assert th.tag_hops(mv, eq).empty


# ===========================================================================
# 5. Mapa opcional de coordenadas: habilita la regla de velocidad sin GPS
# ===========================================================================

def test_point_coords_map_enables_speed():
    """Las islas fijas no traen GPS por transaccion; el mapa opcional aporta sus
    coordenadas y asi la regla de velocidad tambien las cubre."""
    mv = _mv_df([
        _disp("a", "EQ1", "Tank A", 0, dur_s=60),     # sin GPS
        _disp("b", "EQ1", "Tank B", 5, dur_s=60),     # sin GPS
    ])
    coords = {"Tank A": (5.000, -54.000), "Tank B": (5.045, -54.000)}
    ev = th.tag_hops(mv, point_coords=coords)
    assert len(ev) == 1
    assert ev.iloc[0]["reason"] == config.TAG_HOP_REASON_SPEED


# ===========================================================================
# 6. Teletransporte: distancia sin tiempo entre medio -> CRITICO
# ===========================================================================

def test_teleport_critical():
    mv = _mv_df([
        _disp("a", "EQ1", "Tank A", 0, dur_s=0, gps="5.000,-54.000"),
        _disp("b", "EQ1", "Tank B", 0, dur_s=0, gps="5.045,-54.000"),   # mismo instante
    ])
    ev = th.tag_hops(mv)
    assert len(ev) == 1
    r = ev.iloc[0]
    assert r["reason"] == config.TAG_HOP_REASON_SPEED
    assert r["severity"] == "CRITICAL"          # gap 0 -> teletransporte
    assert pd.isna(r["speed_kmh"])              # velocidad infinita -> None


# ===========================================================================
# 7. El tag se resuelve del maestro de equipos
# ===========================================================================

def test_tag_resolved_from_master():
    mv = _mv_df([
        _disp("a", "EQ1", "Tank A", 0, dur_s=600),
        _disp("b", "EQ1", "Tank B", 5, dur_s=120),
    ])
    eq = _eq_df([{"equipment_id": "EQ1", "rfid": "E28011AABBCC, E28099",
                  "description": "Dozer"}])
    ev = th.tag_hops(mv, eq)
    assert ev.iloc[0]["tag"] == "E28011AABBCC"


# ===========================================================================
# 8. Haversine sanity
# ===========================================================================

def test_haversine_known_distance():
    d = th.haversine_km((5.000, -54.000), (5.045, -54.000))
    assert 4.8 <= d <= 5.2          # ~0,045 grados de latitud ≈ 5 km


# ===========================================================================
# 9. Smoke: pipeline real con el simulador (no debe romper)
# ===========================================================================

def test_smoke_simulator_pipeline():
    import tempfile
    db_path = os.path.join(tempfile.mkdtemp(), "smoke.sqlite3")
    db = Database(db_path)
    try:
        src = make_source(config.Settings(demo_mode=True, token="", db_path=db_path))

        async def pull():
            eq_nodes = await src.fetch_equipment(None)
            mv_nodes = []
            for _ in range(20):
                mv_nodes += await src.fetch_movements(None)
            return eq_nodes, mv_nodes

        eq_nodes, mv_nodes = asyncio.run(pull())
        db.upsert("equipment", transform.equipment_to_df(eq_nodes))
        db.upsert("movements", transform.movements_to_df(mv_nodes))

        mv = db.read("movements")
        eq = db.get_equipment()
        ev = th.tag_hops(mv, eq)
        assert list(ev.columns) == th.EVENT_COLS
        alerts = al.detect_tag_hopping_alerts(mv, eq)
        assert list(alerts.columns) == al.ALERT_COLS
    finally:
        db.close()


if __name__ == "__main__":
    tests = [
        test_overlap_different_locations_critical,
        test_same_location_not_flagged,
        test_sequential_no_overlap_not_flagged,
        test_gps_speed_impossible_heavy,
        test_gps_speed_ok_for_light_vehicle,
        test_point_coords_map_enables_speed,
        test_teleport_critical,
        test_tag_resolved_from_master,
        test_haversine_known_distance,
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
    print(f"\n{len(tests) - failed}/{len(tests)} pruebas de tag_hopping superadas.")
    raise SystemExit(1 if failed else 0)
