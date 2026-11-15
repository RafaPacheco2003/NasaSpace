"""Productos del pipeline SphereX: L2 completo (raw / color / clic) y cubo all-sky (102 canales, 6 canales, 3D)."""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Literal

from fastapi import APIRouter, Path, Query
from fastapi.responses import RedirectResponse, Response

from .. import spherex as sx
from ..cache import r1
from ..config import settings
from ..errors import bad_request, not_found
from ..products import common as C
from ..products import cube as CU
from ..products import l2 as L2
from ..schemas import responses
from ..utils import check_dates, date_iso, json_response
from .common import DEC, DETECTOR, RA, run_or_job

router = APIRouter(prefix="/api")

FID = Path(..., pattern=r"^sx-d[1-6]-\d+$", description="Id del frame L2 (de /api/l2, /api/frames o /api/sequence)")
TID = Path(..., pattern=r"^hpx[0-9a-z-]+$", description="Id de la tesela del cubo (de /api/cube), p. ej. hpx5-2858")
SIZE_L2 = Query(1024, ge=64, le=2040, description="Lado mayor del PNG en píxeles (nativo 2040). Usar el MISMO en /pixel.")
SIZE_CUBE = Query(None, ge=64, le=2048, description="Lado mayor del PNG (nativo 512). Usar el MISMO en /pixel.")
PNG_HEADERS = {"Cache-Control": "public, max-age=86400"}


def png(data: bytes) -> Response:
    return Response(data, media_type="image/png", headers=PNG_HEADERS)


def _xy_or_radec(x, y, ra, dec):
    if (x is None) != (y is None) or (ra is None) != (dec is None):
        raise bad_request("Pasar x e y (píxel del PNG) o ra y dec (grados).")
    if x is None and ra is None:
        raise bad_request("Faltan x, y (píxel del PNG) o ra, dec.")


# ======================= L2 COMPLETO ==============================================

@router.get("/l2", tags=["L2 y cubo all-sky"], summary="Imagen L2 completa para una coordenada y fechas (lento → 202 + trabajo)",
            responses=responses(None, 400, 404, 429, 502, job=True))
async def l2_prepare(
    ra: float = RA, dec: float = DEC,
    date_from: date | None = Query(None, description="Desde (YYYY-MM-DD)"),
    date_to: date | None = Query(None, description="Hasta (YYYY-MM-DD)"),
    detector: int | None = DETECTOR,
    frame_id: str | None = Query(None, pattern=r"^sx-d[1-6]-\d+$", description="Elegir una exposición concreta (de `candidates`)"),
    size: int = SIZE_L2,
):
    """**Para qué sirve:** obtener la imagen **L2 completa** (2040×2040, ~3.5°) de SPHEREx que cubre la coordenada,
    como el pipeline `SphereX-pipeline`: entre las exposiciones de las fechas pedidas se elige la **tercera en orden
    cronológico** (o la última si hay menos), o la que se indique con `frame_id`.

    Devuelve las URLs de la imagen **raw** (gris invertido) y **color** (cividis), la posición de la coordenada en el
    PNG (`target.x/y`), su brillo, la lista de exposiciones candidatas y la plantilla del clic (`pixel_url`).
    La primera vez descarga el FITS completo (~65 MB) → 202 + trabajo; después 200 desde caché.

    **API externa:** IRSA (SIA spherex_dr1 + descarga del FITS L2)."""
    check_dates(date_from, date_to)
    p = dict(ra=r1(ra), dec=r1(dec), frame_id=frame_id, detector=detector, date_from=date_from, date_to=date_to, size=size)
    jp = {**p, "date_from": date_iso(date_from), "date_to": date_iso(date_to), "mode": settings.spherex_mode}
    return run_or_job("l2", "l2p", jp, lambda job: L2.build_l2(job, **p))


async def _l2_path(fid: str):
    meta = sx.get_registered(fid)
    if meta is None:
        raise not_found(f"Frame {fid} desconocido: llama primero a GET /api/l2 (o /api/frames) con su coordenada.")
    return meta, await L2.local(meta)


@router.get("/l2/{frame_id}/raw.png", tags=["L2 y cubo all-sky"], summary="L2 'raw': IMAGE normalizada en gris",
            responses=responses(None, 404, png=True))
async def l2_raw(frame_id: str = FID, size: int = SIZE_L2,
                 invert: bool = Query(True, description="true = gris invertido (gray_r, como el pipeline); false = gris normal")):
    """IMAGE del L2 normalizada con percentiles 3.5–96.5 (pipeline), en gris. Orientación del detector
    (fila 0 del FITS abajo); ver `orientation` en /api/l2 para el norte."""
    _, path = await _l2_path(frame_id)
    return png(await asyncio.to_thread(L2.render, path, "raw", size, invert))


@router.get("/l2/{frame_id}/color.png", tags=["L2 y cubo all-sky"], summary="L2 'color': pseudocolor cividis del raw",
            responses=responses(None, 404, png=True))
