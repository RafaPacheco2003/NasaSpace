"""Imágenes: /api/frames, /api/sequence, /api/frame/{id}.png, /api/stack, /api/stack/{id}.png, /api/coverage."""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date
from typing import Literal

from fastapi import APIRouter, Path, Query
from fastapi.responses import Response

from .. import spherex as sx
from ..cache import cget, r1
from ..config import MAX_FRAMES_LIMIT, MAX_IDS, MAX_SEQUENCE_LIMIT, settings
from ..errors import bad_request, not_found
from ..render import auto_limits, render_diff_png, render_mask_png, render_png
from ..services.imaging import build_sequence, build_stack, stack_image_key
from ..utils import check_dates, date_iso, json_response, split_ids, week_start
from ..schemas import CoverageResult, FramesResult, SequenceResult, StackResult, responses
from .common import DEC, DETECTOR, FOV, RA, SIZE, Cmap, Stretch, run_or_job

router = APIRouter(prefix="/api", tags=["Imágenes"])

def _id_path():
    return Path(..., pattern=r"^[A-Za-z0-9_.:+\-]{1,128}$")
PNG_HEADERS = {"Cache-Control": "public, max-age=86400"}


@router.get("/frames", summary="Metadatos de los frames que cubren una región (rápido, sin imágenes)",
            responses=responses(FramesResult, 400, 429, 502, 504))
async def frames(
    ra: float = RA,
    dec: float = DEC,
    fov: float = FOV,
    detector: int | None = DETECTOR,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(200, ge=1, le=MAX_FRAMES_LIMIT),
    offset: int = Query(0, ge=0),
):
    """**Para qué sirve:** saber qué imágenes de SPHEREx hay de ese punto (fechas, detector, longitud de onda)
    sin descargarlas. Sirve para la línea de tiempo y para elegir los ids de `/api/stack`.

    **API externa:** IRSA SIA v2 (`COLLECTION=spherex_dr1`)."""
    check_dates(date_from, date_to)
    rra, rdec = r1(ra), r1(dec)
    metas, hit = await sx.find_frames(rra, rdec)
    sel = sx.filter_frames(metas, detector, date_from, date_to)
    page = sel[offset: offset + limit]
    return json_response({
        "center": {"ra": rra, "dec": rdec},
        "fov_arcsec": fov,
        "total": len(sel),
        "limit": limit,
        "offset": offset,
        "frames": [m.api(sx.frame_info(m.id, rra, rdec)) for m in page],
        "partial": False,
        "warnings": [],
        "cache": "hit" if hit else "miss",
    })


@router.get("/sequence", summary="Secuencia de imágenes de una región (lento → 202 + trabajo)",
            responses=responses(SequenceResult, 400, 429, job=True))
async def sequence(
    ra: float = RA,
    dec: float = DEC,
    fov: float = FOV,
    size: int = SIZE,
    detector: int | None = DETECTOR,
    date_from: date | None = None,
    date_to: date | None = None,
    stretch: Stretch = "asinh",
    cmap: Cmap = "gray",
    scale: Literal["sequence", "frame"] = Query("sequence", description="sequence: mismos vmin/vmax en todos los frames; frame: cada uno por separado."),
    limit: int = Query(60, ge=1, le=MAX_SEQUENCE_LIMIT),
    offset: int = Query(0, ge=0),
):
    """**Para qué sirve:** el "video" (blink comparator). Descarga, recorta y alinea píxel a píxel todas las
    imágenes de la región para reproducirlas en orden. Un objeto que se mueve "salta" entre frames.

    **API externa:** IRSA (búsqueda SIA + servicio de cutouts sobre los FITS de SPHEREx).

    Devuelve 200 si ya está en caché; si no, 202 + Job (consultar `/api/jobs/{id}`)."""
    check_dates(date_from, date_to)
    p = dict(ra=r1(ra), dec=r1(dec), fov=float(fov), size=size, detector=detector, date_from=date_from, date_to=date_to,
             stretch=stretch, cmap=cmap, scale=scale, limit=limit, offset=offset)
    jparams = {**p, "date_from": date_iso(date_from), "date_to": date_iso(date_to), "mode": settings.spherex_mode,
               "clean": settings.clean_bad_pixels, "zodi": settings.zodi_subtract}
    return run_or_job("sequence", "seq", jparams, lambda job: build_sequence(job, **p))


@router.get("/frame/{frame_id}.png", summary="Render PNG del recorte de un frame", response_class=Response,
            responses=responses(None, 400, 404, 502, 504, png=True))
async def frame_png(
    frame_id: str = _id_path(),
    ra: float = RA,
    dec: float = DEC,
    fov: float = FOV,
    size: int = SIZE,
    stretch: Stretch = "asinh",
    cmap: Cmap = "gray",
    vmin: float | None = Query(None, description="Límite inferior (MJy/sr). Sin valor = automático."),
    vmax: float | None = Query(None, description="Límite superior (MJy/sr). Sin valor = automático."),
):
    """**Para qué sirve:** la imagen de un frame. Las `png_url` de `/api/sequence` apuntan aquí con toda la
    query: usarlas tal cual. Rápido si la secuencia ya preparó el recorte; si no, hasta ~10 s.

    **API externa:** IRSA (recorte FITS) si no está en caché."""
    rra, rdec = r1(ra), r1(dec)
    meta = (await sx.lookup_frames([frame_id], rra, rdec))[0]
    data = await sx.get_reprojected(meta, rra, rdec, float(fov), size)
    arr = data["arr"]
    if vmin is None or vmax is None:
        avmin, avmax = auto_limits([arr])
        vmin = avmin if vmin is None else vmin
        vmax = avmax if vmax is None else vmax
    if not vmax > vmin:
        raise bad_request("vmax debe ser mayor que vmin")
    png = await asyncio.to_thread(render_png, arr, vmin, vmax, stretch, cmap)
    return Response(png, media_type="image/png", headers=PNG_HEADERS)


