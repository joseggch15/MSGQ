# -*- coding: utf-8 -*-
"""Pruebas de cortesia con el endpoint (anti fuerza-bruta / DDoS).

Verifican que `AdaptIQClient` se comporta como un buen ciudadano de la API de
Veridapt AdaptIQ:

  • espacia sus peticiones al menos `request_min_interval` segundos (throttle),
  • obedece un HTTP 429 con `Retry-After` y reintenta en vez de fallar,
  • reintenta timeouts transitorios con backoff,
  • interpreta bien la cabecera `Retry-After` (segundos y fecha HTTP).

No tocan la red: inyectan un `httpx.MockTransport` en el cliente.

Ejecutar:   pytest tests/test_throttle.py -v
o:          python tests/test_throttle.py
"""
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx

# Permite `import msgq` al correr el archivo directamente.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msgq import config
from msgq.api.client import AdaptIQClient, AuthError, TransportError, _parse_retry_after


def _run(coro):
    return asyncio.run(coro)


def _client(handler, **overrides) -> AdaptIQClient:
    """Construye un cliente con el sitio ya resuelto y un transporte simulado.
    Los `overrides` ganan sobre los defaults de prueba (rapidos)."""
    params = dict(
        demo_mode=False, token="t", endpoint="http://test/graphql", site_id="1",
        request_min_interval=0.05, request_jitter=0.0,
        max_retries=3, retry_backoff=0.01, retry_backoff_max=0.05,
    )
    params.update(overrides)
    client = AdaptIQClient(config.Settings(**params))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


# ---------------------------------------------------------------------------
# Throttle: espaciado minimo entre peticiones
# ---------------------------------------------------------------------------

def test_throttle_spaces_consecutive_requests():
    """N llamadas a `_execute` tardan al menos (N-1)*intervalo: el cliente NO
    dispara las peticiones en rafaga."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"data": {"ok": True}})

    client = _client(handler, request_min_interval=0.05, request_jitter=0.0)

    async def drive():
        t0 = time.monotonic()
        for _ in range(4):
            await client._execute("{ ok }", {})
        elapsed = time.monotonic() - t0
        await client.aclose()
        return elapsed

    elapsed = _run(drive())
    assert calls["n"] == 4
    # 4 peticiones -> 3 esperas de 0.05s. Margen amplio para el scheduler.
    assert elapsed >= 0.13, f"throttle insuficiente: {elapsed:.3f}s"


def test_throttle_disabled_when_zero():
    """Con intervalo y jitter en 0, no se introduce espera (rapido)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {}})

    client = _client(handler, request_min_interval=0.0, request_jitter=0.0)

    async def drive():
        t0 = time.monotonic()
        for _ in range(5):
            await client._execute("{ ok }", {})
        elapsed = time.monotonic() - t0
        await client.aclose()
        return elapsed

    assert _run(drive()) < 0.1


# ---------------------------------------------------------------------------
# 429 / Retry-After: respetar el "baja el ritmo" del servidor
# ---------------------------------------------------------------------------

def test_retries_on_429_then_succeeds():
    """Un 429 con Retry-After se reintenta (no se propaga) y la 2da respuesta OK
    se devuelve normalmente."""
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")
        return httpx.Response(200, json={"data": {"value": 42}})

    client = _client(handler)

    async def drive():
        data = await client._execute("{ value }", {})
        await client.aclose()
        return data

    data = _run(drive())
    assert state["n"] == 2          # hubo exactamente un reintento
    assert data == {"value": 42}


def test_429_exhausts_retries_raises_transport():
    """Si el 429 persiste mas alla de max_retries, se propaga como TransportError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "0"}, text="nope")

    client = _client(handler, max_retries=2)

    async def drive():
        try:
            await client._execute("{ x }", {})
            return None
        except TransportError as exc:
            return exc
        finally:
            await client.aclose()

    assert isinstance(_run(drive()), TransportError)


def test_retries_on_timeout_then_succeeds():
    """Un timeout transitorio se reintenta con backoff y luego tiene exito."""
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.TimeoutException("boom", request=request)
        return httpx.Response(200, json={"data": {"ok": 1}})

    client = _client(handler)

    async def drive():
        data = await client._execute("{ ok }", {})
        await client.aclose()
        return data

    assert _run(drive()) == {"ok": 1}
    assert state["n"] == 2


def test_auth_error_not_retried():
    """Un 401 es definitivo (token malo): se lanza AuthError sin reintentar."""
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(401, text="bad token")

    client = _client(handler)

    async def drive():
        try:
            await client._execute("{ x }", {})
            return None
        except AuthError as exc:
            return exc
        finally:
            await client.aclose()

    assert isinstance(_run(drive()), AuthError)
    assert state["n"] == 1          # no se reintenta un fallo de autenticacion


# ---------------------------------------------------------------------------
# Parseo de la cabecera Retry-After
# ---------------------------------------------------------------------------

def test_parse_retry_after_seconds():
    assert _parse_retry_after("30") == 30.0
    assert _parse_retry_after("0") == 0.0
    assert _parse_retry_after("  12  ") == 12.0


def test_parse_retry_after_http_date():
    future = datetime.now(timezone.utc) + timedelta(seconds=120)
    delay = _parse_retry_after(format_datetime(future, usegmt=True))
    assert delay is not None
    assert 100 <= delay <= 121      # ~120s, con holgura por el tiempo de ejecucion


def test_parse_retry_after_invalid():
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after("not-a-date") is None


if __name__ == "__main__":
    tests = [
        test_throttle_spaces_consecutive_requests,
        test_throttle_disabled_when_zero,
        test_retries_on_429_then_succeeds,
        test_429_exhausts_retries_raises_transport,
        test_retries_on_timeout_then_succeeds,
        test_auth_error_not_retried,
        test_parse_retry_after_seconds,
        test_parse_retry_after_http_date,
        test_parse_retry_after_invalid,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK     {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"FALLO  {t.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} pruebas de throttle superadas.")
    raise SystemExit(1 if failed else 0)
