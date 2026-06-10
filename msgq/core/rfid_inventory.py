"""Inventario de tags RFID — el reporte 'Inventory Tag Installed' desde el endpoint.

Reproduce la logica del proyecto hermano `Inventory_Equipment` (clasificar cada
cambio de RFID como NEW INSTALLATION / REPLACEMENT / REMOVAL, con KPIs,
agrupaciones y validaciones) PERO alimentado por el endpoint en vez de snapshots
CSV, y corrigiendo la columna DATE.

Diferencia central con el proyecto hermano
------------------------------------------
`Inventory_Equipment` infiere los cambios comparando dos snapshots del maestro y
estampa en DATE la fecha del inventario (la del snapshot). Aqui el insumo es el
**log de auditoria** (`change_events`, recordType `EquipmentRfid`, atributo
`rfid`), que YA trae la fecha REAL del cambio (`changed_at`) y su tipo:

    event create  (None -> tag)   -> NEW INSTALLATION
    event update  (tag  -> tag')  -> REPLACEMENT
    event destroy (tag  -> None)  -> REMOVAL

Asi DATE es la fecha real en que se registro / cambio / quito el tag.

Enlace tag -> equipo
--------------------
El API no expone ningun FK del tag a su equipo (el `ChangeEvent` no tiene
relacion al equipo y `rfidTags` es una lista de strings). El unico enlace posible
es **por VALOR**: se cruza el tag del evento con el maestro actual de equipos
(`equipment.rfid`, que `transform._join_rfids` guarda como string separado por
", "). Para ALTAS y REEMPLAZOS el `after` (tag vigente) casi siempre sigue en un
equipo -> se completa ID / Cost Center / Department / etc. Para REMOCIONES el tag
ya no esta en ningun equipo -> esas columnas quedan vacias (no es recuperable).

El producto no existe en EquipmentItem: se infiere del historial de despachos
(producto mas despachado a ese equipment_id).
"""
from __future__ import annotations

import pandas as pd

from msgq import config

STATUS_OUT = config.STATUS_OUT

# Columnas internas (prefijo _) de soporte para validaciones/agrupaciones.
_REPORT_COLS = config.WEEKLY_REPORT_COLS + [
    "whodunnit", "_Status", "_Category", "_Group", "_Description",
]
# Columnas visibles en la tabla del reporte (semanal + quien hizo el cambio).
DISPLAY_COLS = config.WEEKLY_REPORT_COLS + ["whodunnit"]

_INVENTORY_COLS = [
    "equipment_id", "description", "status", "group", "category",
    "department", "cost_centre", "rfid",
]


# ===========================================================================
# Helpers
# ===========================================================================

def _present(v) -> bool:
    """True si el valor NO es vacio (no None/NaN/''/'<NA>')."""
    if v is None:
        return False
    try:
        if pd.isna(v):
            return False
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return s != "" and s != "<NA>" and s.lower() != "nan"


def _identified(v) -> bool:
    """True si el ID corresponde a un equipo real (no vacio ni el marcador)."""
    return _present(v) and str(v).strip() != config.UNIDENTIFIED


def _blank_series(s: pd.Series) -> pd.Series:
    """True donde el valor es NA / vacio / '<NA>'."""
    txt = s.astype("string").str.strip()
    return s.isna() | txt.eq("") | txt.eq("<NA>") | txt.str.lower().eq("nan")


def _split_tags(raw) -> list[str]:
    """Parte el campo `rfid` del maestro (unido por ', ') en tags individuales."""
    if not _present(raw):
        return []
    return [t.strip() for t in str(raw).split(",") if t.strip()]


# ===========================================================================
# Enlace tag -> equipo  (por valor, contra el maestro actual)
# ===========================================================================

def _compact_id(eq_id) -> str:
    """Variante sin espacios internos de un equipment_id, para puentear los
    registros DUPLICADOS del maestro del FMS (p. ej. 'C- SE-12' vs 'C-SE-12',
    mismo activo fisico y mismo tag, pero los limites de producto cuelgan de
    una sola de las dos variantes)."""
    return "".join(str(eq_id).split())


