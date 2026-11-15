# SI-assisted development used for code development, debugging and documentation.
# Tool: ChatGPT, Claude

"""Interpretación espectral de SPECTRAL_CHANNELS (copiado de SphereX-pipeline/spectral.py, extraído de m101_allsky_102_canales.ipynb)."""
import re
import numpy as np
import astropy.units as u
UNIDAD_SIN_METADATA = u.um
def normalizar(nombre):
    return re.sub(r"[^a-z0-9]", "", nombre.lower())

ALIASES = {
    "centro": ["wavelength", "wave", "lambda", "lambda_center", "lambda_cen",
               "wave_center", "wave_cen", "central_wavelength", "wavelength_center", "center", "cwave"],
    "inferior": ["wl_min", "wave_min", "wavelength_min", "lambda_min", "lambda_low", "wave_low", "lower", "lower_edge", "min_wavelength"],
    "superior": ["wl_max", "wave_max", "wavelength_max", "lambda_max", "lambda_high", "wave_high", "upper", "upper_edge", "max_wavelength"],
    "ancho": ["bandwidth", "band_width", "delta_lambda", "delta_wave", "width", "cband"],
}

def columna(tabla, rol):
    manual = {"centro": None, "inferior": None, "superior": None, "ancho": None}[rol]
    if manual is not None:
        if manual not in tabla.colnames:
            raise ValueError(f"No existe {manual}; columnas: {tabla.colnames}")
        return manual
    nombres = {normalizar(n): n for n in tabla.colnames}
    for alias in ALIASES[rol]:
        if normalizar(alias) in nombres:
            return nombres[normalizar(alias)]
    return None

def valores_um(tabla, nombre):
    if nombre is None:
        return np.full(len(tabla), np.nan)
    c = tabla[nombre]
    unidad = u.Unit(c.unit) if c.unit else UNIDAD_SIN_METADATA
    return (np.asarray(np.ma.filled(c, np.nan), dtype=float) * unidad).to_value(u.um)


def interpret(tabla_espectral):
    nombres = {rol: columna(tabla_espectral, rol) for rol in ("centro", "inferior", "superior", "ancho")}
    
    if nombres["centro"] is None:
        raise ValueError(f"Configura COLUMNAS['centro'] con un nombre de {tabla_espectral.colnames}")
    centros = valores_um(tabla_espectral, nombres["centro"])
    inferiores = valores_um(tabla_espectral, nombres["inferior"])
    superiores = valores_um(tabla_espectral, nombres["superior"])
    anchos = valores_um(tabla_espectral, nombres["ancho"])
    if centros.shape != (102,) or not np.all(np.isfinite(centros)) or not np.all(np.diff(centros) > 0):
        raise ValueError("No se confirma el orden creciente de los 102 planos: revisa SPECTRAL_CHANNELS; no se reordenará silenciosamente.")
    origen_ancho = np.full(102, "no disponible", dtype="U32")
    validos = np.isfinite(anchos) & (anchos > 0)
    origen_ancho[validos] = "columna de ancho"
    por_bordes = ~validos & np.isfinite(inferiores) & np.isfinite(superiores) & (superiores > inferiores)
    anchos[~validos] = np.nan
    anchos[por_bordes] = superiores[por_bordes] - inferiores[por_bordes]
    origen_ancho[por_bordes] = "intervalo entre bordes"
    
    return centros, inferiores, superiores, anchos, origen_ancho
