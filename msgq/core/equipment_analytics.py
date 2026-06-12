"""Analitica de la flota de equipos.

Dos familias de funciones:

  • Snapshot (sobre el DataFrame de equipos, esquema `EQUIPMENT_COLS`): KPIs de
    flota y agrupaciones por categoria/grupo/departamento/marca — adaptado del
    patron del proyecto TLS (`fms_analyzer/core/equipment.py`).

  • Temporal (sobre el DataFrame de `change_events`, el log de auditoria):
    - Frecuencia de cambio de RFID (alta/cambio/remocion) a nivel de flota.
    - Transiciones de estado por equipo, con foco In Service -> Out of Service,
      y tiempo medio en servicio antes de salir.
    - Auditoria: quien (whodunnit) hace los cambios.

Nota: el log NO enlaza el tag RFID con su equipo (recordType `EquipmentRfid`
sin FK), por eso la frecuencia de RFID es de flota/registro-de-tag. Las
transiciones de estado SI son por equipo (recordType `EquipmentItem`,
atributo `equipment_status_id`, enlazable por `internal_id`).
"""
from __future__ import annotations

import pandas as pd

from msgq import config

STATUS_IN = config.STATUS_IN
STATUS_OUT = config.STATUS_OUT
STATUS_DECOM = config.STATUS_DECOM


# ===========================================================================
# Snapshot de flota
# ===========================================================================

def fleet_kpis(eq: pd.DataFrame) -> dict:
    if eq is None or eq.empty:
        return {}
    status = eq["status"].astype("string").str.strip()
    total = len(eq)
    in_service = int((status == STATUS_IN).sum())
    out = int((status == STATUS_OUT).sum())
    decom = int((status == STATUS_DECOM).sum())
    contractor = int(_truthy(eq.get("is_contractor_vehicle")).sum())
    light = int(_truthy(eq.get("is_light_vehicle")).sum())
    return {
        "Total equipos": total,
        "En servicio": in_service,
        "Fuera de servicio": out,
        "Dados de baja": decom,
        "Disponibilidad %": (in_service / total * 100) if total else 0.0,
        "De contratista": contractor,
        "% contratista": (contractor / total * 100) if total else 0.0,
        "Vehiculos ligeros": light,
    }


def group_summary(eq: pd.DataFrame, column: str, label: str) -> pd.DataFrame:
    """Conteo y disponibilidad por una columna (categoria/grupo/depto/marca)."""
    if eq is None or eq.empty or column not in eq.columns:
        return pd.DataFrame()
    work = eq.copy()
    work["_status"] = work["status"].astype("string").str.strip()
    work["_key"] = (work[column].astype("string").str.strip()
                    .replace({"": "(sin dato)"}).fillna("(sin dato)"))
    rows = []
    for key, chunk in work.groupby("_key"):
        total = len(chunk)
        in_service = int((chunk["_status"] == STATUS_IN).sum())
        rows.append({
            label: key,
            "Total": total,
            "En servicio": in_service,
            "Fuera de servicio": int((chunk["_status"] == STATUS_OUT).sum()),
            "Dados de baja": int((chunk["_status"] == STATUS_DECOM).sum()),
            "Disponibilidad %": round((in_service / total * 100) if total else 0.0, 1),
        })
    return pd.DataFrame(rows).sort_values("Total", ascending=False).reset_index(drop=True)


def status_breakdown(eq: pd.DataFrame) -> pd.DataFrame:
    """Conteo por estado (para el grafico de barras)."""
    if eq is None or eq.empty:
        return pd.DataFrame(columns=["Estado", "Equipos"])
    s = eq["status"].astype("string").str.strip().replace({"": "(sin dato)"}).fillna("(sin dato)")
    out = s.value_counts().rename_axis("Estado").reset_index(name="Equipos")
    return out


def contractor_summary(eq: pd.DataFrame) -> pd.DataFrame:
    if eq is None or eq.empty:
        return pd.DataFrame()
    contractors = eq[_truthy(eq.get("is_contractor_vehicle"))]
    if contractors.empty:
        return pd.DataFrame()
    # Agrupa por departamento como proxy de contratista (no hay col contractor).
    col = "department" if "department" in contractors.columns else "make"
    return group_summary(contractors, col, "Contratista/Depto")


_COMPLETENESS_FIELDS = [
    "registration_number", "category", "group", "make", "model",
    "department", "cost_centre", "rfid",
]


