"""Cliente HTTP compartido con timeout por fuente (10 s), 1 reintento y registro de salud."""
from __future__ import annotations

import asyncio
import io
import logging
import time
import warnings

import httpx
from astropy.io.votable import parse as vo_parse
from astropy.table import Table

from .config import settings
from .errors import UpstreamError

log = logging.getLogger("skyapi.http")

_client: httpx.AsyncClient | None = None


class SourceHealth:
    def __init__(self) -> None:
        self.data: dict[str, dict] = {}

    def record(self, source: str, ok: bool, latency_ms: float | None = None, error: str | None = None) -> None:
        d = self.data.setdefault(source, {"ok": True, "latency_ms": None, "last_error": None, "checked_at": 0.0})
        d["ok"] = ok
        d["checked_at"] = time.time()
        if latency_ms is not None:
            d["latency_ms"] = int(latency_ms)
        if error:
            d["last_error"] = error


health = SourceHealth()


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent},
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def request(
    source: str,
    method: str,
    url: str,
    *,
    params=None,
    data=None,
    timeout: float | None = None,
    retries: int | None = None,
    raise_4xx: bool = True,
) -> httpx.Response:
    """Petición con tiempo máximo total `timeout` por intento y `retries` reintentos.

    5xx, 429, timeouts y errores de red se reintentan. 4xx no (error del cliente).
    """
    timeout = float(timeout or settings.external_timeout_s)
    retries = settings.external_retries if retries is None else retries
    last: UpstreamError | None = None
    for attempt in range(retries + 1):
        t0 = time.perf_counter()
        try:
            r = await asyncio.wait_for(
                client().request(method, url, params=params, data=data, timeout=timeout),
                timeout=timeout,
            )
            latency = (time.perf_counter() - t0) * 1000
            if r.status_code >= 500 or r.status_code == 429:
                last = UpstreamError(source, f"HTTP {r.status_code}")
            elif r.status_code >= 400 and raise_4xx:
                ctype = r.headers.get("content-type", "")
                detail = "" if "html" in ctype else f": {r.text[:200]}"
                err = UpstreamError(source, f"HTTP {r.status_code}{detail}")
                health.record(source, False, latency, err.message)
                raise err
            else:
                health.record(source, True, latency)
                return r
        except (asyncio.TimeoutError, httpx.TimeoutException):
            last = UpstreamError(source, f"sin respuesta en {timeout:g} s", timeout=True)
        except httpx.HTTPError as e:
            last = UpstreamError(source, f"{type(e).__name__}: {e}"[:300])
        if attempt < retries:
            log.info("Reintentando %s (%s)", source, last.message if last else "")
            await asyncio.sleep(0.3)
    assert last is not None
    health.record(source, False, error=last.message)
    raise last


def parse_votable(source: str, content: bytes) -> Table:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            vot = vo_parse(io.BytesIO(content), verify="ignore")
        except Exception as e:  # noqa: BLE001
            raise UpstreamError(source, f"respuesta no válida ({type(e).__name__})") from e
        infos = list(getattr(vot, "infos", []))
        for res in vot.resources:
            infos.extend(res.infos)
        for info in infos:
            if info.name == "QUERY_STATUS" and str(info.value).upper() == "ERROR":
                msg = (info.content or "").strip()[:300]
                raise UpstreamError(source, f"error en la consulta: {msg}")
        try:
            table = vot.get_first_table().to_table(use_names_over_ids=True)
        except Exception:  # noqa: BLE001
            return Table()
    lower = [c.lower() for c in table.colnames]
    if len(set(lower)) == len(lower):
        table.rename_columns(table.colnames, lower)
    return table


async def tap_query(source: str, url: str, adql: str, timeout: float | None = None, mirror: str | None = None,
                    retries: int | None = None) -> Table:
    """Consulta TAP síncrona. Con `mirror`, el reintento se hace contra el espejo (p. ej. SIMBAD Harvard)."""
    data = {"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "votable", "QUERY": adql}
    urls = [url, mirror] if mirror else [url]
    last: UpstreamError | None = None
    for i, u in enumerate(urls):
        try:
            r = await request(source, "POST", u, data=data, timeout=timeout,
                              retries=0 if (mirror and i == 0) else retries, raise_4xx=False)
        except UpstreamError as e:
            last = e
            continue
        if r.status_code < 400:
            return parse_votable(source, r.content)
        # Los servicios TAP devuelven el error como VOTable con QUERY_STATUS=ERROR
        try:
            parse_votable(source, r.content)
            last = UpstreamError(source, f"HTTP {r.status_code}")
        except UpstreamError as e:
            last = e if "consulta" in e.message else UpstreamError(source, f"HTTP {r.status_code}")
        health.record(source, False, error=last.message)
    assert last is not None
    raise last
