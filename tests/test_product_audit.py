# -*- coding: utf-8 -*-
"""Pruebas del modulo 'Coherencia Producto <-> Equipo' (posible tag clonado).

METODOLOGIA (igual que el resto del ecosistema): sin mocks. Las deterministas
construyen DataFrames con el esquema real (`config.*_COLS`); la smoke ejercita el
pipeline simulador -> transform -> SQLite -> deteccion.

Valor de negocio: marcar un despacho cuyo producto es AJENO al equipo (DIESEL vs
Coolant/Hidraulico) — tag clonado o maestro mal configurado — SIN falsear cuando
el producto estuvo habilitado y luego se deshabilito (se reconoce por su huella
de uso en el propio historial del equipo).

Ejecutar:   pytest tests/test_product_audit.py -v
o:          python tests/test_product_audit.py
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
from msgq.core import product_audit as pa
from msgq.core import transform
from msgq.storage import Database


# ---------------------------------------------------------------------------
# Constructores de datos (esquema real)
# ---------------------------------------------------------------------------

_T0 = pd.Timestamp("2026-01-01")


def _mv_df(rows: list[dict]) -> pd.DataFrame:
    base = [{**{c: pd.NA for c in config.MOVEMENT_COLS}, **r} for r in rows]
    df = pd.DataFrame(base, columns=config.MOVEMENT_COLS)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df["record_collected_at"] = pd.to_datetime(df["record_collected_at"], errors="coerce")
    return df


def _lim_df(rows: list[dict]) -> pd.DataFrame:
    base = [{**{c: pd.NA for c in config.CONSUMPTION_LIMIT_COLS}, **r} for r in rows]
    return pd.DataFrame(base, columns=config.CONSUMPTION_LIMIT_COLS)


def _ph_df(rows: list[dict]) -> pd.DataFrame:
    base = [{**{c: pd.NA for c in config.PRODUCT_HISTORY_COLS}, **r} for r in rows]
    df = pd.DataFrame(base, columns=config.PRODUCT_HISTORY_COLS)
    for c in ("first_seen", "last_seen"):
        df[c] = pd.to_datetime(df[c], errors="coerce")
    return df


def _disp(id_, eq, product, day_offset=0, volume=500):
    return {"id": id_, "kind": config.KIND_DISPENSE, "equipment_id": eq,
            "equipment_description": f"Equipo {eq}", "equipment_status": config.STATUS_IN,
            "product": product, "volume": volume, "tank": "LFO Lane 3",
            "field_user": "Mitchell Godet",
            "record_collected_at": _T0 + timedelta(days=day_offset)}


def _lim(eq, product, sfl=1893):
    return {"id": f"CT-{eq}-{product}", "equipment_id": eq, "product": product, "sfl": sfl}


# ===========================================================================
# 1. Cross-class aislado -> CRITICO
# ===========================================================================

def test_cross_class_isolated_flagged_critical():
    """Equipo solo-DIESEL con un unico despacho de Coolant -> producto ajeno de
    otra clase (cross-class) = posible tag clonado."""
    mv = _mv_df([
        _disp("d1", "EQ1", "Diesel", 0),
        _disp("d2", "EQ1", "Diesel", 1),
        _disp("x1", "EQ1", "Coolant", 2),       # ajeno y aislado
    ])
    lim = _lim_df([_lim("EQ1", "Diesel")])

    mm = pa.mismatches(mv, lim)
    assert len(mm) == 1, mm
    r = mm.iloc[0]
    assert r["equipment_id"] == "EQ1"
    assert str(r["product"]).upper() == "COOLANT"
    assert bool(r["cross_class"]) is True
    assert r["product_class"] == config.PRODUCT_CLASS_FLUID

    alerts = al.detect_product_mismatch_alerts(mv, lim)
    assert len(alerts) == 1
    assert alerts.iloc[0]["severity"] == al.SEV_CRITICAL
    assert alerts.iloc[0]["category"] == config.ALERT_PRODUCT_FOREIGN


# ===========================================================================
# 2. Habilitado-y-luego-deshabilitado: NO se marca (establecido por uso)
# ===========================================================================

def test_enable_then_disable_not_flagged():
    """Coolant ya NO esta en el maestro, pero el equipo tiene una huella real de
    despachos de Coolant (sostenida en el tiempo) -> legitimo, no se marca. Este
    es el falso positivo que el usuario pidio evitar."""
    rows = [_disp("d0", "EQ1", "Diesel", 0)]
    # 4 despachos de Coolant repartidos en ~21 dias -> establecido por count y span.
    for i, off in enumerate([1, 8, 15, 21]):
        rows.append(_disp(f"c{i}", "EQ1", "Coolant", off))
    mv = _mv_df(rows)
    lim = _lim_df([_lim("EQ1", "Diesel")])      # Coolant deshabilitado en el maestro

    mm = pa.mismatches(mv, lim)
    assert mm.empty, f"no deberia marcar Coolant establecido por uso:\n{mm}"
    assert al.detect_product_mismatch_alerts(mv, lim).empty


# ===========================================================================
# 3. Mismo-clase fuera del maestro -> ADVERTENCIA
# ===========================================================================

def test_same_class_foreign_warning():
    """Equipo de Diesel con un despacho aislado de otro COMBUSTIBLE (Unleaded):
    mismo-clase, fuera del maestro -> WARNING (probable mala configuracion)."""
    mv = _mv_df([
        _disp("d1", "EQ1", "Diesel", 0),
        _disp("u1", "EQ1", "Unleaded Gasoline", 1),
    ])
    lim = _lim_df([_lim("EQ1", "Diesel")])

    mm = pa.mismatches(mv, lim)
    assert len(mm) == 1
    assert bool(mm.iloc[0]["cross_class"]) is False

    alerts = al.detect_product_mismatch_alerts(mv, lim)
    assert len(alerts) == 1
    assert alerts.iloc[0]["severity"] == al.SEV_WARNING
    assert alerts.iloc[0]["category"] == config.ALERT_PRODUCT_OFF_MASTER


# ===========================================================================
# 4. Producto habilitado en el maestro: nunca se marca
# ===========================================================================

def test_enabled_product_never_flagged():
    mv = _mv_df([
        _disp("d1", "EQ1", "Diesel", 0),
        _disp("o1", "EQ1", "Coolant", 1),       # Coolant SI habilitado
    ])
    lim = _lim_df([_lim("EQ1", "Diesel"), _lim("EQ1", "Coolant", sfl=204)])
    assert pa.mismatches(mv, lim).empty


# ===========================================================================
# 5. Equipo sin base (no sabemos su producto): se omite (evita ruido)
# ===========================================================================

def test_no_baseline_equipment_skipped():
    mv = _mv_df([_disp("x1", "EQ9", "Coolant", 0)])   # unico despacho, sin limites
    lim = _lim_df([_lim("EQ1", "Diesel")])            # nada para EQ9
    assert pa.mismatches(mv, lim).empty


# ===========================================================================
# 6. Ventana de product_history legitima el producto (aunque no este en limites)
# ===========================================================================

def test_product_history_makes_legit():
    """Coolant no esta en consumption_limits, pero SI en product_history (el
    software lo observo habilitado) -> el despacho es legitimo, no se marca."""
    mv = _mv_df([
        _disp("d1", "EQ1", "Diesel", 0),
        _disp("c1", "EQ1", "Coolant", 1),
    ])
    lim = _lim_df([_lim("EQ1", "Diesel")])
    ph = _ph_df([
        {"key": "EQ1|DIESEL", "equipment_id": "EQ1", "product": "Diesel",
         "first_seen": _T0, "last_seen": _T0 + timedelta(days=30)},
        {"key": "EQ1|COOLANT", "equipment_id": "EQ1", "product": "Coolant",
         "first_seen": _T0, "last_seen": _T0 + timedelta(days=30)},
    ])
    assert pa.mismatches(mv, lim, ph).empty


# ===========================================================================
# 7. Clasificacion de producto
# ===========================================================================

def test_product_class_unit():
    assert pa.product_class("Diesel") == config.PRODUCT_CLASS_FUEL
    assert pa.product_class("Unleaded Gasoline") == config.PRODUCT_CLASS_FUEL
    assert pa.product_class("Gas Oil") == config.PRODUCT_CLASS_FUEL     # 'OIL' no lo engaña
    assert pa.product_class("Coolant") == config.PRODUCT_CLASS_FLUID
    assert pa.product_class("Hydraulic Fluid") == config.PRODUCT_CLASS_FLUID
    assert pa.product_class("15W40") == config.PRODUCT_CLASS_FLUID
    assert pa.product_class("Mystery Brew") == config.PRODUCT_CLASS_OTHER
    assert pa.product_class(None) == config.PRODUCT_CLASS_OTHER


# ===========================================================================
# 8. enabled_products_df: preserva first_seen y congela los deshabilitados
# ===========================================================================

def test_enabled_products_df_window():
    lim0 = _lim_df([_lim("EQ1", "Diesel"), _lim("EQ1", "Coolant", sfl=204)])
    h0 = transform.enabled_products_df(lim0, None, _T0)
    assert set(h0["key"]) == {"EQ1|DIESEL", "EQ1|COOLANT"}
    assert (h0["first_seen"] == _T0).all()
    assert (h0["last_seen"] == _T0).all()

    # Segundo refresco mas tarde: Coolant deshabilitado, Diesel sigue.
    t1 = _T0 + timedelta(days=10)
    lim1 = _lim_df([_lim("EQ1", "Diesel")])
    h1 = transform.enabled_products_df(lim1, h0, t1)
    assert set(h1["key"]) == {"EQ1|DIESEL"}           # Coolant NO se reinserta (congelado)
    d = h1[h1["key"] == "EQ1|DIESEL"].iloc[0]
    assert pd.Timestamp(d["first_seen"]) == _T0       # first_seen preservado
    assert pd.Timestamp(d["last_seen"]) == t1         # last_seen avanzado


# ===========================================================================
# 9. Smoke: pipeline real con el simulador (no debe sobre-marcar)
# ===========================================================================

def test_smoke_simulator_pipeline(tmp_path=None):
    import tempfile
    db_path = os.path.join(tempfile.mkdtemp(), "smoke.sqlite3")
    db = Database(db_path)
    try:
        src = make_source(config.Settings(demo_mode=True, token="", db_path=db_path))

        async def pull():
            eq_nodes = await src.fetch_equipment(None)
            mv_nodes = []
            for _ in range(8):
                mv_nodes += await src.fetch_movements(None)
            return eq_nodes, mv_nodes

        eq_nodes, mv_nodes = asyncio.run(pull())
        db.upsert("consumption_limits", transform.consumption_limits_to_df(eq_nodes))
        db.upsert("movements", transform.movements_to_df(mv_nodes))

        mv = db.read("movements")
        limits = db.get_consumption_limits()
        mm = pa.mismatches(mv, limits)
        # El simulador habilita Diesel/Unleaded + 15W40 + Coolant por equipo y solo
        # despacha el producto principal (habilitado) -> sin cruces de clase.
        assert list(mm.columns) == pa.MISMATCH_COLS
        assert int(mm["cross_class"].map(bool).sum()) == 0 if not mm.empty else True

        alerts = al.detect_product_mismatch_alerts(mv, limits, db.get_product_history())
        assert list(alerts.columns) == al.ALERT_COLS
    finally:
        db.close()


if __name__ == "__main__":
    tests = [
        test_cross_class_isolated_flagged_critical,
        test_enable_then_disable_not_flagged,
        test_same_class_foreign_warning,
        test_enabled_product_never_flagged,
        test_no_baseline_equipment_skipped,
        test_product_history_makes_legit,
        test_product_class_unit,
        test_enabled_products_df_window,
        test_smoke_simulator_pipeline,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK     {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FALLO  {t.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} pruebas de product_audit superadas.")
    raise SystemExit(1 if failed else 0)