def data_completeness(eq: pd.DataFrame) -> pd.DataFrame:
    """Porcentaje de registros con dato presente en cada campo clave."""
    if eq is None or eq.empty:
        return pd.DataFrame()
    total = len(eq)
    rows = []
    for f in _COMPLETENESS_FIELDS:
        if f in eq.columns:
            missing = int(_blank(eq[f]).sum())
        else:
            missing = total
        filled = total - missing
        rows.append({
            "Campo": f,
            "Con datos": filled,
            "Sin datos": missing,
            "Completitud %": round((filled / total * 100) if total else 0.0, 1),
        })
    return pd.DataFrame(rows)


# ===========================================================================
# Temporal — Cambios de RFID
# ===========================================================================

def rfid_changes(changes: pd.DataFrame) -> pd.DataFrame:
    """Filtra y clasifica los eventos de cambio de RFID."""
    if changes is None or changes.empty:
        return pd.DataFrame(columns=["changed_at", "record_id", "tipo", "before", "after", "whodunnit"])
    mask = (changes["record_type"] == config.CHANGE_RECORD_RFID) & \
           (changes["attribute"] == config.ATTR_RFID)
    df = changes[mask].copy()
    if df.empty:
        return df
    # Clasificacion vectorizada (antes un apply fila a fila sobre todo el log):
    # before y after -> Cambiado; solo after -> Asignado; solo before -> Removido.
    has_before, has_after = df["before"].notna(), df["after"].notna()
    tipo = pd.Series("Removido", index=df.index, dtype=object)
    tipo[has_after] = "Asignado"
    tipo[has_before & has_after] = "Cambiado"
    df["tipo"] = tipo
    return df.sort_values("changed_at", ascending=False).reset_index(drop=True)


def rfid_change_summary(changes: pd.DataFrame) -> dict:
    df = rfid_changes(changes)
    if df.empty:
        return {"Eventos RFID": 0, "Asignados": 0, "Cambiados": 0,
                "Removidos": 0, "Tags (registros)": 0}
    counts = df["tipo"].value_counts()
    return {
        "Eventos RFID": len(df),
        "Asignados": int(counts.get("Asignado", 0)),
        "Cambiados": int(counts.get("Cambiado", 0)),
        "Removidos": int(counts.get("Removido", 0)),
        "Tags (registros)": int(df["record_id"].nunique()),
    }


def rfid_changes_over_time(changes: pd.DataFrame, freq: str = "ME") -> pd.DataFrame:
    """Serie temporal de eventos de RFID por periodo y tipo."""
    df = rfid_changes(changes)
    if df.empty:
        return pd.DataFrame(columns=["Periodo", "Asignado", "Cambiado", "Removido", "Total"])
    df = df.dropna(subset=["changed_at"])
    if df.empty:
        return pd.DataFrame(columns=["Periodo", "Asignado", "Cambiado", "Removido", "Total"])
    g = (df.set_index("changed_at").groupby([pd.Grouper(freq=freq), "tipo"])
         .size().unstack(fill_value=0))
    for col in ("Asignado", "Cambiado", "Removido"):
        if col not in g.columns:
            g[col] = 0
    g["Total"] = g[["Asignado", "Cambiado", "Removido"]].sum(axis=1)
    g = g.reset_index().rename(columns={"changed_at": "Periodo"})
    return g[["Periodo", "Asignado", "Cambiado", "Removido", "Total"]]


def rfid_churn_by_tag(changes: pd.DataFrame) -> pd.DataFrame:
    """Registros de tag con mas cambios (proxy de 're-tagueo')."""
    df = rfid_changes(changes)
    if df.empty:
        return pd.DataFrame(columns=["record_id", "Eventos", "Ultimo cambio"])
    g = df.groupby("record_id").agg(
        Eventos=("tipo", "size"),
        **{"Ultimo cambio": ("changed_at", "max")}).reset_index()
    return g.sort_values("Eventos", ascending=False).reset_index(drop=True)


# ===========================================================================
# Temporal — Transiciones de estado (In <-> Out <-> Decom)
# ===========================================================================

_EQ_DIMS = ["equipment_id", "description", "group", "category", "cost_centre", "department"]


