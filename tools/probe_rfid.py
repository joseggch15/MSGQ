# -*- coding: utf-8 -*-
"""Probe de feasibilidad para el modulo 'Inventario de tags RFID' (solo lectura).

Pregunta central: para reproducir la logica de Inventory_Equipment desde el
endpoint (clasificar cada equipo en NEW / REPLACEMENT / REMOVAL) PERO con la
FECHA REAL del cambio de RFID (no la fecha del inventario), necesitamos una de:

  (A) Un evento de cambio de RFID que TRAIGA el equipo (FK) + la fecha. Hoy
      `changes(recordType:"EquipmentRfid")` solo trae recordId (id del registro
      de tag) y before/after (valores hex), sin FK al equipo. -> confirmar.

  (B) Un tipo/queery que liste los tags RFID con su equipo y fechas de
      asignacion/baja (p. ej. un tipo EquipmentRfid con equipment + timestamps).
      -> buscar por introspeccion.

  (C) Enlace por VALOR: el `after` (tag nuevo) de un evento Asignado/Cambiado se
      cruza con el equipo que HOY tiene ese tag (`rfidTags`). -> validar que los
      valores del log aparecen en los rfidTags del maestro.

Este script NO escribe nada. Usa la misma auth que MSGQ (embedded o env).
Salida ASCII (consola cp1252).

Uso:  python tools/probe_rfid.py
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")

from msgq.api.client import AdaptIQClient
from msgq.config import Settings, load_embedded_settings


def _unwrap(t: dict | None) -> str | None:
    """Desenvuelve NON_NULL/LIST hasta el tipo con nombre, anotando si es lista."""
    wrappers = []
    while t and not t.get("name"):
        k = t.get("kind")
        if k in ("LIST", "NON_NULL"):
            wrappers.append(k)
        t = t.get("ofType")
    name = t.get("name") if t else None
    if "LIST" in wrappers:
        return f"[{name}]"
    return name


def _q_type_fields(name: str) -> str:
    return (
        '{ __type(name:"%s"){ name kind fields { name '
        "type { name kind ofType { name kind ofType { name kind ofType { name kind } } } } } } }" % name
    )


Q_ALL_TYPES = "{ __schema { types { name kind } } }"
Q_QUERY_FIELDS = "{ __schema { queryType { fields { name type { name kind ofType { name kind } } } } } }"
Q_SITE_FIELDS = (
    '{ __type(name:"Site"){ fields { name '
    "type { name kind ofType { name kind ofType { name kind } } } } } }"
)


async def _safe(client: AdaptIQClient, q: str, variables=None):
    try:
        return await client._execute(q, variables or {})
    except Exception as exc:  # noqa: BLE001
        print(f"   [!] query fallo: {type(exc).__name__}: {exc}")
        return None


def _print_type(label: str, data: dict | None):
    tt = (data or {}).get("__type")
    if not tt:
        print(f"\n=== {label}: (tipo no existe / no introspectable) ===")
        return None
    print(f"\n=== {label}  [{tt.get('kind')}] ===")
    for f in (tt.get("fields") or []):
        print(f"    {f['name']:<28} -> {_unwrap(f.get('type'))}")
    return tt


async def main() -> int:
    settings = load_embedded_settings() or Settings.from_env()
    print(f"Endpoint: {settings.endpoint}")
    client = AdaptIQClient(settings)
    try:
        # --- 0. Tipos y campos que mencionan 'rfid' -----------------------
        data = await _safe(client, Q_ALL_TYPES)
        all_types = [t["name"] for t in ((data or {}).get("__schema") or {}).get("types", [])
                     if t.get("name") and not t["name"].startswith("__")]
        rfid_types = sorted(t for t in all_types if "rfid" in t.lower())
        print(f"\n=== Tipos del esquema con 'rfid' ===\n   {rfid_types or '(ninguno)'}")

        data = await _safe(client, Q_QUERY_FIELDS)
        qf = ((data or {}).get("__schema") or {}).get("queryType", {}).get("fields", [])
        qnames = [f["name"] for f in qf]
        print(f"\n=== Query top-level con 'rfid' ===\n   "
              f"{[n for n in qnames if 'rfid' in n.lower()] or '(ninguno)'}")

        data = await _safe(client, Q_SITE_FIELDS)
        sf = ((data or {}).get("__type") or {}).get("fields", [])
        print(f"\n=== Site: campos con 'rfid' ===\n   "
              f"{[ (f['name'], _unwrap(f.get('type'))) for f in sf if 'rfid' in f['name'].lower()] or '(ninguno)'}")

        # --- 1. Forma del ChangeEvent (busca FK al registro) --------------
        _print_type("ChangeEvent", await _safe(client, _q_type_fields("ChangeEvent")))

        # --- 2. EquipmentItem: tipo de rfidTags + cualquier campo rfid ----
        eq_tt = _print_type("EquipmentItem", await _safe(client, _q_type_fields("EquipmentItem")))
        if eq_tt:
            rfid_fields = [f["name"] for f in eq_tt.get("fields", []) if "rfid" in f["name"].lower()]
            print(f"   -> campos RFID en EquipmentItem: {rfid_fields}")

        # --- 3. Cualquier tipo con 'rfid' en el nombre --------------------
        for name in rfid_types:
            _print_type(name, await _safe(client, _q_type_fields(name)))

        # --- 4. Muestra de eventos EquipmentRfid del log ------------------
        print("\n=== Muestra: ultimos eventos de cambio recordType=EquipmentRfid ===")
        changes = await client.fetch_changes("EquipmentRfid", None)
        print(f"   total eventos EquipmentRfid en el historico: {len(changes)}")
        for n in changes[-8:]:
            chs = n.get("changes") or []
            diff = "; ".join(f"{c.get('attribute')}: {c.get('before')!r}->{c.get('after')!r}" for c in chs)
            print(f"    changedAt={n.get('changedAt')} recordId={n.get('recordId')} "
                  f"event={n.get('event')} who={n.get('whodunnit')}")
            print(f"        {diff}")

        # --- 5. Cross-check enlace por VALOR ------------------------------
        # Junta los valores 'after' de los eventos vs los rfidTags del maestro.
        print("\n=== Cross-check: valores del log vs rfidTags del maestro ===")
        log_after = set()
        for n in changes:
            for c in (n.get("changes") or []):
                if c.get("attribute") == "rfid" and c.get("after"):
                    log_after.add(str(c["after"]).strip().upper())
        eq_nodes = await client.fetch_equipment(None)
        # Mapa tag-actual -> equipmentId
        tag_to_eq: dict[str, str] = {}
        multi = 0
        for e in eq_nodes:
            tags = e.get("rfidTags") or []
            if isinstance(tags, str):
                tags = [tags]
            if isinstance(tags, list) and len(tags) > 1:
                multi += 1
            for tg in tags:
                if tg:
                    tag_to_eq[str(tg).strip().upper()] = e.get("equipmentId")
        print(f"   equipos: {len(eq_nodes)} · equipos con >1 tag: {multi}")
        print(f"   tags actuales en maestro: {len(tag_to_eq)}")
        print(f"   valores 'after' distintos en el log: {len(log_after)}")
        hit = len(log_after & set(tag_to_eq))
        print(f"   'after' del log que HOY siguen en un equipo: {hit} "
              f"({(hit/len(log_after)*100 if log_after else 0):.1f}%)")
        # Muestra de 5 equipos con su(s) tag(s)
        print("\n   Muestra de rfidTags del maestro:")
        shown = 0
        for e in eq_nodes:
            tags = e.get("rfidTags")
            if tags:
                print(f"     {e.get('equipmentId')}: {tags!r}")
                shown += 1
            if shown >= 5:
                break

        print("\n" + "=" * 64)
        print("Lee arriba: (a) hay FK equipo en ChangeEvent/EquipmentRfid?  "
              "(b) % de enlace por valor.")
        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
