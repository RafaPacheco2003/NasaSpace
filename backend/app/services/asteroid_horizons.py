"""Asteroides: posición futura con JPL Horizons (sin modelo de IA).

El usuario marca el asteroide en el frame que ve (T3) e indica su número MPC. El backend:
1. pide a Horizons (observador = SPHEREx, `-163182`; si no hay órbita, geocentro) la posición en T3 y comprueba
   que el clic coincide (si no → 422 identity_mismatch);
2. pide la posición en T3 + horizon_days → predicción;
3. pide la trayectoria entre ambas (TRACK_POINTS puntos) para dibujar la línea;
4. proyecta todo sobre la vista (`view` = ra, dec, fov, size del PNG mostrado) → píxeles.

Una sola consulta a Horizons (con caché); responde en ~1–2 s la primera vez e instantáneo después.
"""
from __future__ import annotations

import math
import re

from astropy.time import Time

from .. import spherex as sx
from ..astro import separation_arcsec, target_wcs
from ..cache import cached, key
from ..config import settings
from ..errors import APIError, UpstreamError, bad_request
from ..http import request
from ..sources import horizons
from ..utils import iso_z

MAX_HORIZON_DAYS = 30.0
TRACK_POINTS = 9   # T3 … predicción, incluidos los extremos



def mjd_to_iso(mjd: float) -> str:
    return iso_z(Time(mjd, format="mjd", scale="utc").to_datetime())


async def project_to_view(ra: float, dec: float, view: dict | None, image_id: str | None) -> dict | None:
    """RA/Dec → píxel del PNG de la vista (misma rejilla que /api/sequence: TAN, norte arriba, este a la izquierda).

    Convención del contrato: x desde el borde izquierdo, y_top desde el borde superior, en píxeles continuos
    (el centro del PNG es size/2). Para índice de píxel entero: floor(x), floor(y_top).
    """
    if view is None:
        if image_id:
            raise bad_request("Para proyectar sobre image_id hace falta 'view' (ra, dec, fov, size de la imagen mostrada).")
        return None
    tw = target_wcs(view["ra"], view["dec"], view["fov"], view["size"])
    x0, y0 = (float(v) for v in tw.world_to_pixel_values(ra, dec))
    size = int(view["size"])
    x, y_top = x0 + 0.5, size - (y0 + 0.5)
    inside = bool(math.isfinite(x) and math.isfinite(y_top) and 0 <= x <= size and 0 <= y_top <= size)
    out = {
        "x": round(x, 3) if inside else None,
        "y_top": round(y_top, 3) if inside else None,
        "inside_image": inside,
        "image_width": size,
        "image_height": size,
        "image_id": image_id,
        "image_date_obs": None,
        "in_frame_footprint": None,
    }
    if image_id:
        meta = (await sx.lookup_frames([image_id], view["ra"], view["dec"]))[0]
        out["image_date_obs"] = meta.date_obs
        out["in_frame_footprint"] = bool(sx._covers(meta, ra, dec))
    return out


# --- JPL Horizons (observador SPHEREx) ----------------------------------------------

_NAME = re.compile(r"Target body name:\s*(.+?)\s{2,}")


async def ephemeris(asteroid_id: int, mjds: list[float]) -> tuple[str, str, list[tuple[float, float, float | None]]]:
    """Posiciones de JPL Horizons del asteroide numerado `asteroid_id` vistas desde SPHEREx.

    Devuelve (nombre, centro usado, [(ra, dec, sigma_arcsec)]). Si Horizons no tiene la órbita de SPHEREx para
    esas fechas, repite con el geocentro.
    """
    jds = [m + 2400000.5 for m in mjds]
    last_err: Exception | None = None
    for center in (settings.horizons_spherex_center, "500@399"):
        rows: list = []
        name = f"({asteroid_id})"
        ok = True
        for i in range(0, len(jds), horizons.CHUNK):
            chunk = jds[i: i + horizons.CHUNK]
            params = {
                "format": "json", "COMMAND": f"'{asteroid_id};'", "OBJ_DATA": "'NO'", "MAKE_EPHEM": "'YES'",
                "EPHEM_TYPE": "'OBSERVER'", "CENTER": f"'{center}'",
                "TLIST": "'" + " ".join(f"{jd:.6f}" for jd in chunk) + "'", "TLIST_TYPE": "'JD'",
                "TIME_TYPE": "'UT'", "QUANTITIES": "'1,36'", "CSV_FORMAT": "'YES'", "ANG_FORMAT": "'DEG'",
                "EXTRA_PREC": "'YES'",
            }
            r = await request("horizons", "GET", settings.horizons_url, params=params,
                              timeout=max(settings.external_timeout_s, 20))
            try:
                text = r.json().get("result", "")
            except ValueError:
                text = ""
            part = horizons._parse(text)
            if part is None or len(part) != len(chunk):
                ok = False
                low = text.lower()
                if any(x in low for x in ("no matches found", "unknown target", "no such object", "out of bounds",
                                          "no ephemeris for target")):
                    raise APIError(404, "not_found", f"JPL Horizons no conoce el asteroide número {asteroid_id}.")
                last_err = UpstreamError("horizons", (text.strip().splitlines() or ["respuesta vacía"])[-1][:200])
                break
            m = _NAME.search(text)
            if m:
                name = m.group(1).strip()
            rows.extend(part)
        if ok:
            return name, center, rows
    raise last_err or UpstreamError("horizons", "sin efemérides")


