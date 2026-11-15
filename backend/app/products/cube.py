# SI-assisted development used for code development, debugging and documentation.
# Tool: ChatGPT, Claude

"""Cubo all-sky de SPHEREx (colección spherex_dr1_cube, 102 canales) — basado en SphereX-pipeline.

- Tesela HEALPix (orden 5, 512×512 px, proyección HPX) que CONTIENE la coordenada (verificado con su WCS).
- IMAGE (102, 512, 512) en BUNIT, NHITS, FLAGS, SPECTRAL_CHANNELS (λ, bordes, ancho) y WCS-WAVE.
- Se exigen 102 planos, BUNIT y longitudes de onda crecientes (como el pipeline); no se interpolan canales ni se
  aplican máscaras de calidad.
- Los 6 canales del script: 1, 20, 40, 60, 80, 102.
- Cada plano se normaliza con sus percentiles 3.5–96.5 (gris invertido por defecto, como samples.png).
La tesela completa (~320 MB) se descarga una vez y se lee por planos desde el disco (memmap): no se carga entera.
"""
from __future__ import annotations

import asyncio
import io
import re
import warnings
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.table import Table
from astropy.wcs import WCS, FITSFixedWarning

from ..cache import cached, cget, cset, key, r1
from ..config import settings
from ..errors import APIError, UpstreamError, not_found
from ..http import parse_votable, request
from ..jobs import Job
from ..spherex.base import friendly_flag, parse_flag_bits
from ..utils import fnum as _fnum
from ..utils import val
from . import common as C
from .files import cached_file, fetch_file
from .spectral import interpret

COLLECTION = "spherex_dr1_cube"
SIX = [1, 20, 40, 60, 80, 102]
N_CHANNELS = 102
CMAPS = ("gray_r", "gray", "inferno", "magma", "viridis", "cividis")
_TILE = re.compile(r"Norder(\d+)/.*?Npix(\d+)", re.I)


def tile_id(url: str) -> str:
    m = _TILE.search(url)
    return f"hpx{m.group(1)}-{m.group(2)}" if m else "hpx-" + url.rsplit("/", 1)[-1].split(".")[0].lower()


def _reg_key(tid: str) -> str:
    return key("cube_tile", tid)


def get_tile(tid: str) -> dict:
    t = cget(_reg_key(tid))
    if t is None:
        raise not_found(f"Tesela {tid} desconocida: llama primero a GET /api/cube?ra=…&dec=…")
    return t


async def find_tiles(ra: float, dec: float) -> list[dict]:
    """Teselas del cubo que contienen el punto (IRSA SIA, consulta de 1″ como el pipeline). Caché 24 h."""
    rra, rdec = r1(ra), r1(dec)

    async def fetch():
        params = [("POS", f"CIRCLE {rra:.7f} {rdec:.7f} {1 / 3600:.8f}"), ("COLLECTION", COLLECTION)]
        r = await request("spherex", "GET", settings.irsa_sia_url, params=params,
                          timeout=settings.spherex_search_timeout_s, retries=settings.spherex_search_retries)
        out = []
        for row in parse_votable("spherex", r.content):
            url = str(val(row, "access_url") or "").strip()
            if not url:
                continue
            out.append({"id": tile_id(url), "url": url, "obs_id": str(val(row, "obs_id") or ""),
                        "s_ra": _fnum(val(row, "s_ra")), "s_dec": _fnum(val(row, "s_dec")),
                        "s_region": str(val(row, "s_region") or ""), "size_bytes": None})
        return sorted({t["url"]: t for t in out}.values(), key=lambda t: t["url"])

    tiles, _ = await cached(key("cube_search", rra, rdec), fetch)
    for t in tiles:
        cset(_reg_key(t["id"]), t, ttl=30 * 24 * 3600)
    return tiles


def _open(path: Path):
    warnings.simplefilter("ignore", FITSFixedWarning)
    return fits.open(path, memmap=True, lazy_load_hdus=True)


