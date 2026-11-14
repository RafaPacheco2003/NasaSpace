"""Resolución de nombres: SIMBAD (vía Sesame de CDS) y, si no está, NED."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

from ..config import settings
from ..errors import UpstreamError, not_found, upstream_failure
from ..http import request
from ..utils import fnum

# Alias para los objetivos de calibración del reto (nombres cortos que SIMBAD no siempre reconoce).
ALIASES = {
    "wise 0855": "WISE J085510.83-071442.5",
    "wise0855": "WISE J085510.83-071442.5",
    "wise 0855-0714": "WISE J085510.83-071442.5",
    "epsilon indi b": "eps Ind B",
    "epsilon indi ba": "eps Ind Ba",
    "epsilon indi bb": "eps Ind Bb",
    "eps indi b": "eps Ind B",
    "barnard star": "Barnard's star",
    "barnards star": "Barnard's star",
    "rw aurigae": "RW Aur",
    "r draconis": "R Dra",
}


def normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def canonical(name: str) -> str:
    return ALIASES.get(normalize(name), name.strip())


async def sesame(name: str) -> dict | None:
    """Consulta SIMBAD a través de Sesame (tolerante con la forma de escribir el nombre)."""
    url = f"{settings.sesame_url.rstrip('/')}/-oxp/S?{quote(name, safe='')}"
    r = await request("simbad", "GET", url)
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        raise UpstreamError("simbad", "respuesta XML no válida de Sesame") from e
    for res in root.iter("Resolver"):
        ra, dec = fnum(res.findtext("jradeg")), fnum(res.findtext("jdedeg"))
        if ra is None or dec is None:
            continue
        oname = (res.findtext("oname") or name).strip()
        if oname.startswith("NAME "):
            oname = oname[5:]
        pmra, pmdec = fnum(res.findtext("pm/pmRA")), fnum(res.findtext("pm/pmDE"))
        epmra, epmdec = fnum(res.findtext("pm/epmRA")), fnum(res.findtext("pm/epmDE"))
        err_ra, err_de = fnum(res.findtext("errRAmas")), fnum(res.findtext("errDEmas"))
        return {
            "matched_name": oname,
            "ra": ra,
            "dec": dec,
            "source": "simbad",
            "epoch": 2000.0,
            "pm": {"ra": pmra, "dec": pmdec} if pmra is not None and pmdec is not None else None,
            "pm_err_mas_yr": max([v for v in (epmra, epmdec) if v is not None], default=None),
            "pos_err_mas": max([v for v in (err_ra, err_de) if v is not None], default=None),
            "parallax_mas": fnum(res.findtext("plx/v")),
            "otype": res.findtext("otype"),
        }
    return None


async def ned_lookup(name: str) -> dict | None:
    r = await request(
        "ned", "POST", settings.ned_lookup_url, data={"json": json.dumps({"name": {"v": name}})}, raise_4xx=False
    )
    if r.status_code >= 400:
        return None
    try:
        d = r.json()
    except ValueError as e:
        raise UpstreamError("ned", "respuesta JSON no válida") from e
    pref = d.get("Preferred") or {}
    pos = pref.get("Position") or {}
    ra, dec = fnum(pos.get("RA")), fnum(pos.get("Dec"))
    if ra is None or dec is None:
        return None
    return {
        "matched_name": pref.get("Name") or name,
        "ra": ra,
        "dec": dec,
        "source": "ned",
        "epoch": 2000.0,
        "pm": None,
        "pm_err_mas_yr": None,
        "pos_err_mas": None,
        "parallax_mas": None,
        "otype": (pref.get("ObjType") or {}).get("Value"),
    }


async def resolve_name(name: str) -> dict:
    """SIMBAD primero; si no lo encuentra, NED. Ninguno → 404. Ambos caídos → 502/504."""
    query = canonical(name)
    errors: list[UpstreamError] = []
    try:
        found = await sesame(query)
        if found:
            return found
    except UpstreamError as e:
        errors.append(e)
    try:
        found = await ned_lookup(query)
        if found:
            return found
    except UpstreamError as e:
        errors.append(e)
    if errors:
        raise upstream_failure(errors)
    raise not_found(f'No encontramos "{name}" en SIMBAD ni en NED.')