def equipment_product_map(movements: pd.DataFrame | None,
                          limits: pd.DataFrame | None = None) -> dict:
    """Producto(s) por equipo: {equipment_id (str): product}.

    Fuente primaria: los productos HABILITADOS del equipo (`consumption_limits`,
    replicado de `EquipmentItem.consumptionTanks` — el panel 'Products consumed'
    de AdaptIQ). Asi el producto sale aunque el equipo sea nuevo y no haya
    despachado nunca; varios productos se unen con ', '. Respaldo: el producto
    MAS despachado segun el historial de movimientos (comportamiento previo),
    para equipos sin limite cargado en el FMS. Incluye alias sin espacios
    internos para resolver duplicados del maestro (ver `_compact_id`).
    """
    out: dict = {}

    # 1) Productos habilitados (lo que AdaptIQ muestra como asignado al equipo).
    if (limits is not None and not limits.empty
            and {"equipment_id", "product"}.issubset(limits.columns)):
        lim = limits[limits["equipment_id"].map(_present)
                     & limits["product"].map(_present)]
        prods: dict[str, list[str]] = {}
        for eid, p in zip(lim["equipment_id"], lim["product"]):
            vals = prods.setdefault(str(eid), [])
            if str(p) not in vals:
                vals.append(str(p))
        out = {k: ", ".join(sorted(v)) for k, v in prods.items()}

    # 2) Respaldo: el mas despachado del historial (solo equipos sin limite).
    if (movements is not None and not movements.empty
            and {"equipment_id", "product"}.issubset(movements.columns)):
        df = movements
        if "kind" in df.columns:   # preferir despachos (el producto del repostaje)
            disp = df[df["kind"] == config.KIND_DISPENSE]
            if not disp.empty:
                df = disp
        df = df[df["equipment_id"].map(_present) & df["product"].map(_present)]
        if not df.empty:
            df = df.copy()
            df["equipment_id"] = df["equipment_id"].astype("string")
            for eq_id, chunk in df.groupby("equipment_id"):
                mode = chunk["product"].mode()
                if not mode.empty:
                    out.setdefault(str(eq_id), mode.iloc[0])

    # 3) Alias compactos: una clave con espacios internos tambien responde por
    # su variante compacta (no pisa claves reales existentes).
    for key, val in list(out.items()):
        alias = _compact_id(key)
        if alias != key and alias not in out:
            out[alias] = val
    return out


def _equipment_attrs(e, prod: dict) -> dict:
    """Atributos del equipo que viajan al reporte (ID + cost centre/depto/producto
    + columnas de soporte _*). El producto intenta el id exacto y luego su
    variante compacta (duplicados del maestro: 'C- SE-12' -> 'C-SE-12')."""
    eq_id = e.get("equipment_id")
    product = prod.get(str(eq_id))
    if product is None:
        product = prod.get(_compact_id(eq_id))
    return {
        "ID":          eq_id,
        "Cost Center": e.get("cost_centre"),
        "Department":  e.get("department"),
        "Product":     product,
        "_Status":     e.get("status"),
        "_Category":   e.get("category"),
        "_Group":      e.get("group"),
        "_Description": e.get("description"),
    }


def tag_lookup(equipment: pd.DataFrame | None,
               movements: pd.DataFrame | None = None,
               limits: pd.DataFrame | None = None) -> dict:
    """Mapa {TAG_MAYUSCULAS: atributos del equipo} desde el maestro ACTUAL.

    Cada equipo puede tener mas de un tag (`rfid` separado por comas). El producto
    se toma de `equipment_product_map` (habilitados del FMS -> mas despachado).
    Si el mismo tag estuviera en dos equipos, gana el primero (la duplicidad la
    reporta la validacion de tags duplicados).
    """
    out: dict = {}
    if equipment is None or equipment.empty or "rfid" not in equipment.columns:
        return out
    prod = equipment_product_map(movements, limits)
    for _, e in equipment.iterrows():
        attrs = _equipment_attrs(e, prod)
        for tag in _split_tags(e.get("rfid")):
            key = tag.upper()
            if key not in out:
                out[key] = attrs
    return out


def equipment_by_id(equipment: pd.DataFrame | None,
                    movements: pd.DataFrame | None = None,
                    limits: pd.DataFrame | None = None) -> dict:
    """Mapa {equipment_id: atributos} del maestro actual (para resolver via el
    historial de asignaciones: el equipo sigue existiendo aunque el tag se haya
    quitado, solo cambio su `rfid`)."""
    out: dict = {}
    if equipment is None or equipment.empty:
        return out
    prod = equipment_product_map(movements, limits)
    for _, e in equipment.iterrows():
        eid = e.get("equipment_id")
        if _present(eid):
            out[str(eid)] = _equipment_attrs(e, prod)
    return out


