"""Caché en disco (diskcache). Clave = (consulta, fuente), coordenadas redondeadas a 1″, vigencia 24 h.

Se guarda en disco para que sobreviva a reinicios del contenedor (volumen /data/cache).
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Awaitable, Callable

import diskcache

from .config import settings

_cache: diskcache.Cache | None = None
_MISS = object()
_inflight: dict[str, asyncio.Future] = {}


def get_cache() -> diskcache.Cache:
    global _cache
    if _cache is None:
        os.makedirs(settings.cache_dir, exist_ok=True)
        _cache = diskcache.Cache(
            settings.cache_dir,
            size_limit=int(settings.cache_size_limit_gb * 1024**3),
            eviction_policy="least-recently-used",
        )
    return _cache


def r1(x: float) -> float:
    """Redondea grados a 1″ (para claves de caché y centros de imagen)."""
    return round(float(x) * 3600.0) / 3600.0


def key(*parts: Any) -> str:
    return json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))


def cget(k: str, default: Any = None) -> Any:
    return get_cache().get(k, default)


def cset(k: str, value: Any, ttl: int | None = None) -> None:
    get_cache().set(k, value, expire=ttl or settings.cache_ttl_s)


async def cached(k: str, fetch: Callable[[], Awaitable[Any]], ttl: int | None = None) -> tuple[Any, bool]:
    """Devuelve (valor, hit). Si no está, ejecuta fetch() una sola vez aunque haya peticiones simultáneas."""
    v = get_cache().get(k, _MISS)
    if v is not _MISS:
        return v, True
    v = await singleflight(k, fetch)
    cset(k, v, ttl)
    return v, False


async def singleflight(k: str, fn: Callable[[], Awaitable[Any]]) -> Any:
    """Evita trabajo duplicado: si ya hay una ejecución con la misma clave, espera su resultado."""
    while (fut := _inflight.get(k)) is not None:
        try:
            return await asyncio.shield(fut)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise  # nos cancelaron a nosotros
            # Se canceló el que descargaba (trabajo pausado): este toma el relevo.
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    _inflight[k] = fut
    try:
        result = await fn()
        fut.set_result(result)
        return result
    except asyncio.CancelledError:
        fut.cancel()
        raise
    except BaseException as e:
        fut.set_exception(e)
        fut.exception()  # marca la excepción como recuperada
        raise
    finally:
        _inflight.pop(k, None)
