"""POST /api/asteroids/predict-from-current con JPL Horizons (simulado: asteroide que se mueve en línea recta)."""
import math

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import asteroid_horizons as H

T3 = 61200.40
RA0, DEC0, VRA, VDEC = 120.0, 15.0, 0.20, -0.05  # °/día


def true_pos(mjd):
    dt = mjd - T3
    return RA0 + VRA * dt / math.cos(math.radians(DEC0)), DEC0 + VDEC * dt


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def fake_horizons(monkeypatch):
    calls = []

    async def fake_eph(asteroid_id, mjds):
        calls.append(list(mjds))
        if asteroid_id != 4242:
            raise H.APIError(404, "not_found", f"JPL Horizons no conoce el asteroide número {asteroid_id}.")
        return "4242 Sintético", "500@-163182", [(*true_pos(m), 0.05) for m in mjds]

    monkeypatch.setattr(H, "cached_ephemeris", fake_eph)
    return calls


def post(client, ra=None, dec=None, aid=4242, h=1.0, **kw):
    r3, d3 = true_pos(T3)
    body = {"asteroid_id": aid, "horizon_days": h,
            "current_observation": {"mjd_utc": T3, "ra": r3 if ra is None else ra, "dec": d3 if dec is None else dec}, **kw}
    return client.post("/api/asteroids/predict-from-current", json=body)


def test_prediccion_es_la_de_horizons(client, fake_horizons):
    r = post(client, h=1.5)
    assert r.status_code == 200, r.text          # directo, sin trabajo 202
    d = r.json()
    ra, dec = true_pos(T3 + 1.5)
    assert d["predicted_ra_deg"] == pytest.approx(ra, abs=1e-6) and d["predicted_dec_deg"] == pytest.approx(dec, abs=1e-6)
    assert d["method"] == "horizons" and d["status"] == "ephemeris" and d["target"] == "4242 Sintético"
    assert d["forecast_mjd_utc"] == pytest.approx(T3 + 1.5)
    assert len(d["track"]) == H.TRACK_POINTS
    assert d["track"][0]["mjd_utc"] == pytest.approx(T3) and d["track"][-1]["mjd_utc"] == pytest.approx(T3 + 1.5)
    assert d["current_observation"]["offset_arcsec"] < 0.01
    assert d["pixel"] is None and d["track"][0]["pixel"] is None   # sin view no hay píxeles
    # movimiento hacia el este (RA crece) y un poco al sur → ángulo de posición entre 90° y 180°
    assert 90 < d["motion"]["position_angle_deg"] < 180
    v = math.hypot(VRA, VDEC) * 3600 / 24
    assert d["motion"]["arcsec_per_hour"] == pytest.approx(v, rel=0.01)
    assert len(fake_horizons) == 1                # una sola consulta a Horizons


def test_pixeles_en_la_vista(client, fake_horizons):
    r3, d3 = true_pos(T3)
    view = {"ra": r3, "dec": d3, "fov": 3600, "size": 512}
    d = post(client, h=0.1, view=view).json()
    cur = d["current_observation"]["pixel"]
    assert cur["inside_image"] and cur["x"] == pytest.approx(256, abs=0.01) and cur["y_top"] == pytest.approx(256, abs=0.01)
    # 0.1 d → 0.02° al este = 72″ → ~10 px a la IZQUIERDA (este a la izquierda); 18″ al sur → ~2.6 px ABAJO
    p = d["pixel"]
    assert p["inside_image"] and p["x"] < 256 and p["y_top"] > 256
    assert d["track"][-1]["pixel"]["x"] == pytest.approx(p["x"])
    # horizonte largo: la predicción sale del campo → inside_image false, x None
    far = post(client, h=10, view=view).json()["pixel"]
    assert far["inside_image"] is False and far["x"] is None


def test_identidad_incorrecta_422(client, fake_horizons):
    r = post(client, ra=true_pos(T3)[0] + 0.1)
    assert r.status_code == 422 and r.json()["error"]["code"] == "identity_mismatch"


def test_asteroide_desconocido_404(client, fake_horizons):
    r = post(client, aid=7)
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("kw", [{"h": 0}, {"h": 31}, {"aid": 0}, {"image_id": "sx-d1-1"}])
def test_invalidos_400(client, fake_horizons, kw):
    r = post(client, **kw)
    assert r.status_code == 400, r.text


def test_rutas_del_modelo_ya_no_existen(client):
    assert client.post("/api/asteroids/predict", json={}).status_code in (404, 405)
    assert client.post("/api/asteroids/predict-from-observations", json={}).status_code in (404, 405)
