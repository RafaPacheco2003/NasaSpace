"""Espectro por exposición y predicción de posiciones."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import numpy as np
from astropy.time import Time

from .. import spherex as sx
from ..astro import jyear, propagate
from ..errors import APIError, UpstreamError, not_found
from ..jobs import Job
from ..config import settings
from ..sources import catalogs, horizons, resolver
from ..utils import iso_z
from .imaging import _err_msg

MAX_PREDICT_POINTS = 500


async def build_spectrum(job: Job, *, ra: float, dec: float, aperture: float, date_from: date | None, date_to: date | None) -> dict:
    job.update(0.02, "Buscando exposiciones")
    metas, _ = await sx.find_frames(ra, dec)
    sel = sx.filter_frames(metas, None, date_from, date_to)
    warns: list[str] = []
    if len(sel) > settings.max_spectrum_frames:
        warns.append(f"Se usaron {settings.max_spectrum_frames} de {len(sel)} exposiciones (límite MAX_SPECTRUM_FRAMES).")
        idx = np.linspace(0, len(sel) - 1, settings.max_spectrum_frames).round().astype(int)
        sel = [sel[i] for i in sorted(set(idx))]
    n = len(sel)
    points: list[dict] = []
    failures = 0
    done = 0

    async def one(m):
        nonlocal done, failures
        try:
            p = await sx.photometry(m, ra, dec, aperture)
            if p and p["wavelength"] is not None:
                points.append(p)
        except Exception as e:  # noqa: BLE001
            failures += 1
            if len(warns) < 20:
                warns.append(f"{m.id}: {_err_msg(e)}")
        done += 1
        job.update(0.05 + 0.9 * done / max(n, 1), f"Fotometría {done}/{n}")

    await asyncio.gather(*(one(m) for m in sel))
    if n and not points and failures:
        raise APIError(502, "upstream_error", f"No se pudo medir ninguna exposición ({warns[-1]}).")
    points.sort(key=lambda p: (p["wavelength"], p["date_obs"]))
    return {
        "center": {"ra": ra, "dec": dec},
        "aperture_arcsec": aperture,
        "wavelength_unit": "um",
        "flux_unit": "uJy",
        "points": points,
        "n_exposures": n,
        "partial": failures > 0,
        "warnings": warns,
        "cache": "miss",
    }


async def _frame_times(ra: float, dec: float, date_from: date, date_to: date, warns: list[str]) -> Time:
    """Fechas con imágenes de SPHEREx en el rango; si no hay (o falla IRSA), una por semana."""
    times: list[str] = []
    try:
        metas, _ = await sx.find_frames(ra, dec)
        sel = sx.filter_frames(metas, None, date_from, date_to)
        seen = set()
        for m in sel:
            k = m.date_obs[:16]  # agrupa al minuto
            if k not in seen:
                seen.add(k)
                times.append(m.date_obs.replace("Z", ""))
    except UpstreamError as e:
        warns.append(f"spherex: {e.message}; se usan fechas semanales")
    if not times:
        if not warns:
            warns.append("No hay imágenes de SPHEREx en el rango; se usan fechas semanales.")
        d = date_from
        while d <= date_to:
            times.append(d.isoformat() + "T00:00:00")
            d += timedelta(days=7)
    if len(times) > MAX_PREDICT_POINTS:
        idx = np.linspace(0, len(times) - 1, MAX_PREDICT_POINTS).round().astype(int)
        times = [times[i] for i in sorted(set(idx))]
    return Time(times, format="isot", scale="utc")


def _sigma(target: dict, dt_years: np.ndarray) -> np.ndarray:
    pos = (target.get("pos_err_mas") or 0.0)
    pm_err = target.get("pm_err_mas_yr")
    if pm_err is None:
        pm_err = 1.0 if target.get("pm") else 0.0
    return np.maximum(np.hypot(pos, pm_err * np.abs(dt_years)) / 1000.0, 0.001)


async def build_predict(job: Job, *, ra: float, dec: float, name: str | None, date_from: date, date_to: date) -> dict:
    warns: list[str] = []
    job.update(0.05, "Buscando fechas con imágenes")
    times = await _frame_times(ra, dec, date_from, date_to, warns)

    job.update(0.3, "Identificando el objeto")
    target = None
    if name:
        try:
            target = await resolver.sesame(resolver.canonical(name))
        except UpstreamError as e:
            warns.append(f"simbad: {e.message}")
        if target is None or target.get("pm") is None:
            job.update(0.45, "Consultando efemérides (JPL Horizons)")
            eph = None
            try:
                eph = await horizons.ephemeris(name, times)
            except UpstreamError as e:
                warns.append(f"horizons: {e.message}")
            if eph:
                command, rows = eph
                return {
                    "target": name,
                    "method": "ephemeris",
                    "pm_mas_yr": None,
                    "positions": [
                        {"date": iso_z(t.to_datetime()), "ra": r[0], "dec": r[1], "sigma_arcsec": r[2]}
                        for t, r in zip(times, rows)
                    ],
                    "warnings": warns,
                }
        if target is None:
            raise not_found(f'No encontramos "{name}" en SIMBAD ni en JPL Horizons.')
    else:
        try:
            near = await catalogs.simbad_cone(ra, dec, 300, only_moving=True)
        except UpstreamError as e:
            raise APIError(502, "upstream_error", f"simbad: {e.message}") from e
        near = [o for o in near if o["pm"] and (o["pm"]["ra"] or o["pm"]["dec"])]
        if not near:
            raise not_found("No hay objetos con movimiento propio conocido a menos de 5′ del centro; indica name.")
        o = min(near, key=lambda x: x["distance_arcsec"])
        target = {
            "matched_name": o["name"], "ra": o["ra"], "dec": o["dec"], "epoch": 2000.0, "pm": o["pm"],
            "parallax_mas": o.get("parallax_mas"), "pos_err_mas": o.get("pos_err_mas"), "pm_err_mas_yr": o.get("pm_err_mas_yr"),
        }

    job.update(0.7, "Propagando movimiento propio y paralaje")
    pm = target.get("pm")
    method = "proper_motion" if pm else "static"
    pra, pdec = propagate(
        target["ra"], target["dec"], pm["ra"] if pm else 0.0, pm["dec"] if pm else 0.0,
        target["epoch"], times, plx=target.get("parallax_mas"),
    )
    dt = np.asarray(times.jyear) - target["epoch"]
    sig = _sigma(target, dt)
    pra, pdec = np.atleast_1d(pra), np.atleast_1d(pdec)
    return {
        "target": target["matched_name"],
        "method": method,
        "pm_mas_yr": {"ra": pm["ra"], "dec": pm["dec"]} if pm else None,
        "parallax_mas": target.get("parallax_mas"),
        "catalog_epoch": target["epoch"],
        "catalog_position": {"ra": target["ra"], "dec": target["dec"]},
        "positions": [
            {"date": iso_z(t.to_datetime()), "ra": round(float(a), 7), "dec": round(float(d), 7), "sigma_arcsec": round(float(s), 4)}
            for t, a, d, s in zip(times, pra, pdec, sig)
        ],
        "epoch_range": [jyear(times[0]), jyear(times[-1])] if len(times) else None,
        "warnings": warns,
    }
