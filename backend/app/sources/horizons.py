# SI-assisted development used for code development, debugging and documentation.
# Tool: ChatGPT, Claude

"""Efemérides de JPL Horizons (asteroides, cometas, planetas) para /api/predict."""
from __future__ import annotations

import math

from astropy.time import Time

from ..config import settings
from ..http import request

CHUNK = 40


def _parse(text: str) -> list[tuple[float, float, float | None]] | None:
    if "$$SOE" not in text or "$$EOE" not in text:
        return None
    body = text.split("$$SOE", 1)[1].split("$$EOE", 1)[0]
    out = []
    for line in body.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        # Fecha, presencia solar, presencia lunar, RA, DEC, RA_3sigma, DEC_3sigma, ...
        if len(f) < 5:
            continue
        try:
            ra, dec = float(f[3]), float(f[4])
        except ValueError:
            continue
        sigma = None
        try:
            ra3, de3 = float(f[5]), float(f[6])
            sigma = math.hypot(ra3, de3) / 3.0
        except (ValueError, IndexError):
            pass
        out.append((ra, dec, sigma))
    return out


async def _query(command: str, jds: list[float]) -> list | None:
    params = {
        "format": "json",
        "COMMAND": f"'{command}'",
        "OBJ_DATA": "'NO'",
        "MAKE_EPHEM": "'YES'",
        "EPHEM_TYPE": "'OBSERVER'",
        "CENTER": "'500@399'",  # geocéntrico (SPHEREx está en órbita baja terrestre)
        "TLIST": "'" + " ".join(f"{jd:.6f}" for jd in jds) + "'",
        "TLIST_TYPE": "'JD'",
        "TIME_TYPE": "'UT'",
        "QUANTITIES": "'1,36'",
        "CSV_FORMAT": "'YES'",
        "ANG_FORMAT": "'DEG'",
        "EXTRA_PREC": "'YES'",
    }
    r = await request("horizons", "GET", settings.horizons_url, params=params, timeout=max(settings.external_timeout_s, 15))
    try:
        text = r.json().get("result", "")
    except ValueError:
        return None
    return _parse(text)


async def ephemeris(name: str, times: Time) -> tuple[str, list[tuple[float, float, float | None]]] | None:
    """Devuelve (comando usado, posiciones) o None si Horizons no reconoce el objeto."""
    clean = name.replace("'", "").replace('"', "").strip()
    jds = [float(x) for x in times.utc.jd]
    for command in (clean, f"{clean};"):
        first = await _query(command, jds[:CHUNK])
        if first is None or len(first) != len(jds[:CHUNK]):
            continue
        rows = list(first)
        for i in range(CHUNK, len(jds), CHUNK):
            part = await _query(command, jds[i : i + CHUNK])
            if part is None or len(part) != len(jds[i : i + CHUNK]):
                return None
            rows.extend(part)
        return command, rows
    return None