async def l2_color(frame_id: str = FID, size: int = SIZE_L2):
    """La misma normalización del raw en pseudocolor **cividis** (no es color visible); píxeles inválidos en negro."""
    _, path = await _l2_path(frame_id)
    return png(await asyncio.to_thread(L2.render, path, "color", size))


@router.get("/l2/{frame_id}/pixel", tags=["L2 y cubo all-sky"], summary="Clic en el L2: RA/Dec + brillo de ese píxel",
            responses=responses(None, 400, 404))
async def l2_pixel(frame_id: str = FID,
                   x: float | None = Query(None, description="Píxel del PNG desde el borde IZQUIERDO"),
                   y: float | None = Query(None, description="Píxel del PNG desde el borde SUPERIOR"),
                   size: int = SIZE_L2,
                   ra: float | None = Query(None, ge=0, lt=360, description="Alternativa: coordenada → píxel y valores"),
                   dec: float | None = Query(None, ge=-90, le=90)):
    """**Para qué sirve:** al picar la imagen raw o color, mostrar **RA/Dec** de ese punto y su **brillo**:
    `brightness.value` (MJy/sr, IMAGE del L2), `zodi`, `value_minus_zodi`, `error` (√VARIANCE),
    `wavelength_um` de **ese** píxel (WCS-WAVE: el filtro de SPHEREx cambia a lo largo del detector) y `flags`.
    `x, y` en píxeles del PNG mostrado con el mismo `size`. También acepta `ra, dec`."""
    _xy_or_radec(x, y, ra, dec)
    meta, path = await _l2_path(frame_id)
    s = await asyncio.to_thread(L2.stats, path)
    native = tuple(s["shape"])
    if x is not None:
        col, row = C.png_to_fits(x, y, native, size)
    else:
        import warnings

        from astropy.wcs import WCS
        with warnings.catch_warnings(), L2._open(path) as h:
            warnings.simplefilter("ignore")
            col, row = (float(v) for v in WCS(h["IMAGE"].header).celestial.world_to_pixel_values(ra, dec))
        x, y = C.fits_to_png(col, row, native, size)
    info = await asyncio.to_thread(L2.pixel_info, path, col, row)
    return json_response({"frame_id": frame_id, "date_obs": meta.date_obs, "detector": meta.detector,
                          "x": round(x, 3), "y": round(y, 3), "size": size, **info})


@router.get("/l2/{frame_id}/fits", tags=["L2 y cubo all-sky"], summary="Descargar el FITS L2 original (redirige a IRSA)",
            responses={307: {"description": "Redirección a IRSA"}, **responses(None, 404)})
async def l2_fits(frame_id: str = FID):
    meta = sx.get_registered(frame_id)
    if meta is None:
        raise not_found(f"Frame {frame_id} desconocido.")
    return RedirectResponse(meta.access_url, status_code=307)


# ======================= CUBO ALL-SKY ==============================================

@router.get("/cube", tags=["L2 y cubo all-sky"], summary="Cubo all-sky de 102 canales para una coordenada (lento → 202 + trabajo)",
            responses=responses(None, 400, 404, 429, 502, job=True))
async def cube_prepare(ra: float = RA, dec: float = DEC, size: int | None = SIZE_CUBE):
    """**Para qué sirve:** el **sky cube** de SPHEREx (todo el cielo combinado, 102 canales 0.75–5 µm) en la tesela
    HEALPix que contiene la coordenada, como el pipeline. Devuelve los 102 canales (λ, bordes, ancho de banda) con la URL
    del PNG de cada uno, los **6 canales** del script (1, 20, 40, 60, 80, 102), el **espectro** de 102 puntos en la
    coordenada (brillo, FLAGS, NHITS) y las URLs de la figura de 6 canales, la vista 3D y el clic.

    La primera vez descarga la tesela completa (~320 MB, 30–90 s) → 202 + trabajo con progreso; después 200.
    El cubo no tiene fechas (es la combinación de todo el survey).

    **API externa:** IRSA (SIA spherex_dr1_cube + descarga de la tesela)."""
    p = dict(ra=r1(ra), dec=r1(dec), size=size)
    return run_or_job("cube", "cub", {**p, "mode": settings.spherex_mode}, lambda job: CU.build_cube(job, **p))


async def _cube_path(tid: str):
    t = CU.get_tile(tid)
    return t, await CU.local(t)


@router.get("/cube/{tile_id}/channels", tags=["L2 y cubo all-sky"], summary="Lista de canales del cubo (102 o los 6 del script)",
            responses=responses(None, 404))
