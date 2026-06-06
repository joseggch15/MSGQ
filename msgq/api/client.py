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

import asyncio
import random
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from msgq.api import queries
from msgq.config import Settings

# Tope (s) para no colgar un ciclo si un `Retry-After` viniera con un valor
# absurdamente grande: obedecemos al servidor, pero con sentido comun.
_RETRY_AFTER_CEILING = 300.0


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
        self._dispense_fields: set[str] | None = None   # campos opcionales de hardware descubiertos
        # Control de ritmo (cortesia con el endpoint): instante monotonico a partir
        # del cual se permite la proxima peticion. El lock se crea perezosamente
        # (necesita un event loop corriendo) y serializa el espaciado.
        self._throttle_lock: asyncio.Lock | None = None
        self._next_request_at: float = 0.0

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
        attempt = 0
        while True:
            await self._throttle()   # espacia las peticiones (anti fuerza-bruta)
            try:
                resp = await client.post(self._settings.endpoint, json=payload)
            except httpx.TimeoutException as exc:
                if attempt < self._settings.max_retries:
                    attempt += 1
                    await self._sleep_backoff(attempt)
                    continue
                raise TransportError(
                    f"Timeout al consultar {self._settings.endpoint}") from exc
            except httpx.HTTPError as exc:
                if attempt < self._settings.max_retries:
                    attempt += 1
                    await self._sleep_backoff(attempt)
                    continue
                raise TransportError(f"Fallo de conexion: {exc}") from exc

            # 429 (Too Many Requests) / 503: el servidor pide EXPRESAMENTE que
            # bajemos el ritmo. Respetamos `Retry-After` y reintentamos en vez de
            # propagar el error; es la respuesta correcta y la que evita que un
            # WAF/IDS escale el bloqueo creyendo que es un ataque sostenido.
            if resp.status_code in (429, 503) and attempt < self._settings.max_retries:
                attempt += 1
                await self._sleep_retry_after(resp, attempt)
                continue

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

    # -- cortesia con el endpoint (throttle + backoff) ---------------------

    async def _throttle(self) -> None:
        """Espacia las peticiones: garantiza al menos `request_min_interval`
        segundos (mas un jitter aleatorio) entre el INICIO de dos llamadas
        consecutivas. Asi el backfill historico (miles de paginas) y la
        paginacion en general se reparten en el tiempo y no parecen un escaneo
        ni un ataque de fuerza bruta al endpoint de Veridapt AdaptIQ."""
        interval = max(0.0, self._settings.request_min_interval)
        jitter_max = max(0.0, self._settings.request_jitter)
        if interval <= 0.0 and jitter_max <= 0.0:
            return
        if self._throttle_lock is None:
            self._throttle_lock = asyncio.Lock()
        async with self._throttle_lock:
            now = time.monotonic()
            wait = self._next_request_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_request_at = now + interval + random.uniform(0.0, jitter_max)

    async def _sleep_backoff(self, attempt: int) -> None:
        """Espera exponencial con jitter antes de reintentar un fallo transitorio
        (timeout / conexion), para no martillar un endpoint que ya esta en apuros.
        `attempt` es 1-based: 1ra reintento espera ~base, luego 2x, 4x, ... (con techo)."""
        delay = self._backoff_delay(attempt)
        if delay > 0:
            await asyncio.sleep(delay)

    async def _sleep_retry_after(self, resp: httpx.Response, attempt: int) -> None:
        """Obedece la cabecera `Retry-After` de un 429/503 si viene (en segundos o
        como fecha HTTP); si no, aplica backoff exponencial. Acota con un techo de
        seguridad para no dejar el ciclo colgado indefinidamente."""
        delay = _parse_retry_after(resp.headers.get("Retry-After"))
        if delay is None:
            delay = self._backoff_delay(attempt)
        delay = max(0.0, min(delay, _RETRY_AFTER_CEILING))
        if delay > 0:
            await asyncio.sleep(delay)

    def _backoff_delay(self, attempt: int) -> float:
        """Retardo de backoff exponencial (base * 2^(attempt-1), con techo) mas un
        jitter de hasta `base` para desincronizar reintentos."""
        base = max(0.0, self._settings.retry_backoff)
        capped = min(base * (2 ** max(0, attempt - 1)), self._settings.retry_backoff_max)
        return capped + random.uniform(0.0, base)

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
            query = await self._movement_query(connection, query)
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
            query = await self._movement_query(connection, query)
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

    async def _movement_query(self, connection: str, default_query: str) -> str:
        """Para la conexion `dispenses`, incluye los campos OPCIONALES de hardware
        (medidor, caudal promedio, SMU crudo/calculado) que el endpoint exponga,
        descubiertos por introspeccion. Para deliveries/transfers, la query fija."""
        if connection == "dispenses":
            return queries.build_dispenses_query(await self._discover_dispense_fields())
        return default_query

    async def _discover_dispense_fields(self) -> set[str]:
        """Introspecciona el tipo del nodo de despacho y devuelve que campos
        opcionales (de `OPTIONAL_DISPENSE_FIELDS`) expone realmente. Si la
        introspeccion no esta disponible o el tipo no existe, devuelve un set
        vacio (la query queda sin esos campos: sincronizacion intacta)."""
        if self._dispense_fields is not None:
            return self._dispense_fields
        available: set[str] = set()
        wanted = set(queries.OPTIONAL_DISPENSE_FIELDS.keys())
        for type_name in queries.DISPENSE_TYPE_CANDIDATES:
            try:
                data = await self._execute(queries.dispense_type_introspection(type_name), {})
            except APIError:
                continue
            type_info = data.get("__type")
            if not type_info:
                continue
            names = {f["name"] for f in (type_info.get("fields") or [])}
            available = names & wanted
            break   # tipo hallado: su interseccion es la respuesta (aunque sea vacia)
        self._dispense_fields = available
        return available

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


def _parse_retry_after(value: str | None) -> float | None:
    """Convierte una cabecera `Retry-After` a segundos. Acepta los dos formatos
    del estandar HTTP: un entero de segundos (`Retry-After: 30`) o una fecha HTTP
    (`Retry-After: Wed, 21 Oct 2025 07:28:00 GMT`). Devuelve None si esta ausente
    o no se puede interpretar (el llamador cae a backoff exponencial)."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    return max(0.0, (dt - now).total_seconds())