def _link_equipment(df: pd.DataFrame, equipment: pd.DataFrame | None) -> pd.DataFrame:
    """Agrega columnas del equipo (id/descripcion/grupo/categoria/cost centre/
    departamento) a un df de cambios, enlazando record_id <-> internal_id."""
    out = df.copy()
    for c in _EQ_DIMS:
        out[c] = pd.NA
    if equipment is None or equipment.empty or "internal_id" not in equipment.columns:
        return out
    lut = equipment.dropna(subset=["internal_id"]).copy()
    lut["internal_id"] = lut["internal_id"].astype("string")
    lut = lut.drop_duplicates("internal_id").set_index("internal_id")
    key = out["record_id"].astype("string")
    for c in _EQ_DIMS:
        if c in lut.columns:
            out[c] = key.map(lut[c])
    return out


def status_transitions(changes: pd.DataFrame,
                       equipment: pd.DataFrame | None = None) -> pd.DataFrame:
    """Transiciones de estado por equipo, con nombres legibles y (si se pasa el
    inventario) equipo/grupo/cost centre enlazados por `internal_id`."""
    cols = ["changed_at", "record_id", "equipment_id", "description",
            "group", "cost_centre", "De", "A", "whodunnit"]
    if changes is None or changes.empty:
        return pd.DataFrame(columns=cols)
    mask = (changes["record_type"] == config.CHANGE_RECORD_EQUIPMENT) & \
           (changes["attribute"] == config.ATTR_STATUS)
    df = changes[mask].copy()
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["De"] = df["before"].map(_status_name)
    df["A"] = df["after"].map(_status_name)
    # Solo transiciones reales (descarta el 'create' inicial before=None).
    df = df[df["before"].notna()]
    if df.empty:
        return pd.DataFrame(columns=cols)
    df = _link_equipment(df, equipment)
    return df.sort_values("changed_at", ascending=False)[cols].reset_index(drop=True)


def transitions_by_dimension(transitions: pd.DataFrame, dim_col: str,
                             dim_label: str) -> pd.DataFrame:
    """Transiciones agrupadas por una dimension del equipo (grupo, cost centre,
    categoria, departamento), con desglose In->Out / Out->In."""
    cols = [dim_label, "Total", "In->Out", "Out->In"]
    if transitions is None or transitions.empty or dim_col not in transitions.columns:
        return pd.DataFrame(columns=cols)
    df = transitions.copy()
    df["_k"] = (df[dim_col].astype("string").str.strip()
                .replace({"": "(sin dato)"}).fillna("(sin dato)"))
    rows = []
    for k, ch in df.groupby("_k"):
        rows.append({
            dim_label: k, "Total": len(ch),
            "In->Out": int(((ch["De"] == STATUS_IN) & (ch["A"] == STATUS_OUT)).sum()),
            "Out->In": int(((ch["De"] == STATUS_OUT) & (ch["A"] == STATUS_IN)).sum()),
        })
    return pd.DataFrame(rows).sort_values("Total", ascending=False).reset_index(drop=True)


def top_equipment_by_transition(transitions: pd.DataFrame, de: str, a: str,
                                n: int = 25) -> pd.DataFrame:
    """Equipos con mas transiciones del tipo `de`->`a` (p. ej. Out->In)."""
    cols = ["equipment_id", "description", "group", "cost_centre", "Veces", "Ultimo"]
    if transitions is None or transitions.empty:
        return pd.DataFrame(columns=cols)
    df = transitions[(transitions["De"] == de) & (transitions["A"] == a)]
    if df.empty:
        return pd.DataFrame(columns=cols)
    g = df.groupby("record_id").agg(
        equipment_id=("equipment_id", "first"),
        description=("description", "first"),
        group=("group", "first"),
        cost_centre=("cost_centre", "first"),
        Veces=("changed_at", "size"),
        Ultimo=("changed_at", "max")).reset_index(drop=True)
    return g.sort_values("Veces", ascending=False).head(n)[cols].reset_index(drop=True)


# --- Cambios por atributo (cost centre, grupo, etc.) -----------------------

def attribute_changes(changes: pd.DataFrame, attribute: str,
                      equipment: pd.DataFrame | None = None,
                      real_only: bool = True) -> pd.DataFrame:
    """Eventos de cambio de un atributo de EquipmentItem, enlazados al equipo."""
    if changes is None or changes.empty:
        return pd.DataFrame()
    mask = (changes["record_type"] == config.CHANGE_RECORD_EQUIPMENT) & \
           (changes["attribute"] == attribute)
    df = changes[mask].copy()
    if real_only:
        df = df[df["before"].notna()]   # reasignacion, no alta inicial
    if df.empty:
        return pd.DataFrame()
    df = _link_equipment(df, equipment)
    return df.sort_values("changed_at", ascending=False).reset_index(drop=True)