async def cube_channels(tile_id: str = TID, set: Literal["all", "six"] = Query("all", description="all = 102 canales · six = 1,20,40,60,80,102"),
                        cmap: Literal["gray_r", "gray", "inferno", "magma", "viridis", "cividis"] = "gray_r",
                        size: int | None = SIZE_CUBE):
    """Canal, λ central, bordes y ancho de banda (µm), detector, resolución R y la URL del PNG de cada plano."""
    _, path = await _cube_path(tile_id)
    info = await asyncio.to_thread(CU.inspect, path)
    chans = info["channels"] if set == "all" else [info["channels"][c - 1] for c in CU.SIX]
    return json_response({"tile_id": tile_id, "set": set, "unit": info["unit"], "count": len(chans),
                          "channels": [{**c, "png_url": CU.channel_urls(tile_id, c["channel"], cmap, size)} for c in chans]})


@router.get("/cube/{tile_id}/channel/{channel}.png", tags=["L2 y cubo all-sky"], summary="Un plano del cubo (canal 1–102)",
            responses=responses(None, 404, png=True))
async def cube_channel_png(tile_id: str = TID, channel: int = Path(..., ge=1, le=102),
                           cmap: Literal["gray_r", "gray", "inferno", "magma", "viridis", "cividis"] = "gray_r",
                           size: int | None = SIZE_CUBE):
    """Plano `channel` normalizado con SUS percentiles 3.5–96.5 (como samples.png). Sin datos → transparente.
    Proyección HEALPix de la tesela (no está con el norte arriba: ver `orientation`)."""
    _, path = await _cube_path(tile_id)
    return png(await asyncio.to_thread(CU.render_channel, path, channel, cmap, size))


@router.get("/cube/{tile_id}/pixel", tags=["L2 y cubo all-sky"], summary="Clic en el cubo: RA/Dec + espectro de 102 canales",
            responses=responses(None, 400, 404))
async def cube_pixel(tile_id: str = TID,
                     x: float | None = Query(None, description="Píxel del PNG desde el borde IZQUIERDO"),
                     y: float | None = Query(None, description="Píxel del PNG desde el borde SUPERIOR"),
                     size: int | None = SIZE_CUBE,
                     ra: float | None = Query(None, ge=0, lt=360), dec: float | None = Query(None, ge=-90, le=90)):
    """RA/Dec del punto y su espectro: 102 × (λ, ancho de banda, brillo en BUNIT, FLAGS, NHITS)."""
    _xy_or_radec(x, y, ra, dec)
    _, path = await _cube_path(tile_id)
    info = await asyncio.to_thread(CU.inspect, path)
    native = tuple(info["shape"][1:])
    if x is not None:
        col, row = C.png_to_fits(x, y, native, size)
    else:
        pix = await asyncio.to_thread(CU.contains, path, ra, dec)
        if pix is None:
            raise bad_request("La coordenada no cae dentro de esta tesela.")
        col, row = pix
        x, y = C.fits_to_png(col, row, native, size)
    spec = await asyncio.to_thread(CU.spectrum_at, path, col, row)
    return json_response({"tile_id": tile_id, "x": round(x, 3), "y": round(y, 3), "size": size, **spec})


@router.get("/cube/{tile_id}/samples.png", tags=["L2 y cubo all-sky"], summary="Figura de los 6 canales del script (samples.png)",
            responses=responses(None, 404, png=True))
async def cube_samples(tile_id: str = TID, ra: float | None = Query(None, ge=0, lt=360), dec: float | None = Query(None, ge=-90, le=90)):
    """Los canales 1, 20, 40, 60, 80, 102 con ejes RA/Dec, λ, Δλ y barra de color (y la cruz roja en ra/dec)."""
    _, path = await _cube_path(tile_id)
    pix = await asyncio.to_thread(CU.contains, path, ra, dec) if ra is not None and dec is not None else None
    return png(await asyncio.to_thread(CU.samples_figure, path, *(pix or (None, None))))


@router.get("/cube/{tile_id}/preview3d", tags=["L2 y cubo all-sky"], summary="Malla 3D (102 × 32 × 32) para un visor WebGL",
            responses=responses(None, 404))
async def cube_preview3d(tile_id: str = TID, n: int = Query(32, ge=8, le=64, description="Muestras por eje espacial")):
    """Equivalente a `preview3d.npz` del pipeline: brillo[canal][y][x], ejes x/y (píxel FITS) y λ."""
    _, path = await _cube_path(tile_id)
    return json_response(await asyncio.to_thread(CU.preview3d, path, n))


@router.get("/cube/{tile_id}/cube3d.png", tags=["L2 y cubo all-sky"], summary="Vista 3D estática de los 102 planos (cube3d.png)",
            responses=responses(None, 404, png=True))
async def cube_3d(tile_id: str = TID):
    _, path = await _cube_path(tile_id)
    return png(await asyncio.to_thread(CU.cube3d_png, path))


@router.get("/cube/{tile_id}/fits", tags=["L2 y cubo all-sky"], summary="Descargar la tesela FITS original (redirige a IRSA)",
            responses={307: {"description": "Redirección a IRSA"}, **responses(None, 404)})
async def cube_fits(tile_id: str = TID):
    return RedirectResponse(CU.get_tile(tile_id)["url"], status_code=307)