def _history_map(history: pd.DataFrame | None) -> dict:
    """Mapa {TAG_MAYUSCULAS: equipment_id} desde el historial de asignaciones."""
    out: dict = {}
    if history is None or history.empty or "tag" not in history.columns:
        return out
    for _, h in history.iterrows():
        tag = h.get("tag")
        if _present(tag):
            out[str(tag).strip().upper()] = h.get("equipment_id")
    return out


# ===========================================================================
# Reporte de instalacion (driven por el log de auditoria)
# ===========================================================================

def _classify(before, after) -> tuple[str, object]:
    """(TYPE, Tag) segun la presencia de before/after del evento RFID."""
    has_b, has_a = _present(before), _present(after)
    if not has_b and has_a:
        return config.TYPE_NEW, after          # alta: el tag nuevo
    if has_b and has_a:
        return config.TYPE_REPLACEMENT, after   # reemplazo: el tag vigente
    return config.TYPE_REMOVAL, before          # remocion: el tag que se quito


def installation_report(changes: pd.DataFrame | None,
                        equipment: pd.DataFrame | None,
                        movements: pd.DataFrame | None = None,
                        date_from: pd.Timestamp | None = None,
                        date_to: pd.Timestamp | None = None,
                        history: pd.DataFrame | None = None,
                        limits: pd.DataFrame | None = None) -> pd.DataFrame:
    """Construye el reporte de instalacion de tags a partir del log de auditoria.

    Una fila por evento de cambio de RFID en el rango [date_from, date_to]
    (inclusive). DATE = fecha real del evento. El equipo se resuelve en cascada:
      1. por VALOR del tag contra el maestro ACTUAL (`rfidTags` vigentes);
      2. si el tag ya no esta vigente (removido/reemplazado), por el `history`
         (historial tag->equipo que acumula el poller) -> el equipo sigue
         existiendo, solo cambio su tag;
      3. si tampoco, el ID se marca como `config.UNIDENTIFIED` (no se puede saber).

    `limits` (consumption_limits) aporta el producto ASIGNADO al equipo (los
    'Products consumed' de AdaptIQ): sin el, un equipo recien tagueado y sin
    despachos quedaba con la columna Product vacia.
    """
    if changes is None or changes.empty:
        return pd.DataFrame(columns=_REPORT_COLS)
    mask = (changes["record_type"] == config.CHANGE_RECORD_RFID) & \
           (changes["attribute"] == config.ATTR_RFID)
    df = changes[mask].copy()
    if df.empty:
        return pd.DataFrame(columns=_REPORT_COLS)

    df["changed_at"] = pd.to_datetime(df["changed_at"], errors="coerce")
    df = df.dropna(subset=["changed_at"])
    if date_from is not None:
        df = df[df["changed_at"] >= pd.Timestamp(date_from)]
    if date_to is not None:
        # Incluir el dia completo de date_to.
        end = pd.Timestamp(date_to).normalize() + pd.Timedelta(days=1)
        df = df[df["changed_at"] < end]
    if df.empty:
        return pd.DataFrame(columns=_REPORT_COLS)

    lut = tag_lookup(equipment, movements, limits)
    eq_by_id = equipment_by_id(equipment, movements, limits)
    hist = _history_map(history)
    rows = []
    for _, r in df.iterrows():
        kind, tag = _classify(r.get("before"), r.get("after"))
        key = str(tag).strip().upper() if _present(tag) else None
        attrs: dict = {}
        ident = None
        if key and key in lut:                       # 1. tag vigente en el maestro
            attrs = lut[key]
            ident = attrs.get("ID")
        elif key and key in hist:                    # 2. via historial de asignaciones
            eid = hist[key]
            attrs = eq_by_id.get(str(eid), {})
            ident = attrs.get("ID") if attrs else eid
            if not _present(ident):
                ident = eid
        the_id = ident if _present(ident) else config.UNIDENTIFIED   # 3. marcador
        rows.append({
            "TYPE":         kind,
            "DATE":         r["changed_at"],
            "ID":           the_id,
            "Tag":          tag,
            "Cost Center":  attrs.get("Cost Center"),
            "Department":   attrs.get("Department"),
            "Product":      attrs.get("Product"),
            "whodunnit":    r.get("whodunnit"),
            "_Status":      attrs.get("_Status"),
            "_Category":    attrs.get("_Category"),
            "_Group":       attrs.get("_Group"),
            "_Description": attrs.get("_Description"),
        })
    out = pd.DataFrame(rows, columns=_REPORT_COLS)
    return out.sort_values("DATE").reset_index(drop=True)


