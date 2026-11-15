# SI-assisted development used for code development, debugging and documentation.
# Tool: ChatGPT, Claude

"""Catálogos: /api/resolve y /api/objects."""
from __future__ import annotations

import asyncio
from datetime import date

from fastapi import APIRouter, Query

from ..astro import jyear, propagate, to_time
from ..cache import cached, key, r1
from ..config import MAX_OBJECTS_LIMIT, MAX_RADIUS
from ..errors import UpstreamError, bad_request, upstream_failure
from ..sources import catalogs as cat
from ..sources import resolver
from ..utils import json_response, parse_list
from ..schemas import ObjectsResult, ResolveResult, responses
from .common import AT, DEC, RA

router = APIRouter(prefix="/api", tags=["Catálogos"])


@router.get("/resolve", summary="Resolver un nombre a coordenadas",
            responses=responses(ResolveResult, 400, 404, 429, 502, 504))
async def resolve(
    name: str = Query(..., max_length=200, examples=["Barnard's star"]),
    at: date | None = AT,
):
    """**Para qué sirve:** el usuario escribe un nombre (`Barnard's star`, `WISE 0855`, `RW Aur`…) y obtiene
    dónde está en el cielo.

    **API externa:** SIMBAD (vía Sesame de CDS); si no lo encuentra, NED (ObjectLookup).

    `ra/dec` son la posición de catálogo (época `epoch`). `current` es la posición propagada a la fecha `at`
    con movimiento propio + paralaje: **es la que se debe usar para centrar las imágenes**."""
    q = name.strip()
    if not q:
        raise bad_request("Falta el parámetro obligatorio 'name'.")
    info, _ = await cached(key("resolve", resolver.normalize(q)), lambda: resolver.resolve_name(q))
    t = to_time(at)
    pm = info.get("pm")
    cra, cdec = propagate(
        info["ra"], info["dec"], pm["ra"] if pm else 0.0, pm["dec"] if pm else 0.0, info["epoch"], t,
        plx=info.get("parallax_mas"),
    )
    return json_response({
        "query": q,
        "matched_name": info["matched_name"],
        "ra": info["ra"],
        "dec": info["dec"],
        "source": info["source"],
        "epoch": info["epoch"],
        "pm": pm,
        "parallax_mas": info.get("parallax_mas"),
        "current": {"ra": round(float(cra), 8), "dec": round(float(cdec), 8), "epoch": jyear(t)},
        "object_type": info.get("otype"),
    })


@router.get("/objects", summary="Objetos de SIMBAD y NED en una región (búsqueda de cono)",
            responses=responses(ObjectsResult, 400, 429, 502, 504))
async def objects(
    ra: float = RA,
    dec: float = DEC,
    radius: float = Query(60, gt=0, le=MAX_RADIUS, description="Radio en segundos de arco."),
    catalogs: list[str] | None = Query(None, description="simbad, ned (por defecto ambos)"),
    types: list[str] | None = Query(None, description="star, galaxy, other (por defecto todos)"),
    limit: int = Query(200, ge=1, le=MAX_OBJECTS_LIMIT),
):
    """**Para qué sirve:** marcar sobre la imagen qué puntos ya son conocidos: estrellas (SIMBAD) y galaxias o
    cuásares de fondo (NED). Lo que se mueve y no aparece aquí es candidato a descubrimiento.

    **APIs externas:** SIMBAD TAP (tabla `basic`, espejo de Harvard como reintento) y NED TAP (`NEDTAP.objdir`).
    Si falla solo una → 200 con `partial: true`."""
    cats = parse_list(catalogs, {"simbad", "ned"}, ["simbad", "ned"], "catalogs")
    typs = set(parse_list(types, {"star", "galaxy", "other"}, ["star", "galaxy", "other"], "types"))
    rra, rdec = r1(ra), r1(dec)
    fetchers = {"simbad": cat.simbad_cone, "ned": cat.ned_cone}

    async def run(src):
        return await cached(key("objects", src, rra, rdec, float(radius)), lambda: fetchers[src](rra, rdec, radius))

    results = await asyncio.gather(*(run(s) for s in cats), return_exceptions=True)
    objs, counts, warnings, errors, hits = [], {}, [], [], []
    for src, res in zip(cats, results):
        if isinstance(res, UpstreamError):
            errors.append(res)
            warnings.append(f"{src}: {res.message}")
            continue
        if isinstance(res, BaseException):
            raise res
        lst, hit = res
        hits.append(hit)
        sel = [o for o in lst if o["type"] in typs]
        counts[src] = len(sel)
        objs.extend(sel)
    if errors and len(errors) == len(cats):
        raise upstream_failure(errors)
    objs.sort(key=lambda o: o["distance_arcsec"])
    public = ("id", "name", "source", "ra", "dec", "type", "type_raw", "z", "distance_arcsec", "pm")
    return json_response({
        "center": {"ra": rra, "dec": rdec},
        "radius_arcsec": radius,
        "counts": counts,
        "objects": [{k: o.get(k) for k in public} for o in objs[:limit]],
        "partial": bool(errors),
        "warnings": warnings,
        "cache": "hit" if hits and all(hits) else "miss",
    })