def top_equipment_by_attribute(changes: pd.DataFrame, attribute: str,
                               equipment: pd.DataFrame | None = None,
                               n: int = 25, label: str = "Cambios") -> pd.DataFrame:
    """Equipos que mas veces cambiaron un atributo (p. ej. cost_centre_id)."""
    cols = ["equipment_id", "description", "group", "cost_centre", label, "Ultimo"]
    ac = attribute_changes(changes, attribute, equipment)
    if ac.empty:
        return pd.DataFrame(columns=cols)
    g = ac.groupby("record_id").agg(
        equipment_id=("equipment_id", "first"),
        description=("description", "first"),
        group=("group", "first"),
        cost_centre=("cost_centre", "first"),
        **{label: ("changed_at", "size")},
        Ultimo=("changed_at", "max")).reset_index(drop=True)
    return g.sort_values(label, ascending=False).head(n)[cols].reset_index(drop=True)


def attribute_change_by_dimension(changes: pd.DataFrame, attribute: str,
                                  equipment: pd.DataFrame | None,
                                  dim_col: str, dim_label: str,
                                  value_label: str = "Cambios") -> pd.DataFrame:
    """Cambios de un atributo agrupados por una dimension del equipo (p. ej.
    cambios de cost centre agrupados por el cost centre actual del equipo)."""
    cols = [dim_label, value_label, "Equipos"]
    ac = attribute_changes(changes, attribute, equipment)
    if ac.empty or dim_col not in ac.columns:
        return pd.DataFrame(columns=cols)
    ac = ac.copy()
    ac["_k"] = (ac[dim_col].astype("string").str.strip()
                .replace({"": "(sin dato)"}).fillna("(sin dato)"))
    g = ac.groupby("_k").agg(
        **{value_label: ("changed_at", "size"), "Equipos": ("record_id", "nunique")}
    ).reset_index().rename(columns={"_k": dim_label})
    return g.sort_values(value_label, ascending=False).reset_index(drop=True)


def attribute_change_summary(changes: pd.DataFrame) -> pd.DataFrame:
    """Atributos de equipo que mas se modifican (reasignaciones reales)."""
    cols = ["Atributo", "Cambios", "Equipos"]
    if changes is None or changes.empty:
        return pd.DataFrame(columns=cols)
    mask = (changes["record_type"] == config.CHANGE_RECORD_EQUIPMENT) & changes["before"].notna()
    df = changes[mask]
    if df.empty:
        return pd.DataFrame(columns=cols)
    g = df.groupby("attribute").agg(
        Cambios=("changed_at", "size"), Equipos=("record_id", "nunique")).reset_index()
    g["Atributo"] = g["attribute"].map(attr_label)
    return g[cols].sort_values("Cambios", ascending=False).reset_index(drop=True)


def equipment_audit_log(changes: pd.DataFrame, record_id: str) -> pd.DataFrame:
    """Historial de cambios de UN equipo (como la pestaña Audit Log de AdaptIQ)."""
    cols = ["changed_at", "whodunnit", "event", "Atributo", "De", "A"]
    if changes is None or changes.empty or record_id is None:
        return pd.DataFrame(columns=cols)
    mask = (changes["record_type"] == config.CHANGE_RECORD_EQUIPMENT) & \
           (changes["record_id"].astype("string") == str(record_id))
    df = changes[mask].copy()
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["Atributo"] = df["attribute"].map(attr_label)
    df["De"] = df.apply(lambda r: _render_value(r["attribute"], r["before"]), axis=1)
    df["A"] = df.apply(lambda r: _render_value(r["attribute"], r["after"]), axis=1)
    return df.sort_values("changed_at", ascending=False)[cols].reset_index(drop=True)


def status_transition_summary(transitions: pd.DataFrame) -> pd.DataFrame:
    """Conteo por tipo de transicion (De -> A)."""
    if transitions is None or transitions.empty:
        return pd.DataFrame(columns=["Transicion", "Veces"])
    g = (transitions.groupby(["De", "A"]).size().reset_index(name="Veces"))
    g["Transicion"] = g["De"] + " -> " + g["A"]
    return g[["Transicion", "Veces"]].sort_values("Veces", ascending=False).reset_index(drop=True)