def report_display(report: pd.DataFrame) -> pd.DataFrame:
    """Subconjunto visible del reporte (esquema semanal + Usuario)."""
    if report is None or report.empty:
        return pd.DataFrame(columns=DISPLAY_COLS)
    return report[[c for c in DISPLAY_COLS if c in report.columns]].copy()


def current_inventory(equipment: pd.DataFrame | None) -> pd.DataFrame:
    """Equipos del maestro que hoy tienen al menos un tag RFID asignado."""
    if equipment is None or equipment.empty or "rfid" not in equipment.columns:
        return pd.DataFrame(columns=_INVENTORY_COLS)
    eq = equipment[~_blank_series(equipment["rfid"])]
    cols = [c for c in _INVENTORY_COLS if c in eq.columns]
    return eq[cols].reset_index(drop=True)


# ===========================================================================
# KPIs y agrupaciones
# ===========================================================================

def summary_kpis(report: pd.DataFrame, equipment: pd.DataFrame | None) -> dict:
    """Indicadores ejecutivos del periodo."""
    if report is None or report.empty:
        nuevas = reemplazos = remociones = tags = 0
    else:
        tipos = report["TYPE"].value_counts()
        nuevas = int(tipos.get(config.TYPE_NEW, 0))
        reemplazos = int(tipos.get(config.TYPE_REPLACEMENT, 0))
        remociones = int(tipos.get(config.TYPE_REMOVAL, 0))
        tags = int(report["Tag"].dropna().nunique())
    con_rfid = total = 0
    if equipment is not None and not equipment.empty and "rfid" in equipment.columns:
        con_rfid = int((~_blank_series(equipment["rfid"])).sum())
        total = len(equipment)
    return {
        "Nuevas instalaciones": nuevas,
        "Reemplazos": reemplazos,
        "Remociones": remociones,
        "Tags distintos": tags,
        "Total con RFID": con_rfid,
        "Total equipos": total,
    }


def by_type_summary(report: pd.DataFrame) -> pd.DataFrame:
    """Conteo de cambios por tipo de operacion."""
    if report is None or report.empty:
        return pd.DataFrame(columns=["Tipo de operacion", "Cantidad"])
    counts = report["TYPE"].value_counts().reset_index()
    counts.columns = ["Tipo de operacion", "Cantidad"]
    return counts


def _group_summary(report: pd.DataFrame, col: str, label: str) -> pd.DataFrame:
    """Instalaciones agrupadas por una columna del reporte, con desglose por tipo."""
    cols = [label, "Instalaciones", "Nuevas", "Reemplazos", "Remociones"]
    if report is None or report.empty or col not in report.columns:
        return pd.DataFrame(columns=cols)
    work = report.copy()
    work[col] = work[col].astype("string").str.strip().replace({"": pd.NA}).fillna("(sin dato)")
    rows = []
    for key, chunk in work.groupby(col, sort=True):
        rows.append({
            label:          key,
            "Instalaciones": len(chunk),
            "Nuevas":        int((chunk["TYPE"] == config.TYPE_NEW).sum()),
            "Reemplazos":    int((chunk["TYPE"] == config.TYPE_REPLACEMENT).sum()),
            "Remociones":    int((chunk["TYPE"] == config.TYPE_REMOVAL).sum()),
        })
    return (pd.DataFrame(rows, columns=cols)
            .sort_values("Instalaciones", ascending=False).reset_index(drop=True))


def by_department_summary(report: pd.DataFrame) -> pd.DataFrame:
    return _group_summary(report, "Department", "Departamento")


def by_cost_center_summary(report: pd.DataFrame) -> pd.DataFrame:
    return _group_summary(report, "Cost Center", "Cost Center")


def by_category_summary(report: pd.DataFrame) -> pd.DataFrame:
    return _group_summary(report, "_Category", "Categoria")


# ===========================================================================
# Validaciones (anomalias del reporte)
# ===========================================================================

