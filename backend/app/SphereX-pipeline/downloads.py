"""Descarga íntegra de FITS con rangos HTTP y caché persistente por URL."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

import requests
from astropy.utils.data import download_file, is_url_in_cache


def fetch(url, cache_dir=None, workers=None):
    if urlsplit(str(url)).scheme not in ('http', 'https'):
        return Path(url)
    # Reutilizar también los archivos que fits.open(cache=True) ya descargó.
    if is_url_in_cache(url):
        return Path(download_file(url, cache=True, show_progress=False))
    root = Path(cache_dir or os.environ.get(
        'SPHEREX_CACHE_DIR', Path(__file__).resolve().parent/'.cache'/'downloads'))
    root.mkdir(parents=True, exist_ok=True)
    target = root/(hashlib.sha256(url.encode()).hexdigest()+'.fits')
    if target.is_file():
        return target
    workers = int(workers if workers is not None else os.environ.get('SPHEREX_DOWNLOAD_WORKERS', '4'))
    if not 1 <= workers <= 8:
        raise ValueError('SPHEREX_DOWNLOAD_WORKERS debe estar entre 1 y 8.')
    headers = {'Accept-Encoding': 'identity'}
    timeout = (10, 30)
    with tempfile.TemporaryDirectory(dir=root) as temp:
        path = Path(temp)/'download.fits'
        # Comprobar soporte real de Range sin descargar el archivo completo.
        with requests.get(url, headers={**headers, 'Range': 'bytes=0-0'},
                          stream=True, timeout=timeout) as response:
            response.raise_for_status()
            match = re.fullmatch(r'bytes 0-0/(\d+)', response.headers.get('Content-Range', ''))
            total = int(match[1]) if response.status_code == 206 and match else 0
            validator = response.headers.get('ETag')
            if not validator or validator.startswith('W/'):
                validator = response.headers.get('Last-Modified')
        if total and validator and workers > 1:
            # Cada hilo escribe una región distinta; memoria acotada a 1 MiB/hilo.
            with path.open('wb') as output:
                output.truncate(total)
            count = min(workers, total)
            def part(index):
                start, end = total*index//count, total*(index+1)//count-1
                request_headers = {**headers, 'Range': f'bytes={start}-{end}', 'If-Range': validator}
                with requests.get(url, headers=request_headers, stream=True, timeout=timeout) as response:
                    response.raise_for_status()
                    expected = f'bytes {start}-{end}/{total}'
                    if response.status_code != 206 or response.headers.get('Content-Range') != expected:
                        raise OSError('El servidor cambió o no respetó el rango solicitado.')
                    position = start
                    with path.open('r+b') as output:
                        output.seek(start)
                        for chunk in response.iter_content(1024*1024):
                            position += len(chunk)
                            if position > end+1:
                                raise OSError('Rango HTTP demasiado largo.')
                            output.write(chunk)
                    if position != end+1:
                        raise OSError('Rango HTTP incompleto.')
            try:
                with ThreadPoolExecutor(max_workers=count) as pool:
                    list(pool.map(part, range(count)))
            except (requests.RequestException, OSError):
                total = 0  # Respaldo secuencial ante servidores incompatibles.
        else:
            total = 0
        if not total:
            with requests.get(url, headers=headers, stream=True, timeout=timeout) as response:
                response.raise_for_status()
                if response.status_code != 200:
                    raise OSError('La descarga completa no devolvió HTTP 200.')
                with path.open('wb') as output:
                    for chunk in response.iter_content(1024*1024):
                        output.write(chunk)
                length = response.headers.get('Content-Length')
                if length and path.stat().st_size != int(length):
                    raise OSError('Descarga HTTP incompleta.')
        # Publicar solo descargas completas; no reutilizar parciales tras fallos.
        if not path.stat().st_size:
            raise OSError('Archivo descargado vacío.')
        os.replace(path, target)
    return target
