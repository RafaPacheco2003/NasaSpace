"""POST /api/asteroids/predict (LSTM ONNX). Lista de aceptación de la guía de integración v1.0."""
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.asteroid_forecast import gaia_epoch_from_iso, iso_from_gaia_epoch

URL = "/api/asteroids/predict"
OBS = [
    {"epoch": 1800.0, "ra": 40.0, "dec": 10.0},
    {"epoch": 1800.5, "ra": 40.1, "dec": 10.1},
    {"epoch": 1801.5, "ra": 40.3, "dec": 10.2},
]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def post(client, obs=OBS, h=1.5, **extra):
    return client.post(URL, json={"asteroid_id": 1, "observations": obs, "horizon_days": h, **extra})


def test_ok_dentro_de_rangos(client):
    r = post(client)
    assert r.status_code == 200, r.text
    d = r.json()
    assert 0 <= d["predicted_ra_deg"] < 360 and -90 <= d["predicted_dec_deg"] <= 90
    assert d["forecast_epoch"] == pytest.approx(1803.0)
    assert d["status"] == "experimental" and d["asteroid_id"] == 1
    # Un asteroide no salta grados en día y medio: debe quedar cerca de la última posición
    assert abs(d["predicted_ra_deg"] - 40.3) < 5 and abs(d["predicted_dec_deg"] - 10.2) < 5


def test_horizonte_cambia_la_salida(client):
    a, b = post(client, h=0.5).json(), post(client, h=3.0).json()
    assert (a["predicted_ra_deg"], a["predicted_dec_deg"]) != (b["predicted_ra_deg"], b["predicted_dec_deg"])


@pytest.mark.parametrize("obs,h", [
    ([OBS[1], OBS[0], OBS[2]], 1.5),                         # desordenadas
    (OBS, -1.0),                                             # horizonte negativo
    (OBS, 0),                                                # horizonte cero
    (OBS, 3.5),                                              # horizonte > 3
    (OBS[:2], 1.5),                                          # solo 2 observaciones
    (OBS + [{"epoch": 1802.0, "ra": 40.4, "dec": 10.3}], 1), # 4 observaciones
    ([OBS[0], OBS[1], {"epoch": 1805.0, "ra": 40.3, "dec": 10.2}], 1),  # hueco > 3 días
    ([OBS[0], OBS[1], {"epoch": 1801.5, "ra": 361, "dec": 10.2}], 1),   # RA fuera de rango
    ([OBS[0], OBS[1], {"ra": 40.3, "dec": 10.2}], 1),                   # sin epoch ni time
])
def test_invalidos_400(client, obs, h):
    r = post(client, obs=obs, h=h)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "bad_request"


def test_cruce_de_ra_cero(client):
    obs = [{"epoch": 1800.0, "ra": 359.8, "dec": 1.0},
           {"epoch": 1800.5, "ra": 359.95, "dec": 1.0},
           {"epoch": 1801.0, "ra": 0.1, "dec": 1.0}]
    d = post(client, obs=obs, h=0.5).json()
    sep = min(abs(d["predicted_ra_deg"] - 0.1), 360 - abs(d["predicted_ra_deg"] - 0.1))
    assert sep < 5


def test_time_iso_equivale_a_epoch(client):
    obs_t = [{"time": iso_from_gaia_epoch(o["epoch"]), "ra": o["ra"], "dec": o["dec"]} for o in OBS]
    a, b = post(client).json(), post(client, obs=obs_t).json()
    assert a["predicted_ra_deg"] == pytest.approx(b["predicted_ra_deg"], abs=1e-6)
    assert gaia_epoch_from_iso("2010-01-01T00:00:00Z") == pytest.approx(82.33 / 86400, abs=1e-6)  # TCB−UTC ≈ 82 s en 2010


def test_status_reporta_modelo(client):
    m = client.get("/api/status").json()["models"]["asteroid_lstm"]
    assert m["loaded"] is True


def test_reimplementacion_de_la_guia(client):
    """La salida del endpoint coincide con el código de referencia de la guía (sección 04), paso a paso."""
    import json
    from pathlib import Path

    import astropy.units as u
    import onnxruntime as ort
    from astropy.coordinates import SkyCoord

    from app.config import settings
    cfg = json.loads((Path(settings.model_dir) / "preprocessing.json").read_text())
    ps, ts = np.asarray(cfg["position_scale"]), float(cfg["time_scale"])
    sess = ort.InferenceSession(str(Path(settings.model_dir) / "asteroid_lstm.onnx"), providers=["CPUExecutionProvider"])
    ep = np.array([o["epoch"] for o in OBS]); ra = np.array([o["ra"] for o in OBS]); de = np.array([o["dec"] for o in OBS])
    frame = SkyCoord(ra[-1] * u.deg, de[-1] * u.deg).skyoffset_frame()
    loc = SkyCoord(ra=ra * u.deg, dec=de * u.deg).transform_to(frame)
    X = np.column_stack((np.column_stack((loc.lon.deg, loc.lat.deg)) / ps, np.diff(ep, prepend=ep[0]) / ts))[None].astype(np.float32)
    H = np.asarray([[1.5 / ts]], dtype=np.float32)
    dlt = sess.run(["predicted_displacement"], {"observations": X, "horizon": H})[0][0].astype(np.float64) * ps
    fc = SkyCoord(lon=dlt[0] * u.deg, lat=dlt[1] * u.deg, frame=frame).transform_to("icrs")
    d = post(client).json()
    assert d["predicted_ra_deg"] == pytest.approx(fc.ra.deg, abs=1e-9)
    assert d["predicted_dec_deg"] == pytest.approx(fc.dec.deg, abs=1e-9)