def find_out_of_service(report: pd.DataFrame) -> pd.DataFrame:
    """Tags instalados/reemplazados en un equipo con estado 'Out of Service'."""
    if report is None or report.empty or "_Status" not in report.columns:
        return pd.DataFrame(columns=DISPLAY_COLS)
    mask = report["_Status"].astype("string").str.strip() == STATUS_OUT
    return report_display(report[mask])


def find_duplicate_tags(equipment: pd.DataFrame | None) -> pd.DataFrame:
    """El mismo valor de tag asignado a MAS DE UN equipo en el maestro actual.

    Es un error de datos (un tag fisico no puede estar en dos equipos). Se detecta
    sobre el maestro (no sobre el reporte) porque el enlace por valor asocia cada
    tag a un solo equipo; la doble-asignacion solo se ve en el inventario vigente.
    """
    cols = ["Tag", "equipment_id", "description", "status", "Equipos con este tag"]
    if equipment is None or equipment.empty or "rfid" not in equipment.columns:
        return pd.DataFrame(columns=cols)
    pairs = []
    for _, e in equipment.iterrows():
        for tag in _split_tags(e.get("rfid")):
            pairs.append({
                "Tag": tag.upper(), "equipment_id": e.get("equipment_id"),
                "description": e.get("description"), "status": e.get("status"),
            })
    if not pairs:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(pairs)
    counts = df.groupby("Tag")["equipment_id"].nunique()
    dup = counts[counts > 1].index
    out = df[df["Tag"].isin(dup)].copy()
    if out.empty:
        return pd.DataFrame(columns=cols)
    out["Equipos con este tag"] = out["Tag"].map(counts)
    return out.sort_values(["Tag", "equipment_id"])[cols].reset_index(drop=True)


def find_duplicate_ids(report: pd.DataFrame) -> pd.DataFrame:
    """Equipos cuyo ID aparece mas de una vez en el periodo (re-tagueo legitimo
    o posible inconsistencia)."""
    if report is None or report.empty or "ID" not in report.columns:
        return pd.DataFrame()
    work = report[report["ID"].map(_identified)].copy()
    if work.empty:
        return pd.DataFrame()
    counts = work["ID"].value_counts()
    dup = counts[counts > 1].index
    out = work[work["ID"].isin(dup)].copy()
    if out.empty:
        return pd.DataFrame()
    out["Ocurrencias"] = out["ID"].map(counts)
    cols = DISPLAY_COLS + ["Ocurrencias"]
    return out.sort_values("ID")[[c for c in cols if c in out.columns]].reset_index(drop=True)


def find_incomplete_records(report: pd.DataFrame) -> pd.DataFrame:
    """Altas/reemplazos cuyo equipo NO se pudo identificar (ni por el maestro
    vigente ni por el historial de asignaciones) -> ID vacio o marcado como
    `config.UNIDENTIFIED`. Se excluyen las remociones (su equipo puede ser
    legitimamente desconocido si nunca se observo el tag asignado)."""
    if report is None or report.empty or "ID" not in report.columns:
        return pd.DataFrame()
    work = report[report["TYPE"] != config.TYPE_REMOVAL].copy()
    if work.empty:
        return pd.DataFrame()
    mask = ~work["ID"].map(_identified)
    return report_display(work[mask])


def validation_summary(report: pd.DataFrame,
                       equipment: pd.DataFrame | None = None) -> pd.DataFrame:
    """Tabla resumen con el conteo de cada anomalia."""
    return pd.DataFrame([
        {"Validacion": "Equipos fuera de servicio", "Anomalias": len(find_out_of_service(report)),
         "Descripcion": "Tag instalado en equipo con estado 'Out of Service'"},
        {"Validacion": "Tags hexadecimales duplicados", "Anomalias": len(find_duplicate_tags(equipment)),
         "Descripcion": "El mismo tag asignado a mas de un equipo en el maestro"},
        {"Validacion": "IDs duplicados en el periodo", "Anomalias": len(find_duplicate_ids(report)),
         "Descripcion": "El mismo equipo aparece mas de una vez (re-tagueo)"},
        {"Validacion": "Altas/reemplazos sin equipo", "Anomalias": len(find_incomplete_records(report)),
         "Descripcion": "Alta o reemplazo sin equipo identificado o con campos vacios"},
    ])
