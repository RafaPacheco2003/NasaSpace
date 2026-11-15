"""Servidor local y API asíncrona para el formulario RA/DEC."""
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from pipeline import coordinate, run

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT/'outputs'
JOBS = {}
LOCK = threading.Lock()
POOL = ThreadPoolExecutor(max_workers=1)  # Matplotlib y memoria: un trabajo científico a la vez.

def process(job, ra, dec):
    with LOCK:
        JOBS[job] = {'status': 'running'}
    try:
        result = run(ra, dec, OUTPUT/job)
        state = {'status': result['status'], 'result': result, 'base': f'/outputs/{job}/'}
    except Exception as exc:
        state = {'status': 'failed', 'error': str(exc)}
    with LOCK:
        JOBS[job] = state

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def reply(self, status, body):
        data = json.dumps(body, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def do_POST(self):
        if self.path != '/api/jobs':
            return self.reply(404, {'error': 'Ruta no encontrada'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2048:
                raise ValueError('Cuerpo JSON requerido; máximo 2048 bytes.')
            data = json.loads(self.rfile.read(length))
            coord = coordinate(data['ra'], data['dec'])
        except (ValueError, KeyError, TypeError) as exc:
            return self.reply(400, {'error': str(exc)})
        with LOCK:
            if any(v['status'] in ('queued', 'running') for v in JOBS.values()):
                return self.reply(409, {'error': 'Hay una búsqueda en curso; espera a que termine.'})
            job = uuid.uuid4().hex
            JOBS[job] = {'status': 'queued'}
        POOL.submit(process, job, coord.ra.deg, coord.dec.deg)
        self.reply(202, {'id': job})

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith('/api/jobs/'):
            with LOCK:
                state = JOBS.get(path.rsplit('/', 1)[-1])
            return self.reply(200 if state else 404, state or {'error': 'Trabajo inexistente'})
        if path == '/':
            self.path = '/index.html'
        elif not path.startswith('/outputs/'):
            return self.reply(404, {'error': 'Ruta no encontrada'})
        else:
            # No servir código ni permitir salir del directorio de resultados.
            from urllib.parse import unquote
            resolved = (ROOT/unquote(path).lstrip('/')).resolve()
            if not resolved.is_relative_to(OUTPUT.resolve()) or not resolved.is_file():
                return self.reply(404, {'error': 'Archivo no encontrado'})
        super().do_GET()

if __name__ == '__main__':
    print('Formulario RA/DEC: http://127.0.0.1:8081', flush=True)
    ThreadingHTTPServer(('127.0.0.1', 8081), Handler).serve_forever()
