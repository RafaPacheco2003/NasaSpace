"""Análisis: /api/predict, /api/spectrum, /api/overlay. Trabajos: /api/jobs/{id}."""
from __future__ import annotations

from datetime import date
from typing import Literal

import numpy as np
from fastapi import APIRouter, Path, Query

from .. import spherex as sx
from ..astro import in_square, jyear, propagate, to_time
from ..cache import cached, key, r1
from ..config import MAX_APERTURE, MIN_APERTURE, settings
from ..errors import bad_request, not_found
from ..jobs import jobs
from ..services.analysis import build_predict, build_spectrum
from ..services.composition import CONTEXTS, build_composition
from ..jobs import job_id
from ..sources import overlay as ov
from ..utils import check_dates, date_iso, json_response
from ..schemas import Job as JobSchema
from ..schemas import OverlayResult, PredictResult, SpectrumResult, responses
from .common import AT, DEC, FOV, RA, run_or_job

router = APIRouter(prefix="/api")


@router.get("/jobs/{job_id}", tags=["Trabajos"], summary="Estado de un trabajo",
            responses={200: {"model": JobSchema, "description": "Con status=done trae result; con failed trae error"}, **responses(None, 404)})
async def get_job(job_id: str = Path(..., pattern=r"^[a-z0-9]{3}_[0-9a-f]{6,40}$")):
    """**Para qué sirve:** consultar el progreso de un proceso lento (`progress`, `eta_s`, `stage`,
    `queue_position`) cada `poll_after_ms` hasta `done` (resultado en `result`) o `failed` (`error`).
    Sin API externa."""
    job = jobs.get(job_id)
    if job is not None:
        return json_response(jobs.to_dict(job))
    done = jobs.cached_done(job_id)
    if done is not None:
        return json_response(done)
    raise not_found(f"El trabajo {job_id} no existe o expiró.")


@router.get("/predict", tags=["Análisis"], summary="Posición predicha de un objeto en cada fecha (lento → 202 + trabajo)",
            responses=responses(PredictResult, 400, 404, 429, job=True))
async def predict(
    ra: float = RA,
    dec: float = DEC,
    name: str | None = Query(None, max_length=200),
    date_from: date = Query(...),
    date_to: date = Query(...),
):
    """**Para qué sirve:** saber dónde buscar un objeto que se mueve en cada imagen (círculo de predicción).
    Si no se da `name`, usa el objeto con movimiento conocido más cercano al centro.

    **APIs externas:** SIMBAD (movimiento propio y paralaje: estrellas, enanas marrones), JPL Horizons
    (efemérides: asteroides, cometas) e IRSA (fechas reales de las imágenes)."""
    check_dates(date_from, date_to)
    if (date_to - date_from).days > 365 * 30:
        raise bad_request("El rango de fechas supera el máximo (30 años)")
    name = name.strip() if name and name.strip() else None
    p = dict(ra=r1(ra), dec=r1(dec), name=name, date_from=date_from, date_to=date_to)
    jp = {**p, "date_from": date_iso(date_from), "date_to": date_iso(date_to), "mode": settings.spherex_mode}
    return run_or_job("predict", "prd", jp, lambda job: build_predict(job, **p))


@router.get("/spectrum", tags=["Análisis"], summary="Espectro 0.75–5 µm dentro de una apertura (lento → 202 + trabajo)",
            responses=responses(SpectrumResult, 400, 429, job=True))
async def spectrum(
    ra: float = RA,
    dec: float = DEC,
    aperture: float = Query(6, ge=MIN_APERTURE, le=MAX_APERTURE, description="Radio de la apertura en segundos de arco."),
    date_from: date | None = None,
    date_to: date | None = None,
):
    """**Para qué sirve:** el gráfico espectral de 0.75 a 5 µm de la fuente clicada (para identificar H₂O,
    CH₄, CO₂, CO). Cada exposición aporta un punto; con `date_obs`, ordenados por fecha forman una
    **curva de luz** (estrellas variables). Cada punto trae sus banderas de contaminación.

    **API externa:** IRSA (un recorte FITS pequeño por exposición; λ exacta desde WCS-WAVE)."""
    check_dates(date_from, date_to)
    p = dict(ra=r1(ra), dec=r1(dec), aperture=float(aperture), date_from=date_from, date_to=date_to)
    jp = {**p, "date_from": date_iso(date_from), "date_to": date_iso(date_to), "mode": settings.spherex_mode,
          "zodi": settings.zodi_subtract}
    return run_or_job("spectrum", "spc", jp, lambda job: build_spectrum(job, **p))


@router.get("/composition", tags=["Análisis"],
            summary="Composición estimada (hielos, moléculas, minerales) en una coordenada (lento → 202 + trabajo)",
            responses=responses(None, 400, 429, job=True))
