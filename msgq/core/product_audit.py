"""Auditoria de coherencia Producto <-> Equipo (posible tag clonado).

Detecta despachos cuyo producto es AJENO al equipo: un equipo solo-DIESEL al que
se le despacha 'Coolant' o 'Hydraulic Fluid' (o viceversa) suele indicar un tag
RFID clonado o un equipo mal configurado en el maestro.

Reto temporal (lo dificil, que pidio el usuario): un producto pudo estar
habilitado y luego deshabilitarse, dejando despachos LEGITIMOS en el historico.
La API no expone CUANDO se habilito/deshabilito cada producto por equipo
(`consumptionTanks` es solo el estado actual). Por eso un producto se considera
LEGITIMO para un equipo si cumple cualquiera de:

  • esta en `consumption_limits` (maestro; la tabla ACUMULA y nunca borra, asi que
    ya incluye productos deshabilitados despues de haberse observado), o
  • esta en `product_history` (ventanas de habilitacion observadas por el
    software; resiliencia ante un rebuild del maestro y registro auditable), o
  • esta ESTABLECIDO POR USO: tiene huella real en el propio historial de
    despachos del equipo (>= N despachos, o span >= D dias, o >= X% de share).
    Esto absorbe el caso "estuvo habilitado y luego se quito" SIN necesitar
    timestamps de habilitacion, que la API no da.

Un despacho cuyo producto NO entra en ese conjunto es el ajeno/aislado que se
marca. Se prioriza (cross_class=True -> CRITICO) cuando es de OTRA CLASE que la
del equipo (combustible vs fluido de servicio); mismo-clase pero fuera del
conjunto es la senal mas debil (WARNING). Un equipo del que no conocemos NINGUN
producto (ni maestro ni uso) se omite: sin base para juzgar, evita ruido.

El cruce es vectorizado sobre el grueso de los despachos (un `isin` de pares
(equipo, producto)); solo los pocos candidatos ajenos se clasifican fila a fila.
"""
from __future__ import annotations

import pandas as pd

from msgq import config

# Una fila por despacho con producto ajeno al equipo.
MISMATCH_COLS = [
    "date", "equipment_id", "equipment_description", "equipment_status",
    "product", "product_class", "expected_products", "expected_classes",
    "volume", "field_user", "dispensing_point", "source_id", "cross_class",
]

_BLANK = {"", "<NA>", "NAN", "NONE"}


def product_class(label) -> str:
    """Clase de un producto por su etiqueta: FUEL / FLUID / OTHER.

    Evalua FUEL ANTES que FLUID (ver `config.PRODUCT_CLASS_KEYWORDS`): asi un
    combustible como 'Gas Oil' —que contiene la subcadena 'OIL' (keyword de
    FLUID)— se clasifica correctamente como FUEL por su keyword 'GAS OIL'.
    """
    if label is None:
        return config.PRODUCT_CLASS_OTHER
    try:
        if pd.isna(label):
            return config.PRODUCT_CLASS_OTHER
    except (TypeError, ValueError):
        pass
    up = str(label).strip().upper()
    if not up:
        return config.PRODUCT_CLASS_OTHER
    for kw in config.PRODUCT_CLASS_KEYWORDS[config.PRODUCT_CLASS_FUEL]:
        if kw in up:
            return config.PRODUCT_CLASS_FUEL
    for kw in config.PRODUCT_CLASS_KEYWORDS[config.PRODUCT_CLASS_FLUID]:
        if kw in up:
            return config.PRODUCT_CLASS_FLUID
    return config.PRODUCT_CLASS_OTHER


def _clean_label(orig, fallback_upper: str) -> str:
    """Etiqueta legible para mostrar; si la original no sirve, usa el upper."""
    try:
        if orig is not None and not pd.isna(orig):
            s = str(orig).strip()
            if s and s.upper() not in _BLANK:
                return s
    except (TypeError, ValueError):
        pass
    return str(fallback_upper)


def _eid_series(df: pd.DataFrame, col: str = "equipment_id") -> pd.Series:
    return df[col].astype("string").str.strip()


