"""Composición a partir del espectro: espectros sintéticos con bandas conocidas (sin red)."""
import numpy as np
import pytest

from app.services import composition as C


def synth(tau=None, emission=None, n=3000, snr=200, seed=1, flux0=5000.0):
    """Puntos tipo /api/spectrum: continuo con pendiente + bandas gaussianas de profundidad óptica / emisión."""
    rng = np.random.default_rng(seed)
    lam = np.sort(rng.uniform(0.75, 5.0, n))
    cont = flux0 * (1 + 0.1 * (lam - 2.5))
    t = np.zeros_like(lam)
    for c, sigma, peak in tau or []:
        t += peak * np.exp(-0.5 * ((lam - c) / sigma) ** 2)
    f = cont * np.exp(-t)
    for c, sigma, amp in emission or []:
        f += amp * flux0 * np.exp(-0.5 * ((lam - c) / sigma) ** 2)
    err = cont / snr
    f = f + rng.normal(0, err)
    return [{"wavelength": float(a), "flux": float(b), "error": float(e), "flags": []} for a, b, e in zip(lam, f, err)]


def by_id(res):
    return {m["id"]: m for m in res["features"]}


def test_continuo_liso_no_inventa_bandas():
    res = C.analyze(synth(), "unknown")
    assert res["detected"] == []
    assert res["ices"] == [] and res["elements_supported"] == []
    assert res["quality"]["level"] == "good"


def test_hielos_columna_y_relacion_co2_h2o():
    # H₂O: τ=0.8 a 3.05 µm, σ=0.10 µm; CO₂: τ=0.6 a 4.27 µm, σ=0.012 µm (perfil más estrecho, como en el ISM)
    pts = synth(tau=[(3.05, 0.10, 0.8), (4.27, 0.012, 0.6)])
    res = C.analyze(pts, "yso")
    f = by_id(res)
    assert f["h2o_ice"]["status"] == "detected" and f["co2_ice"]["status"] == "detected"
    assert "co_ice" not in res["detected"]
    # Columna esperada: ∫τ dν = τ0·σ_λ·√(2π)·1e4/λ² ; N = ∫τdν / A
    exp_h2o = 0.8 * 0.10 * np.sqrt(2 * np.pi) * 1e4 / 3.05**2 / 2.0e-16
    n_h2o = f["h2o_ice"]["ice"]["column_density_cm-2"]
    assert n_h2o == pytest.approx(exp_h2o, rel=0.5)          # la ventana recorta las alas: mismo orden de magnitud
    co2 = next(i for i in res["ices"] if i["formula"] == "CO2")
    assert 1 < co2["relative_to_h2o_percent"] < 200
    els = {e["element"]: e for e in res["elements_in_ices"]}
    assert els["O"]["column_density_cm-2"] > 0 and els["H"]["column_density_cm-2"] > 0 and "C" in els
    assert "Hielo de agua" in res["summary"]


def test_linea_de_emision_hidrogeno():
    res = C.analyze(synth(emission=[(4.052, 0.012, 0.5)]), "hii")
    f = by_id(res)["br_alpha"]
    assert f["status"] == "detected" and f["kind"] == "emission"
    assert f["line_flux_W_m2"] > 0 and f["expected_in_context"] is True
    assert {"element": "H", "evidence": [f["name"]], "quantified": False} in res["elements_supported"]


def test_signo_contrario_no_cuenta_como_deteccion():
    # Una absorción donde solo se espera emisión (Brα) no se detecta como Brα
    res = C.analyze(synth(tau=[(4.052, 0.012, 0.5)]), "hii")
    assert by_id(res)["br_alpha"]["status"] == "not_detected"


def test_pah_emision_vs_metano_absorcion():
    em = by_id(C.analyze(synth(emission=[(3.29, 0.02, 0.4)]), "galaxy"))["pah_3_3"]
    ab = by_id(C.analyze(synth(tau=[(3.29, 0.02, 0.4)]), "brown_dwarf"))["pah_3_3"]
    assert em["kind"] == "emission" and "PAH" in em["carriers"][0]
    assert ab["kind"] == "absorption" and "CH₄" in ab["carriers"][0]


def test_banderas_malas_se_descartan():
    pts = synth()
    for p in pts[:50]:
        p["flags"] = ["saturation"]
        p["flux"] *= 10
    res = C.analyze(pts, "unknown")
    assert res["quality"]["flagged_points_removed"] == 50 and res["detected"] == []


def test_contexto_simbad():
    assert C.object_context("BD*") == "brown_dwarf"
    assert C.object_context("Y*O") == "yso"
    assert C.object_context("HII") == "hii"
    assert C.object_context("G") == "galaxy"
    assert C.object_context("V*") == "star"
    assert C.object_context(None) == "unknown"


def test_fuente_saturada_no_inventa_composicion():
    pts = synth(tau=[(3.05, 0.10, 0.8)])
    for p in pts[:-10]:
        p["flags"] = ["saturation", "bloom"]
    res = C.analyze(pts, "yso")
    assert res["detected"] == [] and res["ices"] == []
    assert res["quality"]["level"] == "poor" and res["quality"]["saturated_fraction"] > 0.9
    assert any("satura" in w for w in res["warnings"])


def test_significancia_limitada_por_error_sistematico():
    # S/N altísima (5000) y una banda de 1 %: con el 2 % de sistemático no debe salir con cientos de σ
    res = C.analyze(synth(tau=[(3.05, 0.10, 0.01)], snr=5000), "yso")
    assert by_id(res)["h2o_ice"]["significance_sigma"] < 20


def test_enana_marron_deducida_del_espectro_sin_hielos_falsos():
    # Bandas de H₂O (1.4, 1.9) y CH₄ (2.3, 3.3) profundas: el hueco entre ellas no debe leerse como hielo / Paβ
    pts = synth(tau=[(1.40, 0.05, 1.2), (1.90, 0.05, 1.0), (2.33, 0.05, 1.0), (3.30, 0.08, 2.0), (3.05, 0.08, 1.0)])
    res = C.analyze(pts, "unknown")
    assert res["context"] == {"type": "brown_dwarf", "label": C.CONTEXT_LABEL["brown_dwarf"], "source": "spectrum"}
    assert {"h2o_vapor_1_4", "ch4_3_3"} <= set(res["detected"])
    assert "h2o_ice" in res["attributed_to_other"] and res["ices"] == []


def test_calidad_mala_no_confirma_bandas():
    pts = synth(tau=[(3.05, 0.10, 0.8)])
    for p in pts[::2]:                      # la mitad de las exposiciones, repartidas en todo el rango
        p["flags"] = ["saturation"]
    res = C.analyze(pts, "yso")
    assert res["quality"]["level"] == "poor" and res["detected"] == []
    assert "h2o_ice" in res["tentative"]


def test_co_hielo_dentro_de_co_gas_en_galaxia():
    # Banda ancha de CO gaseoso (4.5–4.85 µm): en una galaxia no se reporta además "hielo de CO"
    res = C.analyze(synth(tau=[(4.65, 0.08, 0.5)]), "galaxy")
    assert "co_gas_4_6" in res["detected"] and "co_ice" in res["attributed_to_other"]
    co = next(m for m in res["features"] if m["id"] == "co_gas_4_6")
    assert co["carriers"][0].startswith("CO gaseoso (supergigantes")