@router.get("/frame/{frame_id}/flags.png", summary="(Extra) Capa de banderas de contaminación del recorte",
            response_class=Response, responses=responses(None, 400, 404, 502, 504, png=True))
async def frame_flags_png(frame_id: str = _id_path(), ra: float = RA, dec: float = DEC, fov: float = FOV, size: int = SIZE):
    """PNG transparente con la misma geometría que el frame; los píxeles con banderas
    (rayos cósmicos, saturación, píxeles calientes…) salen en ámbar. No está en el contrato v0.3."""
    rra, rdec = r1(ra), r1(dec)
    meta = (await sx.lookup_frames([frame_id], rra, rdec))[0]
    data = await sx.get_reprojected(meta, rra, rdec, float(fov), size)
    bad = data["bad"]
    if bad is None:
        import numpy as np

        bad = np.zeros((size, size), dtype=bool)
    png = await asyncio.to_thread(render_mask_png, bad)
    return Response(png, media_type="image/png", headers=PNG_HEADERS)


@router.get("/stack", summary="Apilar o restar frames (lento → 202 + trabajo)",
            responses=responses(StackResult, 400, 404, 429, job=True))
async def stack(
    ra: float = RA,
    dec: float = DEC,
    fov: float = FOV,
    size: int = SIZE,
    stretch: Stretch = "asinh",
    cmap: Cmap = "gray",
    op: Literal["mean", "median", "sum", "diff"] = Query(...),
    a: list[str] = Query(..., description="Ids de frame (repetir ?a=…&a=… o separados por coma)."),
    b: list[str] | None = Query(None, description="Obligatorio si op=diff."),
):
    """**Para qué sirve:** `mean/median/sum` suman imágenes para ver objetos débiles (más señal/ruido).
    `diff` = media(a) − media(b): resta dos épocas; las estrellas fijas se anulan y lo que se movió o cambió
    de brillo aparece en ámbar (positivo) y cian (negativo).

    **API externa:** IRSA (recortes FITS de SPHEREx). Máximo 100 ids por lista."""
    ids_a = sorted(split_ids(a, "a"))
    ids_b = sorted(split_ids(b, "b"))
    if not ids_a:
        raise bad_request("a debe tener al menos un id de frame")
    if len(ids_a) > MAX_IDS or len(ids_b) > MAX_IDS:
        raise bad_request(f"a/b superan el máximo ({MAX_IDS} ids por lista)")
    if op == "diff" and not ids_b:
        raise bad_request("b es obligatorio si op=diff")
    if op != "diff":
        ids_b = []
    p = dict(ra=r1(ra), dec=r1(dec), fov=float(fov), size=size, stretch=stretch, cmap=cmap, op=op, a=ids_a, b=ids_b)
    return run_or_job("stack", "stk", {**p, "mode": settings.spherex_mode, "clean": settings.clean_bad_pixels,
                                               "zodi": settings.zodi_subtract}, lambda job: build_stack(job, **p))


@router.get("/stack/{stack_id}.png", summary="PNG de un apilado ya calculado", response_class=Response,
            responses=responses(None, 404, png=True))
async def stack_png(stack_id: str = _id_path()):
    """**Para qué sirve:** la imagen del apilado/resta (la `png_url` de `StackResult`). Sin API externa: sale de la caché."""
    v = cget(stack_image_key(stack_id))
    if v is None:
        raise not_found(f"El apilado {stack_id} no existe o expiró. Vuelve a pedir /api/stack.")
    if v["op"] == "diff":
        png = await asyncio.to_thread(render_diff_png, v["arr"], v["vmax"], v["stretch"])
    else:
        png = await asyncio.to_thread(render_png, v["arr"], v["vmin"], v["vmax"], v["stretch"], v["cmap"])
    return Response(png, media_type="image/png", headers=PNG_HEADERS)


@router.get("/coverage", summary="Imágenes por semana (para el histograma del selector de fechas)",
            responses=responses(CoverageResult, 400, 502, 504))
async def coverage(ra: float = RA, dec: float = DEC, detector: int | None = DETECTOR):
    """**Para qué sirve:** el histograma del selector de fechas (cuántas imágenes hay cada semana). SPHEREx
    observa cada punto del cielo en tandas cada ~6 meses.

    **API externa:** IRSA SIA v2."""
    rra, rdec = r1(ra), r1(dec)
    metas, hit = await sx.find_frames(rra, rdec)
    sel = sx.filter_frames(metas, detector, None, None)
    days = [date.fromisoformat(m.date_obs[:10]) for m in sel]
    weeks = Counter(week_start(d) for d in days)
    return json_response({
        "center": {"ra": rra, "dec": rdec},
        "first_date": min(days).isoformat() if days else None,
        "last_date": max(days).isoformat() if days else None,
        "weeks": [{"week_start": w.isoformat(), "count": c} for w, c in sorted(weeks.items())],
        "total": len(sel),
        "cache": "hit" if hit else "miss",
    })