def _produ_series(df: pd.DataFrame, col: str = "product") -> pd.Series:
    return df[col].astype("string").str.strip().str.upper()


def _enabled_from_limits(limits: pd.DataFrame | None) -> tuple[set, dict]:
    """Pares (equipo, PRODUCTO) habilitados en el maestro + etiquetas legibles."""
    pairs: set[tuple] = set()
    labels: dict[str, set] = {}
    if (limits is None or limits.empty
            or not {"equipment_id", "product"}.issubset(limits.columns)):
        return pairs, labels
    eid = _eid_series(limits)
    produ = _produ_series(limits)
    for e, pu, orig in zip(eid, produ, limits["product"]):
        if pd.isna(e) or pd.isna(pu) or pu in _BLANK:
            continue
        pairs.add((e, pu))
        labels.setdefault(e, set()).add(_clean_label(orig, pu))
    return pairs, labels


def _enabled_from_history(history: pd.DataFrame | None) -> tuple[set, dict]:
    """Pares (equipo, PRODUCTO) observados habilitados en `product_history`."""
    pairs: set[tuple] = set()
    labels: dict[str, set] = {}
    if (history is None or history.empty
            or not {"equipment_id", "product"}.issubset(history.columns)):
        return pairs, labels
    eid = _eid_series(history)
    produ = _produ_series(history)
    for e, pu, orig in zip(eid, produ, history["product"]):
        if pd.isna(e) or pd.isna(pu) or pu in _BLANK:
            continue
        pairs.add((e, pu))
        labels.setdefault(e, set()).add(_clean_label(orig, pu))
    return pairs, labels


def _established_by_usage(disp: pd.DataFrame) -> tuple[set, dict]:
    """Productos con huella real en el historial de despachos del equipo.

    Un producto cuenta como establecido (legitimo, aunque hoy no este habilitado)
    si en el equipo cumple cualquiera de: >= MIN_EVENTS despachos, span temporal
    >= MIN_DAYS dias, o >= MIN_SHARE del total de despachos del equipo.
    """
    pairs: set[tuple] = set()
    labels: dict[str, set] = {}
    grp = disp.groupby(["_eid", "_prodU"])
    counts = grp.size()
    dmin = grp["_date"].min()
    dmax = grp["_date"].max()
    rep = grp["product"].first()
    totals = disp.groupby("_eid").size()
    for (e, pu), n in counts.items():
        a, b = dmin[(e, pu)], dmax[(e, pu)]
        span_days = (b - a).total_seconds() / 86400.0 if (pd.notna(a) and pd.notna(b)) else 0.0
        tot = int(totals.get(e, 0))
        share = (n / tot) if tot else 0.0
        # La regla de share exige >= 2 despachos: un UNICO despacho aislado nunca
        # cuenta como establecido (es justo el cross-class que se quiere marcar en
        # equipos de baja actividad); se establece solo por volumen o por span.
        if (n >= config.PRODUCT_MISMATCH_MIN_EVENTS
                or span_days >= config.PRODUCT_MISMATCH_MIN_DAYS
                or (share >= config.PRODUCT_MISMATCH_MIN_SHARE and n >= 2)):
            pairs.add((e, pu))
            labels.setdefault(e, set()).add(_clean_label(rep[(e, pu)], pu))
    return pairs, labels


