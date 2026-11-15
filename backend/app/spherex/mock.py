# SI-assisted development used for code development, debugging and documentation.
# Tool: ChatGPT, Claude

"""Proveedor sintético (SPHEREX_MODE=mock): genera FITS con la misma estructura que SPHEREx.

Sirve para desarrollar el Frontend y correr tests sin red. El cielo es determinista:
estrellas fijas, una fuente con movimiento propio alto (tipo enana marrón, más brillante
en el infrarrojo largo) y una estrella variable por cada celda de 0.1°.
"""
from __future__ import annotations

import asyncio
import io
import math

import numpy as np
from astropy.io import fits
from astropy.time import Time
from astropy.wcs import WCS

from ..utils import iso_z
from .base import DEFAULT_FLAG_BITS, DETECTORS, NATIVE_PIXEL_ARCSEC, FrameMeta

SOURCE = "spherex"
SURVEY_START = Time("2025-05-01T00:00:00", scale="utc")
CELL = 0.1  # grados


async def search(ra: float, dec: float) -> list[FrameMeta]:
    now = Time.now()
    n_epochs = max(4, int((now.mjd - SURVEY_START.mjd) // 182.6) + 1)
    rng = np.random.default_rng(int(abs(ra * 1000)) * 7919 + int(abs(dec * 1000)))
    out = []
    for e in range(n_epochs):
        for det in DETECTORS:
            lo, hi = DETECTORS[det]
            for k in range(3):
                mjd = SURVEY_START.mjd + e * 182.6 + k * 1.37 + det * 0.013 + float(rng.uniform(0, 0.2))
                if mjd > now.mjd:
                    continue
                t = Time(mjd, format="mjd", scale="utc")
                fid = f"sx-d{det}-{int(round(t.unix * 1000))}"
                band = lo + (hi - lo) * (k + 0.5) / 3 + float(rng.uniform(-0.02, 0.02))
                out.append(FrameMeta(
                    id=fid, obs_id=f"mock_{e:02d}_{k}_D{det}", date_obs=iso_z(t.to_datetime()), mjd=mjd,
                    detector=det, band_um=round(band, 4), exposure_s=112.0, access_url=f"mock://{fid}",
                    center_ra=ra, center_dec=dec,
                ))
    return sorted(out, key=lambda f: f.mjd)


def _cell_sources(i: int, j: int):
    rng = np.random.default_rng((i * 100003 + j) % (2**32))
    n = 25
    ra = (i + rng.uniform(0, 1, n)) * CELL
    dec = (j + rng.uniform(0, 1, n)) * CELL
    flux = rng.lognormal(0.0, 1.0, n) * 0.6
    kind = np.zeros(n, dtype=int)
    kind[0] = 1  # enana marrón: pm alto
    kind[1] = 2  # variable
    pm = np.zeros((n, 2))
    pm[0] = rng.uniform(-8000, 8000, 2)  # mas/año
    return ra, dec, flux, kind, pm


def _sky_sources(ra0: float, dec0: float, radius: float):
    cosd = max(math.cos(math.radians(dec0)), 0.05)
    i0, i1 = int(math.floor((ra0 - radius / cosd) / CELL)), int(math.floor((ra0 + radius / cosd) / CELL))
    j0, j1 = int(math.floor((dec0 - radius) / CELL)), int(math.floor((dec0 + radius) / CELL))
    parts = [_cell_sources(i, j) for i in range(i0, min(i1, i0 + 40) + 1) for j in range(j0, min(j1, j0 + 40) + 1)]
    return [np.concatenate(p) for p in zip(*parts)]


def _make_fits(meta: FrameMeta, ra0: float, dec0: float, size_deg: float) -> bytes:
    pix = NATIVE_PIXEL_ARCSEC / 3600
    n = max(16, int(math.ceil(size_deg / pix)) + 2)
    rot = math.radians((meta.mjd * 7.3) % 360)
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [ra0, dec0]
    w.wcs.crpix = [n / 2 + 0.3, n / 2 - 0.2]
    w.wcs.cd = np.array([[-pix * math.cos(rot), pix * math.sin(rot)], [pix * math.sin(rot), pix * math.cos(rot)]])
    rng = np.random.default_rng(int(meta.mjd * 1000) % (2**32))

    sra, sdec, flux, kind, pm = _sky_sources(ra0, dec0, size_deg)
    years = (meta.mjd - 51544.5) / 365.25  # desde J2000
    cosd = np.cos(np.radians(sdec))
    sra = sra + pm[:, 0] * (years - 25) / 3.6e6 / cosd
    sdec = sdec + pm[:, 1] * (years - 25) / 3.6e6
    lam = meta.band_um or 2.0
    f = flux.copy()
    f[kind == 1] *= 0.3 * (lam / 1.0) ** 2.2  # enana marrón: roja
    f[kind == 2] *= 1.0 + 0.6 * np.sin(2 * math.pi * meta.mjd / 300.0)  # variable (P≈300 d)
    x, y = w.world_to_pixel_values(sra, sdec)

    yy, xx = np.indices((n, n), dtype=np.float32)
    image = np.zeros((n, n), dtype=np.float32)
    sig = 0.85
    for xi, yi, fi in zip(x, y, f):
        if -5 < xi < n + 5 and -5 < yi < n + 5:
            image += fi * np.exp(-((xx - xi) ** 2 + (yy - yi) ** 2) / (2 * sig**2)).astype(np.float32)
    zodi_level = 0.25 + 0.1 * math.sin(meta.mjd / 58.0) + 0.05 * lam
    image += rng.normal(0, 0.02, (n, n)).astype(np.float32)
    flags = np.zeros((n, n), dtype=np.int32)
    hits = rng.random((n, n)) < 0.002
    image[hits] += 3.0
    flags[hits] |= 1 << DEFAULT_FLAG_BITS["transient"]
    flags[image > 8] |= 1 << DEFAULT_FLAG_BITS["overflow"]

    hdr = w.to_header()
    hdr["BUNIT"] = "MJy/sr"
    hdr["DATE-OBS"] = meta.date_obs
    hdr["DETECTOR"] = meta.detector
    hdul = fits.HDUList([
        fits.PrimaryHDU(),
        fits.ImageHDU(image + zodi_level, header=hdr, name="IMAGE"),
        fits.ImageHDU(flags, header=hdr.copy(), name="FLAGS"),
        fits.ImageHDU(np.full((n, n), 0.02**2, dtype=np.float32), header=hdr.copy(), name="VARIANCE"),
        fits.ImageHDU(np.full((n, n), zodi_level, dtype=np.float32), header=hdr.copy(), name="ZODI"),
    ])
    # Definición de bits en la cabecera FLAGS (MSKNnnnn = nombre), como en SPHEREx DR1
    for name, bit in DEFAULT_FLAG_BITS.items():
        hdul["FLAGS"].header[f"MSKN{bit:04d}"] = name.upper()
    buf = io.BytesIO()
    hdul.writeto(buf)
    return buf.getvalue()


async def download_cutout(meta: FrameMeta, ra: float, dec: float, size_deg: float) -> bytes:
    await asyncio.sleep(0.02)  # simula latencia de red
    return await asyncio.to_thread(_make_fits, meta, ra, dec, size_deg)
