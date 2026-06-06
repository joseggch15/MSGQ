# -*- coding: utf-8 -*-
"""Pruebas de la auditoría de calidad de datos maestros (dirty data / fuzzy).

Sin mocks: se construyen DataFrames de equipos con el esquema real y se verifica
el valor de negocio — detectar «Ford» vs «ford» vs «F0RD» (variantes) y
«Caterpillar» vs «Caterpilar» (duplicados léxicos por typo).

Ejecutar:   pytest tests/test_data_quality.py -v
o:          python tests/test_data_quality.py
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msgq import config
from msgq.core import data_quality as dq


def _eq_df(rows: list[dict]) -> pd.DataFrame:
    base = [{**{c: pd.NA for c in config.EQUIPMENT_COLS}, **r} for r in rows]
    return pd.DataFrame(base, columns=config.EQUIPMENT_COLS)


# ===========================================================================
# 1. Normalización
# ===========================================================================

def test_normalize_key_merges_case_space_homoglyph():
    # Mayúsculas, espacio sobrante y homóglifo 0->O caen en la MISMA clave (fold).
    assert dq.normalize_key("FORD", True) == dq.normalize_key("Ford", True)
    assert dq.normalize_key("ford ", True) == dq.normalize_key("FORD", True)
    assert dq.normalize_key("F0RD", True) == dq.normalize_key("FORD", True)
    # Acentos también se pliegan.
    assert dq.normalize_key("Camión", True) == dq.normalize_key("CAMION", True)
    # Sin fold, los dígitos NO se tocan (no corromper modelos alfanuméricos).
    assert dq.normalize_key("785D", False) == "785D"
    assert dq.normalize_key("D10T", False) != dq.normalize_key("D1OT", False)
    print("OK  test_normalize_key_merges_case_space_homoglyph")


# ===========================================================================
# 2. Detector de variantes (Ford / ford / FORD / ford  / F0RD)
# ===========================================================================

def _ford_fleet() -> pd.DataFrame:
    return _eq_df([
        {"equipment_id": "E1", "make": "FORD", "model": "785D"},
        {"equipment_id": "E2", "make": "Ford", "model": "785D"},
        {"equipment_id": "E3", "make": "ford ", "model": "D10T"},
        {"equipment_id": "E4", "make": "F0RD", "model": "D10T"},
        {"equipment_id": "E5", "make": "CAT", "model": "D11T"},
        {"equipment_id": "E6", "make": "CAT", "model": "785D"},
        {"equipment_id": "E7", "make": "Caterpillar", "model": "EX1200"},
        {"equipment_id": "E8", "make": "Caterpilar", "model": "EX1200"},
        {"equipment_id": "E9", "make": "Komatsu", "model": "PC2000"},
    ])


def test_variant_clusters_flags_dirty_group():
    clusters = dq.variant_clusters(_ford_fleet(), "make", "Marca", True)
    assert len(clusters) == 1                      # solo el grupo FORD está sucio
    r = clusters.iloc[0]
    assert r["Variantes"] == 4 and r["Equipos"] == 4
    assert list(clusters.columns) == dq.VARIANT_CLUSTER_COLS
    # CAT (escrito siempre igual) NO es un grupo sucio.
    assert "CAT" not in clusters["Valor canónico (sugerido)"].tolist()
    print("OK  test_variant_clusters_flags_dirty_group")


def test_variant_detail_lists_offending_ids():
    detail = dq.variant_detail(_ford_fleet(), "make", "Marca", True)
    assert list(detail.columns) == dq.VARIANT_DETAIL_COLS
    # Exactamente una variante canónica y tres que ensucian.
    assert int(detail["¿Canónica?"].sum()) == 1
    offenders = detail[~detail["¿Canónica?"]]
    assert len(offenders) == 3
    ids = set(",".join(offenders["IDs equipos"]).replace(" ", "").split(","))
    assert {"E2", "E3", "E4"}.issubset(ids)        # los IDs que ensucian la marca
    print("OK  test_variant_detail_lists_offending_ids")


def test_clean_master_has_no_variants():
    eq = _eq_df([
        {"equipment_id": "E1", "make": "CAT"},
        {"equipment_id": "E2", "make": "CAT"},
        {"equipment_id": "E3", "make": "Komatsu"},
    ])
    assert dq.variant_clusters(eq, "make", "Marca", True).empty
    assert dq.variant_detail(eq, "make", "Marca", True).empty
    print("OK  test_clean_master_has_no_variants")


# ===========================================================================
# 3. Fuzzy matching (typos que la normalización no fusiona)
# ===========================================================================

def test_fuzzy_detects_typo_duplicate():
    fz = dq.fuzzy_duplicates(_ford_fleet(), "make", "Marca", True)
    assert list(fz.columns) == dq.FUZZY_COLS
    pairs = {frozenset((a, b)) for a, b in zip(fz["Valor A"], fz["Valor B"])}
    assert frozenset(("Caterpillar", "Caterpilar")) in pairs
    assert (fz["Similitud %"] >= 85).all()
    # FORD (ya unificado por clave) no debe aparecer como par fuzzy consigo mismo.
    assert not ((fz["Valor A"] == "FORD") & (fz["Valor B"] == "Ford")).any()
    print("OK  test_fuzzy_detects_typo_duplicate")


def test_fuzzy_ignores_distinct_values():
    eq = _eq_df([
        {"equipment_id": "E1", "make": "Caterpillar"},
        {"equipment_id": "E2", "make": "Komatsu"},
        {"equipment_id": "E3", "make": "Volvo"},
    ])
    assert dq.fuzzy_duplicates(eq, "make", "Marca", True).empty
    print("OK  test_fuzzy_ignores_distinct_values")


# ===========================================================================
# 4. Agregados y KPIs
# ===========================================================================

def test_audit_summary_and_kpis():
    eq = _ford_fleet()
    summary = dq.audit_summary(eq)
    assert list(summary.columns) == dq.SUMMARY_COLS
    make_row = summary[summary["Campo"] == "Marca"].iloc[0]
    # 8 escrituras crudas: FORD, Ford, "ford ", F0RD, CAT, Caterpillar, Caterpilar, Komatsu.
    assert make_row["Valores distintos"] == 8
    assert make_row["Valores reales"] == 5         # tras normalizar (FORD unifica 4)
    assert make_row["Grupos sucios"] == 1
    assert make_row["Equipos afectados"] == 4
    assert make_row["Pares similares"] == 1

    k = dq.audit_kpis(eq)
    assert k["Grupos sucios"] == 1
    assert k["Equipos afectados"] == 4
    assert k["Pares similares"] == 1
    assert k["Campos con problemas"] >= 1
    print("OK  test_audit_summary_and_kpis")


def test_empty_and_missing_columns_are_safe():
    assert dq.audit_summary(pd.DataFrame()).empty
    assert dq.audit_kpis(pd.DataFrame())["Grupos sucios"] == 0
    assert dq.all_variant_detail(pd.DataFrame()).empty
    assert dq.all_fuzzy(pd.DataFrame()).empty
    # DataFrame sin las columnas maestras tampoco rompe.
    eq = pd.DataFrame({"equipment_id": ["E1", "E2"]})
    assert dq.audit_summary(eq).empty
    print("OK  test_empty_and_missing_columns_are_safe")


if __name__ == "__main__":
    tests = [
        test_normalize_key_merges_case_space_homoglyph,
        test_variant_clusters_flags_dirty_group,
        test_variant_detail_lists_offending_ids,
        test_clean_master_has_no_variants,
        test_fuzzy_detects_typo_duplicate,
        test_fuzzy_ignores_distinct_values,
        test_audit_summary_and_kpis,
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
