"""Modelos comunes de SPHEREx: metadatos de frame, recorte y banderas de calidad."""
from __future__ import annotations

import gzip
import io
import warnings
from dataclasses import asdict, dataclass, field

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning

from ..config import settings

try:  # astropy avisa en cada FITS de SPHEREx que CTYPE no lleva "-SIP" (se aplica SIP igualmente)
    from astropy import log as _astropy_log

    _astropy_log.setLevel("WARNING")
except Exception:  # noqa: BLE001
    pass

# Detector → rango de longitudes de onda (µm). Cada uno aporta 17 de las 102 bandas.
DETECTORS = {1: (0.75, 1.09), 2: (1.10, 1.62), 3: (1.63, 2.41), 4: (2.42, 3.82), 5: (3.83, 4.41), 6: (4.42, 5.00)}
NATIVE_PIXEL_ARCSEC = 6.2
FRAME_FOV_ARCSEC = 2040 * NATIVE_PIXEL_ARCSEC  # ~3.5° por lado

# Bits de la extensión FLAGS. Se leen de la cabecera del FITS (MSKNnnnn = nombre del plano n,
# como en SPHEREx DR1); esta tabla es el respaldo y coincide con DR1.
DEFAULT_FLAG_BITS = {
    "transient": 0, "overflow": 1, "sur_error": 2, "nonfunc": 6, "dichroic": 7, "missing_data": 9,
    "hot": 10, "cold": 11, "fullsample": 12, "phanmiss": 14, "nonlinear": 15, "persist": 17,
    "outlier": 19, "crosstalk": 20, "source": 21, "ghost": 22, "ghost_fpa": 23, "ghost_ext": 24,
    "streak": 25, "bloom": 26, "snowball": 27, "halo": 28,
}
# Banderas informativas que no indican contaminación
INFORMATIONAL_FLAGS = {"source", "fullsample", "reference"}
# Nombres amigables para el Frontend (FrameFlag es un enum abierto)
FLAG_ALIASES = {
    "transient": "cosmic_ray", "overflow": "saturation", "persist": "persistence", "hot": "hot_pixel",
    "cold": "cold_pixel", "nonfunc": "nonfunctional", "sur_error": "fit_error", "phanmiss": "phantom_uncorrected",
}


def detector_from_wavelength(um: float | None) -> int | None:
    if um is None:
        return None
    for det, (lo, hi) in DETECTORS.items():
        if lo - 0.005 <= um <= hi + 0.005:
            return det
    return None


@dataclass
class FrameMeta:
    id: str
    obs_id: str
    date_obs: str          # ISO 8601 UTC con Z
    mjd: float
    detector: int
    band_um: float | None  # aproximado (centro del detector) hasta procesar el recorte
    exposure_s: float | None
    access_url: str
    center_ra: float | None = None
    center_dec: float | None = None
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)

    def api(self, info: dict | None = None) -> dict:
        """Forma `Frame` del contrato. `info` = datos medidos en el recorte (band_um exacto, flags)."""
        band = self.band_um
        flags: list[str] = []
        exact = False
        if info:
            band = info.get("band_um") or band
            flags = info.get("flags") or []
            exact = bool(info.get("band_exact"))
        return {
            "id": self.id,
            "date_obs": self.date_obs,
            "detector": self.detector,
            "band_um": round(band, 4) if band is not None else None,
            "band_exact": exact,
            "center": {"ra": self.center_ra, "dec": self.center_dec},
            "fov_arcsec": FRAME_FOV_ARCSEC,
            "exposure_s": self.exposure_s,
            "flags": flags,
            "obs_id": self.obs_id,
        }


@dataclass
class Cutout:
    image: np.ndarray            # MJy/sr (con zodiacal restada si ZODI_SUBTRACT)
    wcs: WCS                     # WCS celeste del recorte
    flags: np.ndarray | None
    variance: np.ndarray | None
    flag_bits: dict[str, int]
    band_um: float | None        # longitud de onda en el píxel del objetivo


def friendly_flag(name: str) -> str:
    return FLAG_ALIASES.get(name, name)


# Defectos de píxel individual: se limpian (rellenan) antes de mostrar/apilar
HARD_FLAGS = {"transient", "overflow", "sur_error", "nonfunc", "missing_data", "hot", "cold", "outlier"}


def hard_mask(flags: np.ndarray | None, flag_bits: dict[str, int]) -> np.ndarray | None:
    if flags is None:
        return None
    bad = 0
    for name, bit in flag_bits.items():
        if name in HARD_FLAGS:
            bad |= 1 << bit
    return (flags.astype(np.int64) & bad) != 0


def inpaint(image: np.ndarray, bad: np.ndarray, passes: int = 3) -> np.ndarray:
    """Sustituye los píxeles malos por la media de sus vecinos 3×3 válidos (varias pasadas para huecos)."""
    from scipy.ndimage import uniform_filter

    img = image.astype(np.float32).copy()
    bad = bad | ~np.isfinite(img)
    for _ in range(passes):
        if not bad.any():
            break
        good = (~bad).astype(np.float32)
        s = uniform_filter(np.where(bad, 0, img), size=3, mode="constant")
        n = uniform_filter(good, size=3, mode="constant")
        fill = bad & (n > 1e-6)
        img[fill] = (s[fill] / n[fill]).astype(np.float32)
        bad = bad & ~fill
    img[bad] = np.nan
    return img


