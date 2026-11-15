# SI-assisted development used for code development, debugging and documentation.
# Tool: ChatGPT, Claude

"""Configuración del servicio. Todo se puede sobreescribir con variables de entorno."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


def _float(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "si", "sí")


@dataclass(frozen=True)
class Settings:
    version: str = "0.3.0"

    # --- Datos SPHEREx --------------------------------------------------
    # irsa = datos reales de IRSA · mock = datos sintéticos (sin red, para desarrollo/demos)
    spherex_mode: str = _str("SPHEREX_MODE", "irsa").lower()
    # Colecciones SIA v2 de IRSA (separadas por coma). Cambiar aquí al salir un nuevo data release.
    spherex_collection: str = _str("SPHEREX_COLLECTION", "spherex_dr1")
    data_release: str = _str("DATA_RELEASE", "SPHEREx DR1 (IRSA, colección spherex_dr1)")
    irsa_sia_url: str = _str("IRSA_SIA_URL", "https://irsa.ipac.caltech.edu/SIA")
    irsa_tap_url: str = _str("IRSA_TAP_URL", "https://irsa.ipac.caltech.edu/TAP/sync")
    # Gator (servicio de catálogos de IRSA): respaldo para WISE si el TAP de IRSA falla
    irsa_gator_url: str = _str("IRSA_GATOR_URL", "https://irsa.ipac.caltech.edu/cgi-bin/Gator/nph-query")
    zodi_subtract: bool = _bool("ZODI_SUBTRACT", True)
    # Rellenar píxeles defectuosos (rayos cósmicos, calientes, NaN…) con la media de sus vecinos buenos
    clean_bad_pixels: bool = _bool("CLEAN_BAD_PIXELS", True)
    spherex_search_timeout_s: float = _float("SPHEREX_SEARCH_TIMEOUT_S", 20)
    # IRSA devuelve 502 intermitentes: más reintentos (con espera creciente) para la búsqueda de frames
    spherex_search_retries: int = _int("SPHEREX_SEARCH_RETRIES", 2)
    download_timeout_s: float = _float("DOWNLOAD_TIMEOUT_S", 60)
    download_concurrency: int = _int("DOWNLOAD_CONCURRENCY", 6)
    max_spectrum_frames: int = _int("MAX_SPECTRUM_FRAMES", 600)

    # --- Catálogos externos --------------------------------------------
    sesame_url: str = _str("SESAME_URL", "https://cds.unistra.fr/cgi-bin/nph-sesame")
    simbad_tap_url: str = _str("SIMBAD_TAP_URL", "https://simbad.cds.unistra.fr/simbad/sim-tap/sync")
    # Espejo oficial de SIMBAD (Harvard/CfA): se usa como reintento si el principal falla
    simbad_tap_mirror: str = _str("SIMBAD_TAP_MIRROR", "https://simbad.harvard.edu/simbad/sim-tap/sync")
    ned_tap_url: str = _str("NED_TAP_URL", "https://ned.ipac.caltech.edu/tap/sync")
    ned_lookup_url: str = _str("NED_LOOKUP_URL", "https://ned.ipac.caltech.edu/srs/ObjectLookup")
    gaia_tap_url: str = _str("GAIA_TAP_URL", "https://gea.esac.esa.int/tap-server/tap/sync")
    horizons_url: str = _str("HORIZONS_URL", "https://ssd.jpl.nasa.gov/api/horizons.api")
    # allwise (posiciones época 2010.56, pm en mas/año) o catwise (época 2015.4, pm en ″/año)
    wise_catalog: str = _str("WISE_CATALOG", "allwise").lower()
    overlay_max_sources: int = _int("OVERLAY_MAX_SOURCES", 5000)

    # --- Asteroides (POST /api/asteroids/predict-from-current, JPL Horizons) ---
    # Observador de Horizons: SPHEREx (-163182). Desde el geocentro la posición difiere ~1 píxel para asteroides.
    horizons_spherex_center: str = _str("HORIZONS_SPHEREX_CENTER", "500@-163182")
    asteroid_identity_tol_arcsec: float = _float("ASTEROID_IDENTITY_TOL_ARCSEC", 60)   # clic vs efeméride

    # --- Productos completos del pipeline (L2 entero ≈ 65 MB, tesela de cubo ≈ 320 MB) ---
    product_cache_gb: float = _float("PRODUCT_CACHE_GB", 3)          # disco máximo para FITS completos (LRU)
    product_download_workers: int = _int("PRODUCT_DOWNLOAD_WORKERS", 4)  # conexiones con rangos HTTP (1–8)

    # --- Red / tiempos --------------------------------------------------
    external_timeout_s: float = _float("EXTERNAL_TIMEOUT_S", 10)
    external_retries: int = _int("EXTERNAL_RETRIES", 1)
    user_agent: str = _str("USER_AGENT", "spherex-sky-api/0.3 (hackathon project)")

    # --- Caché ------------------------------------------------------------
    cache_dir: str = _str("CACHE_DIR", "/data/cache")
    cache_ttl_s: int = _int("CACHE_TTL_S", 24 * 3600)
    result_ttl_s: int = _int("RESULT_TTL_S", 7 * 24 * 3600)           # resultados de trabajos (DR1 no cambia)
    cache_size_limit_gb: float = _float("CACHE_SIZE_LIMIT_GB", 5)

    # --- Trabajos (202 + job) ---------------------------------------------
    max_concurrent_jobs: int = _int("MAX_CONCURRENT_JOBS", 4)
    max_heavy_jobs: int = _int("MAX_HEAVY_JOBS", 1)                  # cubo/L2 (descargas de 65–320 MB) a la vez
    job_orphan_queued_s: int = _int("JOB_ORPHAN_QUEUED_S", 30)       # en cola sin consultas → se pausa
    job_orphan_running_s: int = _int("JOB_ORPHAN_RUNNING_S", 90)     # corriendo sin consultas → se pausa (0 = nunca)
    job_ttl_s: int = _int("JOB_TTL_S", 3600)
    failed_job_retry_s: int = _int("FAILED_JOB_RETRY_S", 30)

    # --- Límite de peticiones -----------------------------------------
    rate_limit_per_min: int = _int("RATE_LIMIT_PER_MIN", 120)
    rate_limit_burst: int = _int("RATE_LIMIT_BURST", 40)
    trust_proxy: bool = _bool("TRUST_PROXY", False)


settings = Settings()

# Límites de validación (400 si se exceden)
MAX_RADIUS = 600          # ″
MIN_FOV, MAX_FOV = 10, 3600  # ″
MIN_SIZE, MAX_SIZE = 64, 1024  # px
MAX_IDS = 100
MAX_SEQUENCE_LIMIT = 200
MAX_FRAMES_LIMIT = 1000
MAX_OBJECTS_LIMIT = 2000
MIN_APERTURE, MAX_APERTURE = 1, 60  # ″
