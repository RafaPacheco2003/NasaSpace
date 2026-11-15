# SI-assisted development used for code development, debugging and documentation.
# Tool: ChatGPT, Claude

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
from .routers import analysis, asteroids, catalogs, images, products, system

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for _noisy in ("reproject", "reproject.common", "httpx"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

DESCRIPTION = """
Backend del **SPHEREx Explorer**: comparador de parpadeo (blink) + espectrómetro para las imágenes de la
misión SPHEREx de la NASA (102 bandas, 0.75–5 µm, todo el cielo cada 6 meses).

El navegador solo llama a esta API. La API consulta estas **APIs externas** (públicas, sin clave):

| API externa | Organización | Se usa en |
|---|---|---|
| **IRSA** SIA v2 + cutouts (SPHEREx DR1) + MOST | NASA/Caltech | frames, coverage, sequence, frame PNG, stack, spectrum, predict, asteroids |
| **SIMBAD** (Sesame + TAP, espejo Harvard) | CDS Estrasburgo | resolve, objects, predict |
| **NED** (ObjectLookup + TAP) | NASA/IPAC | resolve, objects |
| **Gaia DR3** (TAP) | ESA | overlay (catalog=gaia), asteroids (confusión con estrellas) |
| **AllWISE** (IRSA TAP / Gator) | NASA/IPAC | overlay (catalog=wise) |
| **JPL Horizons** | NASA JPL | predict (asteroides y cometas), asteroids (observador SPHEREx) |

**Reglas:** GET (salvo `POST /api/asteroids/predict-from-current`), sin autenticación, CORS abierto. Coordenadas en grados (ICRS/J2000); radio, fov y apertura
en segundos de arco. Timeout 10 s por fuente externa con 1 reintento. Caché 24 h (coordenadas redondeadas a 1″).
Si falla solo una de las fuentes pedidas → 200 con `partial: true`.

**Procesos lentos** (`sequence`, `stack`, `spectrum`, `predict`, `asteroids/predict-from-current`, `l2`, `cube`): 200 si el resultado está en caché; si no,
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
        {"name": "L2 y cubo all-sky", "description": "Pipeline SphereX: L2 completo (raw, color, clic → RA/Dec + brillo) y cubo all-sky (102 canales, 6 canales, espectro, 3D)"},
        {"name": "Asteroides", "description": "Dónde estará un asteroide (JPL Horizons, observador SPHEREx) y en qué píxel de la imagen"},
        {"name": "Sistema", "description": "Salud de la API y de las APIs externas"},
    ],
)

errors.install(app)
app.middleware("http")(rate_limit_middleware)

_access = logging.getLogger("skyapi.clientes")


@app.middleware("http")
async def log_browser_origin(request, call_next):
    """Registra desde qué página (Origin) llama cada navegador: identifica al Frontend aunque Docker oculte la IP."""
    response = await call_next(request)
    origin = request.headers.get("origin")
    if origin and request.url.path.startswith("/api/") and request.method != "OPTIONS":
        _access.info("origen=%s %s %s → %s", origin, request.method, request.url.path, response.status_code)
    return response
app.add_middleware(GZipMiddleware, minimum_size=1024)
# CORS abierto a TODO: cualquier origen, método y cabecera; el navegador puede leer todas las cabeceras de respuesta.
# (Sin cookies/credenciales: la API no usa autenticación, así que "*" es válido.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,          # el navegador recuerda el preflight 24 h (menos peticiones OPTIONS)
)


class PrivateNetworkAccess:
    """Chrome (Private Network Access): una página pública (https://…vercel.app, ngrok) que llama a una IP local
    (http://192.168.x.x:8000) primero manda un preflight con `Access-Control-Request-Private-Network: true` y exige
    `Access-Control-Allow-Private-Network: true` en la respuesta. Va por FUERA de CORSMiddleware."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "OPTIONS":
            return await self.app(scope, receive, send)
        wants = any(k.lower() == b"access-control-request-private-network" for k, _ in scope.get("headers", []))

        async def send_wrapper(message):
            if wants and message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"access-control-allow-private-network", b"true"))
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(PrivateNetworkAccess)

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
app.include_router(asteroids.router)
app.include_router(products.router)
app.include_router(system.router)


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def root():
    return RedirectResponse("/docs")


@app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
async def healthz():
    return {"ok": True}