def contamination_mask(flags: np.ndarray | None, flag_bits: dict[str, int]) -> np.ndarray | None:
    if flags is None:
        return None
    bad = 0
    for name, bit in flag_bits.items():
        if name not in INFORMATIONAL_FLAGS:
            bad |= 1 << bit
    return (flags.astype(np.int64) & bad) != 0


def flags_in_mask(flags: np.ndarray | None, mask: np.ndarray, flag_bits: dict[str, int]) -> list[str]:
    if flags is None or not mask.any():
        return []
    combined = int(np.bitwise_or.reduce(flags[mask].astype(np.int64).ravel()))
    names = [friendly_flag(n) for n, b in flag_bits.items() if (combined >> b) & 1 and n not in INFORMATIONAL_FLAGS]
    return sorted(set(names))


def flags_near(cut: Cutout, ra: float, dec: float, radius_px: float = 1.5) -> list[str]:
    if cut.flags is None:
        return []
    x0, y0 = cut.wcs.world_to_pixel_values(ra, dec)
    ny, nx = cut.flags.shape
    yy, xx = np.indices((ny, nx))
    mask = np.hypot(xx - x0, yy - y0) <= radius_px
    return flags_in_mask(cut.flags, mask, cut.flag_bits)


def parse_flag_bits(header) -> dict[str, int]:
    bits = {}
    for k, v in header.items():
        if k.startswith("MSKN") and k[4:].isdigit() and isinstance(v, str):  # SPHEREx DR1
            bits[v.strip().lower()] = int(k[4:])
        elif k.startswith("MP_") and isinstance(v, int) and 0 <= v < 63:  # convención alternativa
            bits[k[3:].lower()] = v
    return bits or dict(DEFAULT_FLAG_BITS)


KEEP_EXTENSIONS = {"PRIMARY", "IMAGE", "FLAGS", "VARIANCE", "ZODI", "WCS-WAVE"}


def compact_fits(content: bytes) -> bytes:
    """Quita del recorte las extensiones que no usamos (la tabla EPSF ocupa ~3.8 MB por recorte)."""
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    try:
        with fits.open(io.BytesIO(content), memmap=False) as hdul:
            keep = [h for h in hdul if h.name.upper() in KEEP_EXTENSIONS]
            if len(keep) == len(hdul) or not any(h.name.upper() == "IMAGE" for h in keep):
                return content
            buf = io.BytesIO()
            fits.HDUList([h.copy() if i else fits.PrimaryHDU(header=h.header) for i, h in enumerate(keep)]).writeto(buf)
            return buf.getvalue()
    except Exception:  # noqa: BLE001  (si algo falla, se guarda tal cual)
        return content


def parse_spherex_fits(content: bytes, ra: float, dec: float) -> Cutout:
    """Lee un recorte multi-extensión de SPHEREx (IMAGE, FLAGS, VARIANCE, ZODI, WCS-WAVE)."""
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        warnings.simplefilter("ignore", RuntimeWarning)
        with fits.open(io.BytesIO(content), memmap=False) as hdul:
            names = [h.name.upper() for h in hdul]
            if "IMAGE" in names:
                img_hdu = hdul["IMAGE"]
            else:
                img_hdu = next((h for h in hdul if h.data is not None and getattr(h.data, "ndim", 0) == 2), None)
                if img_hdu is None:
                    raise ValueError("El FITS no tiene una imagen 2D")
            image = np.asarray(img_hdu.data, dtype=np.float32)
            hdr = img_hdu.header
            if settings.zodi_subtract and "ZODI" in names and hdul["ZODI"].data is not None:
                image = image - np.asarray(hdul["ZODI"].data, dtype=np.float32)
            flags, bits = None, dict(DEFAULT_FLAG_BITS)
            if "FLAGS" in names and hdul["FLAGS"].data is not None:
                flags = np.asarray(hdul["FLAGS"].data).astype(np.int64)
                bits = parse_flag_bits(hdul["FLAGS"].header)
            variance = None
            if "VARIANCE" in names and hdul["VARIANCE"].data is not None:
                variance = np.asarray(hdul["VARIANCE"].data, dtype=np.float32)
            wcs = WCS(hdr, relax=True).celestial
            band = None
            try:
                wave = WCS(hdr, fobj=hdul, key="W")
                x, y = wcs.world_to_pixel_values(ra, dec)
                w = wave.pixel_to_world_values(x, y)
                band = float(np.ravel(w[0] if isinstance(w, (tuple, list)) else w)[0])
                if band < 1e-3:  # metros → µm
                    band *= 1e6
                if not (0.5 < band < 6):
                    band = None
            except Exception:  # noqa: BLE001  (sin WCS espectral → se usa el valor del SIA)
                band = None
    return Cutout(image=image, wcs=wcs, flags=flags, variance=variance, flag_bits=bits, band_um=band)
