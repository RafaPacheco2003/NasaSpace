"""Parámetros reutilizables y el helper del patrón 202 + trabajo."""
from __future__ import annotations

from typing import Awaitable, Callable, Literal

from fastapi import Query

from ..cache import cget
from ..config import MAX_FOV, MAX_SIZE, MIN_FOV, MIN_SIZE
from ..jobs import Job, job_id, jobs, result_key
from ..utils import json_response

Stretch = Literal["linear", "log", "sqrt", "asinh"]
Cmap = Literal["gray", "viridis", "magma", "inferno"]

RA = Query(..., ge=0, lt=360, description="Ascensión recta en grados (ICRS/J2000).", examples=[133.795129])
DEC = Query(..., ge=-90, le=90, description="Declinación en grados (ICRS/J2000).", examples=[-7.245147])
FOV = Query(300, ge=MIN_FOV, le=MAX_FOV, description="Campo de visión (lado del cuadro) en segundos de arco.")
SIZE = Query(512, ge=MIN_SIZE, le=MAX_SIZE, description="Lado del PNG en píxeles (la imagen siempre es cuadrada).")
DETECTOR = Query(
    None, ge=1, le=6,
    description="Detector de SPHEREx. 1: 0.75–1.09 µm · 2: 1.10–1.62 · 3: 1.63–2.41 · 4: 2.42–3.82 · "
                "5: 3.83–4.41 · 6: 4.42–5.00. Sin valor = todos.",
)
AT = Query(None, description="Fecha a la que se propagan las posiciones con movimiento propio. Sin valor = hoy.")

JOB_202_DOC = {202: {"description": "Trabajo creado o en curso; consultar /api/jobs/{id}"}}


def run_or_job(kind: str, prefix: str, params: dict, fn: Callable[[Job], Awaitable[dict]]):
    """200 con el resultado si está en caché; si no, 202 con el Job (id determinista)."""
    jid = job_id(prefix, params)
    res = cget(result_key(jid))
    if res is not None:
        if isinstance(res, dict) and "cache" in res:
            res = {**res, "cache": "hit"}
        return json_response(res, 200)
    job = jobs.submit(jid, kind, fn)
    if job.status == "done" and job.result is not None:
        return json_response(job.result, 200)
    return json_response(jobs.to_dict(job), 202)