def inspect(path: Path) -> dict:
    """Validación del pipeline + canales + WCS. Se guarda en caché por archivo."""
    k = key("cubeinfo", path.name, path.stat().st_size)
    v = cget(k)
    if v is not None:
        return v
    with warnings.catch_warnings(), _open(path) as h:
        warnings.simplefilter("ignore")
        hdu = h["IMAGE"]
        shape = tuple(int(s) for s in hdu.shape)
        if len(shape) != 3 or shape[0] != N_CHANNELS:
            raise APIError(502, "upstream_error", f"IMAGE debe contener 102 planos: {shape}")
        unit = hdu.header.get("BUNIT")
        if not unit:
            raise APIError(502, "upstream_error", "IMAGE no declara BUNIT.")
        table = Table(h["SPECTRAL_CHANNELS"].data)
        for col in h["SPECTRAL_CHANNELS"].columns:
            if col.unit:
                table[col.name].unit = col.unit
        try:
            wave, lower, upper, width, source = interpret(table)
        except ValueError as e:
            raise APIError(502, "upstream_error", str(e)) from e
        extra = {c: [C.fnum(x) for x in np.asarray(table[c], float)] for c in ("R", "DETECTOR", "SUBCHAN") if c in table.colnames}
        channels = []
        for i in range(N_CHANNELS):
            channels.append({
                "channel": i + 1, "wavelength_um": C.fnum(wave[i]), "lower_um": C.fnum(lower[i]),
                "upper_um": C.fnum(upper[i]), "bandwidth_um": C.fnum(width[i]), "bandwidth_source": str(source[i]),
                "detector": int(extra["DETECTOR"][i]) if "DETECTOR" in extra else None,
                "resolving_power": extra["R"][i] if "R" in extra else None,
            })
        wcs = WCS(hdu.header, h).celestial
        v = {
            "shape": list(shape), "unit": unit, "channels": channels,
            "hpx_order": hdu.header.get("ORDER"), "hpx_npix": hdu.header.get("NPIX"),
            "projection": hdu.header.get("CTYPE1", "")[-3:],
            "orientation": C.orientation(wcs, shape[1:]),
            "extensions": [x.name for x in h],
        }
    cset(k, v)
    return v


def _wcs(h):
    return WCS(h["IMAGE"].header, h).celestial


def contains(path: Path, ra: float, dec: float) -> tuple[float, float] | None:
    with warnings.catch_warnings(), _open(path) as h:
        warnings.simplefilter("ignore")
        xp, yp = (float(v) for v in _wcs(h).world_to_pixel_values(ra, dec))
        ny, nx = h["IMAGE"].shape[-2:]
    if not np.isfinite([xp, yp]).all():
        return None
    x, y = int(np.floor(xp + 0.5)), int(np.floor(yp + 0.5))
    return (xp, yp) if 0 <= x < nx and 0 <= y < ny else None


def plane_stats(path: Path, ch: int) -> tuple[float, float]:
    k = key("cubeplane", path.name, ch)
    v = cget(k)
    if v is not None:
        return tuple(v)
    with warnings.catch_warnings(), _open(path) as h:
        warnings.simplefilter("ignore")
        plane = np.asarray(h["IMAGE"].section[ch - 1], dtype=np.float32)
    valid = plane[np.isfinite(plane)]
    lo, hi = (float(x) for x in np.percentile(valid, C.PCT)) if valid.size else (0.0, 1.0)
    cset(k, [lo, hi])
    return lo, hi


def render_channel(path: Path, ch: int, cmap: str, size: int | None) -> bytes:
    k = key("cubepng", path.name, ch, cmap, size)
    v = cget(k)
    if v is not None:
        return v
    lo, hi = plane_stats(path, ch)
    with warnings.catch_warnings(), _open(path) as h:
        warnings.simplefilter("ignore")
        plane = np.asarray(h["IMAGE"].section[ch - 1], dtype=np.float32)
    norm, valid, _, _ = C.normalize(plane, lo, hi) if np.isfinite(plane).any() else (np.zeros_like(plane), np.isfinite(plane), lo, hi)
    png = C.to_png(norm, valid, cmap, size, invalid="transparent")
    cset(k, png)
    return png


def spectrum_at(path: Path, col: float, row: float) -> dict:
    info = inspect(path)
    native = tuple(info["shape"][1:])
    ci, ri, inside = C.nearest(col, row, native)
    with warnings.catch_warnings(), _open(path) as h:
        warnings.simplefilter("ignore")
        ra, dec = (float(v) for v in _wcs(h).pixel_to_world_values(col, row))
        out = {"ra": ra % 360, "dec": dec, "inside": inside,
               "fits_pixel": {"col": round(col, 3), "row": round(row, 3), "i": ci, "j": ri}}
        if not inside:
            return out
        names = [x.name for x in h]
        b = np.asarray(h["IMAGE"].section[:, ri, ci], dtype=np.float64)
        fl = np.asarray(h["FLAGS"].section[:, ri, ci], dtype=np.int64) if "FLAGS" in names else None
        nh = np.asarray(h["NHITS"].section[:, ri, ci], dtype=np.int64) if "NHITS" in names else None
        bits = parse_flag_bits(h["FLAGS"].header) if "FLAGS" in names else {}
    spec = []
    for i, c in enumerate(info["channels"]):
        f = int(fl[i]) if fl is not None else None
        spec.append({
            "channel": c["channel"], "wavelength_um": c["wavelength_um"], "bandwidth_um": c["bandwidth_um"],
            "brightness": C.fnum(b[i]), "flags_raw": f,
            "flags": sorted(friendly_flag(n) for n, bt in bits.items() if f and (f >> bt) & 1),
            "nhits": int(nh[i]) if nh is not None else None,
        })
    out.update({"unit": info["unit"], "spectrum": spec,
                "valid_channels": sum(s["brightness"] is not None for s in spec)})
    return out