def in_to_out_over_time(transitions: pd.DataFrame, freq: str = "ME") -> pd.DataFrame:
    """Serie temporal de transiciones In Service -> Out of Service."""
    cols = ["Periodo", "In->Out"]
    if transitions is None or transitions.empty:
        return pd.DataFrame(columns=cols)
    df = transitions[(transitions["De"] == STATUS_IN) & (transitions["A"] == STATUS_OUT)]
    df = df.dropna(subset=["changed_at"])
    if df.empty:
        return pd.DataFrame(columns=cols)
    g = (df.set_index("changed_at").groupby(pd.Grouper(freq=freq)).size()
         .reset_index(name="In->Out").rename(columns={"changed_at": "Periodo"}))
    return g


def time_in_service(transitions: pd.DataFrame) -> pd.DataFrame:
    """Por equipo: nº de salidas a Out, y dias promedio en servicio antes de salir.

    El tiempo en servicio se mide entre una entrada a In Service y la siguiente
    salida a Out of Service del mismo equipo.
    """
    cols = ["record_id", "equipment_id", "description", "Salidas a Out", "Dias prom. en servicio"]
    if transitions is None or transitions.empty:
        return pd.DataFrame(columns=cols)
    df = transitions.dropna(subset=["changed_at"]).sort_values("changed_at")
    rows = []
    for rid, chunk in df.groupby("record_id"):
        chunk = chunk.sort_values("changed_at")
        last_in = None
        spans = []
        exits = 0
        for _, r in chunk.iterrows():
            if r["A"] == STATUS_IN:
                last_in = r["changed_at"]
            elif r["A"] == STATUS_OUT:
                exits += 1
                if last_in is not None:
                    spans.append((r["changed_at"] - last_in).total_seconds() / 86400.0)
                    last_in = None
        meta = chunk.iloc[-1]
        rows.append({
            "record_id": rid,
            "equipment_id": meta.get("equipment_id"),
            "description": meta.get("description"),
            "Salidas a Out": exits,
            "Dias prom. en servicio": round(sum(spans) / len(spans), 1) if spans else None,
        })
    out = pd.DataFrame(rows, columns=cols)
    return out.sort_values("Salidas a Out", ascending=False).reset_index(drop=True)


# ===========================================================================
# Temporal — Auditoria (quien) y actividad
# ===========================================================================

def audit_by_user(changes: pd.DataFrame) -> pd.DataFrame:
    if changes is None or changes.empty:
        return pd.DataFrame(columns=["Usuario", "Cambios", "Equipos", "RFID", "Ultimo cambio"])
    df = changes.copy()
    rows = []
    for user, chunk in df.groupby(df["whodunnit"].fillna("(desconocido)")):
        rows.append({
            "Usuario": user,
            "Cambios": len(chunk),
            "Equipos": int((chunk["record_type"] == config.CHANGE_RECORD_EQUIPMENT).sum()),
            "RFID": int((chunk["record_type"] == config.CHANGE_RECORD_RFID).sum()),
            "Ultimo cambio": chunk["changed_at"].max(),
        })
    return pd.DataFrame(rows).sort_values("Cambios", ascending=False).reset_index(drop=True)


# ===========================================================================
# Helpers
# ===========================================================================

def attr_label(attr) -> str:
    """Nombre legible de un atributo del log de auditoria."""
    if attr is None:
        return ""
    return config.ATTR_LABELS.get(attr, str(attr).replace("_", " ").title())


def _render_value(attr, val):
    """Valor legible para el audit log (mapea ids de estado a su nombre)."""
    if _is_na(val):
        return None
    if attr == config.ATTR_STATUS:
        return config.EQUIPMENT_STATUS_BY_ID.get(str(val).strip(), val)
    return val


def _status_name(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "(alta)"
    return config.EQUIPMENT_STATUS_BY_ID.get(str(value).strip(), f"id={value}")


def _is_na(v) -> bool:
    if v is None:
        return True
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _truthy(series) -> pd.Series:
    if series is None:
        return pd.Series(dtype=bool)
    return series.map(lambda v: bool(v) if not _is_na(v) else False)


def _blank(series: pd.Series) -> pd.Series:
    return series.isna() | series.astype("string").str.strip().isin(["", "<NA>", "nan"])
