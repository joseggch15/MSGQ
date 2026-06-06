"""Auditoría de integridad de datos maestros (dirty data / fuzzy matching).

El problema clásico **"Ford vs ford vs FORD vs 'ford ' vs F0RD"**: variantes del
MISMO valor que conviven en el maestro de equipos y rompen las agrupaciones de
KPIs gerenciales (cada escritura cuenta como una "marca" distinta, inflando el
conteo de categorías y repartiendo mal los totales).

Dos detectores, ambos sin dependencias externas (sólo pandas + `difflib`):

  • **Variantes por normalización** (`variant_clusters` / `variant_detail`):
    agrupa los valores por una clave normalizada (mayúsculas, sin acentos, sin
    espacios ni puntuación y —en campos alfabéticos— con homóglifos 0/O 1/I 5/S 8/B
    plegados). Un grupo con ≥2 escrituras crudas es "dirty data": se sugiere la
    escritura más frecuente como canónica y se listan los IDs de equipo que usan
    cada variante (los que NO son la canónica son los que "ensucian" la agrupación).

  • **Duplicados léxicos / fuzzy matching** (`fuzzy_duplicates`): compara los
    valores distintos por similitud de cadenas (`difflib.SequenceMatcher`). Pares
    con similitud ≥ umbral pero no idénticos se marcan como posibles duplicados
    (typos/OCR que la normalización no fusionó, p. ej. 'Caterpillar' vs
    'Caterpilar'). Mide "qué tan cerca" está una cadena de otra, en %.

La auditoría corre sobre el maestro COMPLETO de equipos (no sobre el filtro de la
vista): la calidad del dato es una propiedad del registro, no de la consulta.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

import pandas as pd

# Campos maestros a auditar: (columna, etiqueta, plegar_homoglifos).
# El plegado de homóglifos (0->O, 1->I, 5->S, 8->B) sólo se aplica a campos
# alfabéticos (marca, categoría…), NO a los alfanuméricos (modelo, centro de
# costo) para no corromper códigos legítimos como "785D" o "D10T".
MASTER_FIELDS: tuple[tuple[str, str, bool], ...] = (
    ("make", "Marca", True),
    ("model", "Modelo", False),
    ("category", "Categoría", True),
    ("group", "Grupo", True),
    ("department", "Departamento", True),
    ("cost_centre", "Centro de costo", False),
)

_ID_COL = "equipment_id"
_MAX_IDS = 100          # tope de IDs listados por variante (evita celdas enormes)
_FUZZY_THRESHOLD = 0.85  # similitud mínima para marcar duplicado léxico
_FUZZY_MIN_LEN = 3       # ignora cadenas muy cortas (la similitud es volátil)
_FUZZY_MAX_VALUES = 2500  # cota de seguridad para la comparación O(n²)

# Homóglifos típicos de tipeo/OCR -> letra canónica.
_HOMOGLYPHS = str.maketrans({"0": "O", "1": "I", "5": "S", "8": "B"})


# ===========================================================================
# Normalización
# ===========================================================================

def normalize_display(value) -> str:
    """Limpieza mínima legible: colapsa espacios (incl. NBSP) y recorta extremos.
    No cambia mayúsculas ni acentos (es lo que se sugiere como canónico)."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(value).replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip()


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def normalize_key(value, fold_homoglyphs: bool = False) -> str:
    """Clave de agrupación: mayúsculas, sin acentos, sin espacios ni puntuación y,
    si se pide, con homóglifos plegados. Dos valores con la MISMA clave son la
    misma 'cosa' escrita distinto: 'FORD'/'Ford'/'ford '/'F0RD' o, por
    puntuación/espacios, 'BT-50'/'BT 50'/'BT50' y 'Hi Ace'/'Hiace'. Quitar la
    puntuación deja al detector EXACTO esos casos y reserva el fuzzy para typos
    reales (letras distintas)."""
    d = normalize_display(value)
    if not d:
        return ""
    k = _strip_accents(d).upper()
    if fold_homoglyphs:
        k = k.translate(_HOMOGLYPHS)
    return re.sub(r"[^A-Z0-9]", "", k)


