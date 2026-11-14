"""Secuencia (blink) y apilado/resta de frames."""
from __future__ import annotations

import asyncio
import warnings
from datetime import date
from urllib.parse import urlencode

import numpy as np

from .. import spherex as sx
from ..cache import cset, key
from ..config import settings
from ..errors import APIError, UpstreamError
from ..jobs import Job
from ..render import auto_limits


def _g(v: float) -> str:
    return f"{v:.6g}"


def frame_png_url(fid: str, ra: float, dec: float, fov: float, size: int, stretch: str, cmap: str, vmin: float, vmax: float) -> str:
    q = urlencode({
        "ra": f"{ra:.6f}", "dec": f"{dec:.6f}", "fov": _g(fov), "size": size,
        "stretch": stretch, "cmap": cmap, "vmin": _g(vmin), "vmax": _g(vmax),
    })
    return f"/api/frame/{fid}.png?{q}"


def flags_png_url(fid: str, ra: float, dec: float, fov: float, size: int) -> str:
    q = urlencode({"ra": f"{ra:.6f}", "dec": f"{dec:.6f}", "fov": _g(fov), "size": size})
    return f"/api/frame/{fid}/flags.png?{q}"


def _err_msg(e: BaseException) -> str:
    if isinstance(e, UpstreamError):
        return f"{e.source}: {e.message}"
    if isinstance(e, APIError):
        return e.message
    return f"{type(e).__name__}: {e}"[:200]


async def fetch_many(job: Job, metas, ra, dec, fov, size, p0: float, p1: float, label: str):
    """Descarga y reproyecta en paralelo actualizando el progreso. Devuelve (resultados, avisos)."""
    n = len(metas)
    results: list[dict | None] = [None] * n
    warns: list[str] = []
    done = 0

    async def one(i, m):
        nonlocal done
        try:
            results[i] = await sx.get_reprojected(m, ra, dec, fov, size)
        except Exception as e:  # noqa: BLE001  (un frame que falla no tumba la secuencia)
            warns.append(f"{m.id}: {_err_msg(e)}")
        done += 1
        job.update(p0 + (p1 - p0) * done / max(n, 1), f"{label} {done}/{n}")

    job.update(p0, f"{label} 0/{n}")
    await asyncio.gather(*(one(i, m) for i, m in enumerate(metas)))
    return results, warns


async def build_sequence(job: Job, *, ra: float, dec: float, fov: float, size: int, detector: int | None,
                         date_from: date | None, date_to: date | None, stretch: str, cmap: str, scale: str,
                         limit: int, offset: int) -> dict:
    job.update(0.02, "Buscando frames de SPHEREx")
    metas, _, search_warns = await sx.find_frames(ra, dec)
    sel = sx.filter_frames(metas, detector, date_from, date_to)
    total = len(sel)
    page = sel[offset: offset + limit]
    results, warns = await fetch_many(job, page, ra, dec, fov, size, 0.05, 0.92, "Descargando frames")
    warns = search_warns + warns
    ok = [(m, r) for m, r in zip(page, results) if r is not None]
    if page and not ok:
        raise APIError(502, "upstream_error", f"No se pudo descargar ningún frame ({warns[0] if warns else 'sin detalle'}).")

    job.update(0.94, "Calculando escala de brillo")
    gvmin, gvmax = await asyncio.to_thread(auto_limits, [r["arr"] for _, r in ok]) if ok else (0.0, 1.0)
    frames = []
    for m, r in ok:
        vmin, vmax = (gvmin, gvmax) if scale == "sequence" else auto_limits([r["arr"]])
        d = m.api(r["info"])
        d["vmin"], d["vmax"] = vmin, vmax
        d["png_url"] = frame_png_url(m.id, ra, dec, fov, size, stretch, cmap, vmin, vmax)
        d["flags_png_url"] = flags_png_url(m.id, ra, dec, fov, size)
        frames.append(d)
    return {
        "center": {"ra": ra, "dec": dec},
        "fov_arcsec": fov,
        "size_px": size,
        "scale": scale,
        "vmin": gvmin,
        "vmax": gvmax,
        "unit": "MJy/sr",
        "total": total,
        "limit": limit,
        "offset": offset,
        "frames": frames,
        "partial": bool(warns),
        "warnings": warns[:20],
    }


def _combine(arrs: list[np.ndarray], op: str) -> np.ndarray:
    cube = np.stack(arrs)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        if op == "median":
            return np.nanmedian(cube, axis=0).astype(np.float32)
        if op == "sum":
            cnt = np.isfinite(cube).sum(axis=0)
            return np.where(cnt > 0, np.nansum(cube, axis=0), np.nan).astype(np.float32)
        return np.nanmean(cube, axis=0).astype(np.float32)


def stack_image_key(jid: str) -> str:
    return key("stackimg", jid)


async def build_stack(job: Job, *, ra: float, dec: float, fov: float, size: int, stretch: str, cmap: str,
                      op: str, a: list[str], b: list[str]) -> dict:
    job.update(0.02, "Buscando frames")
    metas_a = await sx.lookup_frames(a, ra, dec)
    metas_b = await sx.lookup_frames(b, ra, dec) if op == "diff" else []
    all_metas = metas_a + metas_b
    results, warns = await fetch_many(job, all_metas, ra, dec, fov, size, 0.05, 0.9, "Descargando frames")
    arrs_a = [r["arr"] for r in results[: len(metas_a)] if r is not None]
    arrs_b = [r["arr"] for r in results[len(metas_a):] if r is not None]
    if not arrs_a or (op == "diff" and not arrs_b):
        raise APIError(502, "upstream_error", f"No se pudieron obtener los frames ({warns[0] if warns else 'sin detalle'}).")

    job.update(0.93, "Combinando" if op != "diff" else "Restando épocas")

    def compute():
        if op == "diff":
            img = _combine(arrs_a, "mean") - _combine(arrs_b, "mean")
            finite = np.abs(img[np.isfinite(img)])
            vmax = float(np.percentile(finite, 99.5)) if finite.size else 1.0
            return img, -max(vmax, 1e-6), max(vmax, 1e-6)
        img = _combine(arrs_a, op)
        vmin, vmax = auto_limits([img])
        return img, vmin, vmax

    img, vmin, vmax = await asyncio.to_thread(compute)
    cset(stack_image_key(job.id), {"arr": img, "op": op, "stretch": stretch, "cmap": cmap, "vmin": vmin, "vmax": vmax},
         ttl=settings.cache_ttl_s + 3600)
    return {
        "op": op,
        "n_frames": len(arrs_a) + len(arrs_b),
        "center": {"ra": ra, "dec": dec},
        "fov_arcsec": fov,
        "size_px": size,
        "vmin": vmin,
        "vmax": vmax,
        "unit": "MJy/sr",
        "png_url": f"/api/stack/{job.id}.png",
        "partial": bool(warns),
        "warnings": warns[:20],
    }
