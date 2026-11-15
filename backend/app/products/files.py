"""Descarga íntegra de FITS grandes (L2 completo ≈ 65 MB, tesela de cubo ≈ 320 MB) con caché en disco.

Portado de SphereX-pipeline/downloads.py a async (httpx):
- Varias conexiones con rangos HTTP (bytes=a-b) y comprobación de Content-Range e If-Range; si el servidor no
  respeta los rangos, descarga secuencial.
- Solo se publican archivos completos (se escribe en un temporal y se renombra).
- Caché por URL en CACHE_DIR/products con límite de tamaño (PRODUCT_CACHE_GB): se borran los menos usados.
- Una sola descarga por URL aunque la pidan varios usuarios a la vez (singleflight).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Callable

import httpx

from ..cache import singleflight
from ..config import settings
from ..errors import UpstreamError

log = logging.getLogger("skyapi.products")
CHUNK = 1024 * 1024


def product_dir() -> Path:
    d = Path(settings.cache_dir) / "products"
    d.mkdir(parents=True, exist_ok=True)
    return d


def local_path(url: str) -> Path:
    return product_dir() / (hashlib.sha256(url.encode()).hexdigest() + ".fits")


def cached_file(url: str) -> Path | None:
    p = local_path(url)
    if p.is_file():
        os.utime(p)  # marca de uso para el LRU
        return p
    return None


def _evict(keep: Path) -> None:
    limit = settings.product_cache_gb * 1024**3
    files = sorted((f for f in product_dir().glob("*.fits")), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    for f in files:
        if total <= limit:
            break
        if f == keep:
            continue
        total -= f.stat().st_size
        try:
            f.unlink()
            log.info("Caché de productos llena: borrado %s", f.name)
        except OSError:
            pass


async def _download(url: str, dest: Path, progress: Callable[[int, int], None] | None, source: str) -> None:
    timeout = httpx.Timeout(settings.download_timeout_s, connect=10)
    headers = {"Accept-Encoding": "identity", "User-Agent": settings.user_agent}
    workers = max(1, min(8, settings.product_download_workers))
    done = 0

    def tick(n: int, total: int) -> None:
        nonlocal done
        done += n
        if progress:
            progress(done, total)

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        total, validator = 0, None
        try:
            r = await client.get(url, headers={"Range": "bytes=0-0"})
            m = re.fullmatch(r"bytes 0-0/(\d+)", r.headers.get("Content-Range", ""))
            if r.status_code == 206 and m:
                total = int(m[1])
                validator = r.headers.get("ETag")
                if not validator or validator.startswith("W/"):
                    validator = r.headers.get("Last-Modified")
            elif r.status_code >= 400:
                raise UpstreamError(source, f"HTTP {r.status_code} al descargar {url.rsplit('/', 1)[-1]}")
        except httpx.HTTPError as e:
            raise UpstreamError(source, f"{type(e).__name__} al descargar", timeout=isinstance(e, httpx.TimeoutException)) from e

        if total and validator and workers > 1:
            with open(dest, "wb") as f:
                f.truncate(total)
            count = min(workers, max(1, total // (4 * CHUNK)))

            async def part(i: int) -> None:
                start, end = total * i // count, total * (i + 1) // count - 1
                h = {"Range": f"bytes={start}-{end}", "If-Range": validator}
                async with client.stream("GET", url, headers=h) as resp:
                    if resp.status_code != 206 or resp.headers.get("Content-Range") != f"bytes {start}-{end}/{total}":
                        raise OSError("El servidor no respetó el rango solicitado")
                    pos = start
                    with open(dest, "r+b") as f:
                        f.seek(start)
                        async for chunk in resp.aiter_bytes(CHUNK):
                            pos += len(chunk)
                            if pos > end + 1:
                                raise OSError("Rango HTTP demasiado largo")
                            f.write(chunk)
                            tick(len(chunk), total)
                    if pos != end + 1:
                        raise OSError("Rango HTTP incompleto")

            try:
                await asyncio.gather(*(part(i) for i in range(count)))
                return
            except (httpx.HTTPError, OSError) as e:
                log.warning("Descarga por rangos falló (%s); se intenta secuencial", e)
                done = 0

        try:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise UpstreamError(source, f"HTTP {resp.status_code} al descargar {url.rsplit('/', 1)[-1]}")
                total = int(resp.headers.get("Content-Length") or 0)
                with open(dest, "wb") as f:
                    async for chunk in resp.aiter_bytes(CHUNK):
                        f.write(chunk)
                        tick(len(chunk), total)
            if total and dest.stat().st_size != total:
                raise UpstreamError(source, "Descarga incompleta")
        except httpx.HTTPError as e:
            raise UpstreamError(source, f"{type(e).__name__} al descargar", timeout=isinstance(e, httpx.TimeoutException)) from e


async def fetch_file(url: str, progress: Callable[[int, int], None] | None = None, source: str = "spherex") -> Path:
    """Ruta local del FITS completo (lo descarga si no está en caché)."""
    if not url.startswith(("http://", "https://")):
        return Path(url)
    hit = cached_file(url)
    if hit:
        return hit
    target = local_path(url)

    async def run() -> Path:
        if target.is_file():
            return target
        t0 = time.time()
        with tempfile.TemporaryDirectory(dir=product_dir()) as tmp:
            dest = Path(tmp) / "download.fits"
            await _download(url, dest, progress, source)
            if not dest.stat().st_size:
                raise UpstreamError(source, "Archivo descargado vacío")
            os.replace(dest, target)
        log.info("Descargado %s (%.1f MB en %.1f s)", url.rsplit("/", 1)[-1], target.stat().st_size / 1e6, time.time() - t0)
        _evict(target)
        return target

    return await singleflight("fetch:" + url, run)
