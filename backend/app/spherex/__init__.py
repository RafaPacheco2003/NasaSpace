"""Servicio SPHEREx: búsqueda de frames, registro de ids, recortes, reproyección y fotometría.

El proveedor (IRSA real o mock) se elige con SPHEREX_MODE. Todo lo demás es común.
"""
from __future__ import annotations

import asyncio
import math
import warnings
from datetime import date, timedelta

import numpy as np
from astropy.time import Time
from astropy.wcs.utils import proj_plane_pixel_area

from ..astro import target_wcs
from ..cache import cached, cget, cset, key, r1, singleflight
from ..config import settings
from ..errors import not_found
from .base import (
    NATIVE_PIXEL_ARCSEC,
    Cutout,
    FrameMeta,
    contamination_mask,
    flags_in_mask,
    flags_near,
    compact_fits,
    hard_mask,
    inpaint,
    parse_spherex_fits,
)

REGISTRY_TTL = 30 * 24 * 3600
_dl_sem: asyncio.Semaphore | None = None


def provider():
    if settings.spherex_mode == "mock":
        from . import mock

        return mock
    from . import irsa

    return irsa


def _sem() -> asyncio.Semaphore:
    global _dl_sem
    if _dl_sem is None:
        _dl_sem = asyncio.Semaphore(settings.download_concurrency)
    return _dl_sem


# --- Búsqueda y registro de frames ---------------------------------------


def register(metas: list[FrameMeta]) -> None:
    for m in metas:
        cset(key("frame", m.id), m.as_dict(), REGISTRY_TTL)


def get_registered(fid: str) -> FrameMeta | None:
    d = cget(key("frame", fid))
    return FrameMeta(**d) if d else None


async def find_frames(ra: float, dec: float) -> tuple[list[FrameMeta], bool]:
    """Todas las exposiciones que cubren (ra, dec). Caché 24 h por punto redondeado a 1″."""
    rra, rdec = r1(ra), r1(dec)

    async def fetch():
        metas = await provider().search(rra, rdec)
        return [m.as_dict() for m in metas]

    raw, hit = await cached(key("spherex_search", settings.spherex_mode, settings.spherex_collection, rra, rdec), fetch)
    metas = [FrameMeta(**d) for d in raw]
    register(metas)
    return metas, hit


def filter_frames(metas: list[FrameMeta], detector: int | None, date_from: date | None, date_to: date | None) -> list[FrameMeta]:
    lo = Time(date_from.isoformat(), scale="utc").mjd if date_from else -math.inf
    hi = Time((date_to + timedelta(days=1)).isoformat(), scale="utc").mjd if date_to else math.inf
    out = [m for m in metas if (detector is None or m.detector == detector) and lo <= m.mjd < hi]
    return sorted(out, key=lambda m: (m.mjd, m.detector))


async def lookup_frames(ids: list[str], ra: float, dec: float) -> list[FrameMeta]:
    """Ids → metadatos. Si no están en el registro, se re-busca en (ra, dec)."""
    found = {i: get_registered(i) for i in ids}
    if any(v is None for v in found.values()):
        await find_frames(ra, dec)
        found = {i: v or get_registered(i) for i, v in found.items()}
    missing = [i for i, v in found.items() if v is None]
    if missing:
        raise not_found(f"No encontramos el frame {', '.join(missing[:5])} en esta región.")
    return [found[i] for i in ids]


def frame_info(fid: str, ra: float, dec: float) -> dict | None:
    return cget(key("frameinfo", fid, r1(ra), r1(dec)))


# --- Recortes ---------------------------------------------------------------


async def get_cutout(meta: FrameMeta, ra: float, dec: float, size_deg: float) -> Cutout:
    size_deg = round(size_deg, 5)
    k = key("cutout", settings.spherex_mode, meta.id, r1(ra), r1(dec), size_deg)

    async def fetch():
        async with _sem():
            raw = await provider().download_cutout(meta, ra, dec, size_deg)
        return await asyncio.to_thread(compact_fits, raw)

    content, _ = await cached(k, fetch)
    return await asyncio.to_thread(parse_spherex_fits, content, ra, dec)


def _reproject(cut: Cutout, ra: float, dec: float, fov: float, size: int):
    from reproject import reproject_interp

    tw = target_wcs(ra, dec, fov, size)
    image = cut.image
    if settings.clean_bad_pixels:
        hm = hard_mask(cut.flags, cut.flag_bits)
        image = inpaint(image, hm if hm is not None else np.zeros(image.shape, dtype=bool))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        arr, foot = reproject_interp((image, cut.wcs), tw, shape_out=(size, size), order="bilinear")
        arr = np.where(foot > 0, arr, np.nan).astype(np.float32)
        bad = None
        mask = contamination_mask(cut.flags, cut.flag_bits)
        if mask is not None:
            m, mfoot = reproject_interp((mask.astype(np.float32), cut.wcs), tw, shape_out=(size, size), order="nearest-neighbor")
            bad = (np.nan_to_num(m) > 0.5) & (mfoot > 0)
    return arr, bad


