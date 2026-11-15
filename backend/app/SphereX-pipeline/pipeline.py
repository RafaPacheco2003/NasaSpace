"""Pipeline RA/DEC basado exclusivamente en los dos notebooks del proyecto."""
import argparse
import json
import shutil
from time import perf_counter
from pathlib import Path
import numpy as np
import requests
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.colors import Normalize
from matplotlib.cm import ScalarMappable
import astropy.units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.table import Table
from astropy.wcs import WCS
from astroquery.ipac.irsa import Irsa
from spectral import interpret
from downloads import fetch

CHANNELS = np.array([1, 20, 40, 60, 80, 102])

def coordinate(ra, dec):
    ra, dec = float(ra), float(dec)
    if not np.isfinite([ra, dec]).all() or not 0 <= ra < 360 or not -90 <= dec <= 90:
        raise ValueError('RA debe estar en [0,360) y DEC en [-90,90], en grados ICRS.')
    return SkyCoord(ra=ra*u.deg, dec=dec*u.deg, frame='icrs')

def normalize(image):
    valid = np.isfinite(image)
    if not valid.any():
        raise ValueError('La imagen no contiene valores finitos.')
    low, high = np.percentile(image[valid], [3.5, 96.5])
    result = np.zeros(image.shape, dtype=np.float32)
    result[valid] = np.clip((image[valid]-low)/max(float(high-low), 1e-10), 0, 1)
    return result, float(low), float(high)

def query(coord, collection):
    table = Irsa.query_sia(pos=(coord, 1*u.arcsec), collection=[collection])
    if len(table) and 't_min' in table.colnames:
        table.sort('t_min')
    records = []
    for row in table:
        if np.ma.is_masked(row['access_url']):
            continue
        url = str(row['access_url']).strip()
        if not url:
            continue
        records.append({k: str(row[k]) for k in table.colnames})
    return records

def l2(coord, out):
    observations = query(coord, 'spherex_dr1')
    (out/'observations.json').write_text(json.dumps(observations, indent=2), encoding='utf-8')
    if not observations:
        raise ValueError('No se encontraron observaciones L2 para la coordenada.')
    # Misma selección cronológica del notebook, con respaldo si hay menos de tres.
    selected = observations[min(2, len(observations)-1)]
    with fits.open(selected['access_url'], cache=True) as hdul:
        image = hdul['IMAGE'].data.astype(np.float32)
        header = hdul['IMAGE'].header.copy()
    if image.ndim != 2:
        raise ValueError(f'IMAGE L2 debe ser 2D, recibido {image.shape}')
    norm, low, high = normalize(image)
    fits.PrimaryHDU(image, header=header).writeto(out/'l2.fits', overwrite=True)
    np.save(out/'l2_normalized.npy', norm)
    plt.imsave(out/'l2_gray.png', norm, cmap='gray_r', origin='lower', vmin=0, vmax=1)
    rgb = plt.get_cmap('cividis')(norm)[..., :3]
    rgb[~np.isfinite(image)] = 0
    plt.imsave(out/'l2_rgb.png', rgb, origin='lower')
    return dict(url=selected['access_url'], count=len(observations), shape=list(image.shape),
                low=low, high=high, unit=header.get('BUNIT'), rgb='Pseudocolor cividis; no RGB visible')