def _cmp_form(value) -> str:
    """Forma para comparar por similitud (case/acentos-insensible)."""
    return _strip_accents(normalize_display(value)).upper()


def _blank(series: pd.Series) -> pd.Series:
    return series.isna() | series.astype("string").str.strip().isin(
        ["", "<NA>", "nan", "NaN", "None"])


def _field_frame(eq: pd.DataFrame, field: str, fold: bool) -> pd.DataFrame:
    """DataFrame auxiliar [_id, raw, disp, key] de los valores NO vacíos de `field`."""
    if eq is None or eq.empty or field not in eq.columns:
        return pd.DataFrame(columns=["_id", "raw", "disp", "key"])
    ids = (eq[_ID_COL].astype("string") if _ID_COL in eq.columns
           else pd.Series(eq.index.astype(str), index=eq.index))
    df = pd.DataFrame({"_id": ids.values, "raw": eq[field].values})
    df = df[~_blank(df["raw"])].copy()
    if df.empty:
        return pd.DataFrame(columns=["_id", "raw", "disp", "key"])
    df["raw"] = df["raw"].astype(str)
    df["disp"] = df["raw"].map(normalize_display)
    df["key"] = df["raw"].map(lambda v: normalize_key(v, fold))
    return df[df["key"] != ""]


def _join_ids(ids) -> str:
    uniq = sorted({str(i) for i in ids if i is not None and str(i) not in ("", "<NA>")})
    if len(uniq) <= _MAX_IDS:
        return ", ".join(uniq)
    return ", ".join(uniq[:_MAX_IDS]) + f"  … (+{len(uniq) - _MAX_IDS})"


# ===========================================================================
# Detector 1 — Variantes por normalización (Ford / ford / FORD / F0RD)
# ===========================================================================

VARIANT_CLUSTER_COLS = [
    "Campo", "Valor canónico (sugerido)", "Variantes", "Equipos",
    "Escrituras", "IDs equipos",
]
VARIANT_DETAIL_COLS = [
    "Campo", "Valor canónico", "Variante", "¿Canónica?", "Equipos", "IDs equipos",
]


def _dirty_groups(df: pd.DataFrame):
    """Itera (canónica, value_counts de escrituras, chunk) de los grupos SUCIOS
    (≥2 escrituras de la misma clave) de un `_field_frame`."""
    for _key, chunk in df.groupby("key"):
        variants = chunk["raw"].value_counts()
        if len(variants) >= 2:
            yield variants.index[0], variants, chunk


def _variant_clusters_from_frame(df: pd.DataFrame, label: str) -> pd.DataFrame:
    rows = []
    for canonical, variants, chunk in _dirty_groups(df):
        rows.append({
            "Campo": label,
            "Valor canónico (sugerido)": normalize_display(canonical),
            "Variantes": int(len(variants)),
            "Equipos": int(len(chunk)),
            "Escrituras": " · ".join(f"«{v}» ({c})" for v, c in variants.items()),
            "IDs equipos": _join_ids(chunk["_id"]),
        })
    if not rows:
        return pd.DataFrame(columns=VARIANT_CLUSTER_COLS)
    return (pd.DataFrame(rows, columns=VARIANT_CLUSTER_COLS)
            .sort_values(["Variantes", "Equipos"], ascending=False)
            .reset_index(drop=True))


def _variant_detail_from_frame(df: pd.DataFrame, label: str) -> pd.DataFrame:
    rows = []
    for canonical, variants, chunk in _dirty_groups(df):
        for raw, cnt in variants.items():
            rows.append({
                "Campo": label,
                "Valor canónico": normalize_display(canonical),
                "Variante": raw,
                "¿Canónica?": bool(raw == canonical),
                "Equipos": int(cnt),
                "IDs equipos": _join_ids(chunk.loc[chunk["raw"] == raw, "_id"]),
            })
    if not rows:
        return pd.DataFrame(columns=VARIANT_DETAIL_COLS)
    out = pd.DataFrame(rows, columns=VARIANT_DETAIL_COLS)
    # Dentro de cada grupo: la canónica primero, luego por nº de equipos.
    out["_canon_first"] = (~out["¿Canónica?"]).astype(int)
    out = out.sort_values(["Campo", "Valor canónico", "_canon_first", "Equipos"],
                          ascending=[True, True, True, False])
    return out.drop(columns="_canon_first").reset_index(drop=True)