async def composition(
    ra: float = RA,
    dec: float = DEC,
    aperture: float = Query(6, ge=MIN_APERTURE, le=MAX_APERTURE, description="Radio de la apertura en segundos de arco."),
    date_from: date | None = None,
    date_to: date | None = None,
    context: Literal["auto", *CONTEXTS] = Query(  # type: ignore[valid-type]
        "auto", description="Tipo de objeto para interpretar las bandas. auto = el de SIMBAD en esa posición; "
                            "solar_system para asteroides/cometas."),
):
    """**Para qué sirve:** el usuario elige un punto en el mapa y obtiene qué compuestos hay ahí y en qué cantidad
    aproximada, a partir del espectro SPHEREx de 0.75–5 µm (102 bandas):

    - **Hielos** (H₂O, CO₂, CO, CH₃OH): densidad de columna (moléculas/cm²), % respecto al agua y átomos H/C/O.
    - **Moléculas en gas** (H₂O vapor, CH₄, CO), **PAH / orgánicos**, **hidrógeno ionizado** (Paβ, Paα, Brγ, Brα)
      y **minerales** (silicatos con Fe, piroxeno, hidratados): profundidad de banda como indicador relativo.
    - Qué **elementos** respalda cada banda detectada.

    Usa el mismo espectro que `/api/spectrum` (si ya se pidió, solo tarda el análisis).

    **APIs externas:** IRSA (espectro) y SIMBAD (tipo de objeto, para interpretar)."""
    check_dates(date_from, date_to)
    p = dict(ra=r1(ra), dec=r1(dec), aperture=float(aperture), date_from=date_from, date_to=date_to)
    spc = {**p, "date_from": date_iso(date_from), "date_to": date_iso(date_to), "mode": settings.spherex_mode,
           "zodi": settings.zodi_subtract}
    spectrum_jid = job_id("spc", spc)                      # misma clave que /api/spectrum
    jp = {**spc, "context": context, "v": 4}
    return run_or_job("composition", "cmp", jp, lambda job: build_composition(
        job, **p, context=context, spectrum_jid=spectrum_jid))


@router.get("/overlay", tags=["Análisis"], summary="Fuentes de Gaia o WISE para dibujar sobre la imagen",
            responses=responses(OverlayResult, 400, 429, 502, 504))
async def overlay(
    ra: float = RA,
    dec: float = DEC,
    fov: float = FOV,
    catalog: Literal["gaia", "wise"] = Query(...),
    at: date | None = AT,
):
    """**Para qué sirve:** dibujar sobre la imagen las fuentes de otros telescopios, con su posición propagada
    a la fecha del frame (`at`) y su vector de movimiento propio.

    **APIs externas:** Gaia DR3 (ESA TAP, época 2016.0) o AllWISE (IRSA TAP, con Gator como respaldo,
    época 2010.56)."""
    rra, rdec = r1(ra), r1(dec)
    fetch = ov.gaia_sources if catalog == "gaia" else ov.wise_sources
    sub = settings.wise_catalog if catalog == "wise" else "dr3"
    data, hit = await cached(key("overlay", catalog, sub, rra, rdec, float(fov)), lambda: fetch(rra, rdec, fov))
    rows = data["rows"]
    t = to_time(at)
    sources = []
    if rows:
        ras = np.array([r["ra"] for r in rows])
        decs = np.array([r["dec"] for r in rows])
        pmra = np.array([r["pmra"] if r["pmra"] is not None else np.nan for r in rows])
        pmdec = np.array([r["pmdec"] if r["pmdec"] is not None else np.nan for r in rows])
        plx = np.array([r["plx"] if r["plx"] is not None and r["plx"] > 0 else 0.0 for r in rows])
        pra, pdec = propagate(ras, decs, pmra, pmdec, data["epoch"], t, plx=plx)
        inside = in_square(pra, pdec, rra, rdec, fov)
        for r, a, d, ok in zip(rows, np.atleast_1d(pra), np.atleast_1d(pdec), np.atleast_1d(inside)):
            if not ok:
                continue
            has_pm = r["pmra"] is not None and r["pmdec"] is not None
            sources.append({
                "id": r["id"],
                "ra": round(float(a), 8),
                "dec": round(float(d), 8),
                "mag": r["mag"],
                "pm": {"ra": r["pmra"], "dec": r["pmdec"]} if has_pm else None,
                "catalog_ra": r["ra"],
                "catalog_dec": r["dec"],
            })
    return json_response({
        "catalog": catalog,
        "center": {"ra": rra, "dec": rdec},
        "fov_arcsec": fov,
        "epoch": jyear(t),
        "catalog_epoch": data["epoch"],
        "mag_band": data.get("mag_band"),
        "sources": sources,
        "partial": False,
        "warnings": [] if len(rows) < settings.overlay_max_sources else
                    [f"Se devolvieron solo las {settings.overlay_max_sources} fuentes más brillantes."],
        "cache": "hit" if hit else "miss",
    })
