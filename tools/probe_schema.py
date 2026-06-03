# -*- coding: utf-8 -*-
"""Probe de introspección del esquema GraphQL de AdaptIQ (solo lectura).

Objetivo: determinar si el endpoint de Veridapt expone datos de TANQUES y de
NIVELES de tanque (el equivalente programático al reporte "Historical Tank
Volumes"), que es el insumo que hoy el FMS Tank Analyzer carga por archivo.

No escribe nada: solo lanza queries `__schema` / `__type` (introspección) usando
la misma autenticación que MSGQ (credenciales embebidas o variables de entorno).

Uso:
    python tools/probe_schema.py

Si el endpoint tiene la introspección deshabilitada, lo reporta claramente.
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")

from msgq.api.client import AdaptIQClient
from msgq.config import Settings, load_embedded_settings

# Palabras clave que delatan tanques / niveles / stock.
KEYWORDS = (
    "tank", "storage", "level", "volume", "stock", "reconcil",
    "historical", "inventory", "reading", "gauge", "silo", "vessel", "capacity",
)

Q_QUERY_FIELDS = "{ __schema { queryType { name fields { name } } } }"
Q_ALL_TYPES = "{ __schema { types { name kind } } }"
Q_SITE_FIELDS = (
    '{ __type(name:"Site"){ fields { name '
    "type { name kind ofType { name kind ofType { name kind } } } } } }"
)


def _q_type_fields(name: str) -> str:
    return (
        '{ __type(name:"%s"){ name kind fields { name '
        "type { name kind ofType { name kind ofType { name kind } } } } } }" % name
    )


def _unwrap(t: dict | None) -> str | None:
    """Desenvuelve NON_NULL/LIST hasta el tipo con nombre."""
    while t and not t.get("name"):
        t = t.get("ofType")
    return t.get("name") if t else None


def _matches(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in KEYWORDS)


async def main() -> int:
    settings = load_embedded_settings() or Settings.from_env()
    print(f"Endpoint: {settings.endpoint}")
    client = AdaptIQClient(settings)
    found_types: set[str] = set()
    try:
        # --- 1. ¿La introspección está habilitada? ---
        try:
            data = await client._execute(Q_QUERY_FIELDS, {})
        except Exception as exc:  # noqa: BLE001
            print("\n[!] No se pudo introspeccionar el esquema:")
            print(f"   {type(exc).__name__}: {exc}")
            print("   (Puede ser introspección deshabilitada, red, o token).")
            return 2

        qfields = [f["name"] for f in (((data.get("__schema") or {}).get("queryType") or {}).get("fields") or [])]
        print(f"\n=== Campos top-level de Query ({len(qfields)}) ===")
        print("  ", ", ".join(sorted(qfields)))
        q_hits = sorted(f for f in qfields if _matches(f))
        print("  -> coincidencias tanque/nivel:", q_hits or "(ninguna)")
        found_types.update(q_hits)

        # --- 2. Campos del tipo Site (donde cuelgan dispenses/deliveries/...) ---
        data = await client._execute(Q_SITE_FIELDS, {})
        sfields = ((data.get("__type") or {}).get("fields")) or []
        named = [(f["name"], _unwrap(f.get("type"))) for f in sfields]
        print(f"\n=== Campos de Site ({len(named)}) ===")
        print("  ", ", ".join(sorted(n for n, _ in named)))
        s_hits = [(n, t) for n, t in named if _matches(n)]
        print("  -> coincidencias tanque/nivel:", s_hits or "(ninguna)")
        for _, t in s_hits:
            if t:
                found_types.add(t)

        # --- 3. Todos los tipos del esquema que suenen a tanque/nivel ---
        data = await client._execute(Q_ALL_TYPES, {})
        all_types = [t["name"] for t in ((data.get("__schema") or {}).get("types") or [])
                     if t.get("name") and not t["name"].startswith("__")]
        t_hits = sorted(t for t in all_types if _matches(t))
        print(f"\n=== Tipos del esquema que coinciden ({len(t_hits)}) ===")
        print("  ", t_hits or "(ninguno)")
        found_types.update(t_hits)

        # --- 4. Detallar los tipos prometedores (y el node de las *Connection) ---
        to_inspect: list[str] = []
        for name in found_types:
            if name and name not in to_inspect:
                to_inspect.append(name)
                if name.endswith("Connection"):
                    base = name[: -len("Connection")]
                    if base not in to_inspect:
                        to_inspect.append(base)

        print("\n=== Detalle de tipos candidatos ===")
        if not to_inspect:
            print("  (no se hallaron tipos candidatos)")
        for name in to_inspect:
            try:
                d = await client._execute(_q_type_fields(name), {})
            except Exception as exc:  # noqa: BLE001
                print(f"\n  - {name}: (no introspectable: {exc})")
                continue
            tt = d.get("__type")
            if not tt or not tt.get("fields"):
                continue
            print(f"\n  - {name} ({tt.get('kind')}):")
            for f in tt["fields"]:
                mark = "  >>" if _matches(f["name"]) else "    "
                print(f"    {mark}{f['name']} -> {_unwrap(f.get('type'))}")

        # --- Veredicto heurístico ---
        print("\n" + "=" * 60)
        has_tank_entity = any("tank" in t.lower() or "storage" in t.lower() for t in found_types)
        has_level_signal = any(
            k in (t or "").lower() for t in found_types for k in ("level", "volume", "reading", "stock", "historical")
        )
        if has_tank_entity and has_level_signal:
            print("VEREDICTO: el esquema parece exponer tanques Y niveles/stock.")
            print("-> Paridad total en tiempo real es viable. Revisa el detalle arriba")
            print("  para los nombres exactos de campos de nivel/tiempo.")
        elif has_tank_entity:
            print("VEREDICTO: hay entidad de tanque, pero NO se ve una serie de NIVEL clara.")
            print("-> Revisa el detalle: el nivel podría estar como sub-campo del Tank,")
            print("  o solo en la capa de reportes (no en GraphQL).")
        else:
            print("VEREDICTO: no se detectaron tanques/niveles por introspección.")
            print("-> Probable que los niveles vivan solo en reportes; la mitad de")
            print("  transacciones sí es 100% viable desde el endpoint.")
        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
