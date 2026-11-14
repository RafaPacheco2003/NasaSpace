"""Asteroides (ML): POST /api/asteroids/predict — LSTM exportado a ONNX."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool

from ..schemas import AsteroidForecastRequest, AsteroidForecastResult, responses
from ..services.asteroid_forecast import run_forecast
from ..utils import json_response

router = APIRouter(prefix="/api/asteroids", tags=["Asteroides (ML)"])


@router.post("/predict", summary="Posición futura de un asteroide con el LSTM (EXPERIMENTAL)",
             responses=responses(AsteroidForecastResult, 400, 429, 500, 503))
async def predict_asteroid(req: AsteroidForecastRequest):
    """**Para qué sirve:** dadas **3 observaciones** del mismo asteroide (huecos ≤ 3 días) y un horizonte
    `horizon_days` (0 < h ≤ 3), estima dónde estará (RA/Dec) con una red LSTM entrenada con
    observaciones de **Gaia** y exportada a ONNX. Sin API externa; corre en CPU en milisegundos.

    **Tiempo:** `epoch` = días TCB desde JD 2455197.5 (columna `epoch` de Gaia SSO), **no** Unix ni año.
    Si prefieres fechas normales, manda `time` (ISO UTC) en lugar de `epoch` y la API lo convierte.

    **Límites:** geometría Gaia-céntrica (no es la posición vista desde la Tierra ni desde SPHEREx),
    sin incertidumbre calibrada y no validado fuera de 3 días. Para efemérides de verdad usar
    `GET /api/predict` (JPL Horizons). Validación fallida → **400** con la forma de error del contrato."""
    obs = [o.model_dump() for o in req.observations]
    result = await run_in_threadpool(run_forecast, req.asteroid_id, obs, req.horizon_days)
    return json_response(result)