def variant_clusters(eq: pd.DataFrame, field: str, label: str,
                     fold: bool) -> pd.DataFrame:
    """Un renglón por grupo SUCIO de `field` (≥2 escrituras de la misma clave)."""
    return _variant_clusters_from_frame(_field_frame(eq, field, fold), label)


def variant_detail(eq: pd.DataFrame, field: str, label: str,
                   fold: bool) -> pd.DataFrame:
    """Listado fino: un renglón por (grupo sucio × escritura), con los IDs que la
    usan. Las filas con '¿Canónica?'=No son las que ensucian la agrupación."""
    return _variant_detail_from_frame(_field_frame(eq, field, fold), label)


# ===========================================================================
# Detector 2 — Duplicados léxicos (fuzzy matching)
# ===========================================================================

FUZZY_COLS = ["Campo", "Valor A", "Equipos A", "Valor B", "Equipos B", "Similitud %"]


def _fuzzy_from_frame(df: pd.DataFrame, label: str,
                      threshold: float = _FUZZY_THRESHOLD) -> pd.DataFrame:
    """Pares de valores DISTINTOS (claves distintas) con similitud ≥ threshold.

    No congela la GUI aunque el maestro sea grande: descarta cada par con dos cotas
    O(longitud) antes del `ratio()` (O(n·m)) completo —
      1) por longitud: `ratio` no puede llegar a T si min/max < T/(2-T);
      2) `quick_ratio()` (cota superior por multiconjunto de caracteres).
    Reutiliza la tabla de `seq2` comparando una cadena contra todas las demás."""
    if df.empty:
        return pd.DataFrame(columns=FUZZY_COLS)
    # Representante por clave: escritura más frecuente + nº de equipos del grupo.
    reps = []
    for _key, chunk in df.groupby("key"):
        disp = normalize_display(chunk["raw"].value_counts().index[0])
        if len(disp) >= _FUZZY_MIN_LEN:
            reps.append((disp, int(len(chunk)), _cmp_form(disp)))
    if len(reps) < 2 or len(reps) > _FUZZY_MAX_VALUES:
        return pd.DataFrame(columns=FUZZY_COLS)

    # Cota por longitud: si min(la,lb)/max(la,lb) < ratio_floor, el ratio de
    # difflib (= 2·coincidencias/(la+lb)) NO puede alcanzar `threshold`.
    ratio_floor = threshold / (2.0 - threshold)
    rows = []
    sm = SequenceMatcher(autojunk=False)
    for i in range(len(reps)):
        da, ca, cmpa = reps[i]
        sm.set_seq2(cmpa)
        la = len(cmpa)
        for j in range(i + 1, len(reps)):
            db, cb, cmpb = reps[j]
            lb = len(cmpb)
            if cmpa == cmpb or min(la, lb) < ratio_floor * max(la, lb):
                continue
            sm.set_seq1(cmpb)
            if sm.quick_ratio() < threshold:
                continue
            sim = sm.ratio()
            if sim >= threshold:
                rows.append({
                    "Campo": label, "Valor A": da, "Equipos A": ca,
                    "Valor B": db, "Equipos B": cb,
                    "Similitud %": round(sim * 100, 1),
                })
    if not rows:
        return pd.DataFrame(columns=FUZZY_COLS)
    return (pd.DataFrame(rows, columns=FUZZY_COLS)
            .sort_values("Similitud %", ascending=False).reset_index(drop=True))


def fuzzy_duplicates(eq: pd.DataFrame, field: str, label: str, fold: bool,
                     threshold: float = _FUZZY_THRESHOLD) -> pd.DataFrame:
    """Probables duplicados por typo/OCR que la normalización no fusionó
    ('Caterpillar' vs 'Caterpilar', 'John Deere' vs 'Jhon Deere')."""
    return _fuzzy_from_frame(_field_frame(eq, field, fold), label, threshold)


