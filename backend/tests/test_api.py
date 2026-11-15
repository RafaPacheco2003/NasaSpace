# SI-assisted development used for code development, debugging and documentation.
# Tool: ChatGPT, Claude

"""Tests sin red (SPHEREX_MODE=mock y catálogos simulados)."""
import io
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from astropy.time import Time

from app.astro import propagate, target_wcs
from app.main import app
from app.sources import catalogs as cat
from app.sources import resolver

RA, DEC = 133.795129, -7.245147  # WISE 0855


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def wait_job(client, resp, timeout=60):
    if resp.status_code == 200:
        return resp.json()
    assert resp.status_code == 202, resp.text
    job = resp.json()
    assert {"id", "kind", "status", "progress", "poll_after_ms"} <= job.keys()
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/jobs/{job['id']}").json()
        if j["status"] == "done":
            return j["result"]
        assert j["status"] != "failed", j["error"]
        time.sleep(0.2)
    raise AssertionError("timeout")


def test_error_shape_validation(client):
    r = client.get("/api/objects", params={"ra": 10, "dec": 5, "radius": 601})
    assert r.status_code == 400
    assert r.json() == {"error": {"code": "bad_request", "message": "radius supera el máximo (600)"}}
    r = client.get("/api/frames", params={"ra": 400, "dec": 0})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"
    r = client.get("/api/frames", params={"dec": 0})
    assert "Falta" in r.json()["error"]["message"]


def test_unknown_route_and_job(client):
    assert client.get("/api/nope").json()["error"]["code"] == "not_found"
    r = client.get("/api/jobs/seq_ffffffffffff")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_cors(client):
    r = client.get("/healthz", headers={"Origin": "http://example.com"})
    assert r.headers["access-control-allow-origin"] == "*"


def test_geometry_matches_contract():
    size, fov, ra0, dec0 = 512, 300.0, RA, DEC
    w = target_wcs(ra0, dec0, fov, size)
    scale = size / fov
    ra, dec = ra0 + 0.01, dec0 + 0.005
    px, py = w.world_to_pixel_values(ra, dec)
    # Convención del contrato: x desde la izquierda, y desde arriba (PNG con norte arriba)
    x = px + 0.5
    y = size - (py + 0.5)
    x_c = size / 2 - (ra - ra0) * np.cos(np.radians(dec0)) * 3600 * scale
    y_c = size / 2 - (dec - dec0) * 3600 * scale
    assert abs(x - x_c) < 0.05 and abs(y - y_c) < 0.05


def test_barnard_proper_motion():
    # Barnard: pmDE ≈ +10.36″/año → en 26.5 años ≈ +275″ al norte
    ra, dec = propagate(269.452, 4.6934, -801.551, 10362.394, 2000.0, Time("2026-07-01"))
    assert 250 < (float(dec) - 4.6934) * 3600 < 300


def test_resolve_with_current(client, monkeypatch):
    async def fake(name):
        return {"matched_name": "Barnard's star", "ra": 269.452, "dec": 4.6934, "source": "simbad", "epoch": 2000.0,
                "pm": {"ra": -801.551, "dec": 10362.394}, "parallax_mas": 546.98, "otype": "BY*"}
    monkeypatch.setattr(resolver, "resolve_name", fake)
    r = client.get("/api/resolve", params={"name": "Barnard test", "at": "2026-01-01"})
    body = r.json()
    assert r.status_code == 200
    assert body["source"] == "simbad" and body["epoch"] == 2000.0
    assert body["current"]["dec"] > body["dec"] and abs(body["current"]["epoch"] - 2026.0) < 0.01


def test_objects_partial(client, monkeypatch):
    async def ok(ra, dec, r):
        return [{"id": "simbad:X", "name": "X", "source": "simbad", "ra": ra, "dec": dec, "type": "star",
                 "type_raw": "*", "z": None, "distance_arcsec": 1.0, "pm": None}]

    async def boom(ra, dec, r):
        from app.errors import UpstreamError
        raise UpstreamError("ned", "sin respuesta en 10 s", timeout=True)

    monkeypatch.setattr(cat, "simbad_cone", ok)
    monkeypatch.setattr(cat, "ned_cone", boom)
    r = client.get("/api/objects", params={"ra": 11.1, "dec": 2.2})
    body = r.json()
    assert r.status_code == 200 and body["partial"] is True and body["counts"] == {"simbad": 1}
    assert body["warnings"][0].startswith("ned")
    r = client.get("/api/objects", params={"ra": 11.2, "dec": 2.2, "catalogs": "ned"})
    assert r.status_code == 504


