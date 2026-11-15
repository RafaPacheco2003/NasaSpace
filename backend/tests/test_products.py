"""L2 completo y cubo all-sky (pipeline SphereX) con FITS sintéticos: sin red."""
import io
import time

import numpy as np
import pytest
from astropy.io import fits
from astropy.table import Table
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app
from app.products import cube as CU
from app.products import l2 as L2

RA, DEC = 133.795129, -7.245147


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def wait(client, resp, timeout=60):
    if resp.status_code == 200:
        return resp.json()
    assert resp.status_code == 202, resp.text
    jid = resp.json()["id"]
    assert jid[:4] in ("l2p_", "cub_")
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/jobs/{jid}").json()
        if j["status"] == "done":
            return j["result"]
        assert j["status"] != "failed", j["error"]
        time.sleep(0.1)
    raise AssertionError("timeout")


def tan_header(n, scale_deg, naxis=2):
    h = fits.Header()
    h["CTYPE1"], h["CTYPE2"] = "RA---TAN", "DEC--TAN"
    h["CRVAL1"], h["CRVAL2"] = RA, DEC
    h["CRPIX1"] = h["CRPIX2"] = n / 2 + 1  # (RA, DEC) en el centro del píxel [n/2, n/2]
    h["CDELT1"], h["CDELT2"] = -scale_deg, scale_deg
    h["CUNIT1"] = h["CUNIT2"] = "deg"
    if naxis == 3:
        h["CTYPE3"], h["CRVAL3"], h["CDELT3"], h["CRPIX3"], h["CUNIT3"] = "WAVE", 0.75, 0.0425, 1, "um"
    h["BUNIT"] = "MJy/sr"
    return h


@pytest.fixture(scope="module")
def l2_file(tmp_path_factory):
    n = 200
    img = np.full((n, n), 0.1, np.float32)
    img[100, 100] = 13.0  # fuente en el centro = (RA, DEC)
    img[0:5, 0:5] = np.nan
    flags = np.zeros((n, n), np.int32)
    flags[100, 100] = 1 << 21
    fh = fits.Header()
    fh["MSKN0021"] = "SOURCE"
    hdul = fits.HDUList([fits.PrimaryHDU(),
                         fits.ImageHDU(img, tan_header(n, 6.2 / 3600), name="IMAGE"),
                         fits.ImageHDU(flags, fh, name="FLAGS"),
                         fits.ImageHDU(np.full((n, n), 0.01, np.float32), name="VARIANCE"),
                         fits.ImageHDU(np.full((n, n), 0.08, np.float32), name="ZODI")])
    p = tmp_path_factory.mktemp("l2") / "l2.fits"
    hdul.writeto(p)
    return p


@pytest.fixture(scope="module")
def cube_file(tmp_path_factory):
    n = 64
    cube = np.full((102, n, n), 1.0, np.float32) * np.arange(1, 103, dtype=np.float32)[:, None, None]
    cube[:, 32, 32] += 100.0
    wl = 0.75 + 0.0425 * np.arange(102)
    tab = Table({"DETECTOR": np.repeat(np.arange(1, 7), 17).astype(np.int16), "WAVELENGTH": wl.astype(np.float32),
                 "WL_MIN": (wl - 0.01).astype(np.float32), "WL_MAX": (wl + 0.01).astype(np.float32),
                 "R": np.full(102, 41.0, np.float32), "BANDWIDTH": np.full(102, 0.02, np.float32)})
    for c in ("WAVELENGTH", "WL_MIN", "WL_MAX", "BANDWIDTH"):
        tab[c].unit = "um"
    hdr = tan_header(n, 10 / 3600, naxis=3)
    hdr["ORDER"], hdr["NPIX"] = 5, 1234
    hdul = fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(cube, hdr, name="IMAGE"),
                         fits.ImageHDU(np.full((102, n, n), 7, np.int32), name="NHITS"),
                         fits.ImageHDU(np.zeros((102, n, n), np.int32), name="FLAGS"),
                         fits.BinTableHDU(tab, name="SPECTRAL_CHANNELS")])
    p = tmp_path_factory.mktemp("cube") / "cube.fits"
    hdul.writeto(p)
    return p


@pytest.fixture()
def offline(monkeypatch, l2_file, cube_file):
    async def fetch(url, progress=None, source="spherex"):
        if progress:
            progress(50, 100)
        return cube_file if "cube" in url else l2_file

    for mod in (L2, CU):
        monkeypatch.setattr(mod, "fetch_file", fetch)
        monkeypatch.setattr(mod, "cached_file", lambda url: None)

    async def tiles(ra, dec):
        t = {"id": "hpx5-1234", "url": "https://x/Norder5/Dir0/Npix1234_cube.fits", "obs_id": "hips5_cube",
             "s_ra": RA, "s_dec": DEC, "s_region": "", "size_bytes": None}
        CU.cset(CU._reg_key(t["id"]), t)
        return [t]

    monkeypatch.setattr(CU, "find_tiles", tiles)