def mismatches(movements: pd.DataFrame | None,
               limits: pd.DataFrame | None,
               product_history: pd.DataFrame | None = None) -> pd.DataFrame:
    """Despachos cuyo producto es ajeno al equipo (ver docstring del modulo).

    Devuelve un DataFrame con `MISMATCH_COLS`; `cross_class=True` marca el cruce
    entre clases (combustible vs fluido), la senal fuerte de tag clonado.
    """
    if movements is None or movements.empty:
        return pd.DataFrame(columns=MISMATCH_COLS)
    mv = movements
    if "kind" in mv.columns:
        mv = mv[mv["kind"] == config.KIND_DISPENSE]
    if mv.empty or not {"equipment_id", "product"}.issubset(mv.columns):
        return pd.DataFrame(columns=MISMATCH_COLS)

    disp = mv.copy()
    disp["_eid"] = _eid_series(disp)
    disp["_prodU"] = _produ_series(disp)
    blank_eid = (disp["_eid"].isna() | disp["_eid"].str.upper().isin(_BLANK)
                 | disp["_eid"].str.upper().eq("UNAUTHORISED"))
    blank_prod = disp["_prodU"].isna() | disp["_prodU"].isin(_BLANK)
    disp = disp[~(blank_eid.fillna(True) | blank_prod.fillna(True))].copy()
    if disp.empty:
        return pd.DataFrame(columns=MISMATCH_COLS)

    date_col = "record_collected_at" if "record_collected_at" in disp.columns else "updated_at"
    disp["_date"] = pd.to_datetime(disp.get(date_col), errors="coerce")
    if "volume" in disp.columns:
        disp["volume"] = pd.to_numeric(disp["volume"], errors="coerce")

    # Conjunto permitido por equipo = maestro U historico observado U uso establecido.
    en_pairs, en_labels = _enabled_from_limits(limits)
    hi_pairs, hi_labels = _enabled_from_history(product_history)
    us_pairs, us_labels = _established_by_usage(disp)
    allowed_pairs = en_pairs | hi_pairs | us_pairs

    # Etiquetas y clases esperadas por equipo (para el detalle y el cruce de clase).
    display_labels: dict[str, set] = {}
    for src in (en_labels, hi_labels, us_labels):
        for e, s in src.items():
            display_labels.setdefault(e, set()).update(s)
    allowed_upper: dict[str, set] = {}
    for (e, pu) in allowed_pairs:
        allowed_upper.setdefault(e, set()).add(pu)
    allowed_classes: dict[str, set] = {
        e: {product_class(pu) for pu in puset} for e, puset in allowed_upper.items()
    }

    # Candidatos: despachos cuyo (equipo, producto) NO esta permitido.
    disp["_pair"] = list(zip(disp["_eid"], disp["_prodU"]))
    cand = disp[~disp["_pair"].isin(allowed_pairs)]
    if cand.empty:
        return pd.DataFrame(columns=MISMATCH_COLS)

    known = {config.PRODUCT_CLASS_FUEL, config.PRODUCT_CLASS_FLUID}
    rows: list[dict] = []
    for _, r in cand.iterrows():
        e = r["_eid"]
        eq_classes = allowed_classes.get(e)
        if not eq_classes:
            continue   # sin base para juzgar el equipo -> se omite (evita ruido)
        pclass = product_class(r.get("product"))
        known_eq = {c for c in eq_classes if c in known}
        cross = (pclass in known) and bool(known_eq) and (pclass not in known_eq)
        rows.append({
            "date": r.get("_date"),
            "equipment_id": r.get("equipment_id"),
            "equipment_description": r.get("equipment_description"),
            "equipment_status": r.get("equipment_status"),
            "product": r.get("product"),
            "product_class": pclass,
            "expected_products": ", ".join(sorted(display_labels.get(e, set()))) or None,
            "expected_classes": ", ".join(sorted(eq_classes)) or None,
            "volume": r.get("volume"),
            "field_user": r.get("field_user"),
            "dispensing_point": r.get("tank"),
            "source_id": r.get("id"),
            "cross_class": bool(cross),
        })
    if not rows:
        return pd.DataFrame(columns=MISMATCH_COLS)
    out = pd.DataFrame(rows, columns=MISMATCH_COLS)
    return out.sort_values(["cross_class", "date"], ascending=[False, False]).reset_index(drop=True)


def kpis(mm: pd.DataFrame | None) -> dict:
    """Resumen para tarjetas/depuracion."""
    if mm is None or mm.empty:
        return {"Mismatches": 0, "Cross-class": 0, "Equipos afectados": 0}
    return {
        "Mismatches": len(mm),
        "Cross-class": int(mm["cross_class"].map(bool).sum()),
        "Equipos afectados": int(mm["equipment_id"].nunique()),
    }
