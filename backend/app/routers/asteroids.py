"""Asteroides: posición futura con JPL Horizons.

- POST /api/asteroids/predict-from-current → el usuario marca el asteroide (T3) en el frame; Horizons da la posición
  futura, la trayectoria y los píxeles para dibujarla. Sin modelo de IA.
"""
from __future__ import annotations

from fastapi import APIRouter

from ..schemas import CurrentForecastRequest, CurrentForecastResult, responses
from ..services.asteroid_horizons import horizons_forecast
from ..utils import json_response

router = APIRouter(prefix="/api/asteroids", tags=["Asteroides"])


@router.post("/predict-from-current",
             summary="Dónde estará un asteroide (JPL Horizons) + píxeles para dibujarlo en la imagen",
             responses=responses(CurrentForecastResult, 400, 404, 422, 429, 502))
async def predict_from_current(req: CurrentForecastRequest):
    """**Para qué sirve:** el usuario marca el asteroide en el frame que está viendo (T3) y da su número MPC.

    1. **Identidad:** JPL Horizons (observador = SPHEREx, `-163182`) en la fecha de T3. Si el clic está a más de
       60″ de la efeméride → `422 identity_mismatch`.
    2. **Predicción:** posición en T3 + `horizon_days` (0 < h ≤ 30).
    3. **Trayectoria:** 9 posiciones de T3 a la predicción (`track`) para dibujar la línea.
    4. **Píxeles:** con `view` (= ra, dec, fov, size del PNG mostrado) cada posición trae `pixel.x` / `pixel.y_top`
       desde la esquina superior izquierda e `inside_image`.

    Responde 200 directo (~1–2 s la primera vez, instantáneo después; con caché).

    **API externa:** JPL Horizons (e IRSA solo si se manda `image_id` que no está en caché)."""
    p = req.model_dump()
    return json_response(await horizons_forecast(
        asteroid_id=req.asteroid_id, current=p["current_observation"], horizon_days=req.horizon_days,
        image_id=req.image_id, view=p["view"]))
