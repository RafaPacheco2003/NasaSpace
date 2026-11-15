"""Sistema: /api/status."""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter

from ..config import settings
from ..errors import UpstreamError
from ..http import health, request
from ..jobs import jobs
from ..schemas import StatusResult
from ..utils import json_response

router = APIRouter(prefix="/api", tags=["Sistema"])

PING_URLS = {
    "spherex": "https://irsa.ipac.caltech.edu/SIA?FORMAT=METADATA",
    "simbad": "https://simbad.cds.unistra.fr/simbad/sim-tap/availability",
    "ned": "https://ned.ipac.caltech.edu/tap/availability",
    "gaia": "https://gea.esac.esa.int/tap-server/tap/availability",
    "wise": "https://irsa.ipac.caltech.edu/TAP/availability",
    "horizons": "https://ssd.jpl.nasa.gov/api/horizons.api",
}
FRESH_S = 300


async def _ping(source: str) -> None:
    try:
        await request(source, "GET", PING_URLS[source], timeout=5, retries=0, raise_4xx=False)
    except UpstreamError:
        pass  # request() ya registró el fallo en `health`


@router.get("/status", summary="Estado de la API, versión de datos y fuentes externas",
            responses={200: {"model": StatusResult, "description": "Estado actual"}})
async def status():
    """**Para qué sirve:** mostrar en el Frontend si todo funciona y qué versión de datos se usa
    (`data_release`). Comprueba cada API externa (cada 5 min como mucho): spherex/wise (IRSA),
    simbad (CDS), ned (NASA/IPAC), gaia (ESA), horizons (JPL)."""
    now = time.time()
    names = list(PING_URLS)
    if settings.spherex_mode == "mock":
        health.record("spherex", True, 0)
    stale = [s for s in names if now - health.data.get(s, {}).get("checked_at", 0) > FRESH_S]
    if stale:
        await asyncio.gather(*(_ping(s) for s in stale))
    sources = {
        s: {
            "ok": health.data.get(s, {}).get("ok", False),
            "latency_ms": health.data.get(s, {}).get("latency_ms"),
            "last_error": health.data.get(s, {}).get("last_error"),
        }
        for s in names
    }
    all_ok = all(v["ok"] for v in sources.values())
    return json_response({
        "status": "ok" if all_ok else "degraded",
        "version": settings.version,
        "data_release": settings.data_release + (" [MODO MOCK: datos sintéticos]" if settings.spherex_mode == "mock" else ""),
        "spherex_mode": settings.spherex_mode,
        "sources": sources,
        "jobs": jobs.summary(),
    })
