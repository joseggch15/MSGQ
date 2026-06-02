"""Diagnostico de conexion y descubrimiento de esquema GraphQL de AdaptIQ.

Objetivo: reemplazar suposiciones por hechos. Con un token valido, este script
se conecta al endpoint y reporta EXACTAMENTE que ofrece tu tenant:

  • Que el token y el endpoint funcionan.
  • La lista de `sites` con su `id` (necesario para consultar movimientos).
  • Los campos del Root Query y del tipo Site (para ver que conexiones existen:
    dispenses / deliveries / transfers / movements / adaptMacs ... y, sobre todo,
    SI existe alguna conexion para listar equipos — p. ej. `equipmentItems`).
  • Cualquier tipo cuyo nombre contenga 'Equipment'.
  • Los campos reales (camelCase) de Dispense / Movement y los valores del enum
    DispenseTransactionType (para fijar nombres en queries.py sin adivinar).

Uso:
    set MSGQ_ENDPOINT=https://<tu-app>/graphql
    set MSGQ_TOKEN=<token>
    python -m msgq.diagnose

    # o pasando argumentos:
    python -m msgq.diagnose --endpoint https://<app>/graphql --token <token>

El esquema (nombres de campos/tipos) NO es informacion sensible; puedes
compartir la salida para finalizar las queries.
"""
from __future__ import annotations

import argparse
import sys

import httpx

from msgq.config import Settings

# --- Introspeccion focalizada ----------------------------------------------
_Q_SITES = "{ sites { id code description } }"

_Q_ROOT_AND_TYPES = """
{
  __schema {
    queryType { name fields { name } }
    types { name kind }
  }
}
""".strip()

_Q_TYPE_FIELDS = """
query T($name: String!) {
  __type(name: $name) {
    name kind
    fields { name }
    inputFields { name }
    enumValues { name }
  }
}
""".strip()


def _post(client: httpx.Client, endpoint: str, query: str, variables: dict | None = None) -> dict:
    resp = client.post(endpoint, json={"query": query, "variables": variables or {}})
    if resp.status_code in (401, 403):
        raise SystemExit(f"[AUTH] Token rechazado (HTTP {resp.status_code}). "
                         "Revisa 'Authorization: Token token=<token>'.")
    if resp.status_code >= 400:
        raise SystemExit(f"[HTTP {resp.status_code}] {resp.text[:400]}")
    body = resp.json()
    if body.get("errors"):
        msgs = "; ".join(str(e.get("message", e)) for e in body["errors"])
        # No abortamos: algunas queries de introspeccion pueden fallar por tipo inexistente.
        print(f"   (GraphQL errors: {msgs})")
    return body.get("data") or {}


def _print_list(title: str, items, key=None):
    print(f"\n=== {title} ({len(items)}) ===")
    for it in items:
        print("  -", it if key is None else it.get(key))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Diagnostico GraphQL AdaptIQ")
    ap.add_argument("--endpoint")
    ap.add_argument("--token")
    args = ap.parse_args(argv)

    settings = Settings.from_env()
    endpoint = args.endpoint or settings.endpoint
    token = args.token or settings.token
    if not token:
        raise SystemExit("Falta token. Usa --token o la variable MSGQ_TOKEN.")

    settings.token = token
    print(f"Endpoint: {endpoint}")
    with httpx.Client(headers=settings.auth_header(),
                      timeout=settings.request_timeout,
                      verify=settings.verify_tls) as client:

        # 1) Sites — valida el token y entrega los IDs de sitio.
        print("\n--- Probando conexion (sites) ---")
        data = _post(client, endpoint, _Q_SITES)
        sites = data.get("sites") or []
        if sites:
            print("Conexion OK. Sites disponibles:")
            for s in sites:
                print(f"  id={s.get('id')!r:8}  code={s.get('code')!r}  desc={s.get('description')!r}")
        else:
            print("Conexion OK pero no se listaron sites (revisa permisos).")

        # 2) Root query + nombres de todos los tipos.
        data = _post(client, endpoint, _Q_ROOT_AND_TYPES)
        schema = data.get("__schema") or {}
        root_fields = [f["name"] for f in (schema.get("queryType") or {}).get("fields", [])]
        _print_list("Campos del Root Query", root_fields)

        all_types = [t["name"] for t in schema.get("types", []) if t.get("name")]
        equip_types = [n for n in all_types if "equip" in n.lower()]
        _print_list("Tipos que contienen 'Equipment'", equip_types)

        # 3) Campos del tipo Site (¿hay conexion de equipos?).
        for type_name in ("Site", "EquipmentItem", "Equipment", "Dispense",
                          "Movement", "MovementQuery", "DispenseTransactionType"):
            d = _post(client, endpoint, _Q_TYPE_FIELDS, {"name": type_name})
            t = d.get("__type")
            if not t:
                print(f"\n=== Tipo '{type_name}': NO existe en el esquema ===")
                continue
            fields = [f["name"] for f in (t.get("fields") or [])]
            inputs = [f["name"] for f in (t.get("inputFields") or [])]
            enums = [e["name"] for e in (t.get("enumValues") or [])]
            print(f"\n=== Tipo '{t['name']}' (kind={t['kind']}) ===")
            if fields:
                print("  fields:", ", ".join(fields))
            if inputs:
                print("  inputFields:", ", ".join(inputs))
            if enums:
                print("  enumValues:", ", ".join(enums))

    print("\nListo. Comparte esta salida para finalizar queries.py con datos reales.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