def samples_figure(path: Path, col: float | None, row: float | None) -> bytes:
    """La figura del script (samples.png): los 6 canales con su λ y ancho de banda, ejes WCS y la cruz roja."""
    k = key("cubesamples", path.name, None if col is None else round(col, 1), None if row is None else round(row, 1))
    v = cget(k)
    if v is not None:
        return v
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    info = inspect(path)
    with warnings.catch_warnings(), _open(path) as h:
        warnings.simplefilter("ignore")
        wcs = _wcs(h)
        fig, axes = plt.subplots(2, 3, figsize=(15, 9), subplot_kw={"projection": wcs})
        for ch, ax in zip(SIX, axes.flat):
            plane = np.asarray(h["IMAGE"].section[ch - 1], dtype=np.float32)
            lo, hi = plane_stats(path, ch)
            im = ax.imshow(plane, origin="lower", cmap="gray_r", vmin=lo, vmax=hi)
            if col is not None:
                ax.plot(col, row, "+", color="red", ms=12)
            c = info["channels"][ch - 1]
            ax.set_title(f"Canal {ch} · {c['wavelength_um']:.5f} µm\nΔλ={c['bandwidth_um']:.5f} µm")
            fig.colorbar(im, ax=ax, label=info["unit"], fraction=0.046)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=80)
        plt.close(fig)
    cset(k, buf.getvalue())
    return buf.getvalue()


def _grid(path: Path, n: int):
    info = inspect(path)
    _, ny, nx = info["shape"]
    xs = np.unique(np.linspace(0, nx - 1, min(n, nx), dtype=int))
    ys = np.unique(np.linspace(0, ny - 1, min(n, ny), dtype=int))
    with warnings.catch_warnings(), _open(path) as h:
        warnings.simplefilter("ignore")
        data = h["IMAGE"].data  # memmap
        view = np.asarray(data[:, ys[:, None], xs], dtype=np.float32)
    return info, xs, ys, view


def preview3d(path: Path, n: int = 32) -> dict:
    """Malla de la vista 3D del pipeline (preview3d.npz) en JSON: brillo[canal][y][x] muestreado hasta n×n."""
    k = key("cubepreview", path.name, n)
    v = cget(k)
    if v is not None:
        return v
    info, xs, ys, view = _grid(path, n)
    finite = view[np.isfinite(view)]
    lo, hi = (float(x) for x in np.percentile(finite, C.PCT)) if finite.size else (0.0, 1.0)
    v = {
        "unit": info["unit"], "shape": list(view.shape), "x": xs.tolist(), "y": ys.tolist(),
        "wavelength_um": [c["wavelength_um"] for c in info["channels"]],
        "color_range": {"low": lo, "high": hi, "percentiles": list(C.PCT), "cmap": "inferno"},
        "brightness": [[[None if not np.isfinite(b) else float(f"{b:.5g}") for b in rowv] for rowv in plane] for plane in view],
        "note": "y = fila FITS (crece hacia arriba); muestreo espacial uniforme; valores originales en BUNIT",
    }
    cset(k, v)
    return v


def cube3d_png(path: Path, n: int = 32) -> bytes:
    """La figura cube3d.png del pipeline: los 102 planos apilados con el eje Z espectral."""
    k = key("cube3d", path.name, n)
    v = cget(k)
    if v is not None:
        return v
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.cm import ScalarMappable
    from matplotlib.colors import Normalize

    info, xs, ys, view = _grid(path, n)
    wave = [c["wavelength_um"] for c in info["channels"]]
    finite = view[np.isfinite(view)]
    if not finite.size:
        raise APIError(502, "upstream_error", "No hay datos finitos en la malla 3D.")
    lo, hi = np.percentile(finite, C.PCT)
    if lo == hi:
        hi = lo + max(abs(float(lo)) * 0.01, 1e-6)
    norm = Normalize(lo, hi, clip=True)
    fig = plt.figure(figsize=(12, 9))
    ax = fig.add_subplot(111, projection="3d")
    xx, yy = np.meshgrid(xs, ys)
    for plane, wl in zip(view, wave):
        colors = plt.get_cmap("inferno")(norm(np.ma.masked_invalid(plane)))
        colors[..., 3] = np.where(np.isfinite(plane), 0.18, 0)
        ax.plot_surface(xx, yy, np.full(xx.shape, wl), facecolors=colors, rstride=1, cstride=1,
                        linewidth=0, antialiased=False, shade=False)
    ax.set(xlabel="x (píxel)", ylabel="y (píxel)", zlabel="Longitud de onda (µm)", title="102 canales SPHEREx")
    fig.colorbar(ScalarMappable(norm=norm, cmap="inferno"), ax=ax, label=info["unit"], shrink=0.65)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=80)
    plt.close(fig)
    cset(k, buf.getvalue())
    return buf.getvalue()