# ===========================================================================
# Auditoría completa (calcula cada pieza UNA vez) + agregados/KPIs
# ===========================================================================

SUMMARY_COLS = [
    "Campo", "Valores distintos", "Valores reales", "Grupos sucios",
    "Equipos afectados", "Pares similares",
]


@dataclass
class AuditResult:
    """Resultado de la auditoría de calidad: resumen, listado de variantes,
    pares fuzzy y KPIs — todo calculado en una sola pasada (la GUI lo usa así para
    no recomputar el fuzzy O(n²) varias veces)."""
    summary: pd.DataFrame
    variant_detail: pd.DataFrame
    fuzzy: pd.DataFrame
    kpis: dict


def _fields(eq: pd.DataFrame, fields) -> list[tuple[str, str, bool]]:
    return [(c, lbl, fold) for (c, lbl, fold) in (fields or MASTER_FIELDS)
            if eq is not None and not eq.empty and c in eq.columns]


def _kpis_from_summary(summary: pd.DataFrame) -> dict:
    if summary is None or summary.empty:
        return {"Campos con problemas": 0, "Grupos sucios": 0,
                "Equipos afectados": 0, "Pares similares": 0}
    dirty = summary[(summary["Grupos sucios"] > 0) | (summary["Pares similares"] > 0)]
    return {
        "Campos con problemas": int(len(dirty)),
        "Grupos sucios": int(summary["Grupos sucios"].sum()),
        "Equipos afectados": int(summary["Equipos afectados"].sum()),
        "Pares similares": int(summary["Pares similares"].sum()),
    }


def audit(eq: pd.DataFrame, fields=MASTER_FIELDS) -> AuditResult:
    """Auditoría completa de los campos maestros en UNA pasada. Para cada campo
    arma el frame una vez y deriva variantes, fuzzy y conteos sin repetir el
    trabajo caro (lo que la GUI llama en su refresco)."""
    summary_rows, vparts, fparts = [], [], []
    for field, label, fold in _fields(eq, fields):
        df = _field_frame(eq, field, fold)
        if df.empty:
            continue
        clusters = _variant_clusters_from_frame(df, label)
        vdet = _variant_detail_from_frame(df, label)
        fz = _fuzzy_from_frame(df, label)
        if not vdet.empty:
            vparts.append(vdet)
        if not fz.empty:
            fparts.append(fz)
        summary_rows.append({
            "Campo": label,
            "Valores distintos": int(df["raw"].nunique()),
            "Valores reales": int(df["key"].nunique()),
            "Grupos sucios": int(len(clusters)),
            "Equipos afectados": int(clusters["Equipos"].sum()) if not clusters.empty else 0,
            "Pares similares": int(len(fz)),
        })
    summary = (pd.DataFrame(summary_rows, columns=SUMMARY_COLS)
               .sort_values(["Grupos sucios", "Pares similares"], ascending=False)
               .reset_index(drop=True)
               if summary_rows else pd.DataFrame(columns=SUMMARY_COLS))
    variant = (pd.concat(vparts, ignore_index=True) if vparts
               else pd.DataFrame(columns=VARIANT_DETAIL_COLS))
    fuzzy = (pd.concat(fparts, ignore_index=True) if fparts
             else pd.DataFrame(columns=FUZZY_COLS))
    return AuditResult(summary, variant, fuzzy, _kpis_from_summary(summary))


def audit_summary(eq: pd.DataFrame, fields=MASTER_FIELDS) -> pd.DataFrame:
    """Resumen gerencial: por campo, valores distintos vs reales (la brecha es la
    magnitud del dirty data), grupos sucios, equipos afectados y pares fuzzy."""
    return audit(eq, fields).summary


def all_variant_detail(eq: pd.DataFrame, fields=MASTER_FIELDS) -> pd.DataFrame:
    return audit(eq, fields).variant_detail


def all_fuzzy(eq: pd.DataFrame, fields=MASTER_FIELDS) -> pd.DataFrame:
    return audit(eq, fields).fuzzy


def audit_kpis(eq: pd.DataFrame, fields=MASTER_FIELDS) -> dict:
    """KPIs de la alerta de Data Quality."""
    return audit(eq, fields).kpis