def allsky(coord, out):
    started = perf_counter()
    candidates = query(coord, 'spherex_dr1_cube')
    (out/'cube_candidates.json').write_text(json.dumps(candidates, indent=2), encoding='utf-8')
    errors = []
    download_seconds = 0.0
    for url in sorted({r['access_url'] for r in candidates}):
        try:
            before_download = perf_counter()
            local = fetch(url)
            download_seconds += perf_counter()-before_download
            with fits.open(local, lazy_load_hdus=True) as hdul:
                hdu = hdul['IMAGE']
                wcs = WCS(hdu.header, hdul).celestial
                xp, yp = wcs.world_to_pixel(coord)
                if not np.isfinite([xp, yp]).all():
                    continue
                x, y = int(np.floor(float(xp)+.5)), int(np.floor(float(yp)+.5))
                if not (0 <= x < hdu.shape[-1] and 0 <= y < hdu.shape[-2]):
                    continue
                if len(hdu.shape) != 3 or hdu.shape[0] != 102:
                    raise ValueError(f'IMAGE debe contener 102 planos: {hdu.shape}')
                unit = hdu.header.get('BUNIT')
                if not unit:
                    raise ValueError('IMAGE no declara BUNIT.')
                table = Table(hdul['SPECTRAL_CHANNELS'].data)
                for col in hdul['SPECTRAL_CHANNELS'].columns:
                    if col.unit:
                        table[col.name].unit = col.unit
                wave, lower, upper, width, source = interpret(table)
                cube = hdu.data.copy()
                # Conservar HDUs, WCS, tabla espectral y calidad del producto original.
                shutil.copyfile(local, out/'allsky_cube.fits')
                spectrum = Table(dict(channel=np.arange(1, 103), wavelength=wave,
                                      lower=lower, upper=upper, bandwidth=width,
                                      bandwidth_source=source, brightness=cube[:, y, x]))
                for name in ('wavelength', 'lower', 'upper', 'bandwidth'):
                    spectrum[name].unit = u.um
                spectrum['brightness'].unit = u.Unit(unit)
                for name in ('FLAGS', 'NHITS'):
                    if name in hdul:
                        spectrum[name] = hdul[name].data[:, y, x]
        except (OSError, ValueError, KeyError, requests.RequestException) as exc:
            errors.append(f'{url}: {exc}')
            continue
        break
    else:
        raise ValueError('No se pudo cargar una tesela de 102 canales que contenga la coordenada. '+ '; '.join(errors))
    spectrum.write(out/'spectrum.ecsv', overwrite=True)
    spectrum[CHANNELS-1].write(out/'samples.ecsv', overwrite=True)
    np.save(out/'samples.npy', cube[CHANNELS-1])
    fig, axes = plt.subplots(2, 3, figsize=(15, 9), subplot_kw={'projection': wcs})
    for ch, ax in zip(CHANNELS, axes.flat):
        plane = cube[ch-1]
        valid = plane[np.isfinite(plane)]
        lo, hi = np.percentile(valid, [3.5, 96.5]) if valid.size else (0, 1)
        im = ax.imshow(plane, origin='lower', cmap='gray_r', vmin=lo, vmax=hi)
        ax.plot(x, y, '+', color='red')
        ax.set_title(f'Canal {ch} · {wave[ch-1]:.5f} µm\nΔλ={width[ch-1]:.5f} µm')
        fig.colorbar(im, ax=ax, label=unit, fraction=.046)
    fig.tight_layout(); fig.savefig(out/'samples.png'); plt.close(fig)
    xs = np.unique(np.linspace(0, cube.shape[2]-1, min(32, cube.shape[2]), dtype=int))
    ys = np.unique(np.linspace(0, cube.shape[1]-1, min(32, cube.shape[1]), dtype=int))
    view = cube[:, ys[:, None], xs]
    finite = view[np.isfinite(view)]
    if not finite.size:
        raise ValueError('No hay datos finitos en la malla 3D.')
    lo, hi = np.percentile(finite, [3.5, 96.5])
    if lo == hi:
        hi = lo + max(abs(float(lo))*.01, 1e-6)
    norm = Normalize(lo, hi, clip=True)
    fig = plt.figure(figsize=(12, 9)); ax = fig.add_subplot(111, projection='3d')
    xx, yy = np.meshgrid(xs, ys)
    for plane, wavelength in zip(view, wave):
        colors = plt.get_cmap('inferno')(norm(np.ma.masked_invalid(plane)))
        colors[..., 3] = np.where(np.isfinite(plane), .18, 0)
        ax.plot_surface(xx, yy, np.full(xx.shape, wavelength), facecolors=colors,
                        rstride=1, cstride=1, linewidth=0, antialiased=False, shade=False)
    ax.set(xlabel='x (píxel)', ylabel='y (píxel)', zlabel='Longitud de onda (µm)', title='102 canales SPHEREx')
    fig.colorbar(ScalarMappable(norm=norm, cmap='inferno'), ax=ax, label=unit, shrink=.65)
    fig.savefig(out/'cube3d.png'); plt.close(fig)
    # Contrato pequeño para clientes que quieran una vista 3D interactiva.
    np.savez_compressed(out/'preview3d.npz', brightness=view, x=xs, y=ys, wavelength_um=wave)
    return dict(url=url, shape=list(cube.shape), pixel=[x,y], unit=unit,
                download_seconds=round(download_seconds, 3),
                total_seconds=round(perf_counter()-started, 3),
                channels=CHANNELS.tolist(), preview_shape=list(view.shape),
                wavelength_unit_assumption='µm solo cuando FITS no declara unidad')

def run(ra, dec, destination, stage='both'):
    coord = coordinate(ra, dec)
    out = Path(destination); out.mkdir(parents=True, exist_ok=True)
    result = dict(ra=float(coord.ra.deg), dec=float(coord.dec.deg), frame='ICRS')
    for name, operation in [('l2', l2), ('allsky', allsky)]:
        if stage in ('both', name):
            try:
                result[name] = operation(coord, out)
            except Exception as exc:
                result[name] = {'error': str(exc)}
    result['status'] = 'partial' if any('error' in v for v in result.values() if isinstance(v, dict)) else 'complete'
    (out/'manifest.json').write_text(json.dumps(result, indent=2, allow_nan=False), encoding='utf-8')
    return result

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ra', type=float, required=True)
    parser.add_argument('--dec', type=float, required=True)
    parser.add_argument('--output', default='outputs')
    parser.add_argument('--stage', choices=['both', 'l2', 'allsky'], default='both')
    args = parser.parse_args()
    result = run(args.ra, args.dec, args.output, args.stage)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['status'] == 'complete' else 1)
