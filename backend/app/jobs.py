"""Procesos lentos: patrón 202 + trabajo.

- El id es determinista (hash de la consulta): la misma consulta devuelve el mismo trabajo.
- Como máximo MAX_CONCURRENT_JOBS corren a la vez; el resto espera en cola (queue_position).
- Al terminar, el resultado se guarda en la caché (24 h): la siguiente petición igual recibe 200.
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

PREFIX_KIND = {"seq": "sequence", "stk": "stack", "prd": "predict", "spc": "spectrum"}


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
        self._durations: dict[str, list[float]] = {}

    @property
    def sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(settings.max_concurrent_jobs)
        return self._sem

    def _expire(self) -> None:
        now = time.time()
        for jid in [j.id for j in self.jobs.values() if j.finished and now - j.finished > settings.job_ttl_s]:
            self.jobs.pop(jid, None)

    def get(self, jid: str) -> Job | None:
        self._expire()
        return self.jobs.get(jid)

    def submit(self, jid: str, kind: str, fn: Callable[[Job], Awaitable[dict]]) -> Job:
        self._expire()
        job = self.jobs.get(jid)
        if job is not None:
            retry = job.status == "failed" and job.finished and time.time() - job.finished > settings.failed_job_retry_s
            if not retry:
                return job
        job = Job(jid, kind)
        self.jobs[jid] = job
        self.queue.append(jid)
        job.task = asyncio.create_task(self._run(job, fn))
        return job

    async def _run(self, job: Job, fn: Callable[[Job], Awaitable[dict]]) -> None:
        try:
            async with self.sem:
                if job.id in self.queue:
                    self.queue.remove(job.id)
                job.status = "running"
                job.started = time.time()
                job.stage = "Iniciando"
                result = await fn(job)
            job.result = result
            job.progress = 1.0
            job.stage = "Listo"
            job.status = "done"
            cset(result_key(job.id), result)
            self._durations.setdefault(job.kind, []).append(time.time() - (job.started or job.created))
            self._durations[job.kind] = self._durations[job.kind][-20:]
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
            job.finished = time.time()
            if job.id in self.queue:
                self.queue.remove(job.id)
            if job.status == "failed":
                job.stage = "Falló"

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
