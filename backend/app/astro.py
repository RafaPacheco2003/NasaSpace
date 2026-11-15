# SI-assisted development used for code development, debugging and documentation.
# Tool: ChatGPT, Claude

"""Astronomía: épocas, movimiento propio + paralaje, geometría TAN de las imágenes."""
from __future__ import annotations

from datetime import date, datetime, timezone

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord, get_body_barycentric
from astropy.time import Time
from astropy.wcs import WCS


def to_time(d: date | datetime | None) -> Time:
    """Fecha → astropy Time (UTC). Sin valor = ahora. Una fecha sin hora = 00:00 UTC."""
    if d is None:
        return Time.now()
    if isinstance(d, datetime):
        if d.tzinfo is not None:
            d = d.astimezone(timezone.utc).replace(tzinfo=None)
        return Time(d, scale="utc")
    return Time(datetime(d.year, d.month, d.day), scale="utc")


def jyear(t: Time) -> float:
    return round(float(t.jyear), 3)


def parallax_factors(ra_deg, dec_deg, t: Time):
    """Factores de paralaje (en AU) para Δα·cosδ y Δδ dada la posición baricéntrica de la Tierra."""
    xyz = get_body_barycentric("earth", t).xyz.to_value(u.au)
    x, y, z = xyz[0], xyz[1], xyz[2]
    a = np.radians(ra_deg)
    d = np.radians(dec_deg)
    fa = x * np.sin(a) - y * np.cos(a)
    fd = x * np.cos(a) * np.sin(d) + y * np.sin(a) * np.sin(d) - z * np.cos(d)
    return fa, fd


def propagate(ra, dec, pmra, pmdec, epoch: float, t: Time, plx=None):
    """Propaga posiciones de catálogo (época `epoch`, años julianos) a la fecha `t`.

    pmra ya incluye cosδ (convención Gaia/SIMBAD), en mas/año. plx en mas (opcional):
    se suma el desplazamiento por paralaje anual visto desde la Tierra.
    Admite vectores (muchas fuentes, una fecha) o (una fuente, muchas fechas).
    """
    ra = np.asarray(ra, dtype=float)
    dec = np.asarray(dec, dtype=float)
    pmra = np.nan_to_num(np.asarray(0.0 if pmra is None else pmra, dtype=float))
    pmdec = np.nan_to_num(np.asarray(0.0 if pmdec is None else pmdec, dtype=float))
    dt = np.asarray(t.jyear, dtype=float) - float(epoch)
    dx = pmra * dt
    dy = pmdec * dt
    if plx is not None:
        p = np.nan_to_num(np.asarray(plx, dtype=float))
        if np.any(p):
            fa, fd = parallax_factors(ra, dec, t)
            dx = dx + p * fa
            dy = dy + p * fd
    ra_b, dec_b, dx, dy = np.broadcast_arrays(ra, dec, dx, dy)
    if not np.any(dx) and not np.any(dy):
        return ra_b.astype(float), dec_b.astype(float)
    c = SkyCoord(ra_b * u.deg, dec_b * u.deg, frame="icrs").spherical_offsets_by(dx * u.mas, dy * u.mas)
    return np.asarray(c.ra.deg), np.asarray(c.dec.deg)


def target_wcs(ra0: float, dec0: float, fov_arcsec: float, size: int) -> WCS:
    """WCS de salida: TAN, norte arriba, este a la izquierda, centrado en (ra0, dec0).

    En la convención del contrato (x desde el borde izquierdo, y desde el borde superior):
        escala = size / fov
        x = size/2 − (ra − ra0)·cos(dec0)·3600·escala
        y = size/2 − (dec − dec0)·3600·escala
    """
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [ra0, dec0]
    w.wcs.crpix = [size / 2 + 0.5, size / 2 + 0.5]
    s = fov_arcsec / size / 3600.0
    w.wcs.cdelt = [-s, s]
    w.wcs.cunit = ["deg", "deg"]
    w.wcs.radesys = "ICRS"
    return w


def gnomonic(ra, dec, ra0: float, dec0: float):
    """Proyección tangente: (ξ, η) en segundos de arco (ξ positivo hacia el este)."""
    ra = np.radians(np.asarray(ra, dtype=float))
    dec = np.radians(np.asarray(dec, dtype=float))
    a0, d0 = np.radians(ra0), np.radians(dec0)
    cosc = np.sin(d0) * np.sin(dec) + np.cos(d0) * np.cos(dec) * np.cos(ra - a0)
    xi = np.cos(dec) * np.sin(ra - a0) / cosc
    eta = (np.cos(d0) * np.sin(dec) - np.sin(d0) * np.cos(dec) * np.cos(ra - a0)) / cosc
    k = np.degrees(1.0) * 3600.0
    return xi * k, eta * k


def in_square(ra, dec, ra0: float, dec0: float, fov_arcsec: float):
    xi, eta = gnomonic(ra, dec, ra0, dec0)
    half = fov_arcsec / 2.0
    return (np.abs(xi) <= half) & (np.abs(eta) <= half)


def separation_arcsec(ra1, dec1, ra2, dec2):
    """Distancia angular (haversine), vectorizada, en ″."""
    r1, d1, r2, d2 = map(lambda v: np.radians(np.asarray(v, dtype=float)), (ra1, dec1, ra2, dec2))
    h = np.sin((d2 - d1) / 2) ** 2 + np.cos(d1) * np.cos(d2) * np.sin((r2 - r1) / 2) ** 2
    return np.degrees(2 * np.arcsin(np.sqrt(np.clip(h, 0, 1)))) * 3600.0
