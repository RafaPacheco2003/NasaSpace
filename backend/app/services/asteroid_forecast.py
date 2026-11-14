"""Predicción experimental de la posición de un asteroide con el LSTM exportado a ONNX.

Reproduce EXACTAMENTE el preprocesamiento del notebook de entrenamiento (guía de integración v1.0):

1. La última observación (T3) es el origen local: SkyOffsetFrame centrado en ella convierte las tres
   posiciones RA/Dec en offsets (lon, lat) en grados. Así el modelo no ve RA absoluta y no sufre el
   salto 359° → 0°.
2. X = [lon / position_scale[0], lat / position_scale[1], dt / time_scale]  → float32[1, 3, 3]
   (dt = días desde la observación anterior; 0 en la primera).
3. H = horizon_days / time_scale  → float32[1, 1]
4. Salida predicted_displacement float32[1, 2] × position_scale = (Δlon, Δlat) en grados respecto a T3,
   que se transforma de vuelta a ICRS.

Los escaladores fueron RobustScaler(with_centering=False): solo se divide/multiplica por scale_.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..config import settings
from ..errors import APIError, bad_request

log = logging.getLogger("skyapi.asteroid_lstm")

# Escala de tiempo de las observaciones de objetos del Sistema Solar de Gaia:
# días TCB (Gaia-céntricos) contados desde JD 2455197.5 (2010-01-01T00:00 TCB).
GAIA_EPOCH_JD0 = 2455197.5
SEQ_LEN = 3
INPUT_OBS = "observations"
INPUT_HORIZON = "horizon"
OUTPUT = "predicted_displacement"


@dataclass
class _Model:
    session: object
    pos_scale: np.ndarray
    time_scale: float
    onnx_path: Path


class AsteroidForecaster:
    """Carga perezosa (y segura entre hilos) del modelo; un solo InferenceSession para todo el proceso."""

    def __init__(self, model_dir: str | Path):
        self.model_dir = Path(model_dir)
        self._model: _Model | None = None
        self._error: str | None = None
        self._lock = threading.Lock()

    # --- carga ---------------------------------------------------------------
    def _load(self) -> _Model:
        import onnxruntime as ort  # import diferido: la API arranca aunque falte el modelo

        onnx_path = self.model_dir / "asteroid_lstm.onnx"
        cfg_path = self.model_dir / "preprocessing.json"
        if not onnx_path.is_file() or not cfg_path.is_file():
            raise FileNotFoundError(f"Faltan asteroid_lstm.onnx o preprocessing.json en {self.model_dir}")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        pos_scale = np.asarray(cfg["position_scale"], dtype=np.float64)
        time_scale = float(cfg["time_scale"])
        if pos_scale.shape != (2,) or not np.all(pos_scale > 0):
            raise ValueError("preprocessing.json: position_scale debe tener 2 valores > 0")
        if not time_scale > 0:
            raise ValueError("preprocessing.json: time_scale debe ser > 0")
        if int(cfg.get("seq_len", SEQ_LEN)) != SEQ_LEN:
            raise ValueError(f"preprocessing.json: seq_len debe ser {SEQ_LEN}")

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1  # modelo diminuto: más hilos solo añaden overhead
        session = ort.InferenceSession(str(onnx_path), sess_options=opts, providers=["CPUExecutionProvider"])
        names_in = {i.name for i in session.get_inputs()}
        names_out = {o.name for o in session.get_outputs()}
        if {INPUT_OBS, INPUT_HORIZON} - names_in or OUTPUT not in names_out:
            raise ValueError(f"El ONNX no tiene los nombres esperados (entradas {sorted(names_in)}, salidas {sorted(names_out)})")
        log.info("Modelo LSTM de asteroides cargado (%s, position_scale=%s, time_scale=%g)",
                 onnx_path.name, pos_scale.tolist(), time_scale)
        return _Model(session, pos_scale, time_scale, onnx_path)

    def get(self) -> _Model:
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is None:
                try:
                    self._model = self._load()
                    self._error = None
                except Exception as e:  # noqa: BLE001
                    self._error = f"{type(e).__name__}: {e}"
                    log.error("No se pudo cargar el modelo de asteroides: %s", self._error)
                    raise APIError(503, "model_unavailable",
                                   f"El modelo de predicción de asteroides no está disponible ({self._error}).") from e
        return self._model

    def info(self) -> dict:
        """Para /api/status: intenta cargar y dice si está listo."""
        try:
            m = self.get()
            return {"loaded": True, "file": m.onnx_path.name, "error": None}
        except APIError:
            return {"loaded": False, "file": None, "error": self._error}

    # --- inferencia ----------------------------------------------------------
    def predict(self, epoch: np.ndarray, ra: np.ndarray, dec: np.ndarray, horizon_days: float) -> tuple[float, float]:
        import astropy.units as u
        from astropy.coordinates import SkyCoord

        m = self.get()
        # La última posición es el origen de SkyOffsetFrame.
        reference = SkyCoord(ra[-1] * u.deg, dec[-1] * u.deg, frame="icrs")
        frame = reference.skyoffset_frame()
        local = SkyCoord(ra=ra * u.deg, dec=dec * u.deg, frame="icrs").transform_to(frame)
        positions = np.column_stack((local.lon.wrap_at(180 * u.deg).deg, local.lat.deg))

        dt = np.diff(epoch, prepend=epoch[0])  # [0, t2-t1, t3-t2]
        X = np.column_stack((positions / m.pos_scale, dt / m.time_scale))[None, :, :].astype(np.float32)  # (1,3,3)
        H = np.asarray([[horizon_days / m.time_scale]], dtype=np.float32)  # (1,1)

        out = m.session.run([OUTPUT], {INPUT_OBS: X, INPUT_HORIZON: H})[0]  # (1,2)
        delta = out[0].astype(np.float64) * m.pos_scale
        if not np.all(np.isfinite(delta)):
            raise APIError(500, "internal_error", "El modelo devolvió valores no finitos.")
        fc = SkyCoord(lon=delta[0] * u.deg, lat=delta[1] * u.deg, frame=frame).transform_to("icrs")
        return float(fc.ra.wrap_at(360 * u.deg).deg) % 360.0, float(fc.dec.deg)


forecaster = AsteroidForecaster(settings.model_dir)


# --- utilidades de tiempo -------------------------------------------------------

def _epoch(t) -> float:
    tcb = t.tcb  # jd1/jd2 por separado: restar primero la parte grande conserva la precisión (µs)
    return float((tcb.jd1 - GAIA_EPOCH_JD0) + tcb.jd2)


def gaia_epoch_from_iso(t: str) -> float:
    """Fecha ISO (UTC) → epoch Gaia (días TCB desde JD 2455197.5)."""
    from astropy.time import Time

    try:
        return _epoch(Time(t.replace("Z", ""), format="isot", scale="utc"))
    except ValueError:
        try:
            return _epoch(Time(t.replace("Z", ""), scale="utc"))
        except ValueError as e:
            raise bad_request(f"Fecha no válida: '{t}' (usar ISO 8601, p. ej. 2016-03-01T12:00:00Z)") from e


def iso_from_gaia_epoch(epoch: float) -> str:
    """Epoch Gaia → fecha ISO UTC con milisegundos y sufijo Z."""
    from astropy.time import Time

    from datetime import timedelta

    # jd en dos partes para no perder precisión; redondeo a milisegundos
    dt = Time(GAIA_EPOCH_JD0, epoch, format="jd", scale="tcb").utc.to_datetime() + timedelta(microseconds=500)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def run_forecast(asteroid_id: int | None, observations: list[dict], horizon_days: float) -> dict:
    """Valida, normaliza y ejecuta el modelo. `observations`: dicts con epoch|time, ra, dec."""
    if len(observations) != SEQ_LEN:
        raise bad_request(f"Se requieren exactamente {SEQ_LEN} observaciones (llegaron {len(observations)}).")
    epochs = []
    for i, o in enumerate(observations):
        has_e, has_t = o.get("epoch") is not None, o.get("time") is not None
        if has_e == has_t:
            raise bad_request(f"observations.{i}: indicar 'epoch' (días Gaia) o 'time' (ISO UTC), solo uno de los dos.")
        epochs.append(float(o["epoch"]) if has_e else gaia_epoch_from_iso(o["time"]))
    epoch = np.asarray(epochs, dtype=np.float64)
    ra = np.asarray([o["ra"] for o in observations], dtype=np.float64)
    dec = np.asarray([o["dec"] for o in observations], dtype=np.float64)

    if not np.all(np.isfinite(epoch)) or not np.all(np.isfinite(ra)) or not np.all(np.isfinite(dec)):
        raise bad_request("Epoch, RA o Dec no son números finitos.")
    gaps = np.diff(epoch)
    if np.any(gaps <= 0):
        raise bad_request("Las observaciones deben venir ordenadas por fecha, con epochs estrictamente crecientes.")
    if np.any(gaps > 3.0):
        raise bad_request("La separación entre observaciones consecutivas debe ser ≤ 3 días (rango del entrenamiento).")

    pra, pdec = forecaster.predict(epoch, ra, dec, horizon_days)
    f_epoch = float(epoch[-1] + horizon_days)
    return {
        "asteroid_id": asteroid_id,
        "predicted_ra_deg": pra,
        "predicted_dec_deg": pdec,
        "forecast_epoch": f_epoch,
        "forecast_time": iso_from_gaia_epoch(f_epoch),
        "horizon_days": horizon_days,
        "status": "experimental",
        "frame": "ICRS, geometría Gaia-céntrica (la del entrenamiento)",
        "time_scale": "epoch = días TCB desde JD 2455197.5 (Gaia)",
    }
