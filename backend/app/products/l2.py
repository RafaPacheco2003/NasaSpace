"""Imagen L2 COMPLETA de SPHEREx (producto publicado por IRSA, colección spherex_dr1) — basado en SphereX-pipeline.

- Selección: exposiciones que cubren la coordenada (consulta de 1″), ordenadas por fecha; dentro de las fechas
  pedidas se toma la TERCERA (o la última si hay menos de tres), como el pipeline. O una concreta con `frame_id`.
- "raw": IMAGE normalizada con percentiles 3.5–96.5, en gris invertido (`gray_r`, como el pipeline).
- "color": la misma normalización en pseudocolor `cividis` (no es color visible); píxeles inválidos en negro.
- Clic: (x, y) del PNG → RA/Dec (WCS de la imagen), brillo (MJy/sr), luz zodiacal, error (√VARIANCE),
  longitud de onda de ESE píxel (WCS-WAVE) y banderas.
No se recalibra nada: se usa el L2 publicado.
"""
from __future__ import annotations

import asyncio
import math
import warnings
from datetime import date
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning

from .. import spherex as sx
from ..cache import cget, cset, key
from ..errors import APIError, not_found
from ..jobs import Job
from ..spherex.base import friendly_flag, parse_flag_bits
from . import common as C
from .files import cached_file, fetch_file

SELECTION_RULE = "tercera exposición en orden cronológico dentro del rango (o la última si hay menos de tres), como el pipeline"


def _open(path: Path):
    warnings.simplefilter("ignore", FITSFixedWarning)
    return fits.open(path, memmap=True, lazy_load_hdus=True)


def stats(path: Path) -> dict:
    """Forma, unidad, normalización y WCS del L2 (se calcula una vez y se guarda)."""
    k = key("l2stats", path.name, path.stat().st_size)
    v = cget(k)
    if v is not None:
        return v
    with warnings.catch_warnings(), _open(path) as h:
        warnings.simplefilter("ignore")
        img = np.asarray(h["IMAGE"].data, dtype=np.float32)
        if img.ndim != 2:
            raise APIError(502, "upstream_error", f"IMAGE L2 debe ser 2D, recibido {img.shape}")
        hdr = h["IMAGE"].header
        _, valid, low, high = C.normalize(img)
        wcs = WCS(hdr).celestial
        v = {
            "shape": [int(img.shape[0]), int(img.shape[1])],
            "unit": hdr.get("BUNIT", "MJy/sr"),
            "low": low, "high": high,
            "valid_fraction": round(float(valid.mean()), 4),
            "orientation": C.orientation(wcs, img.shape),
            "extensions": [x.name for x in h],
        }
    cset(k, v)
    return v


def render(path: Path, kind: str, size: int | None, invert: bool = True) -> bytes:
    s = stats(path)
    k = key("l2png", path.name, kind, size, invert)
    v = cget(k)
    if v is not None:
        return v
    with warnings.catch_warnings(), _open(path) as h:
        warnings.simplefilter("ignore")
        img = np.asarray(h["IMAGE"].data, dtype=np.float32)
    norm, valid, _, _ = C.normalize(img, s["low"], s["high"])
    if kind == "raw":
        png = C.to_png(norm, valid, "gray_r" if invert else "gray", size, invalid="cmap")
    else:
        png = C.to_png(norm, valid, "cividis", size, invalid="black")
    cset(k, png)
    return png


def pixel_info(path: Path, col: float, row: float) -> dict:
    """Valores del L2 en un píxel (col, fila continuos del FITS)."""
    s = stats(path)
    native = tuple(s["shape"])
    ci, ri, inside = C.nearest(col, row, native)
    with warnings.catch_warnings(), _open(path) as h:
        warnings.simplefilter("ignore")
        hdr = h["IMAGE"].header
        wcs = WCS(hdr).celestial
        ra, dec = (float(v) for v in wcs.pixel_to_world_values(col, row))
        out = {"ra": ra % 360, "dec": dec, "inside": inside, "fits_pixel": {"col": round(col, 3), "row": round(row, 3),
                                                                           "i": ci, "j": ri}}
        if not inside:
            return out
        names = [x.name for x in h]
        value = C.fnum(h["IMAGE"].section[ri, ci])
        zodi = C.fnum(h["ZODI"].section[ri, ci]) if "ZODI" in names else None
        var = C.fnum(h["VARIANCE"].section[ri, ci]) if "VARIANCE" in names else None
        flags_raw, flags = None, []
        if "FLAGS" in names:
            flags_raw = int(h["FLAGS"].section[ri, ci])
            bits = parse_flag_bits(h["FLAGS"].header)
            flags = sorted(friendly_flag(n) for n, b in bits.items() if (flags_raw >> b) & 1)
        wave = None
        try:
            w = WCS(hdr, fobj=h, key="W").pixel_to_world_values(ci, ri)
            wave = float(np.ravel(w[0] if isinstance(w, (tuple, list)) else w)[0])
            if wave < 1e-3:
                wave *= 1e6
        except Exception:  # noqa: BLE001
            wave = None
    out.update({
        "brightness": {
            "value": value, "unit": s["unit"], "zodi": zodi,
            "value_minus_zodi": (value - zodi) if value is not None and zodi is not None else None,
            "error": math.sqrt(var) if var is not None and var >= 0 else None,
        },
        "wavelength_um": round(wave, 5) if wave and math.isfinite(wave) else None,
        "flags": flags, "flags_raw": flags_raw,
        "valid": value is not None,
    })
    return out