async def cached_ephemeris(asteroid_id: int, mjds: list[float]):
    """Como `ephemeris`, con caché. Devuelve las filas en el MISMO orden que `mjds`
    (Horizons ordena la TLIST por fecha, así que se consulta ordenada y se reordena)."""
    order = sorted(range(len(mjds)), key=lambda i: mjds[i])
    asc = [mjds[i] for i in order]
    k = key("ast_eph", asteroid_id, settings.horizons_spherex_center, [round(m, 6) for m in asc])

    async def fetch():
        name, center, rows = await ephemeris(asteroid_id, asc)
        return {"name": name, "center": center, "rows": rows}

    v, _ = await cached(k, fetch)
    rows: list = [None] * len(mjds)
    for pos, i in enumerate(order):
        rows[i] = tuple(v["rows"][pos])
    return v["name"], v["center"], rows


def _motion(ra1: float, dec1: float, ra2: float, dec2: float, dt_days: float) -> dict:
    """Velocidad aparente (″/h) y dirección (ángulo de posición, grados desde el norte hacia el este)."""
    dra = ((ra2 - ra1 + 540) % 360 - 180) * math.cos(math.radians((dec1 + dec2) / 2))
    ddec = dec2 - dec1
    pa = (math.degrees(math.atan2(dra, ddec)) + 360) % 360
    speed = float(separation_arcsec(ra1, dec1, ra2, dec2)) / max(dt_days * 24, 1e-9)
    return {"arcsec_per_hour": round(speed, 3), "position_angle_deg": round(pa, 2)}


async def horizons_forecast(*, asteroid_id: int, current: dict, horizon_days: float,
                            image_id: str | None, view: dict | None) -> dict:
    if not (0 < horizon_days <= MAX_HORIZON_DAYS):
        raise bad_request(f"horizon_days debe estar entre 0 (excluido) y {MAX_HORIZON_DAYS:.0f} días.")
    if image_id and view is None:
        raise bad_request("Para proyectar sobre image_id hace falta 'view' (ra, dec, fov, size de la imagen mostrada).")

    t3 = float(current["mjd_utc"])
    tf = t3 + horizon_days
    mjds = [t3 + horizon_days * i / (TRACK_POINTS - 1) for i in range(TRACK_POINTS)]
    name, center, eph = await cached_ephemeris(asteroid_id, mjds)

    ra3e, dec3e, sig3 = eph[0]
    raf, decf, sigf = eph[-1]
    off3 = float(separation_arcsec(current["ra"], current["dec"], ra3e, dec3e))
    tol = max(settings.asteroid_identity_tol_arcsec, 3 * (sig3 or 0))
    if off3 > tol:
        raise APIError(422, "identity_mismatch",
                       f"La posición marcada está a {off3:.0f}″ de donde JPL Horizons pone a {name} en esa fecha "
                       f"(tolerancia {tol:.0f}″): no parece el mismo asteroide o la fecha es incorrecta.")

    warns: list[str] = []
    if center != settings.horizons_spherex_center:
        warns.append("horizons: sin órbita de SPHEREx para estas fechas; posiciones geocéntricas (error ~1 píxel).")

    track = []
    for m, (ra, dec, sig) in zip(mjds, eph):
        track.append({"mjd_utc": round(m, 6), "time": mjd_to_iso(m), "ra": round(float(ra), 7), "dec": round(float(dec), 7),
                      "sigma_arcsec": sig, "pixel": await project_to_view(ra, dec, view, None)})

    return {
        "asteroid_id": asteroid_id,
        "target": name,
        "method": "horizons",
        "observer": center,
        "current_observation": {
            "mjd_utc": t3, "time": mjd_to_iso(t3), "ra": float(current["ra"]), "dec": float(current["dec"]),
            "ephemeris_ra": round(float(ra3e), 7), "ephemeris_dec": round(float(dec3e), 7),
            "offset_arcsec": round(off3, 2),
            "pixel": await project_to_view(float(current["ra"]), float(current["dec"]), view, None),
        },
        "predicted_ra_deg": round(float(raf), 7),
        "predicted_dec_deg": round(float(decf), 7),
        "sigma_arcsec": sigf,
        "forecast_mjd_utc": round(tf, 6),
        "forecast_time": mjd_to_iso(tf),
        "horizon_days": horizon_days,
        "motion": _motion(ra3e, dec3e, raf, decf, horizon_days),
        "image_id": image_id,
        "pixel": await project_to_view(raf, decf, view, image_id),
        "track": track,
        "warnings": warns,
        "status": "ephemeris",
    }
