# -*- coding: utf-8 -*-
"""Probe de feasibilidad para el modulo 'Despachos sobre Safe Fill Level' (solo lectura).

Pregunta central: ¿el endpoint expone el Safe Fill Level (SFL) por equipo y
producto (la tabla 'Products consumed' de la pantalla del equipo: Diesel 1893 L,
etc.)? La introspeccion de EquipmentItem mostro `consumptionTanks -> [ConsumptionTank]`,
candidato obvio. Aqui se introspecciona ConsumptionTank y se trae una muestra real
para confirmar los nombres de campo (safeFillLevel?, product, dispenseLimit?) y que
los valores coinciden con la UI (p. ej. HTK0819 Diesel = 1893 L).

No escribe nada. Usa la misma auth que MSGQ. Salida ASCII.

Uso:  python tools/probe_sfl.py
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")

from msgq.api.client import AdaptIQClient
from msgq.config import Settings, load_embedded_settings


def _unwrap(t: dict | None):
    """(nombre_base, kind_base, es_lista) desenvolviendo NON_NULL/LIST."""
    is_list = False
    while t and not t.get("name"):
        if t.get("kind") == "LIST":
            is_list = True
        t = t.get("ofType")
    return (t.get("name") if t else None, t.get("kind") if t else None, is_list)


def _q_type(name: str) -> str:
    return ('{ __type(name:"%s"){ name kind fields { name '
            "type { name kind ofType { name kind ofType { name kind ofType { name kind } } } } } } }" % name)


async def main() -> int:
    s = load_embedded_settings() or Settings.from_env()
    print(f"Endpoint: {s.endpoint}")
    c = AdaptIQClient(s)
    try:
        # 1) Introspeccion de ConsumptionTank
        data = await c._execute(_q_type("ConsumptionTank"), {})
        tt = (data or {}).get("__type")
        if not tt:
            print("[!] No existe el tipo ConsumptionTank. Revisa EquipmentItem.consumptionTanks.")
            return 2
        print(f"\n=== ConsumptionTank [{tt.get('kind')}] ===")
        scalar_fields, product_field = [], None
        for f in tt.get("fields", []):
            base, kind, is_list = _unwrap(f.get("type"))
            tag = "[]" if is_list else ""
            print(f"    {f['name']:<22} -> {base}{tag} ({kind})")
            if kind in ("SCALAR", "ENUM"):
                scalar_fields.append(f["name"])
            elif "product" in f["name"].lower():
                product_field = f["name"]

        # 2) Muestra real: equipmentItems con sus consumptionTanks
        sel = list(scalar_fields)
        if product_field:
            sel.append(f"{product_field} {{ code description }}")
        selection = " ".join(sel)
        site_id = s.site_id or "1"
        q = ("""
        query($siteId: ID!, $first: Int) {
          site(id: $siteId) {
            equipmentItems(first: $first) {
              edges { node { equipmentId description consumptionTanks { %s } } }
            }
          }
        }""" % selection)
        data = await c._execute(q, {"siteId": site_id, "first": 8})
        edges = (((data or {}).get("site") or {}).get("equipmentItems") or {}).get("edges", [])
        print(f"\n=== Muestra de consumptionTanks (primeros {len(edges)} equipos) ===")
        with_sfl = 0
        for e in edges:
            n = e.get("node") or {}
            tanks = n.get("consumptionTanks") or []
            if tanks:
                with_sfl += 1
            print(f"  {n.get('equipmentId')} ({n.get('description')}): {len(tanks)} productos")
            for ct in tanks:
                print(f"      {ct}")

        # 3) Buscar HTK0819 puntualmente (Diesel debe dar 1893 L)
        print("\n=== Busqueda dirigida: HTK0819 (Diesel esperado 1893 L) ===")
        cursor = None
        found = False
        for _ in range(40):
            qv = {"siteId": site_id, "first": 100}
            if cursor:
                qv["after"] = cursor
            qq = ("""
            query($siteId: ID!, $first: Int, $after: String) {
              site(id: $siteId) { equipmentItems(first:$first, after:$after) {
                pageInfo { hasNextPage endCursor }
                edges { node { equipmentId consumptionTanks { %s } } }
              } }
            }""" % selection)
            d = await c._execute(qq, qv)
            conn = (((d or {}).get("site") or {}).get("equipmentItems") or {})
            for e in conn.get("edges", []):
                n = e.get("node") or {}
                if n.get("equipmentId") == "HTK0819":
                    found = True
                    print(f"  HTK0819 -> {n.get('consumptionTanks')}")
            pi = conn.get("pageInfo") or {}
            if found or not pi.get("hasNextPage"):
                break
            cursor = pi.get("endCursor")
        if not found:
            print("  (no se encontro HTK0819 en el barrido)")

        print("\n" + "=" * 60)
        print(f"Equipos (de la muestra) con consumptionTanks: {with_sfl}/{len(edges)}")
        print("Confirma arriba el nombre del campo SFL y que el valor cuadra con la UI.")
        return 0
    finally:
        await c.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