# --- Selección y preparación (trabajo 202) ----------------------------------------

def select(metas, frame_id: str | None, detector: int | None, date_from: date | None, date_to: date | None):
    cands = sx.filter_frames(metas, detector, date_from, date_to)
    if frame_id:
        m = next((m for m in metas if m.id == frame_id), None) or sx.get_registered(frame_id)
        if m is None:
            raise not_found(f"El frame {frame_id} no cubre esta coordenada o no existe.")
        return m, cands, None
    if not cands:
        raise not_found("No hay exposiciones L2 de SPHEREx que cubran la coordenada en esas fechas.")
    idx = min(2, len(cands) - 1)
    return cands[idx], cands, idx


def urls(fid: str, size: int | None) -> dict:
    q = f"?size={size}" if size else ""
    return {
        "raw_png_url": f"/api/l2/{fid}/raw.png{q}",
        "color_png_url": f"/api/l2/{fid}/color.png{q}",
        "pixel_url": f"/api/l2/{fid}/pixel?x={{x}}&y={{y}}" + (f"&size={size}" if size else ""),
        "fits_url": f"/api/l2/{fid}/fits",
    }


async def local(meta) -> Path:
    return cached_file(meta.access_url) or await fetch_file(meta.access_url)


async def build_l2(job: Job, *, ra: float, dec: float, frame_id: str | None, detector: int | None,
                   date_from: date | None, date_to: date | None, size: int | None) -> dict:
    job.update(0.03, "Buscando exposiciones L2 (IRSA SIA, spherex_dr1)")
    metas, _, warns = await sx.find_frames(ra, dec)
    meta, cands, idx = select(metas, frame_id, detector, date_from, date_to)

    def prog(done: int, total: int) -> None:
        if total:
            job.update(0.08 + 0.8 * done / total, f"Descargando L2 completo {done / 1e6:.0f}/{total / 1e6:.0f} MB")

    job.update(0.08, "Descargando L2 completo")
    path = cached_file(meta.access_url) or await fetch_file(meta.access_url, prog)
    job.update(0.9, "Normalizando (percentiles 3.5–96.5)")
    s = await asyncio.to_thread(stats, path)
    with warnings.catch_warnings(), _open(path) as h:
        warnings.simplefilter("ignore")
        wcs = WCS(h["IMAGE"].header).celestial
        col, row = (float(v) for v in wcs.world_to_pixel_values(ra, dec))
    target = await asyncio.to_thread(pixel_info, path, col, row)
    native = tuple(s["shape"])
    tx, ty = C.fits_to_png(col, row, native, size)
    w, hgt = C.png_size(native, size)
    return {
        "frame": {**meta.api(), "access_url": meta.access_url, "mjd": meta.mjd},
        "selection": {"rule": SELECTION_RULE if frame_id is None else "frame_id pedido",
                      "index": idx, "candidates_total": len(cands)},
        "candidates": [{"id": m.id, "date_obs": m.date_obs, "detector": m.detector, "band_um": m.band_um}
                       for m in cands[:300]],
        "native_size": {"width": native[1], "height": native[0]},
        "png_size": {"width": w, "height": hgt},
        "unit": s["unit"],
        "normalization": {"low": s["low"], "high": s["high"], "percentiles": list(C.PCT)},
        "orientation": s["orientation"],
        "target": {"ra": ra, "dec": dec, "x": round(tx, 3), "y": round(ty, 3),
                   **{k: target.get(k) for k in ("inside", "brightness", "wavelength_um", "flags")}},
        **urls(meta.id, size),
        "colormaps": {"raw": "gray_r (gris invertido, como el pipeline)", "color": "cividis (pseudocolor, no RGB visible)"},
        "partial": bool(warns),
        "warnings": warns,
    }
