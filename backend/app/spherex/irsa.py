"""Proveedor real: SPHEREx en IRSA.

- Búsqueda de exposiciones que cubren un punto: SIA v2 (https://irsa.ipac.caltech.edu/SIA, COLLECTION=spherex_dr1).
- Recortes: servicio de cutouts de IRSA añadiendo ?center=ra,dec&size=grados a la access_url del FITS;
  devuelve el multi-extensión recortado (IMAGE, FLAGS, VARIANCE, ZODI, PSF, WCS-WAVE).
"""
from __future__ import annotations

import re

from astropy.time import Time

from ..config import settings
from ..http import parse_votable, request
from ..utils import fnum, iso_z, val
from .base import DETECTORS, FrameMeta, detector_from_wavelength

SOURCE = "spherex"
DET_RE = re.compile(r"D([1-6])(?![0-9])")


def _detector(row, band_um: float | None) -> int | None:
    det = detector_from_wavelength(band_um)
    if det:
        return det
    for col in ("energy_bandpassname", "obs_id", "access_url"):
        m = DET_RE.search(str(val(row, col) or ""))
        if m:
            return int(m.group(1))
    return None


async def search(ra: float, dec: float) -> list[FrameMeta]:
    params = [("POS", f"CIRCLE {ra:.7f} {dec:.7f} {1 / 3600:.8f}")]
    for coll in settings.spherex_collection.split(","):
        if coll.strip():
            params.append(("COLLECTION", coll.strip()))
    r = await request(SOURCE, "GET", settings.irsa_sia_url, params=params, timeout=settings.spherex_search_timeout_s)
    table = parse_votable(SOURCE, r.content)
    frames: dict[str, FrameMeta] = {}
    for row in table:
        url = val(row, "access_url")
        if not url:
            continue
        fmt = str(val(row, "access_format") or "").lower()
        if fmt and "fits" not in fmt:
            continue
        ptype = str(val(row, "dataproduct_type") or "image").lower()
        if ptype != "image" or str(val(row, "obs_collection") or "").endswith("_cube"):
            continue
        tmin = fnum(val(row, "t_min"))
        if tmin is None:
            continue
        em_min, em_max = fnum(val(row, "em_min")), fnum(val(row, "em_max"))
        band = (em_min + em_max) / 2 * 1e6 if em_min and em_max else None
        det = _detector(row, band)
        if det is None:
            continue
        if band is None:
            band = sum(DETECTORS[det]) / 2
        t = Time(tmin, format="mjd", scale="utc")
        ms = int(round(t.unix * 1000))
        fid = f"sx-d{det}-{ms}"
        if fid in frames:
            continue
        frames[fid] = FrameMeta(
            id=fid,
            obs_id=str(val(row, "obs_id") or fid),
            date_obs=iso_z(t.to_datetime()),
            mjd=float(tmin),
            detector=det,
            band_um=round(band, 4),
            exposure_s=fnum(val(row, "t_exptime")),
            access_url=str(url),
            center_ra=fnum(val(row, "s_ra")),
            center_dec=fnum(val(row, "s_dec")),
        )
    return sorted(frames.values(), key=lambda f: f.mjd)


async def download_cutout(meta: FrameMeta, ra: float, dec: float, size_deg: float) -> bytes:
    sep = "&" if "?" in meta.access_url else "?"
    url = f"{meta.access_url}{sep}center={ra:.7f},{dec:.7f}&size={size_deg:.6f}&gzip=false"
    r = await request(SOURCE, "GET", url, timeout=settings.download_timeout_s)
    return r.content