# --- Preparación (trabajo 202) --------------------------------------------------

async def local(tile: dict) -> Path:
    return cached_file(tile["url"]) or await fetch_file(tile["url"])


def channel_urls(tid: str, ch: int, cmap: str = "gray_r", size: int | None = None) -> str:
    q = f"?cmap={cmap}" + (f"&size={size}" if size else "")
    return f"/api/cube/{tid}/channel/{ch}.png{q}"


async def build_cube(job: Job, *, ra: float, dec: float, size: int | None) -> dict:
    job.update(0.02, "Buscando la tesela del cubo (IRSA SIA, spherex_dr1_cube)")
    tiles = await find_tiles(ra, dec)
    if not tiles:
        raise not_found("No hay teselas del cubo all-sky de SPHEREx para esa coordenada.")
    errors = []
    for t in tiles:
        def prog(done: int, total: int, t=t) -> None:
            if total:
                job.update(0.05 + 0.85 * done / total, f"Descargando tesela {t['id']} {done / 1e6:.0f}/{total / 1e6:.0f} MB")

        try:
            path = cached_file(t["url"]) or await fetch_file(t["url"], prog)
            job.update(0.92, "Validando 102 canales y WCS")
            info = await asyncio.to_thread(inspect, path)
            pix = await asyncio.to_thread(contains, path, ra, dec)
            if pix is None:
                errors.append(f"{t['id']}: la coordenada no cae dentro de IMAGE")
                continue
        except (APIError, UpstreamError, OSError, KeyError) as e:
            errors.append(f"{t['id']}: {getattr(e, 'message', e)}")
            continue
        break
    else:
        raise APIError(502, "upstream_error", "No se pudo cargar una tesela de 102 canales que contenga la coordenada. "
                       + "; ".join(errors))
    col, row = pix
    job.update(0.96, "Extrayendo el espectro de 102 canales")
    spec = await asyncio.to_thread(spectrum_at, path, col, row)
    native = tuple(info["shape"][1:])
    tx, ty = C.fits_to_png(col, row, native, size)
    w, hgt = C.png_size(native, size)
    tid = t["id"]
    chans = [{**c, "png_url": channel_urls(tid, c["channel"], size=size)} for c in info["channels"]]
    return {
        "tile": {"id": tid, "obs_id": t["obs_id"], "access_url": t["url"], "hpx_order": info["hpx_order"],
                 "hpx_npix": info["hpx_npix"], "projection": info["projection"],
                 "size_mb": round(path.stat().st_size / 1e6, 1)},
        "shape": info["shape"], "unit": info["unit"],
        "native_size": {"width": native[1], "height": native[0]}, "png_size": {"width": w, "height": hgt},
        "orientation": info["orientation"],
        "target": {"ra": ra, "dec": dec, "x": round(tx, 3), "y": round(ty, 3), "fits_pixel": spec["fits_pixel"]},
        "spectrum": spec.get("spectrum", []),
        "channels": chans,
        "six_channels": [chans[c - 1] for c in SIX],
        "urls": {
            "channels_all": f"/api/cube/{tid}/channels?set=all" + (f"&size={size}" if size else ""),
            "channels_six": f"/api/cube/{tid}/channels?set=six" + (f"&size={size}" if size else ""),
            "samples_png": f"/api/cube/{tid}/samples.png?ra={ra}&dec={dec}",
            "pixel": f"/api/cube/{tid}/pixel?x={{x}}&y={{y}}" + (f"&size={size}" if size else ""),
            "preview3d": f"/api/cube/{tid}/preview3d",
            "cube3d_png": f"/api/cube/{tid}/cube3d.png",
            "fits": f"/api/cube/{tid}/fits",
        },
        "partial": bool(errors), "warnings": errors,
    }
