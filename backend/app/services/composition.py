# SI-assisted development used for code development, debugging and documentation.
# Tool: ChatGPT, Claude

"""Composición estimada en una coordenada a partir del espectro SPHEREx (0.75–5 µm).

Qué se puede y qué no se puede saber con SPHEREx (resolución R≈40 en 0.75–3.8 µm y R≈110–130 en 3.8–5 µm):

* Se detectan BANDAS de moléculas y minerales (absorción o emisión) a longitudes de onda conocidas. Cada banda se
  mide contra un continuo local (ajuste lineal a ventanas a ambos lados): profundidad, significancia (σ), anchura
  equivalente y, para las emisiones, flujo de línea.
* Para los HIELOS (H₂O, CO₂, CO, CH₃OH) la profundidad óptica integrada da una CANTIDAD física estándar:
  la densidad de columna N = ∫τ dν / A (moléculas por cm² a lo largo de la línea de visión), con las fuerzas de
  banda A de laboratorio, y la abundancia relativa al hielo de agua (%). De ahí sale el recuento de átomos
  (H, C, O) en los hielos.
* Para MINERALES (silicatos con Fe, piroxeno, minerales hidratados) solo hay un INDICADOR relativo (profundidad
  de banda en %), no una masa: la profundidad depende también del tamaño de grano y la geometría.
* La composición ELEMENTAL completa (p. ej. % de Fe o Si) NO se puede medir con este rango espectral: se informa qué
  elementos están respaldados por cada banda detectada.

El mismo rasgo puede tener portadores distintos según el tipo de objeto (p. ej. 3.3 µm: emisión = PAH, absorción
= metano en una enana marrón). Por eso se añade el tipo de objeto de SIMBAD en esa posición como contexto.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

C_UM_S = 2.99792458e14   # velocidad de la luz en µm/s
UJY_TO_W_M2_HZ = 1e-32   # 1 µJy = 1e-32 W m⁻² Hz⁻¹

# Banderas de la fotometría que indican un punto no fiable (nombres tal como salen en /api/spectrum)
BAD_FLAGS = {
    "saturation", "cosmic_ray", "persistence", "nonfunctional", "missing_data", "hot_pixel", "cold_pixel",
    "ghost", "ghost_fpa", "ghost_ext", "crosstalk", "bloom", "snowball", "streak", "fit_error", "nonlinear",
    "phantom_uncorrected", "outlier",
}

DETECT_SIGMA = 3.0
TENTATIVE_SIGMA = 2.0
SYSTEMATIC_FRACTION = 0.02   # error mínimo por canal (calibración del filtro variable): evita significancias infladas
MIN_CLEAN_POINTS = 15


@dataclass(frozen=True)
class Feature:
    id: str
    name: str
    center_um: float
    band: tuple[float, float]
    cont_left: tuple[float, float] | None
    cont_right: tuple[float, float] | None
    sign: str                         # "absorption" | "emission" | "either"
    group: str                        # ice | emission_line | pah | molecule_gas | mineral
    carriers_absorption: tuple[str, ...] = ()
    carriers_emission: tuple[str, ...] = ()
    elements: tuple[str, ...] = ()
    band_strength_cm: float | None = None    # A (cm/molécula), solo hielos
    formula: str | None = None
    atoms: dict = field(default_factory=dict)  # átomos por molécula, solo hielos
    contexts: tuple[str, ...] = ()            # tipos de objeto donde este rasgo es la interpretación habitual


FEATURES: tuple[Feature, ...] = (
    # --- Hielos (nubes moleculares, protoestrellas, cometas, cuerpos helados) -------------------------------
    Feature("h2o_ice", "Hielo de agua (H₂O)", 3.05, (2.88, 3.30), (2.40, 2.62), (3.72, 3.92), "absorption", "ice",
            ("H₂O sólido (hielo)", "minerales hidratados (OH/H₂O) en superficies"), (), ("H", "O"),
            2.0e-16, "H2O", {"H": 2, "O": 1}, ("yso", "cloud", "solar_system", "galaxy", "unknown")),
    Feature("ch3oh_ice", "Hielo de metanol (CH₃OH)", 3.53, (3.48, 3.58), (3.40, 3.46), (3.62, 3.70), "absorption",
            "ice", ("CH₃OH sólido",), (), ("C", "H", "O"), 5.3e-18, "CH3OH", {"C": 1, "H": 4, "O": 1},
            ("yso", "cloud")),
    Feature("co2_ice", "Hielo de dióxido de carbono (CO₂)", 4.27, (4.23, 4.31), (4.10, 4.19), (4.43, 4.52),
            "absorption", "ice", ("CO₂ sólido (hielo)", "CO₂ gaseoso"), (), ("C", "O"), 7.6e-17, "CO2",
            {"C": 1, "O": 2}, ("yso", "cloud", "solar_system", "galaxy", "unknown")),
    Feature("co_ice", "Hielo de monóxido de carbono (CO)", 4.67, (4.645, 4.695), (4.53, 4.58), (4.75, 4.85),
            "absorption", "ice", ("CO sólido (hielo)", "CO gaseoso (estrellas frías, enanas marrones)"), (),
            ("C", "O"), 1.1e-17, "CO", {"C": 1, "O": 1}, ("yso", "cloud")),
    # --- Hidrocarburos aromáticos / alifáticos -----------------------------------------------------------
    Feature("pah_3_3", "PAH aromáticos (3.3 µm)", 3.29, (3.25, 3.33), (3.08, 3.18), (3.56, 3.68), "either", "pah",
            ("CH₄ (metano, enanas T / planetas gigantes)",),
            ("PAH: hidrocarburos aromáticos policíclicos (polvo con carbono, formación estelar)",), ("C", "H"),
            contexts=("galaxy", "hii", "cloud", "pn", "unknown")),
    Feature("aliphatic_3_4", "Hidrocarburos alifáticos / orgánicos (3.4 µm)", 3.42, (3.38, 3.47), (3.08, 3.18),
            (3.56, 3.68), "either", "pah", ("orgánicos C–H alifáticos (polvo interestelar, superficies)",),
            ("emisión alifática C–H (polvo con carbono)",), ("C", "H"), contexts=("galaxy", "hii", "pn", "cloud")),
    # --- Líneas de hidrógeno ionizado y otras emisiones ------------------------------------------------------
    Feature("pa_beta", "Hidrógeno ionizado Paschen β (1.282 µm)", 1.282, (1.255, 1.31), (1.17, 1.23),
            (1.33, 1.39), "emission", "emission_line", (), ("H II (Paβ)", "[Fe II] 1.257 µm"), ("H",),
            contexts=("hii", "galaxy", "pn", "yso", "star")),
    Feature("pa_alpha", "Hidrógeno ionizado Paschen α (1.875 µm)", 1.875, (1.85, 1.90), (1.74, 1.80),
            (1.97, 2.04), "emission", "emission_line", (), ("H II (Paα)",), ("H",),
            contexts=("hii", "galaxy", "pn", "yso")),
    Feature("br_gamma_h2", "Brackett γ / H₂ molecular (2.12–2.17 µm)", 2.15, (2.105, 2.19), (2.00, 2.07),
            (2.22, 2.27), "emission", "emission_line", (), ("H II (Brγ 2.166 µm)", "H₂ excitado por choques (2.122 µm)"),
            ("H",), contexts=("hii", "galaxy", "yso", "pn")),
    Feature("br_alpha", "Hidrógeno ionizado Brackett α (4.05 µm)", 4.052, (4.025, 4.08), (3.92, 3.99),
            (4.11, 4.18), "emission", "emission_line", (), ("H II (Brα)",), ("H",),
            contexts=("hii", "galaxy", "pn", "yso")),
    # --- Moléculas en atmósferas frías (estrellas M, enanas marrones, planetas) -------------------------------
    Feature("h2o_vapor_1_4", "Vapor de agua (1.4 µm)", 1.40, (1.34, 1.47), (1.22, 1.29), (1.55, 1.65),
            "absorption", "molecule_gas", ("H₂O gaseoso (atmósferas frías: enanas M/L/T)",), (), ("H", "O"),
            contexts=("brown_dwarf", "star")),
    Feature("h2o_vapor_1_9", "Vapor de agua (1.9 µm)", 1.90, (1.82, 1.98), (1.62, 1.72), (2.08, 2.16),
            "absorption", "molecule_gas", ("H₂O gaseoso (atmósferas frías)",), (), ("H", "O"),
            contexts=("brown_dwarf", "star")),
    Feature("ch4_2_3", "Metano / CO (2.2–2.45 µm)", 2.33, (2.27, 2.42), (2.08, 2.18), (2.55, 2.66), "absorption",
            "molecule_gas", ("CH₄ (enanas T, planetas gigantes)", "CO bandhead 2.29 µm (estrellas frías)"), (),
            ("C", "H", "O"), contexts=("brown_dwarf", "star", "galaxy")),
    Feature("ch4_3_3", "Metano (3.3 µm)", 3.30, (3.20, 3.42), (2.08, 2.20), (3.85, 4.10), "absorption",
            "molecule_gas", ("CH₄ gaseoso (enanas T/Y, planetas gigantes)",), (), ("C", "H"),
            contexts=("brown_dwarf",)),
    Feature("co_gas_4_6", "Monóxido de carbono gaseoso (4.6 µm)", 4.62, (4.50, 4.85), (3.95, 4.15), None,
            "absorption", "molecule_gas", ("CO gaseoso (estrellas frías, enanas marrones)",), (), ("C", "O"),
            contexts=("brown_dwarf", "star", "galaxy")),
    # --- Minerales (luz solar reflejada: asteroides, cometas, superficies) ---------------------------------
    Feature("fe_silicate_1um", "Silicatos con hierro (olivino/piroxeno, 1 µm)", 0.98, (0.90, 1.06), (0.74, 0.80),
            (1.45, 1.60), "absorption", "mineral", ("Fe²⁺ en olivino (Mg,Fe)₂SiO₄ y piroxeno (Mg,Fe,Ca)SiO₃",), (),
            ("Fe", "Mg", "Si", "O"), contexts=("solar_system",)),
    Feature("pyroxene_2um", "Piroxeno (2 µm)", 1.95, (1.85, 2.10), (1.45, 1.60), (2.40, 2.55), "absorption",
            "mineral", ("Fe²⁺ en piroxeno (Mg,Fe,Ca)SiO₃",), (), ("Fe", "Mg", "Ca", "Si", "O"),
            contexts=("solar_system",)),
    Feature("hydrated_2_7um", "Minerales hidratados (OH, 2.7 µm)", 2.75, (2.68, 2.82), (2.30, 2.50), None,
            "absorption", "mineral", ("OH/H₂O en filosilicatos (arcillas)", "H₂O gaseoso (atmósferas frías)"), (),
            ("O", "H", "Mg", "Fe", "Si"), contexts=("solar_system",)),
)


# --- Contexto (tipo de objeto en SIMBAD) -------------------------------------------------------------------

# Rasgos físicamente imposibles (o casi) en cada tipo de objeto: si aparecen, la caída/subida del espectro se
# atribuye a otra banda (p. ej. en una enana T los "hielos" y el "Paβ" son huecos entre bandas de H₂O y CH₄).
EXCLUDED_GROUPS = {
    "brown_dwarf": {"ice", "pah", "emission_line", "mineral"},
    "star": {"pah", "mineral"},
    "galaxy": {"mineral"},
    "hii": {"mineral"}, "pn": {"mineral"}, "cloud": {"mineral"}, "yso": {"mineral"},
    "solar_system": {"emission_line"},
}
EXCLUDED_IDS = {
    "galaxy": {"ch4_3_3"}, "hii": {"ch4_3_3"}, "pn": {"ch4_3_3"}, "cloud": {"ch4_3_3"}, "yso": {"ch4_3_3"},
    "star": {"ch4_3_3"},
    "solar_system": {"co_gas_4_6", "h2o_vapor_1_4", "h2o_vapor_1_9", "pah_3_3", "aliphatic_3_4"},
}
# Orden de portadores según contexto (el primero es la interpretación principal)
CARRIER_ORDER = {
    ("ch4_2_3", "galaxy"): ("CO bandhead 2.29 µm (estrellas frías)", "CH₄ (enanas T, planetas gigantes)"),
    ("ch4_2_3", "star"): ("CO bandhead 2.29 µm (estrellas frías)", "CH₄ (enanas T, planetas gigantes)"),
    ("ch4_2_3", "yso"): ("CO bandhead 2.29 µm (estrellas frías)", "CH₄ (enanas T, planetas gigantes)"),
    ("co_gas_4_6", "galaxy"): ("CO gaseoso (supergigantes rojas y gas de la galaxia)",),
    ("h2o_ice", "solar_system"): ("minerales hidratados / hielo de agua en la superficie",),
}


def infer_context_from_spectrum(features: list[dict]) -> str | None:
    """Sin contexto de SIMBAD: vapor de agua y metano profundos a la vez = atmósfera fría (enana marrón)."""
    f = {m["id"]: m for m in features}

    def deep(i, d):
        m = f.get(i, {})
        return m.get("status") == "detected" and m.get("kind") == "absorption" and m.get("depth_percent", 0) >= d
    if deep("h2o_vapor_1_4", 30) and (deep("ch4_3_3", 30) or deep("ch4_2_3", 30)):
        return "brown_dwarf"
    return None


def object_context(otype: str | None) -> str:
    """Tipo de objeto de SIMBAD → contexto para interpretar las bandas."""
    t = (otype or "").strip()
    if not t:
        return "unknown"
    groups = {
        "brown_dwarf": ("BD*", "BD?", "LM*", "LM?"),
        "yso": ("Y*O", "Y*?", "TT*", "TT?", "Ae*", "Ae?", "Or*", "out", "HH", "YSO", "MYS"),
        "cloud": ("MoC", "DNe", "cor", "glb", "ISM", "SFR", "Cld", "bub", "smm", "sub", "mm", "of?", "cir"),
        "hii": ("HII", "RNe", "EmO", "GNe"),
        "pn": ("PN", "PN?", "pA*", "pA?"),
        "galaxy": ("G", "GiG", "GiC", "BiC", "IG", "PaG", "SBG", "SyG", "Sy1", "Sy2", "AGN", "QSO", "LIN", "EmG",
                   "BLL", "Bla", "rG", "H2G", "LSB", "GiP", "GrG", "ClG", "LeG", "AG?", "Q?", "G?", "Gr?"),
    }
    for ctx, codes in groups.items():
        if t in codes:
            return ctx
    if t.endswith("*") or t.endswith("*?") or t in ("V*", "Pe*", "Em*", "s*r", "s*y", "s*b", "C*", "S*", "Mi*",
                                                   "AB*", "RG*", "HB*", "WD*", "**", "EB*", "SB*"):
        return "star"
    return "unknown"


CONTEXT_LABEL = {
    "brown_dwarf": "enana marrón / estrella de muy baja masa",
    "yso": "objeto estelar joven (protoestrella)",
    "cloud": "nube molecular / polvo interestelar",
    "hii": "región H II (gas ionizado, formación estelar)",
    "pn": "nebulosa planetaria",
    "galaxy": "galaxia",
    "star": "estrella",
    "solar_system": "cuerpo del Sistema Solar (luz solar reflejada)",
    "unknown": "desconocido",
}


# --- Espectro: limpieza y binning ----------------------------------------------------------------------------

def _resolution(um: float) -> float:
    """Poder resolutivo aproximado de SPHEREx por detector."""
    if um < 2.42:
        return 41.0
    if um < 3.82:
        return 36.0
    if um < 4.42:
        return 110.0
    return 130.0


def clean_points(points: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict]:
    lam, flux, err, flagged = [], [], [], 0
    saturated = sum("saturation" in (p.get("flags") or []) for p in points)
    for p in points:
        w, f, e = p.get("wavelength"), p.get("flux"), p.get("error")
        if w is None or f is None or e is None or not (math.isfinite(w) and math.isfinite(f) and math.isfinite(e)) or e <= 0:
            continue
        if set(p.get("flags") or []) & BAD_FLAGS:
            flagged += 1
            continue
        lam.append(w), flux.append(f), err.append(e)
    info = {"input_points": len(points), "used_points": len(lam), "flagged_points_removed": flagged,
            "saturated_fraction": round(saturated / len(points), 3) if points else 0.0}
    return np.asarray(lam), np.asarray(flux), np.asarray(err), info


def bin_spectrum(lam: np.ndarray, flux: np.ndarray, err: np.ndarray, oversample: float = 2.0) -> dict:
    """Combina las exposiciones en canales de ~1/oversample de elemento de resolución (media ponderada robusta)."""
    if lam.size == 0:
        return {"wavelength": np.array([]), "flux": np.array([]), "error": np.array([]), "n": np.array([]),
                "reduced_chi2": None}
    edges = [0.74]
    while edges[-1] < 5.02:
        edges.append(edges[-1] * (1 + 1 / (_resolution(edges[-1]) * oversample)))
    edges = np.asarray(edges)
    idx = np.digitize(lam, edges) - 1
    out_l, out_f, out_e, out_n, chi = [], [], [], [], []
    for b in np.unique(idx):
        m = idx == b
        f, e, w = flux[m], err[m], lam[m]
        if f.size >= 3:   # recorte 3σ alrededor de la mediana (rayos cósmicos residuales, fuentes cercanas)
            med = np.median(f)
            mad = 1.4826 * np.median(np.abs(f - med)) or np.median(e)
            keep = np.abs(f - med) <= 3 * max(mad, np.median(e))
            f, e, w = f[keep], e[keep], w[keep]
        wt = 1 / e**2
        mean = float(np.sum(wt * f) / np.sum(wt))
        e_stat = float(1 / math.sqrt(np.sum(wt)))
        if f.size >= 2:
            scatter = float(np.std(f, ddof=1) / math.sqrt(f.size))
            chi.append(float(np.sum(((f - mean) / e) ** 2) / (f.size - 1)))
        else:
            scatter = 0.0
        out_l.append(float(np.sum(wt * w) / np.sum(wt)))
        out_f.append(mean)
        out_e.append(math.hypot(max(e_stat, scatter), SYSTEMATIC_FRACTION * abs(mean)))
        out_n.append(int(f.size))
    return {"wavelength": np.asarray(out_l), "flux": np.asarray(out_f), "error": np.asarray(out_e),
            "n": np.asarray(out_n), "reduced_chi2": float(np.median(chi)) if chi else None}


# --- Medición de una banda -----------------------------------------------------------------------------------

def _in(lam: np.ndarray, win: tuple[float, float] | None) -> np.ndarray:
    if win is None:
        return np.zeros(lam.shape, bool)
    return (lam >= win[0]) & (lam <= win[1])


def measure(feat: Feature, spec: dict) -> dict:
    lam, f, e = spec["wavelength"], spec["flux"], spec["error"]
    base = {"id": feat.id, "name": feat.name, "group": feat.group, "center_um": feat.center_um,
            "band_um": list(feat.band)}
    mb, ml, mr = _in(lam, feat.band), _in(lam, feat.cont_left), _in(lam, feat.cont_right)
    mc = ml | mr
    if mb.sum() < 1 or ml.sum() < 1 or (feat.cont_right is not None and mr.sum() < 1):
        return {**base, "status": "no_coverage", "reason": "Faltan datos en la banda o en el continuo."}

    # Continuo: recta por mínimos cuadrados ponderados (plano si solo hay un lado)
    xc, yc, ec = lam[mc], f[mc], e[mc]
    w = 1 / ec**2
    if feat.cont_right is None or len(np.unique(np.round(xc, 3))) < 2:
        a, b = 0.0, float(np.sum(w * yc) / np.sum(w))
        cov = np.array([[0.0, 0.0], [0.0, float(1 / np.sum(w))]])
    else:
        A = np.vstack([xc, np.ones_like(xc)]).T
        Aw = A * np.sqrt(w)[:, None]
        cov = np.linalg.inv(Aw.T @ Aw)
        a, b = cov @ (Aw.T @ (yc * np.sqrt(w)))
        a, b = float(a), float(b)
    xb, yb, eb = lam[mb], f[mb], e[mb]
    cont = a * xb + b
    cont_err = np.sqrt(np.maximum(cov[0, 0] * xb**2 + 2 * cov[0, 1] * xb + cov[1, 1], 0))
    cont_snr = float(np.median(cont) / max(float(np.median(cont_err)), 1e-30))
    if not np.all(cont > 0) or cont_snr < 3:
        return {**base, "status": "continuum_too_faint",
                "reason": "El continuo alrededor de la banda es demasiado débil o negativo para medirla.",
                "continuum_snr": round(cont_snr, 2)}

    ratio = yb / cont
    ratio_err = np.sqrt((eb / cont) ** 2 + (yb * cont_err / cont**2) ** 2)
    wr = 1 / ratio_err**2
    mean_ratio = float(np.sum(wr * ratio) / np.sum(wr))
    mean_ratio_err = float(1 / math.sqrt(np.sum(wr)))
    depth = 1 - mean_ratio                 # > 0 absorción, < 0 emisión
    sig = depth / mean_ratio_err
    kind = "absorption" if depth > 0 else "emission"
    if feat.sign != "either" and kind != feat.sign:
        sig_eff = 0.0
    else:
        sig_eff = abs(sig)
    status = "detected" if sig_eff >= DETECT_SIGMA else "tentative" if sig_eff >= TENTATIVE_SIGMA else "not_detected"

    # Anchura equivalente (µm) sobre los canales de la banda
    order = np.argsort(xb)
    xs = xb[order]
    dl = np.gradient(xs) if xs.size > 1 else np.array([feat.band[1] - feat.band[0]])
    dl = np.clip(dl, 1e-4, None)
    ew = float(np.sum((1 - ratio[order]) * dl))

    out = {**base, "status": status, "kind": kind if status != "not_detected" else feat.sign,
           "significance_sigma": round(float(abs(sig)), 2),
           "depth_percent": round(100 * depth, 2), "depth_error_percent": round(100 * mean_ratio_err, 2),
           "equivalent_width_um": round(ew, 5), "continuum_snr": round(cont_snr, 1),
           "channels_in_band": int(mb.sum()),
           "carriers": list(feat.carriers_absorption if kind == "absorption" else feat.carriers_emission)
           or list(feat.carriers_absorption or feat.carriers_emission),
           "elements": list(feat.elements)}
    if status == "not_detected":
        out["upper_limit_depth_percent"] = round(100 * DETECT_SIGMA * mean_ratio_err, 2)

    # Hielos: profundidad óptica integrada → densidad de columna
    tau = -np.log(np.clip(ratio[order], 1e-6, None))
    dnu = 1e4 * dl / xs**2                                      # cm⁻¹ por canal
    tau_int = float(np.sum(tau * dnu))
    if (feat.band_strength_cm and kind == "absorption" and status != "not_detected" and np.all(ratio > 0)
            and tau_int > 0):
        n_col = tau_int / feat.band_strength_cm
        out["ice"] = {
            "formula": feat.formula,
            "peak_optical_depth": round(float(tau.max()), 4),
            "integrated_optical_depth_cm-1": round(tau_int, 3),
            "band_strength_cm_per_molecule": feat.band_strength_cm,
            "column_density_cm-2": float(f"{n_col:.3e}"),
            "column_density_error_cm-2": float(f"{n_col * mean_ratio_err / max(depth, 1e-9):.3e}"),
        }
    # Emisión: flujo de línea (W/m²) sobre el continuo
    if kind == "emission" and status != "not_detected":
        excess_ujy = (yb - cont)[order]
        nu_width_hz = C_UM_S * dl / xs**2
        out["line_flux_W_m2"] = float(f"{float(np.sum(excess_ujy * UJY_TO_W_M2_HZ * nu_width_hz)):.3e}")
    return out


# --- Análisis completo --------------------------------------------------------------------------------------

def analyze(points: list[dict], context: str = "unknown") -> dict:
    lam, flux, err, info = clean_points(points)
    if lam.size < MIN_CLEAN_POINTS:
        # Nunca se usan los puntos marcados (saturados, rayos cósmicos…): darían bandas falsas.
        lam, flux, err = lam[:0], flux[:0], err[:0]
    spec = bin_spectrum(lam, flux, err)
    features = [measure(ft, spec) for ft in FEATURES]
    by_id = {ft.id: ft for ft in FEATURES}

    context_source = "given" if context != "unknown" else "none"
    if context == "unknown":
        guess = infer_context_from_spectrum(features)
        if guess:
            context, context_source = guess, "spectrum"

    covered = [m for m in features if m["status"] not in ("no_coverage", "continuum_too_faint")]
    snr = float(np.median(spec["flux"] / spec["error"])) if spec["flux"].size else 0.0
    quality = "good" if snr >= 20 and len(covered) >= 10 else "fair" if snr >= 5 and len(covered) >= 5 else "poor"
    if info["used_points"] < MIN_CLEAN_POINTS or info["saturated_fraction"] >= 0.3:
        quality = "poor"

    # Interpretación según contexto
    excluded_groups = EXCLUDED_GROUPS.get(context, set())
    excluded_ids = EXCLUDED_IDS.get(context, set())
    for m in features:
        ft = by_id[m["id"]]
        m["expected_in_context"] = context in ft.contexts
        if (m["id"], context) in CARRIER_ORDER and m.get("carriers"):
            m["carriers"] = list(CARRIER_ORDER[(m["id"], context)])
        if m["status"] in ("detected", "tentative") and (ft.group in excluded_groups or m["id"] in excluded_ids):
            m["status"] = "attributed_to_other"
            m["reason"] = (f"Rasgo presente pero no físico en un objeto de tipo «{CONTEXT_LABEL.get(context, context)}»: "
                           "probablemente es parte de otra banda vecina.")
            m.pop("ice", None)
        elif (m["id"] == "co_ice" and m["status"] in ("detected", "tentative") and context not in ("yso", "cloud")
              and next(x for x in features if x["id"] == "co_gas_4_6")["status"] == "detected"):
            m["status"] = "attributed_to_other"
            m["reason"] = ("Cae dentro de la banda ancha de CO gaseoso (4.5–4.85 µm) y en este tipo de objeto no se "
                           "esperan hielos de CO: se atribuye al gas.")
            m.pop("ice", None)
        elif m["status"] == "detected" and quality == "poor":
            m["status"] = "tentative"
            m["reason"] = "Calidad de datos baja (saturación o pocas exposiciones válidas): no se confirma."
    detected = [m for m in features if m["status"] == "detected"]
    tentative = [m for m in features if m["status"] == "tentative"]
    detected.sort(key=lambda m: (not m["expected_in_context"], -m["significance_sigma"]))

    # Hielos: abundancias relativas a H₂O y recuento de átomos
    ices = {m["ice"]["formula"]: m for m in features if "ice" in m and m["status"] == "detected"}
    ice_table = []
    n_h2o = ices.get("H2O", {}).get("ice", {}).get("column_density_cm-2")
    atoms: dict[str, float] = {}
    for formula, m in ices.items():
        n = m["ice"]["column_density_cm-2"]
        ice_table.append({
            "formula": formula, "name": m["name"], "column_density_cm-2": n,
            "relative_to_h2o_percent": round(100 * n / n_h2o, 1) if n_h2o else None,
        })
        for el, k in by_id[m["id"]].atoms.items():
            atoms[el] = atoms.get(el, 0.0) + k * n
    total_atoms = sum(atoms.values())
    elements_in_ices = [
        {"element": el, "column_density_cm-2": float(f"{v:.3e}"), "fraction_percent": round(100 * v / total_atoms, 1)}
        for el, v in sorted(atoms.items(), key=lambda kv: -kv[1])
    ] if total_atoms else []

    # Elementos respaldados por alguna banda detectada (sin cantidad)
    support: dict[str, list[str]] = {}
    for m in detected:
        for el in m["elements"]:
            support.setdefault(el, []).append(m["name"])
    elements = [{"element": el, "evidence": names, "quantified": el in atoms} for el, names in sorted(support.items())]

    # Mineral / molécula: indicador relativo (profundidad de banda)
    indicators = [{"id": m["id"], "name": m["name"], "kind": m["kind"], "depth_percent": m["depth_percent"],
                   "significance_sigma": m["significance_sigma"], "carriers": m["carriers"]}
                  for m in detected if m["group"] in ("mineral", "molecule_gas", "pah", "emission_line")]

    warnings = []
    if info["saturated_fraction"] >= 0.3:
        warnings.append(f"La fuente satura SPHEREx en el {100 * info['saturated_fraction']:.0f}% de las exposiciones "
                        "(es demasiado brillante): esas medidas se descartan y la composición puede no ser medible.")
    if info["used_points"] < MIN_CLEAN_POINTS:
        warnings.append(f"Solo {info['used_points']} de {info['input_points']} exposiciones son válidas: no hay datos "
                        "suficientes para estimar la composición.")
    if spec["reduced_chi2"] and spec["reduced_chi2"] > 5:
        warnings.append(f"Dispersión entre exposiciones {spec['reduced_chi2']:.1f}× mayor que el error: el objeto puede "
                        "ser variable o moverse; las profundidades de banda son menos fiables.")
    if quality == "poor":
        warnings.append("Señal débil o poca cobertura espectral: los resultados son poco fiables.")
    if not detected:
        warnings.append("No se detectó ninguna banda con ≥ 3σ: el espectro es compatible con un continuo sin rasgos.")

    return {
        "context": {"type": context, "label": CONTEXT_LABEL.get(context, context), "source": context_source},
        "summary": _summary(detected, tentative, ice_table, context),
        "detected": [m["id"] for m in detected],
        "tentative": [m["id"] for m in tentative],
        "attributed_to_other": [m["id"] for m in features if m["status"] == "attributed_to_other"],
        "ices": ice_table,
        "elements_in_ices": elements_in_ices,
        "elements_supported": elements,
        "indicators": indicators,
        "features": features,
        "spectrum_binned": [
            {"wavelength_um": round(float(l_), 4), "flux_uJy": round(float(f_), 3), "error_uJy": round(float(e_), 3),
             "n": int(n_)} for l_, f_, e_, n_ in zip(spec["wavelength"], spec["flux"], spec["error"], spec["n"])
        ],
        "quality": {"level": quality, "median_snr_per_channel": round(snr, 1), "channels": int(spec["flux"].size),
                    "features_measurable": len(covered), "reduced_chi2": spec["reduced_chi2"] and round(spec["reduced_chi2"], 2),
                    **info},
        "method": ("Bandas medidas contra un continuo lineal local; detección ≥ 3σ. Hielos: N = ∫τ dν / A con fuerzas "
                   "de banda de laboratorio (H₂O 2.0e-16, CO₂ 7.6e-17, CO 1.1e-17, CH₃OH 5.3e-18 cm/molécula). "
                   "Minerales y moléculas en gas: solo indicador relativo (profundidad de banda)."),
        "limitations": [
            "SPHEREx tiene baja resolución (R≈40 hasta 3.8 µm): bandas vecinas pueden mezclarse.",
            "Las cantidades absolutas solo son válidas para hielos (densidad de columna en la línea de visión).",
            "La composición elemental total no es medible en 0.75–5 µm; se indica qué elementos respalda cada banda.",
            "El portador de una banda depende del tipo de objeto (ver context y carriers).",
        ],
        "warnings": warnings,
    }


def _summary(detected: list[dict], tentative: list[dict], ices: list[dict], context: str) -> str:
    if not detected:
        t = f" Posibles, sin confirmar: {', '.join(m['name'] for m in tentative)}." if tentative else ""
        return "No se detectan bandas de composición con confianza en este punto." + t
    parts = []
    for m in detected[:6]:
        carrier = m["carriers"][0] if m["carriers"] else m["name"]
        parts.append(f"{m['name']} en {'absorción' if m['kind'] == 'absorption' else 'emisión'} "
                     f"({m['significance_sigma']:.0f}σ, {abs(m['depth_percent']):.0f}%) → {carrier}")
    s = "Detectado: " + "; ".join(parts) + "."
    if ices:
        h2o = next((i for i in ices if i["formula"] == "H2O"), None)
        if h2o:
            s += f" Columna de hielo de agua ≈ {h2o['column_density_cm-2']:.1e} moléculas/cm²."
    if context != "unknown":
        s += f" Contexto SIMBAD: {CONTEXT_LABEL.get(context, context)}."
    return s


# --- Trabajo (202) ----------------------------------------------------------------------------------------

CONTEXTS = tuple(CONTEXT_LABEL)


async def build_composition(job, *, ra: float, dec: float, aperture: float, date_from, date_to,
                            context: str, spectrum_jid: str) -> dict:
    """Usa el espectro de /api/spectrum (misma caché: si ya se pidió, no se vuelve a medir) y lo analiza."""
    import asyncio

    from ..cache import cget, cset
    from ..config import settings
    from ..errors import UpstreamError
    from ..jobs import result_key
    from ..sources import catalogs
    from .analysis import build_spectrum

    warns: list[str] = []
    spec = cget(result_key(spectrum_jid))
    if spec is None:
        spec = await build_spectrum(job, ra=ra, dec=dec, aperture=aperture, date_from=date_from, date_to=date_to)
        cset(result_key(spectrum_jid), spec, settings.result_ttl_s)
    job.update(0.96, "Analizando bandas de composición")

    simbad = None
    if context == "auto":
        try:
            objs = await catalogs.simbad_cone(ra, dec, max(2 * aperture, 10.0))
            objs = sorted(objs or [], key=lambda x: x.get("distance_arcsec") or 1e9)
            known = [o for o in objs if object_context(o.get("type_raw")) != "unknown"]
            o = (known or objs or [None])[0]
            if o:
                simbad = {"name": o.get("name"), "otype": o.get("type_raw"), "distance_arcsec": o.get("distance_arcsec")}
            context = object_context(o.get("type_raw")) if o else "unknown"
        except UpstreamError as e:
            warns.append(f"simbad: {e.message} (sin contexto de tipo de objeto)")
            context = "unknown"

    given = context
    res = await asyncio.to_thread(analyze, spec.get("points", []), context)
    if res["context"]["source"] == "given":
        res["context"]["source"] = "simbad" if simbad and given != "unknown" else "user"
    res["warnings"] = warns + res["warnings"] + [w for w in spec.get("warnings", [])[:5]]
    return {
        "center": {"ra": ra, "dec": dec},
        "aperture_arcsec": aperture,
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "simbad_match": simbad,
        "n_exposures": spec.get("n_exposures"),
        "spectrum_url": f"/api/spectrum?ra={ra}&dec={dec}&aperture={aperture}"
                        + (f"&date_from={date_from.isoformat()}" if date_from else "")
                        + (f"&date_to={date_to.isoformat()}" if date_to else ""),
        **res,
        "partial": bool(spec.get("partial")),
    }
