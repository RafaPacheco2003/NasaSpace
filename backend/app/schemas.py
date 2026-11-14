"""Esquemas de respuesta para Swagger/OpenAPI (/docs).

Solo documentan la forma de las respuestas (con ejemplos reales de SPHEREx DR1);
los endpoints devuelven JSON directamente, así que no alteran la salida.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


def _ex(example: dict) -> ConfigDict:
    return ConfigDict(json_schema_extra={"example": example})


# --- Comunes ----------------------------------------------------------------


class ErrorBody(BaseModel):
    code: str = Field(description="bad_request · not_found · rate_limited · upstream_error · upstream_timeout · internal_error")
    message: str = Field(description="Texto legible para mostrar al usuario")


class Error(BaseModel):
    """Forma única de todos los errores."""
    model_config = _ex({"error": {"code": "bad_request", "message": "radius supera el máximo (600)"}})
    error: ErrorBody


class Center(BaseModel):
    ra: float = Field(description="Ascensión recta (grados, ICRS)")
    dec: float = Field(description="Declinación (grados, ICRS)")


class ProperMotion(BaseModel):
    ra: Optional[float] = Field(None, description="μα·cosδ en mas/año")
    dec: Optional[float] = Field(None, description="μδ en mas/año")


class PartialMixin(BaseModel):
    partial: bool = Field(False, description="true si falló alguna de las fuentes pedidas (el resto sí respondió)")
    warnings: list[str] = Field(default_factory=list, description="Motivo de cada fallo parcial")
    cache: Literal["hit", "miss"] = Field("miss", description="hit = servido desde la caché de 24 h")


# --- Catálogos --------------------------------------------------------------


class CurrentPosition(BaseModel):
    ra: float
    dec: float
    epoch: float = Field(description="Fecha de la posición en años julianos (p. ej. 2026.868)")


class ResolveResult(BaseModel):
    """Nombre → coordenadas. Fuente externa: SIMBAD (Sesame) y, si no está, NED."""
    model_config = _ex({
        "query": "WISE 0855", "matched_name": "WISEA J085510.74-071442.5",
        "ra": 133.81890825, "dec": -7.247048, "source": "simbad", "epoch": 2000.0,
        "pm": {"ra": -8123.7, "dec": 673.2}, "parallax_mas": 439.0,
        "current": {"ra": 133.75790575, "dec": -7.24205776, "epoch": 2026.868}, "object_type": "BD*",
    })
    query: str = Field(description="Texto buscado tal cual")
    matched_name: str = Field(description="Nombre principal en el catálogo")
    ra: float = Field(description="Posición de catálogo (época `epoch`)")
    dec: float
    source: Literal["simbad", "ned"]
    epoch: float = Field(description="Época de ra/dec (SIMBAD/NED: 2000.0)")
    pm: Optional[ProperMotion] = Field(None, description="Movimiento propio (null si no se conoce)")
    parallax_mas: Optional[float] = Field(None, description="Paralaje en milisegundos de arco")
    current: CurrentPosition = Field(description="Posición propagada a `at` (pm + paralaje). USAR ESTA para centrar las imágenes")
    object_type: Optional[str] = Field(None, description="(extra) Tipo SIMBAD/NED, p. ej. BD* = enana marrón")


class SkyObject(BaseModel):
    id: str = Field(description="'<fuente>:<nombre>'")
    name: str
    source: Literal["simbad", "ned"]
    ra: float
    dec: float
    type: Literal["star", "galaxy", "other"]
    type_raw: str = Field(description="Tipo original del catálogo (otype de SIMBAD / prefphytype de NED)")
    z: Optional[float] = Field(None, description="Corrimiento al rojo")
    distance_arcsec: float = Field(description="Distancia al centro de la búsqueda (″)")
    pm: Optional[ProperMotion] = None


class ObjectsResult(PartialMixin):
    """Objetos conocidos en un cono. Fuentes externas: SIMBAD TAP y NED TAP."""
    model_config = _ex({
        "center": {"ra": 133.757778, "dec": -7.241944}, "radius_arcsec": 120.0, "counts": {"simbad": 1, "ned": 55},
        "objects": [{"id": "ned:WISEA J085501.95-071428.1", "name": "WISEA J085501.95-071428.1", "source": "ned",
                     "ra": 133.758195, "dec": -7.241165, "type": "other", "type_raw": "", "z": None,
                     "distance_arcsec": 3.177, "pm": None}],
        "partial": False, "warnings": [], "cache": "hit",
    })
    center: Center
    radius_arcsec: float
    counts: dict[str, int] = Field(description="Objetos encontrados por catálogo (tras filtrar por tipo)")
    objects: list[SkyObject] = Field(description="Ordenados por distance_arcsec ascendente")


# --- Imágenes ---------------------------------------------------------------


class Frame(BaseModel):
    id: str = Field(description="sx-d<detector>-<t_inicio en ms Unix>")
    date_obs: str = Field(description="Inicio de la exposición (ISO 8601 UTC)")
    detector: int = Field(ge=1, le=6, description="1: 0.75–1.09 µm … 6: 4.42–5.00 µm")
    band_um: Optional[float] = Field(None, description="Longitud de onda en el punto pedido (µm)")
    band_exact: bool = Field(description="(extra) true = medida en el FITS (WCS-WAVE); false = centro del detector")
    center: Center = Field(description="Centro de apuntado de la exposición completa")
    fov_arcsec: float = Field(description="Lado de la exposición completa (~12648″ = 3.5°)")
    exposure_s: Optional[float] = None
    flags: list[str] = Field(description="Banderas de contaminación en el píxel del objetivo (enum abierto)")
    obs_id: Optional[str] = Field(None, description="(extra) Identificador de observación de IRSA")


FRAME_EXAMPLE = {
    "id": "sx-d6-1746463682419", "date_obs": "2025-05-05T16:48:02.419Z", "detector": 6, "band_um": 4.9674,
    "band_exact": True, "center": {"ra": 134.40997823, "dec": -9.02337658}, "fov_arcsec": 12648.0,
    "exposure_s": 113.5826, "flags": [], "obs_id": "2025W19_1B_0116_1",
}


class FramesResult(PartialMixin):
    """Metadatos de las exposiciones. Fuente externa: IRSA SIA v2 (SPHEREx DR1)."""
    model_config = _ex({"center": {"ra": 133.757778, "dec": -7.241944}, "fov_arcsec": 300.0, "total": 45,
                        "limit": 1, "offset": 0, "frames": [FRAME_EXAMPLE], "partial": False, "warnings": [],
                        "cache": "hit"})
    center: Center
    fov_arcsec: float
    total: int = Field(description="Total de frames (no solo esta página)")
    limit: int
    offset: int
    frames: list[Frame] = Field(description="Ordenados por fecha")


class SequenceFrame(Frame):
    png_url: str = Field(description="URL del PNG con toda la query. Usar tal cual")
    flags_png_url: Optional[str] = Field(None, description="(extra) Capa PNG con los píxeles con banderas")
    vmin: Optional[float] = None
    vmax: Optional[float] = None


class SequenceResult(BaseModel):
    """Secuencia alineada para el blink. Fuente externa: IRSA (recortes FITS de SPHEREx)."""
    model_config = _ex({
        "center": {"ra": 133.757778, "dec": -7.241944}, "fov_arcsec": 300.0, "size_px": 256, "scale": "sequence",
        "vmin": -0.1146, "vmax": 0.7778, "unit": "MJy/sr", "total": 45, "limit": 6, "offset": 0,
        "frames": [{**FRAME_EXAMPLE, "vmin": -0.1146, "vmax": 0.7778,
                    "png_url": "/api/frame/sx-d6-1746463682419.png?ra=133.757778&dec=-7.241944&fov=300&size=256&stretch=asinh&cmap=gray&vmin=-0.114633&vmax=0.777781",
                    "flags_png_url": "/api/frame/sx-d6-1746463682419/flags.png?ra=133.757778&dec=-7.241944&fov=300&size=256"}],
        "partial": False, "warnings": [],
    })
    center: Center
    fov_arcsec: float
    size_px: int
    scale: Literal["sequence", "frame"]
    vmin: float = Field(description="Límite inferior de brillo común (MJy/sr)")
    vmax: float
    unit: str = "MJy/sr"
    total: int
    limit: int
    offset: int
    frames: list[SequenceFrame]
    partial: bool = False
    warnings: list[str] = []


class StackResult(BaseModel):
    """Apilado o resta de épocas. Fuente externa: IRSA (recortes FITS de SPHEREx)."""
    model_config = _ex({"op": "diff", "n_frames": 6, "center": {"ra": 269.446111, "dec": 4.770556}, "fov_arcsec": 120.0,
                        "size_px": 256, "vmin": -3.2164, "vmax": 3.2164, "unit": "MJy/sr",
                        "png_url": "/api/stack/stk_cc6f162d39c8.png", "partial": False, "warnings": []})
    op: Literal["mean", "median", "sum", "diff"]
    n_frames: int
    center: Center
    fov_arcsec: float
    size_px: int
    vmin: float
    vmax: float = Field(description="En diff la escala es simétrica: vmin = −vmax")
    unit: str = "MJy/sr"
    png_url: str
    partial: bool = False
    warnings: list[str] = []


class CoverageWeek(BaseModel):
    week_start: str = Field(description="Lunes de la semana (YYYY-MM-DD)")
    count: int


class CoverageResult(BaseModel):
    """Exposiciones por semana. Fuente externa: IRSA SIA v2."""
    model_config = _ex({"center": {"ra": 133.757778, "dec": -7.241944}, "first_date": "2025-05-05",
                        "last_date": "2026-05-14", "weeks": [{"week_start": "2025-05-05", "count": 30},
                                                             {"week_start": "2025-10-27", "count": 24}],
                        "total": 258, "cache": "hit"})
    center: Center
    first_date: Optional[str]
    last_date: Optional[str]
    weeks: list[CoverageWeek]
    total: Optional[int] = None
    cache: Optional[str] = None


# --- Trabajos ---------------------------------------------------------------


class Job(BaseModel):
    """Trabajo de un proceso lento. Consultar /api/jobs/{id} cada poll_after_ms."""
    model_config = _ex({"id": "seq_03900b21fbf0", "kind": "sequence", "status": "running", "progress": 0.485,
                        "eta_s": 9, "stage": "Descargando frames 3/6", "queue_position": 0, "poll_after_ms": 800,
                        "created_at": "2026-11-14T17:34:13Z", "result": None, "error": None})
    id: str = Field(description="Determinista: la misma consulta → el mismo id")
    kind: Literal["sequence", "stack", "predict", "spectrum"]
    status: Literal["queued", "running", "done", "failed"]
    progress: float = Field(ge=0, le=1)
    eta_s: Optional[int] = Field(None, description="Segundos estimados restantes")
    stage: str = Field(description="Texto para la tarjeta de progreso")
    queue_position: int = Field(description="0 = en ejecución; ≥1 = posición en la cola")
    poll_after_ms: int
    created_at: Optional[str]
    result: Optional[Any] = Field(None, description="Con status=done: el mismo objeto que daría el 200 del endpoint")
    error: Optional[ErrorBody] = Field(None, description="Con status=failed")


# --- Análisis ---------------------------------------------------------------


class PredictPosition(BaseModel):
    date: str
    ra: float
    dec: float
    sigma_arcsec: Optional[float] = Field(None, description="Incertidumbre 1σ de la posición")


class PredictResult(BaseModel):
    """Posición en cada fecha. Fuentes externas: SIMBAD (pm/paralaje), JPL Horizons (efemérides), IRSA (fechas)."""
    model_config = _ex({"target": "Barnard's star", "method": "proper_motion", "pm_mas_yr": {"ra": -801.551, "dec": 10362.394},
                        "parallax_mas": 546.9759, "catalog_epoch": 2000.0,
                        "positions": [{"date": "2025-08-15T20:45:11.376Z", "ra": 269.446229, "dec": 4.7671599, "sigma_arcsec": 0.001}],
                        "warnings": []})
    target: str
    method: Literal["proper_motion", "ephemeris", "static"]
    pm_mas_yr: Optional[ProperMotion] = None
    positions: list[PredictPosition]
    parallax_mas: Optional[float] = None
    catalog_epoch: Optional[float] = None
    warnings: list[str] = []


class SpectrumPoint(BaseModel):
    wavelength: float = Field(description="µm (exacta, del WCS-WAVE del FITS)")
    flux: float = Field(description="µJy (fotometría de apertura menos fondo)")
    error: float = Field(description="µJy")
    date_obs: str = Field(description="Fecha de la exposición → ordenando por fecha es una curva de luz")
    detector: int
    frame_id: str
    flags: list[str]
    wavelength_exact: Optional[bool] = None


class SpectrumResult(PartialMixin):
    """Espectro 0.75–5 µm. Fuente externa: IRSA (una exposición de SPHEREx = un punto)."""
    model_config = _ex({"center": {"ra": 269.446111, "dec": 4.770556}, "aperture_arcsec": 12.0, "wavelength_unit": "um",
                        "flux_unit": "uJy", "points": [{"wavelength": 0.7431, "flux": 1440043.44, "error": 5960.26,
                                                        "date_obs": "2026-03-11T02:11:37.219Z", "detector": 1,
                                                        "frame_id": "sx-d1-1773195097219",
                                                        "flags": ["bloom", "cosmic_ray", "saturation"], "wavelength_exact": True}],
                        "n_exposures": 239, "partial": True, "warnings": ["sx-d6-1756083361046: spherex: HTTP 503"],
                        "cache": "miss"})
    center: Center
    aperture_arcsec: float
    wavelength_unit: str = "um"
    flux_unit: str = "uJy"
    points: list[SpectrumPoint] = Field(description="Ordenados por longitud de onda")
    n_exposures: Optional[int] = None


class OverlaySource(BaseModel):
    id: str
    ra: float = Field(description="Posición propagada a `at`")
    dec: float
    mag: Optional[float] = Field(None, description="G (Gaia) o W1 (WISE)")
    pm: Optional[ProperMotion] = None
    catalog_ra: Optional[float] = Field(None, description="(extra) Posición original del catálogo")
    catalog_dec: Optional[float] = None


class OverlayResult(PartialMixin):
    """Fuentes para dibujar sobre la imagen. Fuentes externas: Gaia DR3 (ESA) o AllWISE (IRSA)."""
    model_config = _ex({"catalog": "gaia", "center": {"ra": 269.446111, "dec": 4.769444}, "fov_arcsec": 300.0,
                        "epoch": 2026.413, "catalog_epoch": 2016.0, "mag_band": "G",
                        "sources": [{"id": "Gaia DR3 4472832130942575872", "ra": 269.44622734, "dec": 4.76946331,
                                     "mag": 8.194, "pm": {"ra": -801.551, "dec": 10362.394},
                                     "catalog_ra": 269.44850253, "catalog_dec": 4.73942005}],
                        "partial": False, "warnings": [], "cache": "hit"})
    catalog: Literal["gaia", "wise"]
    center: Center
    fov_arcsec: float
    epoch: float = Field(description="Época de las posiciones devueltas (= at)")
    catalog_epoch: Optional[float] = Field(None, description="(extra) Época original del catálogo")
    mag_band: Optional[str] = None
    sources: list[OverlaySource]


class SourceStatus(BaseModel):
    ok: bool
    latency_ms: Optional[int] = None
    last_error: Optional[str] = None


class StatusResult(BaseModel):
    """Estado de la API y de cada fuente externa."""
    model_config = _ex({"status": "ok", "version": "0.3.0", "data_release": "SPHEREx DR1 (IRSA, colección spherex_dr1)",
                        "spherex_mode": "irsa", "sources": {"spherex": {"ok": True, "latency_ms": 383, "last_error": None},
                                                           "simbad": {"ok": True, "latency_ms": 535, "last_error": None}}})
    status: Literal["ok", "degraded"]
    version: str
    data_release: str
    spherex_mode: Optional[str] = None
    sources: dict[str, SourceStatus] = Field(description="spherex, simbad, ned, gaia, wise, horizons")


# --- Bloques de respuestas para los decoradores ---------------------------------

ERR = {
    400: {"model": Error, "description": "Falta un parámetro, coordenadas fuera de rango o límites excedidos"},
    404: {"model": Error, "description": "No se encontró el nombre, el frame o el trabajo (expirado)"},
    429: {"model": Error, "description": "Demasiadas peticiones desde la misma IP (cabecera Retry-After)"},
    502: {"model": Error, "description": "Fallaron todas las fuentes pedidas"},
    504: {"model": Error, "description": "Se agotó el tiempo en todas las fuentes pedidas"},
}
PNG = {200: {"content": {"image/png": {}}, "description": "Imagen PNG (TAN, norte arriba, este a la izquierda)"}}


def responses(model=None, *codes: int, job: bool = False, png: bool = False) -> dict:
    out: dict = {}
    if png:
        out.update(PNG)
    elif model is not None:
        out[200] = {"model": model, "description": "Resultado (en caché si es un proceso lento)"}
    if job:
        out[202] = {"model": Job, "description": "Trabajo creado o en curso; consultar /api/jobs/{id}"}
    for c in codes:
        out[c] = ERR[c]
    return out