async def get_reprojected(meta: FrameMeta, ra: float, dec: float, fov: float, size: int) -> dict:
    """Recorte alineado a la rejilla común (TAN, norte arriba, este a la izquierda).

    Devuelve {"arr": float32 size×size (MJy/sr, NaN sin datos), "bad": máscara de flags, "info": {...}}.
    """
    ra, dec = r1(ra), r1(dec)
    k = key("reproj", settings.spherex_mode, meta.id, ra, dec, float(fov), int(size), settings.zodi_subtract,
            settings.clean_bad_pixels)
    v = cget(k)
    if v is not None:
        return v

    async def compute():
        size_deg = fov / 3600 * 1.45 + 4 * NATIVE_PIXEL_ARCSEC / 3600
        cut = await get_cutout(meta, ra, dec, size_deg)
        arr, bad = await asyncio.to_thread(_reproject, cut, ra, dec, fov, size)
        info = {
            "band_um": cut.band_um or meta.band_um,
            "band_exact": cut.band_um is not None,
            "flags": flags_near(cut, ra, dec),
        }
        out = {"arr": arr, "bad": bad, "info": info}
        cset(k, out)
        cset(key("frameinfo", meta.id, ra, dec), info)
        return out

    return await singleflight(k, compute)


# --- Fotometría de apertura -----------------------------------------------


def _aperture(cut: Cutout, ra: float, dec: float, aperture: float, r_in: float, r_out: float) -> dict | None:
    img = cut.image
    ny, nx = img.shape
    x0, y0 = (float(v) for v in cut.wcs.world_to_pixel_values(ra, dec))
    if not (0 <= x0 < nx and 0 <= y0 < ny):
        return None
    area_deg2 = float(proj_plane_pixel_area(cut.wcs))
    pixscale = math.sqrt(area_deg2) * 3600
    omega_sr = area_deg2 * (math.pi / 180) ** 2
    yy, xx = np.indices(img.shape)
    r = np.hypot(xx - x0, yy - y0) * pixscale
    ap = r <= max(aperture, 0.75 * pixscale)
    if not ap.any():
        ap = np.zeros_like(ap)
        ap[int(round(y0)), int(round(x0))] = True
    finite = np.isfinite(img)
    bad = contamination_mask(cut.flags, cut.flag_bits)
    ann = (r >= r_in) & (r <= r_out) & finite
    if bad is not None:
        ann &= ~bad
    if ann.sum() >= 5:
        bkg = float(np.median(img[ann]))
        bkg_std = float(1.4826 * np.median(np.abs(img[ann] - bkg)))
        n_ann = int(ann.sum())
    else:
        bkg, bkg_std, n_ann = 0.0, 0.0, 1
    sel = ap & finite
    n_ap = int(sel.sum())
    if n_ap == 0:
        return None
    total = float(np.sum(img[sel] - bkg))
    if cut.variance is not None and np.isfinite(cut.variance[sel]).all():
        var = float(np.sum(cut.variance[sel]))
    else:
        var = n_ap * bkg_std**2
    var += n_ap**2 * bkg_std**2 / max(n_ann, 1)
    k = omega_sr * 1e12  # MJy/sr · sr → MJy → µJy
    return {
        "flux": total * k,
        "error": math.sqrt(max(var, 0.0)) * k,
        "flags": flags_in_mask(cut.flags, ap, cut.flag_bits),
        "band_um": cut.band_um,
    }


async def photometry(meta: FrameMeta, ra: float, dec: float, aperture: float) -> dict | None:
    pix = NATIVE_PIXEL_ARCSEC
    r_in = max(2 * aperture, aperture + 2 * pix)
    r_out = r_in + 3 * pix
    size_deg = (2 * r_out + 4 * pix) / 3600
    cut = await get_cutout(meta, ra, dec, size_deg)
    res = await asyncio.to_thread(_aperture, cut, ra, dec, aperture, r_in, r_out)
    if res is None:
        return None
    band = res["band_um"] or meta.band_um
    return {
        "wavelength": round(band, 4) if band else None,
        "flux": round(res["flux"], 4),
        "error": round(res["error"], 4),
        "date_obs": meta.date_obs,
        "detector": meta.detector,
        "frame_id": meta.id,
        "flags": res["flags"],
        "wavelength_exact": res["band_um"] is not None,
    }
