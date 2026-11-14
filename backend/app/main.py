"""SPHEREx Sky API — Backend del SPHEREx Explorer (FastAPI)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import RedirectResponse

from . import errors, http
from .config import settings
from .ratelimit import rate_limit_middleware
from .routers import analysis, catalogs, images, system

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for _noisy in ("reproject", "reproject.common", "httpx"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

DESCRIPTION = """
Backend del **SPHEREx Explorer**: comparador de parpadeo (blink) + espectrómetro para las imágenes de la
misión SPHEREx de la NASA (102 bandas, 0.75–5 µm, todo el cielo cada 6 meses).

El navegador solo llama a esta API. La API consulta estas **APIs externas** (públicas, sin clave):

| API externa | Organización | Se usa en |
|---|---|---|
| **IRSA** SIA v2 + cutouts (SPHEREx DR1) | NASA/Caltech | frames, coverage, sequence, frame PNG, stack, spectrum, predict |
| **SIMBAD** (Sesame + TAP, espejo Harvard) | CDS Estrasburgo | resolve, objects, predict |
| **NED** (ObjectLookup + TAP) | NASA/IPAC | resolve, objects |
| **Gaia DR3** (TAP) | ESA | overlay (catalog=gaia) |
| **AllWISE** (IRSA TAP / Gator) | NASA/IPAC | overlay (catalog=wise) |
| **JPL Horizons** | NASA JPL | predict (asteroides y cometas) |

**Reglas:** solo GET, sin autenticación, CORS abierto. Coordenadas en grados (ICRS/J2000); radio, fov y apertura
en segundos de arco. Timeout 10 s por fuente externa con 1 reintento. Caché 24 h (coordenadas redondeadas a 1″).
Si falla solo una de las fuentes pedidas → 200 con `partial: true`.

**Procesos lentos** (`sequence`, `stack`, `spectrum`, `predict`): 200 si el resultado está en caché; si no,
**202 + Job** → consultar `GET /api/jobs/{id}` cada `poll_after_ms` hasta `done` o `failed`.

**Errores:** siempre `{"error": {"code", "message"}}`.

**Prueba rápida:** WISE 0855 → `ra=133.757901`, `dec=-7.242058` · Barnard → `ra=269.446227`, `dec=4.769463`.
"""


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.getLogger("skyapi").info("Arrancando (SPHEREX_MODE=%s, colección=%s)", settings.spherex_mode, settings.spherex_collection)
    yield
    await http.aclose()


app = FastAPI(
    title="SpaceX Sky API",
    version=settings.version,
    description=DESCRIPTION,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "Catálogos", "description": "Nombres y objetos conocidos (SIMBAD, NED)"},
        {"name": "Imágenes", "description": "Imágenes de SPHEREx: metadatos, secuencia (blink), PNG, apilado y resta (IRSA)"},
        {"name": "Trabajos", "description": "Estado de los procesos lentos (patrón 202 + trabajo)"},
        {"name": "Análisis", "description": "Espectro, predicción de posiciones y overlays (IRSA, SIMBAD, Horizons, Gaia, WISE)"},
        {"name": "Sistema", "description": "Salud de la API y de las APIs externas"},
    ],
)

errors.install(app)
app.middleware("http")(rate_limit_middleware)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Retry-After"],
)

def custom_openapi():
    """OpenAPI sin las respuestas 422 de FastAPI: esta API devuelve 400 con la forma de error del contrato."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, description=app.description,
                         routes=app.routes, tags=app.openapi_tags)
    for path in schema.get("paths", {}).values():
        for op in path.values():
            op.get("responses", {}).pop("422", None)
    comps = schema.get("components", {}).get("schemas", {})
    comps.pop("HTTPValidationError", None)
    comps.pop("ValidationError", None)
    schema["servers"] = [{"url": "/", "description": "Este servidor"},
                         {"url": "http://localhost:8000", "description": "Local"}]
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi  # type: ignore[method-assign]

app.include_router(catalogs.router)
app.include_router(images.router)
app.include_router(analysis.router)
app.include_router(system.router)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/docs")


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"ok": True}
