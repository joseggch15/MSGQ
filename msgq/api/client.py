"""Cliente GraphQL asincrono contra la API de AdaptIQ (AdaptFMS).

Modelo de la API (site-scoped): se entra por `site(id:)` y de ahi cuelgan las
conexiones `dispenses` / `deliveries` / `transfers` (movimientos), la lista
`adaptMacs`, y —si el tenant la expone— una conexion de equipos descubierta por
introspeccion.

Responsabilidades:
  • Resolver el `site id` (configurado o auto-descubierto via `sites`).
  • Ejecutar queries via POST con `Authorization: Token token=<token>`.
  • Paginar por cursor (`pageInfo.hasNextPage` / `endCursor`, limite 100).
  • Filtrar incrementalmente con `filter: { updatedFrom: ISO8601 }`.
  • Traducir fallos de red/HTTP/GraphQL a excepciones de dominio claras.

Se usa desde el hilo de polling, que mantiene un event loop asyncio vivo.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from msgq.api import queries
from msgq.config import Settings


# ===========================================================================
# Excepciones de dominio
# ===========================================================================

class APIError(Exception):
    """Error generico de la capa de API."""


class AuthError(APIError):
    """Token ausente, invalido o expirado (HTTP 401/403)."""


class TransportError(APIError):
    """Fallo de red / conexion / timeout al hablar con el endpoint."""


class GraphQLError(APIError):
    """El servidor respondio 200 pero con errores en el cuerpo GraphQL."""


# Sentinela para 'ya se introspecciono y no hay conexion de equipos'.
_NO_EQUIPMENT = "\x00none"


class AdaptIQClient:
    """Cliente GraphQL paginado para la API de AdaptIQ (contrato `DataSource`)."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client: httpx.AsyncClient | None = None
        self._site_id: str | None = settings.site_id or None
        self._equipment_field: str | None = None   # None=sin descubrir; _NO_EQUIPMENT=no hay

    # -- ciclo de vida ------------------------------------------------------

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=self._settings.auth_header(),
                timeout=self._settings.request_timeout,
                verify=self._settings.verify_tls,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- ejecucion de bajo nivel -------------------------------------------

    async def _execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        client = self._ensure_client()
        payload = {"query": query, "variables": variables}
        try:
            resp = await client.post(self._settings.endpoint, json=payload)
        except httpx.TimeoutException as exc:
            raise TransportError(f"Timeout al consultar {self._settings.endpoint}") from exc
        except httpx.HTTPError as exc:
            raise TransportError(f"Fallo de conexion: {exc}") from exc

        if resp.status_code in (401, 403):
            raise AuthError(
                "Autenticacion rechazada (HTTP %d). Verifica el token en "
                "'Authorization: Token token=<token>'." % resp.status_code)
        if resp.status_code >= 400:
            raise TransportError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            body = resp.json()
        except ValueError as exc:
            raise GraphQLError(f"Respuesta no es JSON valido: {resp.text[:300]}") from exc

        if body.get("errors"):
            messages = "; ".join(str(e.get("message", e)) for e in body["errors"])
            raise GraphQLError(messages)
        return body.get("data") or {}

    async def _paginate_site_connection(
        self, query: str, connection: str, variables: dict[str, Any],
    ) -> list[dict]:
        """Recorre `site.<connection>` por cursor y acumula los `node`."""
        nodes: list[dict] = []
        cursor: str | None = None
        for _ in range(10_000):  # cota de seguridad
            page_vars = dict(variables)
            if cursor:
                page_vars["after"] = cursor
            data = await self._execute(query, page_vars)
            conn = ((data.get("site") or {}).get(connection)) or {}
            for edge in conn.get("edges", []):
                node = edge.get("node")
                if node is not None:
                    nodes.append(node)
            page_info = conn.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break
        return nodes

    async def _paginate_root_connection(
        self, query: str, root_key: str, variables: dict[str, Any],
    ) -> list[dict]:
        """Pagina una conexion top-level (p. ej. `changes`) por cursor."""
        nodes: list[dict] = []
        cursor: str | None = None
        for _ in range(10_000):
            page_vars = dict(variables)
            if cursor:
                page_vars["after"] = cursor
            data = await self._execute(query, page_vars)
            conn = data.get(root_key) or {}
            for edge in conn.get("edges", []):
                node = edge.get("node")
                if node is not None:
                    nodes.append(node)
            page_info = conn.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break
        return nodes

    # -- resolucion de sitio -----------------------------------------------

    async def _resolve_site_id(self) -> str:
        if self._site_id:
            return self._site_id
        data = await self._execute(queries.SITES_QUERY, {})
        sites = data.get("sites") or []
        if not sites:
            raise APIError("La API no devolvio sitios; revisa permisos del token.")
        match = (self._settings.site_match or "").lower()
        chosen = None
        if match:
            for s in sites:
                blob = f"{s.get('code', '')} {s.get('description', '')}".lower()
                if match in blob:
                    chosen = s
                    break
        chosen = chosen or sites[0]
        self._site_id = str(chosen.get("id"))
        return self._site_id

    # -- contrato DataSource -----------------------------------------------

    async def fetch_movements(self, updated_from: datetime | None) -> list[dict]:
        site_id = await self._resolve_site_id()
        filt = {"updatedFrom": _iso(updated_from)} if updated_from else {}
        out: list[dict] = []
        for connection, (query, kind) in queries.MOVEMENT_CONNECTIONS.items():
            nodes = await self._paginate_site_connection(
                query, connection,
                {"siteId": site_id, "filter": filt, "first": self._settings.page_size},
            )
            for n in nodes:
                n["kind"] = kind
            out.extend(nodes)
        return out

    async def fetch_movements_paged(self, updated_from: datetime | None, on_page) -> None:
        """Pagina las 3 conexiones de movimientos llamando `on_page(nodes)` por
        pagina (ingesta progresiva). Se usa para el backfill historico del primer
        arranque sin acumular todo en memoria. Cada node se etiqueta con `kind`."""
        site_id = await self._resolve_site_id()
        filt = {"updatedFrom": _iso(updated_from)} if updated_from else {}
        for connection, (query, kind) in queries.MOVEMENT_CONNECTIONS.items():
            cursor: str | None = None
            for _ in range(1_000_000):  # cota de seguridad
                page_vars: dict[str, Any] = {
                    "siteId": site_id, "filter": filt, "first": self._settings.page_size}
                if cursor:
                    page_vars["after"] = cursor
                data = await self._execute(query, page_vars)
                conn = ((data.get("site") or {}).get(connection)) or {}
                nodes = [e["node"] for e in conn.get("edges", []) if e.get("node")]
                for n in nodes:
                    n["kind"] = kind
                if nodes:
                    on_page(nodes)
                page_info = conn.get("pageInfo") or {}
                if not page_info.get("hasNextPage"):
                    break
                cursor = page_info.get("endCursor")
                if not cursor:
                    break

    async def fetch_equipment(self, updated_from: datetime | None) -> list[dict]:
        site_id = await self._resolve_site_id()
        field = await self._discover_equipment_field()
        if field == _NO_EQUIPMENT:
            return []
        query = queries.build_equipment_query(field)
        return await self._paginate_site_connection(
            query, field, {"siteId": site_id, "first": self._settings.page_size})

    async def fetch_adaptmacs(self, updated_from: datetime | None) -> list[dict]:
        site_id = await self._resolve_site_id()
        return await self._paginate_site_connection(
            queries.ADAPTMACS_QUERY, "adaptMacs",
            {"siteId": site_id, "first": self._settings.page_size})

    async def fetch_tanks(self, updated_from: datetime | None) -> list[dict]:
        site_id = await self._resolve_site_id()
        return await self._paginate_site_connection(
            queries.TANKS_QUERY, "tanks",
            {"siteId": site_id, "first": self._settings.page_size})

    async def fetch_reconciliations(self, updated_from: datetime | None) -> list[dict]:
        site_id = await self._resolve_site_id()
        filt = {"updatedFrom": _iso(updated_from)} if updated_from else {}
        return await self._paginate_site_connection(
            queries.RECONCILIATIONS_QUERY, "reconciliations",
            {"siteId": site_id, "filter": filt, "first": self._settings.page_size})

    async def fetch_changes(self, record_type: str,
                            changes_from: datetime | None) -> list[dict]:
        out: list[dict] = []
        await self.fetch_changes_paged(record_type, changes_from, out.extend)
        return out

    async def fetch_changes_paged(self, record_type: str,
                                  changes_from: datetime | None, on_page) -> None:
        """Pagina `changes` llamando `on_page(nodes)` por pagina (ingesta progresiva)."""
        site_id = await self._resolve_site_id()
        query_filter: dict[str, Any] = {"siteId": site_id, "recordType": record_type}
        if changes_from is not None:
            query_filter["changesFrom"] = _iso(changes_from)
        cursor: str | None = None
        for _ in range(100_000):
            page_vars: dict[str, Any] = {"filter": query_filter, "first": self._settings.page_size}
            if cursor:
                page_vars["after"] = cursor
            data = await self._execute(queries.CHANGES_QUERY, page_vars)
            conn = data.get("changes") or {}
            nodes = [e["node"] for e in conn.get("edges", []) if e.get("node")]
            if nodes:
                on_page(nodes)
            page_info = conn.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break

    async def _discover_equipment_field(self) -> str:
        """Introspecciona el tipo Site para hallar la conexion de equipos.

        Devuelve el nombre del campo, o `_NO_EQUIPMENT` si el tenant no expone
        ninguno de los candidatos (en cuyo caso los equipos no se pueden listar
        por GraphQL en este esquema).
        """
        if self._equipment_field is not None:
            return self._equipment_field
        data = await self._execute(queries.SITE_FIELDS_INTROSPECTION, {})
        fields = {f["name"] for f in ((data.get("__type") or {}).get("fields") or [])}
        for candidate in queries.EQUIPMENT_FIELD_CANDIDATES:
            if candidate in fields:
                self._equipment_field = candidate
                return candidate
        self._equipment_field = _NO_EQUIPMENT
        return _NO_EQUIPMENT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    """Formatea un datetime a ISO8601 (lo que espera `updatedFrom`)."""
    return dt.isoformat()
