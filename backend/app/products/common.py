"""Utilidades comunes de L2 completo y cubos all-sky: normalización del pipeline, PNG y conversión de píxeles.

Convención de píxeles para el Frontend (igual que el resto de la API): coordenadas CONTINUAS en el PNG mostrado,
x desde el borde IZQUIERDO, y desde el borde SUPERIOR. El PNG se dibuja como el pipeline (`origin='lower'`):
la fila 0 del FITS queda ABAJO. Si el PNG se pidió con `size` distinto del nativo, pasar ese mismo `size`.
"""
from __future__ import annotations

import io
import math

import numpy as np
from PIL import Image

PCT = (3.5, 96.5)  # percentiles de normalización del pipeline


def normalize(image: np.ndarray, low: float | None = None, high: float | None = None):
    """Normalización del pipeline: float32 [0,1] con percentiles 3.5–96.5 de los valores finitos."""
    valid = np.isfinite(image)
    if not valid.any():
        raise ValueError("La imagen no contiene valores finitos.")
    if low is None or high is None:
        low, high = (float(v) for v in np.percentile(image[valid], PCT))
    out = np.zeros(image.shape, dtype=np.float32)
    out[valid] = np.clip((image[valid] - low) / max(float(high - low), 1e-10), 0, 1)
    return out, valid, float(low), float(high)


def to_png(norm: np.ndarray, valid: np.ndarray, cmap: str, size: int | None, invalid: str = "cmap") -> bytes:
    """[0,1] → PNG con un mapa de color de matplotlib, origen abajo (como plt.imsave(origin='lower')).

    invalid: "cmap" = el color del valor 0 (como el gris del pipeline), "black" (como el RGB del pipeline)
    o "transparent".
    """
    from matplotlib import colormaps

    # Por bloques de filas: colormaps() devuelve RGBA float64 (32 B/píxel) y un L2 de 2040² tiene 4.2 M píxeles;
    # de una vez serían ~400 MB de temporales (en Render, con 512 MB, el servicio se caía). Resultado idéntico.
    cm = colormaps[cmap]
    rgba = np.empty(norm.shape + (4,), dtype=np.uint8)
    step = 128
    for i in range(0, norm.shape[0], step):
        rgba[i:i + step] = (cm(norm[i:i + step]) * 255).round().astype(np.uint8)
    if invalid == "black":
        rgba[~valid, :3] = 0
    elif invalid == "transparent":
        rgba[~valid, 3] = 0
    img = Image.fromarray(np.ascontiguousarray(rgba[::-1]), "RGBA")
    if invalid != "transparent":
        img = img.convert("RGB")
    if size and size != max(img.size):
        k = size / max(img.size)
        img = img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), Image.Resampling.BOX
                         if k < 1 else Image.Resampling.NEAREST)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False, compress_level=6)
    return buf.getvalue()


def png_size(native: tuple[int, int], size: int | None) -> tuple[int, int]:
    ny, nx = native
    if not size or size == max(nx, ny):
        return nx, ny
    k = size / max(nx, ny)
    return max(1, round(nx * k)), max(1, round(ny * k))


def png_to_fits(x: float, y: float, native: tuple[int, int], size: int | None) -> tuple[float, float]:
    """(x, y) continuos en el PNG mostrado → (col, fila) del FITS, 0-based con centro de píxel en enteros."""
    ny, nx = native
    w, h = png_size(native, size)
    return x * nx / w - 0.5, (h - y) * ny / h - 0.5


def fits_to_png(col: float, row: float, native: tuple[int, int], size: int | None) -> tuple[float, float]:
    ny, nx = native
    w, h = png_size(native, size)
    return (col + 0.5) * w / nx, h - (row + 0.5) * h / ny


def orientation(wcs, native: tuple[int, int]) -> dict:
    """Hacia dónde apuntan el norte y el este en el PNG (grados desde 'arriba', sentido horario) y escala."""
    ny, nx = native
    c0, r0 = nx / 2 - 0.5, ny / 2 - 0.5
    ra0, dec0 = (float(v) for v in wcs.pixel_to_world_values(c0, r0))
    d = 0.01
    out = {}
    for name, (ra1, dec1) in {"north": (ra0, dec0 + d), "east": (ra0 + d / max(math.cos(math.radians(dec0)), 1e-6), dec0)}.items():
        c1, r1 = (float(v) for v in wcs.world_to_pixel_values(ra1, dec1))
        dx, dy_up = c1 - c0, r1 - r0  # en el PNG "arriba" = fila FITS creciente
        out[f"{name}_deg_from_up"] = round(math.degrees(math.atan2(dx, dy_up)) % 360, 2)
    c1, r1 = (float(v) for v in wcs.world_to_pixel_values(ra0, dec0 + d))
    out["pixel_scale_arcsec"] = round(d * 3600 / math.hypot(c1 - c0, r1 - r0), 4)
    out["center"] = {"ra": ra0 % 360, "dec": dec0}
    return out


def nearest(col: float, row: float, native: tuple[int, int]) -> tuple[int, int, bool]:
    ny, nx = native
    ci, ri = int(math.floor(col + 0.5)), int(math.floor(row + 0.5))
    return ci, ri, 0 <= ci < nx and 0 <= ri < ny


def fnum(v):
    v = float(v)
    return v if math.isfinite(v) else None
