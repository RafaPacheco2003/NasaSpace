"""Fuentes de Gaia DR3 y WISE (AllWISE / CatWISE2020) para dibujar sobre la imagen."""
from __future__ import annotations

import math

from ..config import settings
from ..errors import UpstreamError
from ..http import parse_votable, request, tap_query
from ..utils import fnum, val

GAIA_EPOCH = 2016.0
WISE = {
    # tabla IRSA, columna id, columna magnitud, época de las posiciones, factor pm → mas/año
    "allwise": {"table": "allwise_p3as_psd", "id": "designation", "mag": "w1mpro", "epoch": 2010.5589, "pm_factor": 1.0},
    "catwise": {"table": "catwise_2020", "id": "source_name", "mag": "w1mpro", "epoch": 2015.4, "pm_factor": 1000.0},
}


def _radius_deg(fov_arcsec: float) -> float:
    return fov_arcsec / 2 * math.sqrt(2) / 3600 * 1.02


async def gaia_sources(ra: float, dec: float, fov_arcsec: float) -> dict:
    adql = (
        f"SELECT TOP {settings.overlay_max_sources} source_id, ra, dec, pmra, pmdec, parallax, phot_g_mean_mag "
        f"FROM gaiadr3.gaia_source WHERE 1 = CONTAINS(POINT('ICRS', ra, dec), "
        f"CIRCLE('ICRS', {ra:.8f}, {dec:.8f}, {_radius_deg(fov_arcsec):.8f})) ORDER BY phot_g_mean_mag ASC"
    )
    t = await tap_query("gaia", settings.gaia_tap_url, adql)
    rows = []
    for row in t:
        sra, sdec = fnum(val(row, "ra")), fnum(val(row, "dec"))
        if sra is None or sdec is None:
            continue
        rows.append({
            "id": f"Gaia DR3 {val(row, 'source_id')}",
            "ra": sra,
            "dec": sdec,
            "mag": fnum(val(row, "phot_g_mean_mag")),
            "pmra": fnum(val(row, "pmra")),
            "pmdec": fnum(val(row, "pmdec")),
            "plx": fnum(val(row, "parallax")),
        })
    return {"epoch": GAIA_EPOCH, "rows": rows, "mag_band": "G"}


async def _wise_gator(ra: float, dec: float, fov_arcsec: float, cfg: dict):
    params = {
        "catalog": cfg["table"], "spatial": "cone", "objstr": f"{ra:.7f} {dec:+.7f}",
        "radius": f"{_radius_deg(fov_arcsec) * 3600:.2f}", "radunits": "arcsec", "outfmt": "3",
        "selcols": f"{cfg['id']},ra,dec,{cfg['mag']},pmra,pmdec",
    }
    r = await request("wise", "GET", settings.irsa_gator_url, params=params, retries=0)
    return parse_votable("wise", r.content)


async def wise_sources(ra: float, dec: float, fov_arcsec: float) -> dict:
    cfg = WISE.get(settings.wise_catalog, WISE["allwise"])
    adql = (
        f"SELECT TOP {settings.overlay_max_sources} {cfg['id']}, ra, dec, {cfg['mag']}, pmra, pmdec "
        f"FROM {cfg['table']} WHERE CONTAINS(POINT('ICRS', ra, dec), "
        f"CIRCLE('ICRS', {ra:.8f}, {dec:.8f}, {_radius_deg(fov_arcsec):.8f})) = 1 ORDER BY {cfg['mag']} ASC"
    )
    try:
        # Intento único por TAP; el reintento va a Gator (otro servicio de IRSA, mismo catálogo)
        t = await tap_query("wise", settings.irsa_tap_url, adql, retries=0)
    except UpstreamError:
        t = await _wise_gator(ra, dec, fov_arcsec, cfg)
    rows = []
    for row in t:
        sra, sdec = fnum(val(row, "ra")), fnum(val(row, "dec"))
        if sra is None or sdec is None:
            continue
        pmra, pmdec = fnum(val(row, "pmra")), fnum(val(row, "pmdec"))
        rows.append({
            "id": f"{'AllWISE' if settings.wise_catalog != 'catwise' else 'CatWISE'} {val(row, cfg['id'])}",
            "ra": sra,
            "dec": sdec,
            "mag": fnum(val(row, cfg["mag"])),
            "pmra": pmra * cfg["pm_factor"] if pmra is not None else None,
            "pmdec": pmdec * cfg["pm_factor"] if pmdec is not None else None,
            "plx": None,
        })
    rows.sort(key=lambda r: r["mag"] if r["mag"] is not None else 99)
    return {"epoch": cfg["epoch"], "rows": rows[: settings.overlay_max_sources], "mag_band": "W1"}
