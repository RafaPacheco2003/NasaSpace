"""Render de arreglos (MJy/sr) a PNG: estiramiento, mapa de color, transparencia donde no hay datos."""
from __future__ import annotations

import io
import warnings
from functools import lru_cache

import numpy as np
from matplotlib import colormaps
from PIL import Image

STRETCHES = ("linear", "log", "sqrt", "asinh")
CMAPS = ("gray", "viridis", "magma", "inferno")

AMBER = np.array([255, 176, 0], dtype=np.float32)
CYAN = np.array([0, 229, 255], dtype=np.float32)


@lru_cache(maxsize=16)
def _lut(cmap: str) -> np.ndarray:
    return (colormaps[cmap](np.linspace(0, 1, 256)) * 255).round().astype(np.uint8)


def apply_stretch(x: np.ndarray, stretch: str) -> np.ndarray:
    """x normalizado a [0, 1] → [0, 1] con la curva pedida."""
    if stretch == "linear":
        return x
    if stretch == "sqrt":
        return np.sqrt(x)
    if stretch == "log":
        a = 1000.0
        return np.log10(1 + a * x) / np.log10(1 + a)
    if stretch == "asinh":
        return np.arcsinh(10 * x) / np.arcsinh(10)
    raise ValueError(stretch)


def auto_limits(arrays: list[np.ndarray], lo: float = 0.5, hi: float = 99.5) -> tuple[float, float]:
    """vmin/vmax por percentiles sobre todos los píxeles válidos (submuestreado si es muy grande)."""
    samples = []
    for a in arrays:
        v = a[np.isfinite(a)]
        if v.size > 200_000:
            v = v[:: v.size // 200_000 + 1]
        samples.append(v)
    vals = np.concatenate(samples) if samples else np.array([])
    if vals.size == 0:
        return 0.0, 1.0
    vmin, vmax = np.percentile(vals, [lo, hi])
    if not vmax > vmin:
        vmax = vmin + max(abs(vmin) * 1e-3, 1e-6)
    return float(vmin), float(vmax)


def _to_png(rgba: np.ndarray) -> bytes:
    # Los arreglos están en orientación FITS (fila 0 = sur); el PNG va con el norte arriba.
    img = Image.fromarray(np.ascontiguousarray(np.flipud(rgba)), mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


def render_png(arr: np.ndarray, vmin: float, vmax: float, stretch: str = "asinh", cmap: str = "gray") -> bytes:
    finite = np.isfinite(arr)
    with np.errstate(invalid="ignore", divide="ignore"):
        x = np.clip((np.where(finite, arr, vmin) - vmin) / (vmax - vmin), 0, 1)
    s = apply_stretch(x, stretch)
    idx = np.clip((s * 255).round(), 0, 255).astype(np.uint8)
    rgba = _lut(cmap)[idx].copy()
    rgba[~finite, 3] = 0
    return _to_png(rgba)


def render_diff_png(arr: np.ndarray, vmax: float, stretch: str = "asinh") -> bytes:
    """Resta: positivos en ámbar, negativos en cian, escala simétrica alrededor de 0."""
    finite = np.isfinite(arr)
    vmax = vmax if vmax > 0 else 1e-6
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        x = np.clip(np.where(finite, arr, 0) / vmax, -1, 1)
    m = apply_stretch(np.abs(x), stretch)[..., None]
    rgb = np.where((x > 0)[..., None], AMBER * m, CYAN * m)
    rgba = np.empty(arr.shape + (4,), dtype=np.uint8)
    rgba[..., :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    rgba[..., 3] = np.where(finite, 255, 0)
    return _to_png(rgba)


def render_mask_png(mask: np.ndarray, color=(255, 176, 0), alpha: int = 200) -> bytes:
    """Máscara booleana → PNG transparente con los píxeles marcados en color (capa de flags)."""
    rgba = np.zeros(mask.shape + (4,), dtype=np.uint8)
    rgba[mask, 0], rgba[mask, 1], rgba[mask, 2] = color
    rgba[mask, 3] = alpha
    return _to_png(rgba)
