"""Auditoria de Tag Hopping ("el tag en el bolsillo").

El tag RFID identifica al equipo: cada despacho queda imputado al equipo cuyo tag
se leyo. Si el MISMO tag autoriza dos despachos en puntos de despacho fisicamente
distintos en un lapso imposible (el equipo no pudo viajar entre ellos), alguien
removio el tag del equipo para robar combustible —o el tag esta clonado—. Se
detecta de dos formas COMPLEMENTARIAS (el usuario pidio ambas):

  1. SOLAPAMIENTO temporal (sin coordenadas): para cada equipo se ordenan sus
     despachos por hora; si dos CONSECUTIVOS ocurren en puntos distintos y sus
     intervalos [inicio, inicio+duracion] se solapan (mas que la holgura de
     reloj), es fisicamente imposible -> CRITICO. Cubre el ~99% de despachos de
     islas fijas, que NO traen GPS por transaccion. Es la senal de mayor confianza.

  2. VELOCIDAD implicita (con coordenadas): cuando ambos despachos traen GPS
     (`gps_coordinates`, presente en los surtidores moviles) —o el punto figura en
     el mapa OPCIONAL `point_coords` de coordenadas de islas fijas— se calcula la
     distancia (haversine) sobre el tiempo transcurrido; si la velocidad implicita
     supera lo plausible para ese equipo (umbral mas alto para vehiculos ligeros,
     que si se desplazan rapido), se marca.

Verificado contra los datos reales de Merian: el GPS por transaccion solo se
puebla en los 3 surtidores MOVILES (0,4% de los despachos); por eso la regla de
solapamiento —que no necesita coordenadas— es la que da cobertura, y la de
velocidad la complementa donde hay GPS (o donde se carga el mapa opcional).

El "punto de despacho" (la ubicacion) se deriva del ACTIVO surtidor: el PRIMER
segmento del `tank` ("TFL0847 - Diesel - iTank 6" -> "TFL0847"), no el tanque de
producto exacto. Asi, dos tanques distintos sobre el MISMO camion/taller/isla son
el mismo lugar — clave porque los service trucks llevan varios tanques (diesel +
lubricantes) y entregan a un equipo a la vez sin que eso sea hopping. Si la etiqueta
es de un medidor ("MER.13.1.6"), colapsa a la consola fisica ("MER.13").

Ademas, el solapamiento temporal solo cuenta como robo si es el MISMO producto en
ambos puntos: dos productos distintos solapados = servicio multi-producto legitimo
(no puedes verter el mismo combustible en dos sitios a la vez). La regla de
velocidad/GPS no filtra por producto (un teletransporte real es imposible igual).
`point_coords` (opcional) mapea esa etiqueta de ubicacion -> (lat, lon).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from msgq import config

# --- Un evento: par de despachos del mismo tag en dos lugares, imposible ------
EVENT_COLS = [
    "equipment_id", "equipment_description", "tag", "category",
    "date_prev", "location_prev", "date", "location",
    "gap_min", "distance_km", "speed_kmh", "reason", "severity",
    "source_id_prev", "source_id",
]

_BLANK = {"", "<NA>", "NAN", "NONE", "UNAUTHORISED"}
_EARTH_RADIUS_KM = 6371.0088


def _empty(cols: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=cols)


def _is_blank(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip().upper() in _BLANK


def parse_coords(value) -> tuple[float, float] | None:
    """Parsea unas coordenadas "lat,lon" a (lat, lon). None si no son validas."""
    if _is_blank(value):
        return None
    try:
        lat_s, lon_s = str(value).split(",", 1)
        lat, lon = float(lat_s.strip()), float(lon_s.strip())
    except (ValueError, AttributeError):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    if lat == 0.0 and lon == 0.0:        # (0,0) es el "sin fix" tipico, no un lugar
        return None
    return (lat, lon)


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distancia en km entre dos (lat, lon) por la formula del haversine."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def _equipment_maps(equipment: pd.DataFrame | None) -> dict[str, dict]:
    """Mapa {equipment_id: {rfid, category, description, light}} del maestro, para
    resolver el tag, la categoria (filtro de la ventana) y el tipo de vehiculo
    (umbral de velocidad). Vacio si no hay maestro."""
    out: dict[str, dict] = {}
    if equipment is None or equipment.empty or "equipment_id" not in equipment.columns:
        return out
    cols = equipment.columns
    for _, e in equipment.iterrows():
        eid = e.get("equipment_id")
        if _is_blank(eid):
            continue
        rfid = e.get("rfid") if "rfid" in cols else None
        tag = None
        if not _is_blank(rfid):
            tag = str(rfid).split(",")[0].strip().upper() or None
        lv = e.get("is_light_vehicle") if "is_light_vehicle" in cols else None
        out[str(eid).strip()] = {
            "tag": tag,
            "category": e.get("category") if "category" in cols else None,
            "description": e.get("description") if "description" in cols else None,
            "light": (not _is_blank(lv)) and bool(lv),
        }
    return out


def _location_series(df: pd.DataFrame) -> pd.Series:
    """Etiqueta de ubicacion DETALLADA por despacho (para mostrar): `tank`; si
    falta, `meter_id`."""
    tank = df["tank"].astype("string").str.strip() if "tank" in df.columns else None
    meter = df["meter_id"].astype("string").str.strip() if "meter_id" in df.columns else None
    if tank is None and meter is None:
        return pd.Series(pd.NA, index=df.index, dtype="string")
    loc = tank if tank is not None else pd.Series(pd.NA, index=df.index, dtype="string")
    blank = loc.isna() | loc.str.upper().isin(_BLANK)
    if meter is not None:
        loc = loc.where(~blank, meter)
    return loc


def _site_series(loc: pd.Series) -> pd.Series:
    """Ubicacion FISICA (el activo surtidor: un camion de servicio, el taller, la
    granja LFO), derivada del PRIMER segmento del `tank`:
    "TFL0847 - Diesel - iTank 6" -> "TFL0847". Asi, dos tanques de PRODUCTO distintos
    sobre el MISMO activo —p. ej. un service truck que entrega diesel y un lubricante
    al mismo equipo a la vez— quedan como el MISMO lugar y NO se marcan como tag
    hopping (era un falso positivo real: los service trucks llevan varios tanques).
    Si la etiqueta es de un medidor ("MER.13.1.6", sin " - "), colapsa a la consola
    fisica ("MER.13"); dos boquillas de la misma consola = el mismo lugar."""
    s = loc.astype("string").str.strip()
    # Activo (camion/taller/isla): lo que va antes del primer " - ".
    site = s.str.split(" - ").str[0].str.strip()
    # Etiqueta de medidor sin " - " (p. ej. MER.13.1.6): colapsa a consola MER.13.
    is_meter = ~s.str.contains(" - ", na=False) & s.str.contains(r"^[^.]+\.\d", na=False)
    console = s.str.split(".").str[:2].str.join(".")
    site = site.where(~is_meter, console)
    blank = site.isna() | site.str.upper().isin(_BLANK)
    return site.where(~blank, s)


def tag_hops(movements: pd.DataFrame | None,
             equipment: pd.DataFrame | None = None,
             point_coords: dict[str, tuple[float, float]] | None = None) -> pd.DataFrame:
    """Pares de despachos del MISMO equipo (tag) en puntos distintos cuyo lapso es
    fisicamente imposible. Devuelve un DataFrame con `EVENT_COLS`; `severity` =
    CRITICAL (solapamiento temporal o teletransporte) o WARNING (velocidad
    implicita implausible). `point_coords` (opcional) mapea etiqueta-de-ubicacion ->
    (lat, lon) para extender la regla de velocidad a las islas fijas sin GPS."""
    if movements is None or movements.empty:
        return _empty(EVENT_COLS)
    mv = movements
    if "kind" in mv.columns:
        mv = mv[mv["kind"] == config.KIND_DISPENSE]
    if mv.empty or "equipment_id" not in mv.columns:
        return _empty(EVENT_COLS)

    d = mv.copy()
    d["_eid"] = d["equipment_id"].astype("string").str.strip()
    d = d[~d["_eid"].isna() & ~d["_eid"].str.upper().isin(_BLANK)].copy()
    if d.empty:
        return _empty(EVENT_COLS)

    date_col = "record_collected_at" if "record_collected_at" in d.columns else "updated_at"
    d["_t"] = pd.to_datetime(d.get(date_col), errors="coerce")
    d = d[d["_t"].notna()].copy()
    if d.empty:
        return _empty(EVENT_COLS)
    d["_loc"] = _location_series(d)
    d = d[~d["_loc"].isna()].copy()
    if d.empty:
        return _empty(EVENT_COLS)
    # `_site` = ubicacion FISICA (el activo surtidor) para decidir si hubo cambio de
    # lugar; `_loc` se conserva solo para MOSTRAR el tanque/medidor exacto.
    d["_site"] = _site_series(d["_loc"])
    # Producto normalizado: el solapamiento solo es robo si es el MISMO producto en
    # dos lugares (dos productos distintos a la vez = servicio multi-producto, legitimo).
    d["_prod"] = (d["product"].astype("string").str.strip().str.upper()
                  if "product" in d.columns
                  else pd.Series(pd.NA, index=d.index, dtype="string"))

    dur = (pd.to_numeric(d["flow_duration_s"], errors="coerce")
           if "flow_duration_s" in d.columns else pd.Series(pd.NA, index=d.index))
    d["_dur_s"] = dur.fillna(0.0).clip(lower=0.0)
    gps = d["gps_coordinates"] if "gps_coordinates" in d.columns else pd.Series(pd.NA, index=d.index)
    pc = point_coords or {}
    # Coordenadas por despacho: GPS de la transaccion; si falta, el mapa opcional.
    d["_coords"] = [parse_coords(g) or pc.get(loc) for g, loc in zip(gps, d["_loc"])]
    d["_sid"] = d["id"] if "id" in d.columns else pd.Series(pd.NA, index=d.index)
    d["_desc"] = d["equipment_description"] if "equipment_description" in d.columns else pd.NA

    # Orden temporal estable dentro de cada equipo y desplazamiento al previo.
    d = d.sort_values(["_eid", "_t"], kind="mergesort")
    g = d.groupby("_eid", sort=False)
    d["_t_prev"] = g["_t"].shift(1)
    d["_dur_prev"] = g["_dur_s"].shift(1)
    d["_loc_prev"] = g["_loc"].shift(1)
    d["_site_prev"] = g["_site"].shift(1)
    d["_prod_prev"] = g["_prod"].shift(1)
    d["_coords_prev"] = g["_coords"].shift(1)
    d["_sid_prev"] = g["_sid"].shift(1)

    # Candidatos: hay previo y el ACTIVO FISICO cambio. Comparar por `_site` (no por
    # `_loc`) evita el falso positivo de un service truck que entrega varios productos
    # —desde tanques distintos del MISMO camion— al mismo equipo a la vez.
    cand = d[d["_t_prev"].notna() & d["_site_prev"].notna()
             & (d["_site"].astype("string") != d["_site_prev"].astype("string"))]
    if cand.empty:
        return _empty(EVENT_COLS)

    emaps = _equipment_maps(equipment)
    slack_min = max(0.0, config.TAG_HOP_CLOCK_SLACK_MIN)
    rows: list[dict] = []
    for _, r in cand.iterrows():
        gap_s = (r["_t"] - r["_t_prev"]).total_seconds()
        gap_min = gap_s / 60.0
        # Regla 1 — solapamiento: el actual empieza antes de que termine el previo,
        # PERO solo cuenta como robo si es el MISMO producto en ambos puntos. Dos
        # productos distintos solapados = servicio multi-producto (diesel + lubricantes
        # desde un service truck o el taller al mismo equipo), no robo de combustible.
        overlap_min = (r["_dur_prev"] / 60.0) - gap_min   # min que se solapan
        same_product = (not _is_blank(r["_prod"]) and not _is_blank(r["_prod_prev"])
                        and str(r["_prod"]) == str(r["_prod_prev"]))
        is_overlap = (overlap_min > slack_min) and same_product

        # Regla 2 — velocidad implicita (si hay coordenadas en ambos extremos).
        distance_km = None
        speed_kmh = None
        is_speed = False
        ca, cb = r["_coords_prev"], r["_coords"]
        if ca is not None and cb is not None:
            distance_km = haversine_km(ca, cb)
            if distance_km >= config.TAG_HOP_MIN_DISTANCE_KM:
                hours = gap_s / 3600.0
                speed_kmh = float("inf") if hours <= 0 else distance_km / hours
                info = emaps.get(str(r["_eid"]), {})
                limit = (config.TAG_HOP_LIGHT_MAX_SPEED_KMH if info.get("light")
                         else config.TAG_HOP_MAX_SPEED_KMH)
                is_speed = speed_kmh > limit

        if not (is_overlap or is_speed):
            continue
        if is_overlap:
            reason, severity = config.TAG_HOP_REASON_OVERLAP, "CRITICAL"
        else:
            reason = config.TAG_HOP_REASON_SPEED
            # Teletransporte (sin tiempo entre puntos distantes) = CRITICO; una
            # velocidad alta pero finita es WARNING (a investigar).
            severity = "CRITICAL" if gap_min <= slack_min else "WARNING"

        info = emaps.get(str(r["_eid"]), {})
        desc = r["_desc"]
        if _is_blank(desc):
            desc = info.get("description")
        cat = info.get("category")
        rows.append({
            "equipment_id": r.get("equipment_id"),
            "equipment_description": desc,
            "tag": info.get("tag"),
            "category": "(sin dato)" if _is_blank(cat) else cat,
            "date_prev": r["_t_prev"],
            "location_prev": r["_loc_prev"],
            "date": r["_t"],
            "location": r["_loc"],
            "gap_min": round(gap_min, 1),
            "distance_km": None if distance_km is None else round(distance_km, 2),
            "speed_kmh": (None if speed_kmh is None
                          else (None if math.isinf(speed_kmh) else round(speed_kmh, 1))),
            "reason": reason,
            "severity": severity,
            "source_id_prev": r["_sid_prev"],
            "source_id": r["_sid"],
        })
    if not rows:
        return _empty(EVENT_COLS)
    out = pd.DataFrame(rows, columns=EVENT_COLS)
    # Criticos primero, luego los mas recientes.
    out["_crit"] = (out["severity"] == "CRITICAL")
    out = out.sort_values(["_crit", "date"], ascending=[False, False])
    return out.drop(columns="_crit").reset_index(drop=True)


def summary_kpis(events: pd.DataFrame | None) -> dict:
    """KPIs de la franja superior de la ventana."""
    if events is None or events.empty:
        return {
            "Eventos de tag hopping": 0, "Críticos": 0,
            "Equipos involucrados": 0, "Por velocidad GPS": 0,
        }
    crit = int((events["severity"] == "CRITICAL").sum())
    by_speed = int((events["reason"] == config.TAG_HOP_REASON_SPEED).sum())
    return {
        "Eventos de tag hopping": int(len(events)),
        "Críticos": crit,
        "Equipos involucrados": int(events["equipment_id"].nunique()),
        "Por velocidad GPS": by_speed,
    }


@dataclass
class TagHopResult:
    """Resultado de la auditoria de tag hopping en una sola pasada: eventos,
    subconjunto critico y KPIs."""
    events: pd.DataFrame
    critical: pd.DataFrame
    kpis: dict


def audit(movements: pd.DataFrame | None,
          equipment: pd.DataFrame | None = None,
          point_coords: dict[str, tuple[float, float]] | None = None) -> TagHopResult:
    """Calcula TODA la auditoria de tag hopping de una vez (la GUI la usa asi)."""
    events = tag_hops(movements, equipment, point_coords)
    crit = (events[events["severity"] == "CRITICAL"].reset_index(drop=True)
            if not events.empty else events)
    return TagHopResult(events, crit, summary_kpis(events))
