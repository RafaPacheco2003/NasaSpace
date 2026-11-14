"""Búsquedas de cono en SIMBAD y NED (TAP/ADQL) y clasificación star/galaxy/other."""
from __future__ import annotations

from ..astro import separation_arcsec
from ..config import settings
from ..http import tap_query
from ..utils import fnum, val

ROW_LIMIT = 2000

SIMBAD_GALAXY = {
    "G", "LSB", "bCG", "SBG", "H2G", "EmG", "AGN", "SyG", "Sy1", "Sy2", "rG", "LIN", "QSO", "Bla", "BLL",
    "GiP", "GiG", "GiC", "BiC", "IG", "PaG", "GrG", "CGG", "ClG", "PCG", "SCG", "LeG", "LeQ", "OVV", "AG?",
    "Q?", "G?", "Gr?", "C?G", "SC?", "Bz?", "BL?", "Sy?", "LI?",
}
SIMBAD_NOT_STAR = {"Cl*", "GlC", "OpC", "As*", "St*", "MGr", "C?*", "Gl?", "Cl?"}

NED_GALAXY = {"QSO", "QGroup", "Q_Lens", "AbLS", "EmLS"}
NED_NOT_STAR = {"*Cl", "*Ass"}


def classify_simbad(otype: str) -> str:
    t = (otype or "").strip()
    if t in SIMBAD_GALAXY:
        return "galaxy"
    if "*" in t and t not in SIMBAD_NOT_STAR:
        return "star"
    return "other"


def classify_ned(pretype: str) -> str:
    t = (pretype or "").strip()
    if t.startswith("G") or t in NED_GALAXY:
        return "galaxy"
    if "*" in t and t not in NED_NOT_STAR:
        return "star"
    return "other"


def _circle(ra: float, dec: float, radius_arcsec: float, frame: str = "ICRS") -> str:
    return f"CONTAINS(POINT('{frame}', ra, dec), CIRCLE('{frame}', {ra:.8f}, {dec:.8f}, {radius_arcsec / 3600:.8f})) = 1"


async def simbad_cone(ra: float, dec: float, radius_arcsec: float, only_moving: bool = False) -> list[dict]:
    extra = " AND pmra IS NOT NULL AND pmdec IS NOT NULL" if only_moving else ""
    adql = (
        f"SELECT TOP {ROW_LIMIT} main_id, ra, dec, otype, rvz_redshift, pmra, pmdec, plx_value, "
        f"coo_err_maj, pm_err_maj FROM basic WHERE {_circle(ra, dec, radius_arcsec)}{extra}"
    )
    t = await tap_query("simbad", settings.simbad_tap_url, adql, mirror=settings.simbad_tap_mirror)
    out = []
    for row in t:
        name, ora, odec = val(row, "main_id"), fnum(val(row, "ra")), fnum(val(row, "dec"))
        if not name or ora is None or odec is None:
            continue
        otype = val(row, "otype") or ""
        pmra, pmdec = fnum(val(row, "pmra")), fnum(val(row, "pmdec"))
        out.append({
            "id": f"simbad:{name}",
            "name": name,
            "source": "simbad",
            "ra": ora,
            "dec": odec,
            "type": classify_simbad(otype),
            "type_raw": otype,
            "z": fnum(val(row, "rvz_redshift")),
            "distance_arcsec": round(float(separation_arcsec(ra, dec, ora, odec)), 3),
            "pm": {"ra": pmra, "dec": pmdec} if pmra is not None and pmdec is not None else None,
            "parallax_mas": fnum(val(row, "plx_value")),
            "pos_err_mas": fnum(val(row, "coo_err_maj")),
            "pm_err_mas_yr": fnum(val(row, "pm_err_maj")),
        })
    return out


async def ned_cone(ra: float, dec: float, radius_arcsec: float) -> list[dict]:
    adql = f"SELECT TOP {ROW_LIMIT} prefname, ra, dec, prefphytype, z FROM NEDTAP.objdir WHERE {_circle(ra, dec, radius_arcsec, 'J2000')}"
    t = await tap_query("ned", settings.ned_tap_url, adql)
    out = []
    for row in t:
        name, ora, odec = val(row, "prefname"), fnum(val(row, "ra")), fnum(val(row, "dec"))
        if not name or ora is None or odec is None:
            continue
        ptype = val(row, "prefphytype") or ""
        out.append({
            "id": f"ned:{name}",
            "name": name,
            "source": "ned",
            "ra": ora,
            "dec": odec,
            "type": classify_ned(ptype),
            "type_raw": ptype,
            "z": fnum(val(row, "z")),
            "distance_arcsec": round(float(separation_arcsec(ra, dec, ora, odec)), 3),
            "pm": None,
        })
    return out
