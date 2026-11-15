# SI-assisted development used for code development, debugging and documentation.
# Tool: ChatGPT, Claude

"""CORS abierto a todo: cualquier origen, método y cabecera, también en errores (404, 500) y con Private Network Access."""
import pytest
from fastapi.testclient import TestClient

from app.main import app

ORIGIN = {"Origin": "https://cualquier-sitio.ejemplo"}


def _boom():
    raise RuntimeError("fallo de prueba")


app.add_api_route("/api/_test_boom", _boom, include_in_schema=False)


@pytest.fixture(scope="module")
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_get_cualquier_origen_y_expone_cabeceras(client):
    r = client.get("/api/status", headers=ORIGIN)
    assert r.headers["access-control-allow-origin"] == "*"
    assert r.headers["access-control-expose-headers"] == "*"


def test_preflight_cualquier_metodo_y_cabecera(client):
    r = client.options("/api/asteroids/predict-from-current", headers={
        **ORIGIN, "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": "content-type, ngrok-skip-browser-warning, x-lo-que-sea"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"
    assert "PUT" in r.headers["access-control-allow-methods"]
    assert "x-lo-que-sea" in r.headers["access-control-allow-headers"]
    assert r.headers["access-control-max-age"] == "86400"


def test_private_network_access(client):
    r = client.options("/api/status", headers={**ORIGIN, "Access-Control-Request-Method": "GET",
                                               "Access-Control-Request-Private-Network": "true"})
    assert r.headers["access-control-allow-private-network"] == "true"


def test_errores_tambien_llevan_cors(client):
    r404 = client.get("/api/no-existe", headers=ORIGIN)
    assert r404.status_code == 404 and r404.headers["access-control-allow-origin"] == "*"
    r500 = client.get("/api/_test_boom", headers=ORIGIN)
    assert r500.status_code == 500 and r500.headers["access-control-allow-origin"] == "*"
    assert r500.json()["error"]["code"] == "internal_error"