def png_size(content):
    im = Image.open(io.BytesIO(content))
    return im.size


def test_l2_completo_raw_color_y_clic(client, offline):
    r = wait(client, client.get("/api/l2", params={"ra": RA, "dec": DEC, "size": 400}))
    assert r["native_size"] == {"width": 200, "height": 200} and r["png_size"] == {"width": 400, "height": 400}
    assert r["selection"]["index"] == min(2, r["selection"]["candidates_total"] - 1)
    t = r["target"]
    assert t["inside"] and t["brightness"]["value"] == pytest.approx(13.0)
    assert t["brightness"]["value_minus_zodi"] == pytest.approx(12.92, abs=1e-4)
    assert t["brightness"]["error"] == pytest.approx(0.1, abs=1e-6) and t["flags"] == ["source"]
    # la fuente está en el centro del FITS → centro del PNG (el FITS se dibuja con la fila 0 abajo)
    assert t["x"] == pytest.approx(201, abs=1.5) and t["y"] == pytest.approx(199, abs=1.5)
    fid = r["frame"]["id"]
    for kind in ("raw", "color"):
        resp = client.get(f"/api/l2/{fid}/{kind}.png", params={"size": 400})
        assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
        assert png_size(resp.content) == (400, 400)
    # clic en el PNG → mismas coordenadas y brillo que target
    p = client.get(f"/api/l2/{fid}/pixel", params={"x": t["x"], "y": t["y"], "size": 400}).json()
    assert p["ra"] == pytest.approx(RA, abs=2 / 3600) and p["dec"] == pytest.approx(DEC, abs=2 / 3600)
    assert p["brightness"]["value"] == pytest.approx(13.0)
    # por coordenadas
    q = client.get(f"/api/l2/{fid}/pixel", params={"ra": RA, "dec": DEC, "size": 400}).json()
    assert q["x"] == pytest.approx(t["x"], abs=0.5)  # t usa la coordenada redondeada a 1″ (caché)
    # fondo, borde inválido y fuera de la imagen
    assert client.get(f"/api/l2/{fid}/pixel", params={"x": 50, "y": 50, "size": 400}).json()["brightness"]["value"] == pytest.approx(0.1)
    assert client.get(f"/api/l2/{fid}/pixel", params={"x": 1, "y": 399, "size": 400}).json()["valid"] is False
    assert client.get(f"/api/l2/{fid}/pixel", params={"x": 900, "y": 10, "size": 400}).json()["inside"] is False


def test_l2_errores(client, offline):
    assert client.get("/api/l2/sx-d1-123/raw.png").status_code == 404
    assert client.get("/api/l2/sx-d1-123/pixel", params={"x": 1}).status_code == 400
    assert client.get("/api/l2", params={"ra": RA, "dec": DEC, "frame_id": "sx-d1-999"}).status_code in (202, 404)


def test_cubo_102_y_6_canales_espectro_y_figuras(client, offline):
    r = wait(client, client.get("/api/cube", params={"ra": RA, "dec": DEC}))
    assert r["shape"] == [102, 64, 64] and len(r["channels"]) == 102
    assert [c["channel"] for c in r["six_channels"]] == [1, 20, 40, 60, 80, 102]
    assert r["channels"][0]["wavelength_um"] == pytest.approx(0.75, abs=1e-4)
    spec = r["spectrum"]
    assert len(spec) == 102 and spec[0]["brightness"] == pytest.approx(101.0) and spec[101]["brightness"] == pytest.approx(202.0)
    assert spec[0]["nhits"] == 7
    tid = r["tile"]["id"]
    six = client.get(f"/api/cube/{tid}/channels", params={"set": "six"}).json()
    assert six["count"] == 6 and six["channels"][5]["png_url"].startswith(f"/api/cube/{tid}/channel/102.png")
    assert client.get(f"/api/cube/{tid}/channels").json()["count"] == 102
    pngr = client.get(f"/api/cube/{tid}/channel/40.png", params={"cmap": "inferno", "size": 128})
    assert pngr.status_code == 200 and png_size(pngr.content) == (128, 128)
    assert client.get(f"/api/cube/{tid}/channel/103.png").status_code == 400
    t = r["target"]
    p = client.get(f"/api/cube/{tid}/pixel", params={"x": t["x"], "y": t["y"]}).json()
    assert p["spectrum"][19]["brightness"] == pytest.approx(120.0) and p["valid_channels"] == 102
    assert client.get(f"/api/cube/{tid}/pixel", params={"x": 3, "y": 3}).json()["spectrum"][0]["brightness"] == pytest.approx(1.0)
    assert client.get(f"/api/cube/{tid}/samples.png", params={"ra": RA, "dec": DEC}).status_code == 200
    pv = client.get(f"/api/cube/{tid}/preview3d", params={"n": 16}).json()
    assert pv["shape"] == [102, 16, 16] and len(pv["wavelength_um"]) == 102
    assert client.get(f"/api/cube/{tid}/cube3d.png").status_code == 200
    assert client.get("/api/cube/hpx5-9999/channels").status_code == 404