def test_frames_and_coverage(client):
    r = client.get("/api/frames", params={"ra": RA, "dec": DEC, "detector": 3, "limit": 5})
    body = r.json()
    assert r.status_code == 200 and body["total"] > 0 and len(body["frames"]) == 5
    assert all(f["detector"] == 3 for f in body["frames"])
    dates = [f["date_obs"] for f in body["frames"]]
    assert dates == sorted(dates)
    cov = client.get("/api/coverage", params={"ra": RA, "dec": DEC}).json()
    assert sum(w["count"] for w in cov["weeks"]) == cov["total"]


def test_sequence_stack_flow(client):
    params = {"ra": RA, "dec": DEC, "fov": 120, "size": 128, "detector": 6, "limit": 4}
    res = wait_job(client, client.get("/api/sequence", params=params))
    assert res["size_px"] == 128 and res["unit"] == "MJy/sr" and len(res["frames"]) == 4
    assert res["vmax"] > res["vmin"]
    # Segunda vez: 200 desde caché
    assert client.get("/api/sequence", params=params).status_code == 200
    png = client.get(res["frames"][0]["png_url"])
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(png.content)).size == (128, 128)
    assert client.get(res["frames"][0]["flags_png_url"]).status_code == 200

    ids = [f["id"] for f in res["frames"]]
    st = wait_job(client, client.get("/api/stack", params={"ra": RA, "dec": DEC, "fov": 120, "size": 128,
                                                           "op": "diff", "a": ids[-1], "b": ids[0]}))
    assert st["op"] == "diff" and st["vmin"] == -st["vmax"]
    img = client.get(st["png_url"])
    assert img.status_code == 200 and Image.open(io.BytesIO(img.content)).size == (128, 128)
    r = client.get("/api/stack", params={"ra": RA, "dec": DEC, "op": "diff", "a": ids[0]})
    assert r.status_code == 400


def test_spectrum(client):
    res = wait_job(client, client.get("/api/spectrum", params={"ra": RA, "dec": DEC, "aperture": 6}))
    assert res["wavelength_unit"] == "um" and res["flux_unit"] == "uJy"
    wl = [p["wavelength"] for p in res["points"]]
    assert len(wl) > 10 and wl == sorted(wl) and 0.7 < wl[0] and wl[-1] < 5.1
    assert {"date_obs", "detector", "frame_id", "flags", "error"} <= res["points"][0].keys()


def test_irsa_down_uses_local_index(client, monkeypatch):
    """Si la búsqueda de IRSA cae (502), se usan las exposiciones conocidas que cubren el punto."""
    from app import spherex as sx
    from app.errors import UpstreamError

    base = client.get("/api/frames", params={"ra": 200.0, "dec": 10.0}).json()
    assert base["total"] > 0 and base["partial"] is False

    async def down(ra, dec):
        raise UpstreamError("spherex", "HTTP 502")
    monkeypatch.setattr(sx.provider(), "search", down)

    # Punto nuevo a 2′ del anterior: no está en caché → índice local
    r = client.get("/api/frames", params={"ra": 200.03, "dec": 10.01})
    body = r.json()
    assert r.status_code == 200 and body["partial"] is True and body["total"] == base["total"]
    assert "índice local" in body["warnings"][0]
    # Punto lejano (fuera de toda huella conocida) → 502 con la forma del contrato
    r = client.get("/api/frames", params={"ra": 10.0, "dec": -50.0})
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_error"


def test_polygon_coverage():
    from app.spherex import _covers
    from app.spherex.base import FrameMeta

    m = FrameMeta(id="x", obs_id="x", date_obs="2025-01-01T00:00:00Z", mjd=0, detector=1, band_um=1.0,
                  exposure_s=1, access_url="u", center_ra=10, center_dec=0,
                  extra={"s_region": "POLYGON ICRS 9 -1 11 -1 11 1 9 1"})
    assert _covers(m, 10.5, 0.5) and not _covers(m, 11.5, 0.0)
