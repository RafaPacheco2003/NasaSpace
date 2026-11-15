# SI-assisted development used for code development, debugging and documentation.
# Tool: ChatGPT, Claude

"""Procesos lentos: patrón 202 + trabajo.

- El id es determinista (hash de la consulta): la misma consulta devuelve el mismo trabajo.
- Como máximo MAX_CONCURRENT_JOBS corren a la vez; el resto espera en cola (queue_position).
  Las descargas pesadas (cubo, L2) además se limitan a MAX_HEAVY_JOBS para que no acaparen los lugares.
- Trabajos huérfanos: si nadie consulta un trabajo (ni lo vuelve a pedir) durante JOB_ORPHAN_QUEUED_S (en cola)
  o JOB_ORPHAN_RUNNING_S (corriendo), se PAUSA: libera su lugar. Si alguien lo vuelve a consultar o pedir, se
  reanuda solo con el mismo id (lo ya descargado sigue en caché). Para el cliente es transparente.
- Al terminar, el resultado se guarda en la caché (RESULT_TTL_S, 7 días): la siguiente petición igual recibe 200.
- Los trabajos viven en memoria JOB_TTL_S; después /api/jobs/{id} usa la caché o responde 404.

Nota: el estado vive en memoria → ejecutar uvicorn con UN solo worker (ver Dockerfile).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

from .cache import cget, cset, key
from .config import settings
from .errors import APIError, UpstreamError, upstream_failure

log = logging.getLogger("skyapi.jobs")

HEAVY_KINDS = {"cube", "l2"}

PREFIX_KIND = {"seq": "sequence", "stk": "stack", "prd": "predict", "spc": "spectrum", "l2p": "l2", "cub": "cube", "cmp": "composition"}


def job_id(prefix: str, params: dict) -> str:
    h = hashlib.sha1(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:12]
    return f"{prefix}_{h}"


def result_key(jid: str) -> str:
    return key("result", jid)


class Job:
    def __init__(self, jid: str, kind: str) -> None:
        self.id = jid
        self.kind = kind
        self.status = "queued"
        self.progress = 0.0
        self.stage = "En cola"
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.result: dict | None = None
        self.error: dict | None = None
        self.task: asyncio.Task | None = None
        self.fn: Callable[["Job"], Awaitable[dict]] | None = None
        self.last_seen = time.time()   # última vez que alguien lo pidió o consultó
        self.paused = False
        self.p_ref: float | None = None  # progreso/tiempo de referencia para estimar la velocidad
        self.t_ref: float | None = None

    def update(self, progress: float | None = None, stage: str | None = None) -> None:
        if progress is not None:
            self.progress = max(self.progress, min(0.99, float(progress)))
            if self.p_ref is None:
                self.p_ref, self.t_ref = self.progress, time.time()
        if stage:
            self.stage = stage


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.queue: list[str] = []
        self._sem: asyncio.Semaphore | None = None
        self._heavy: asyncio.Semaphore | None = None
        self._durations: dict[str, list[float]] = {}
        self.paused_total = 0

    @property
    def sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(settings.max_concurrent_jobs)
        return self._sem

    @property
    def heavy(self) -> asyncio.Semaphore:
        if self._heavy is None:
            self._heavy = asyncio.Semaphore(max(1, settings.max_heavy_jobs))
        return self._heavy

    def _expire(self) -> None:
        now = time.time()
        for jid in [j.id for j in self.jobs.values() if j.finished and now - j.finished > settings.job_ttl_s]:
            self.jobs.pop(jid, None)
        for jid in [j.id for j in self.jobs.values() if j.paused and now - j.last_seen > settings.job_ttl_s]:
            self.jobs.pop(jid, None)
        self._pause_orphans(now)

    def _pause_orphans(self, now: float) -> None:
        """Pausa los trabajos que nadie espera: liberan su lugar en la cola / el semáforo."""
        for job in list(self.jobs.values()):
            if job.paused or job.task is None or job.task.done():
                continue
            limit = settings.job_orphan_queued_s if job.status == "queued" else settings.job_orphan_running_s
            if limit and now - job.last_seen > limit:
                log.info("Trabajo %s pausado: nadie lo consulta desde hace %.0f s (%s)", job.id, now - job.last_seen, job.status)
                job.paused = True
                job.task.cancel()
                self.paused_total += 1

    def _start(self, job: Job) -> None:
        job.status = "queued"
        job.stage = "En cola"
        job.paused = False
        job.p_ref = job.t_ref = None
        if job.id not in self.queue:
            self.queue.append(job.id)
        assert job.fn is not None
        job.task = asyncio.create_task(self._run(job, job.fn))

    def _touch(self, job: Job) -> None:
        job.last_seen = time.time()
        if job.paused and job.fn is not None:
            log.info("Trabajo %s reanudado", job.id)
            self._start(job)

    def get(self, jid: str) -> Job | None:
        self._expire()
        job = self.jobs.get(jid)
        if job is not None:
            self._touch(job)
        return job

    def submit(self, jid: str, kind: str, fn: Callable[[Job], Awaitable[dict]]) -> Job:
        self._expire()
        job = self.jobs.get(jid)
        if job is not None:
            retry = job.status == "failed" and job.finished and time.time() - job.finished > settings.failed_job_retry_s
            if not retry:
                self._touch(job)
                return job
        job = Job(jid, kind)
        job.fn = fn
        self.jobs[jid] = job
        self._start(job)
        return job

    async def _run(self, job: Job, fn: Callable[[Job], Awaitable[dict]]) -> None:
        heavy = self.heavy if job.kind in HEAVY_KINDS else None
        try:
            if heavy is not None:
                job.stage = "En cola (otra descarga grande en curso)" if heavy.locked() else job.stage
                await heavy.acquire()   # espera SIN ocupar un lugar general
            try:
                async with self.sem:
                    if job.id in self.queue:
                        self.queue.remove(job.id)
                    job.status = "running"
                    job.started = time.time()
                    job.stage = "Iniciando"
                    result = await fn(job)
            finally:
                if heavy is not None:
                    heavy.release()
            job.result = result
            job.progress = 1.0
            job.stage = "Listo"
            job.status = "done"
            cset(result_key(job.id), result, settings.result_ttl_s)
            self._durations.setdefault(job.kind, []).append(time.time() - (job.started or job.created))
            self._durations[job.kind] = self._durations[job.kind][-20:]
        except asyncio.CancelledError:
            if asyncio.current_task() is not job.task:
                return  # tarea vieja de un trabajo ya reanudado: no tocar su estado
            if not job.paused:
                raise
            # Pausado por huérfano: vuelve a "queued" y espera a que alguien lo consulte (ver _touch).
            job.status = "queued"
            job.stage = "En pausa (nadie lo consultaba)"
            job.started = None
            if job.id in self.queue:
                self.queue.remove(job.id)
            return
        except APIError as e:
            job.error = e.payload()
            job.status = "failed"
        except UpstreamError as e:
            job.error = upstream_failure([e]).payload()
            job.status = "failed"
        except Exception as e:  # noqa: BLE001
            log.exception("Trabajo %s falló", job.id)
            job.error = {"code": "internal_error", "message": f"Error interno: {type(e).__name__}: {e}"[:300]}
            job.status = "failed"
        finally:
            if asyncio.current_task() is job.task and not job.paused:  # (pausado/reanudado: no tocar)
                job.finished = time.time()
                if job.id in self.queue:
                    self.queue.remove(job.id)
                if job.status == "failed":
                    job.stage = "Falló"

    def summary(self) -> dict:
        act = [j for j in self.jobs.values() if not j.paused and j.task is not None and not j.task.done()]
        return {"running": sum(j.status == "running" for j in act), "queued": sum(j.status == "queued" for j in act),
                "paused_now": sum(j.paused for j in self.jobs.values()), "paused_total": self.paused_total,
                "max_concurrent": settings.max_concurrent_jobs, "max_heavy": settings.max_heavy_jobs}

    def queue_position(self, job: Job) -> int:
        if job.status != "queued" or job.id not in self.queue:
            return 0
        return self.queue.index(job.id) + 1

    def _avg(self, kind: str) -> float:
        d = self._durations.get(kind)
        return sum(d) / len(d) if d else 30.0

    def eta(self, job: Job) -> int | None:
        if job.status in ("done", "failed"):
            return 0
        if job.status == "queued":
            return int(round(self._avg(job.kind) * (self.queue_position(job) / max(1, settings.max_concurrent_jobs) + 1)))
        now = time.time()
        elapsed = now - (job.started or job.created)
        if job.p_ref is not None and job.t_ref is not None and job.progress - job.p_ref > 0.02:
            rate = (job.progress - job.p_ref) / max(now - job.t_ref, 1e-3)
            return int(round((1 - job.progress) / rate))
        return int(round(max(self._avg(job.kind) - elapsed, 2)))

    def to_dict(self, job: Job) -> dict:
        return {
            "id": job.id,
            "kind": job.kind,
            "status": job.status,
            "progress": round(job.progress, 3),
            "eta_s": self.eta(job),
            "stage": job.stage,
            "queue_position": self.queue_position(job),
            "poll_after_ms": 1500 if job.status == "queued" else 800,
            "created_at": datetime.fromtimestamp(job.created, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "result": job.result if job.status == "done" else None,
            "error": job.error if job.status == "failed" else None,
        }

    def cached_done(self, jid: str) -> dict | None:
        """Trabajo ya expirado en memoria pero con resultado en caché → se reporta como done."""
        res = cget(result_key(jid))
        if res is None:
            return None
        kind = PREFIX_KIND.get(jid.split("_", 1)[0], "sequence")
        return {
            "id": jid, "kind": kind, "status": "done", "progress": 1.0, "eta_s": 0, "stage": "Listo",
            "queue_position": 0, "poll_after_ms": 800, "created_at": None, "result": res, "error": None,
        }


jobs = JobManager()
