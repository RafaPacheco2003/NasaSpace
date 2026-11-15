# L2 completo y cubo all-sky — endpoints para el Frontend

Integración en el backend del pipeline `backend/app/SphereX-pipeline` (RA/DEC → L2 y all-sky). Con **unas mismas
coordenadas** el Frontend puede obtener:

| Producto | Qué es | Endpoint |
|---|---|---|
| **Raw** | Imagen **L2 completa** (2040×2040 px, ~3.5°) publicada por IRSA, normalizada (percentiles 3.5–96.5) en gris invertido | `GET /api/l2/{frame_id}/raw.png` |
| **Color** | La misma normalización en pseudocolor **cividis** | `GET /api/l2/{frame_id}/color.png` |
| **Clic en L2** | RA/Dec del punto + **brillo** (MJy/sr), zodiacal, error, λ de ese píxel, banderas | `GET /api/l2/{frame_id}/pixel` |
| **Sky cube, 102 canales** | Cubo all-sky (todo el survey combinado), un PNG por canal + espectro de 102 puntos | `GET /api/cube/{tile_id}/channels?set=all` |
| **Sky cube, 6 canales** | Canales 1, 20, 40, 60, 80, 102 (los del script) + la figura `samples.png` | `GET /api/cube/{tile_id}/channels?set=six` |

Todo es **GET**, mismo formato de errores `{"error": {"code", "message"}}`, CORS abierto. Los PNG no cuentan para el
límite de peticiones y se cachean 24 h.

---

## Índice

1. [Flujo en el Frontend](#1-flujo-en-el-frontend)
2. [Lista de endpoints](#2-lista-de-endpoints)
3. [L2: preparar](#3-get-apil2--preparar-la-imagen-l2)
4. [L2: imágenes raw y color](#4-imágenes-raw-y-color)
5. [L2: clic → RA/Dec + brillo](#5-get-apil2frame_idpixel--clic--radec--brillo)
6. [Cubo: preparar](#6-get-apicube--preparar-el-cubo)
7. [Cubo: 102 canales y 6 canales](#7-canales-102-y-6)
8. [Cubo: clic → espectro, figura, 3D](#8-clic-en-el-cubo-figura-de-6-canales-y-3d)
9. [Píxeles, orientación y tamaños](#9-píxeles-orientación-y-tamaños)
10. [Código TypeScript](#10-código-typescript)
11. [Rendimiento, caché y despliegue](#11-rendimiento-caché-y-despliegue)
12. [Qué se tomó del pipeline y qué cambió](#12-qué-se-tomó-del-pipeline-y-qué-cambió)

---

## 1. Flujo en el Frontend

```
ra, dec (+ fechas)
   │
   ├─► GET /api/l2?ra&dec&date_from&date_to&size=1024 ──(202 → /api/jobs/{id})──► L2Result
   │        ├─ <img src=raw_png_url>     ← "Raw"
   │        ├─ <img src=color_png_url>   ← "Color"
   │        ├─ clic (x,y) → GET pixel_url ─► RA/Dec + brillo
   │        └─ candidates[] → cambiar de exposición con &frame_id=…
   │
   └─► GET /api/cube?ra&dec ──(202 → /api/jobs/{id})──► CubeResult
            ├─ channels[102].png_url     ← "Sky cube 102 canales" (slider de λ)
            ├─ six_channels[6].png_url   ← "Sky cube 6 canales"
            ├─ spectrum[102]             ← gráfico del espectro en la coordenada
            ├─ clic (x,y) → GET urls.pixel ─► RA/Dec + espectro de 102 canales
            ├─ urls.samples_png          ← figura de 6 paneles del script
            └─ urls.preview3d / cube3d_png ← vista 3D
```

Las dos llamadas de preparación son lentas la primera vez (descargan el FITS completo) y siguen el **patrón 202 +
trabajo** del resto de la API: sondear `GET /api/jobs/{id}` cada `poll_after_ms` hasta `done` (resultado en `result`) o
`failed` (`error`). `progress` y `stage` muestran los MB descargados. La segunda vez responden **200** directo.

---

## 2. Lista de endpoints

| # | Método y ruta | Devuelve | Lento |
|---|---|---|---|
| 1 | `GET /api/l2?ra&dec[&date_from&date_to&detector&frame_id&size]` | `L2Result` (JSON) | **202** la 1ª vez (~65 MB) |
| 2 | `GET /api/l2/{frame_id}/raw.png?size&invert` | PNG gris | 1ª vez ~5 s |
| 3 | `GET /api/l2/{frame_id}/color.png?size` | PNG cividis | 1ª vez ~4 s |
| 4 | `GET /api/l2/{frame_id}/pixel?x&y&size` · `?ra&dec&size` | RA/Dec + brillo (JSON) | < 0.5 s |
| 5 | `GET /api/l2/{frame_id}/fits` | 307 → FITS original en IRSA | — |
| 6 | `GET /api/cube?ra&dec[&size]` | `CubeResult` (JSON) | **202** la 1ª vez (~320 MB, 30–90 s) |
| 7 | `GET /api/cube/{tile_id}/channels?set=all\|six&cmap&size` | lista de canales con `png_url` | — |
| 8 | `GET /api/cube/{tile_id}/channel/{1..102}.png?cmap&size` | PNG de un plano | ~0.1 s |
| 9 | `GET /api/cube/{tile_id}/pixel?x&y&size` · `?ra&dec` | RA/Dec + espectro 102 canales | ~0.3 s |
| 10 | `GET /api/cube/{tile_id}/samples.png?ra&dec` | figura de los 6 canales (como el script) | 1ª vez ~4 s |
| 11 | `GET /api/cube/{tile_id}/preview3d?n=32` | malla 102×n×n (JSON) para WebGL | ~0.3 s |
| 12 | `GET /api/cube/{tile_id}/cube3d.png` | vista 3D estática (como el script) | 1ª vez ~9 s |
| 13 | `GET /api/cube/{tile_id}/fits` | 307 → tesela FITS original en IRSA | — |
| — | `GET /api/jobs/{id}` | estado de los trabajos `l2p_…` y `cub_…` | — |

`frame_id` (p. ej. `sx-d3-1746761764061`) sale de `/api/l2` (o de `/api/frames`, `/api/sequence`); `tile_id`
(p. ej. `hpx5-2858`) sale de `/api/cube`.

---

## 3. `GET /api/l2` — preparar la imagen L2

| Parámetro | Por defecto | Qué hace |
|---|---|---|
| `ra`, `dec` | — | Grados ICRS |
| `date_from`, `date_to` | — | Rango de fechas (YYYY-MM-DD) para elegir la exposición |
| `detector` | todos | 1–6 (banda: 1 = 0.75–1.09 µm … 6 = 4.42–5.00 µm) |
| `frame_id` | — | Exposición concreta (de `candidates`); ignora la regla de selección |
| `size` | 1024 | Lado del PNG en px (64–2040; nativo 2040). Se usa en las URLs devueltas |

**Selección (como el pipeline):** exposiciones que cubren el punto (consulta de 1″), en orden cronológico; dentro de
las fechas se toma la **tercera** (o la última si hay menos de tres). Las exposiciones simultáneas de dos detectores
(D1/D4, D2/D5, D3/D6) se ordenan por detector.

Ejemplo real (M101):

```bash
curl "http://localhost:8000/api/l2?ra=210.80227&dec=54.34895&size=1024"
# → 202 {"id": "l2p_00715b94eebb", "kind": "l2", "progress": 0.4, "stage": "Descargando L2 completo 26/65 MB", ...}
```

`result` (resumido):

```json
{
  "frame": {"id": "sx-d3-1746761764061", "date_obs": "2025-05-09T03:36:04.060Z", "detector": 3, "band_um": 2.0324,
            "obs_id": "2025W19_2B_0101_2", "access_url": "https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/…D3_spx_l2b-v27-2026-198.fits"},
  "selection": {"rule": "tercera exposición en orden cronológico dentro del rango (o la última si hay menos de tres), como el pipeline",
                "index": 2, "candidates_total": 246},
  "candidates": [{"id": "sx-d3-1746761635152", "date_obs": "2025-05-09T03:33:55.152Z", "detector": 3, "band_um": 2.0324}, "…"],
  "native_size": {"width": 2040, "height": 2040},
  "png_size": {"width": 1024, "height": 1024},
  "unit": "MJy / sr",
  "normalization": {"low": 0.0803, "high": 0.2089, "percentiles": [3.5, 96.5]},
  "orientation": {"north_deg_from_up": 230.08, "east_deg_from_up": 139.75, "pixel_scale_arcsec": 6.2731,
                  "center": {"ra": 209.1914, "dec": 53.8287}},
  "target": {"ra": 210.80222, "dec": 54.34889, "x": 576.304, "y": 825.098, "inside": true,
             "brightness": {"value": 13.1029, "unit": "MJy / sr", "zodi": 0.0818, "value_minus_zodi": 13.021, "error": 0.12},
             "wavelength_um": 2.24207, "flags": ["source"]},
  "raw_png_url": "/api/l2/sx-d3-1746761764061/raw.png?size=1024",
  "color_png_url": "/api/l2/sx-d3-1746761764061/color.png?size=1024",
  "pixel_url": "/api/l2/sx-d3-1746761764061/pixel?x={x}&y={y}&size=1024",
  "fits_url": "/api/l2/sx-d3-1746761764061/fits",
  "colormaps": {"raw": "gray_r (gris invertido, como el pipeline)", "color": "cividis (pseudocolor, no RGB visible)"},
  "partial": false, "warnings": []
}
```

| Campo | Para qué sirve |
|---|---|
| `raw_png_url`, `color_png_url` | `<img src>` de las dos vistas |
| `target.x/y` | Dónde está la coordenada pedida en el PNG → dibujar un marcador |
| `target.brightness`, `wavelength_um` | Valor en la coordenada pedida, sin tener que hacer clic |
| `pixel_url` | Plantilla del clic: sustituir `{x}` `{y}` |
| `candidates` | Selector de exposición/fecha → volver a llamar con `frame_id` |
| `orientation` | Flecha de norte/este: el L2 está en la orientación del detector, **no** con el norte arriba |
| `normalization` | Rango usado (para una barra de color) |

---

## 4. Imágenes raw y color

```
GET /api/l2/sx-d3-1746761764061/raw.png?size=1024              → PNG 1024×1024 gris invertido (1.8 MB)
GET /api/l2/sx-d3-1746761764061/raw.png?size=1024&invert=false → gris normal (fondo negro, estrellas blancas)
GET /api/l2/sx-d3-1746761764061/color.png?size=1024            → PNG 1024×1024 cividis (2.5 MB)
```

- Normalización del pipeline: `(IMAGE − p3.5) / (p96.5 − p3.5)` recortado a [0, 1].
- Raw: `gray_r` (como `l2_gray.png`); píxeles inválidos = valor 0 (blanco en gris invertido).
- Color: `cividis` (como `l2_rgb.png`); píxeles inválidos en **negro**.
- `size` 2040 = resolución nativa (≈ 3–7 MB). Para pantalla, 1024 basta.

---

## 5. `GET /api/l2/{frame_id}/pixel` — clic → RA/Dec + brillo

| Parámetro | Qué es |
|---|---|
| `x`, `y` | Píxel del PNG mostrado: x desde el borde **izquierdo**, y desde el borde **superior** (continuos) |
| `size` | El **mismo** `size` con el que se pidió el PNG |
| `ra`, `dec` | Alternativa a x/y: devuelve el píxel y los valores de esa coordenada |

Ejemplo real (clic sobre el núcleo de M101):

```bash
curl "http://localhost:8000/api/l2/sx-d3-1746761764061/pixel?x=576.304&y=825.098&size=1024"
```

```json
{
  "frame_id": "sx-d3-1746761764061", "date_obs": "2025-05-09T03:36:04.060Z", "detector": 3,
  "x": 576.304, "y": 825.098, "size": 1024,
  "ra": 210.8022225, "dec": 54.3488888,
  "inside": true,
  "fits_pixel": {"col": 1147.606, "row": 395.75, "i": 1148, "j": 396},
  "brightness": {"value": 13.102856636047363, "unit": "MJy / sr", "zodi": 0.08183363080024719,
                 "value_minus_zodi": 13.021023005247116, "error": 0.12004563286002266},
  "wavelength_um": 2.24207,
  "flags": ["source"], "flags_raw": 2097152,
  "valid": true
}
```

| Campo | Qué es |
|---|---|
| `ra`, `dec` | Coordenada exacta del punto clicado (WCS de la imagen) |
| `brightness.value` | Brillo superficial del L2 en ese píxel (MJy/sr), tal como lo publica IRSA (incluye luz zodiacal) |
| `brightness.zodi` | Modelo de luz zodiacal en ese píxel (extensión ZODI) |
| `brightness.value_minus_zodi` | Brillo del cielo/objeto sin la zodiacal |
| `brightness.error` | √VARIANCE (incertidumbre 1σ) |
| `wavelength_um` | Longitud de onda de **ese** píxel (WCS-WAVE): el filtro de SPHEREx varía a lo largo del detector |
| `flags` | Banderas de calidad del píxel (`source` = hay una fuente detectada, no es un error) |
| `fits_pixel` | Columna/fila del FITS (0-based) |
| `inside` / `valid` | Fuera de la imagen / píxel sin dato |

Fondo de cielo del mismo frame (clic en `x=100, y=100`): `value 0.128`, `zodi 0.115`, `value_minus_zodi 0.013`,
`wavelength_um 1.693`.

---

## 6. `GET /api/cube` — preparar el cubo

| Parámetro | Por defecto | Qué hace |
|---|---|---|
| `ra`, `dec` | — | Grados ICRS |
| `size` | nativo (512) | Lado de los PNG de los canales (64–2048) |

Busca la tesela HEALPix (colección `spherex_dr1_cube`) que **contiene** la coordenada, la descarga completa (~320 MB),
verifica 102 planos, `BUNIT` y longitudes crecientes (como el pipeline). El cubo **no tiene fechas**: combina todo el
survey.

Ejemplo real (M101; descarga de 320.9 MB en 67 s, luego 200 directo):

```json
{
  "tile": {"id": "hpx5-2858", "obs_id": "hips5_cube-hips_20261026", "hpx_order": 5, "hpx_npix": 2858,
           "projection": "HPX", "size_mb": 320.9,
           "access_url": "https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/hips5_cube/hips_20261026/Norder5/Dir0/Npix2858_cube.fits"},
  "shape": [102, 512, 512], "unit": "MJy/sr",
  "native_size": {"width": 512, "height": 512}, "png_size": {"width": 512, "height": 512},
  "orientation": {"north_deg_from_up": 151.26, "east_deg_from_up": 225.0, "pixel_scale_arcsec": 14.6594,
                  "center": {"ra": 211.875, "dec": 54.3409}},
  "target": {"ra": 210.80222, "dec": 54.34889, "x": 403.38, "y": 111.279,
             "fits_pixel": {"col": 402.88, "row": 400.221, "i": 403, "j": 400}},
  "spectrum": [
    {"channel": 1,   "wavelength_um": 0.753, "bandwidth_um": 0.018, "brightness": 5.618,  "flags": ["source"], "nhits": 5},
    {"channel": 40,  "wavelength_um": 1.861, "bandwidth_um": 0.046, "brightness": 11.485, "flags": ["source"], "nhits": 8},
    {"channel": 102, "wavelength_um": 4.988, "bandwidth_um": 0.037, "brightness": 2.744,  "flags": ["source"], "nhits": 10},
    "… 102 en total"
  ],
  "channels": [
    {"channel": 1, "wavelength_um": 0.753, "lower_um": 0.743, "upper_um": 0.762, "bandwidth_um": 0.018,
     "bandwidth_source": "columna de ancho", "detector": 1, "resolving_power": 42.0,
     "png_url": "/api/cube/hpx5-2858/channel/1.png?cmap=gray_r"},
    "… 102 en total"
  ],
  "six_channels": ["los canales 1, 20, 40, 60, 80, 102 con la misma forma"],
  "urls": {
    "channels_all": "/api/cube/hpx5-2858/channels?set=all",
    "channels_six": "/api/cube/hpx5-2858/channels?set=six",
    "samples_png": "/api/cube/hpx5-2858/samples.png?ra=210.8022222222222&dec=54.34888888888889",
    "pixel": "/api/cube/hpx5-2858/pixel?x={x}&y={y}",
    "preview3d": "/api/cube/hpx5-2858/preview3d",
    "cube3d_png": "/api/cube/hpx5-2858/cube3d.png",
    "fits": "/api/cube/hpx5-2858/fits"
  },
  "partial": false, "warnings": []
}
```

| Campo | Para qué sirve |
|---|---|
| `channels[].png_url` | Las 102 imágenes: slider de longitud de onda |
| `six_channels[].png_url` | Las 6 del script: galería de 6 paneles |
| `spectrum` | Gráfico λ vs brillo en la coordenada (con `flags` y `nhits` = nº de exposiciones combinadas) |
| `target.x/y` | Marcador de la coordenada en los PNG del cubo |
| `orientation` | La tesela HEALPix **no** está con el norte arriba (aquí el norte apunta a 151° desde arriba) |

Seis canales del script en esta tesela:

| Canal | λ (µm) | Δλ (µm) | Brillo M101 (MJy/sr) |
|---|---|---|---|
| 1 | 0.753 | 0.018 | 5.62 |
| 20 | 1.167 | 0.028 | 8.50 |
| 40 | 1.861 | 0.046 | 11.49 |
| 60 | 3.033 | 0.089 | 5.45 |
| 80 | 4.220 | 0.037 | 3.88 |
| 102 | 4.988 | 0.037 | 2.74 |

---

## 7. Canales (102 y 6)

```bash
curl "http://localhost:8000/api/cube/hpx5-2858/channels?set=six"     # 6 canales
curl "http://localhost:8000/api/cube/hpx5-2858/channels?set=all"     # 102 canales
curl "http://localhost:8000/api/cube/hpx5-2858/channels?set=all&cmap=inferno&size=256"
```

```json
{"tile_id": "hpx5-2858", "set": "six", "unit": "MJy/sr", "count": 6,
 "channels": [{"channel": 1, "wavelength_um": 0.753, "lower_um": 0.743, "upper_um": 0.762, "bandwidth_um": 0.018,
               "detector": 1, "resolving_power": 42.0, "png_url": "/api/cube/hpx5-2858/channel/1.png?cmap=gray_r"}, "…"]}
```

Imagen de un canal: `GET /api/cube/hpx5-2858/channel/40.png?cmap=inferno&size=512`

- Cada plano se normaliza con **sus** percentiles 3.5–96.5 (como `samples.png`).
- `cmap`: `gray_r` (por defecto, como el script), `gray`, `inferno`, `magma`, `viridis`, `cividis`.
- Sin datos → transparente. ~450–700 KB a 512 px; 0.1 s.

---

## 8. Clic en el cubo, figura de 6 canales y 3D

**Clic → RA/Dec + espectro de 102 canales**

```bash
curl "http://localhost:8000/api/cube/hpx5-2858/pixel?x=403.38&y=111.279"
```

```json
{"tile_id": "hpx5-2858", "x": 403.38, "y": 111.279, "size": null,
 "ra": 210.80222, "dec": 54.34889, "inside": true, "fits_pixel": {"col": 402.88, "row": 400.22, "i": 403, "j": 400},
 "unit": "MJy/sr", "valid_channels": 100,
 "spectrum": [{"channel": 1, "wavelength_um": 0.753, "bandwidth_um": 0.018, "brightness": 5.618,
               "flags_raw": 2097152, "flags": ["source"], "nhits": 5}, "… 102"]}
```

**Figura del script** (`samples.png`, 6 paneles con ejes RA/Dec, λ, Δλ, barra de color y cruz roja):
`GET /api/cube/hpx5-2858/samples.png?ra=210.80227&dec=54.34895`

**3D:**
- `GET /api/cube/hpx5-2858/preview3d?n=32` → `{"shape": [102, 32, 32], "x": […], "y": […], "wavelength_um": […102],
  "brightness": [[[…]]], "color_range": {"low", "high", "cmap": "inferno"}}` — el `preview3d.npz` del pipeline en JSON,
  para un visor WebGL (Three.js / Plotly `surface` por plano).
- `GET /api/cube/hpx5-2858/cube3d.png` → la figura estática `cube3d.png` del pipeline.

---

## 9. Píxeles, orientación y tamaños

- **Coordenadas de clic** = píxeles **continuos** del PNG que se muestra: `x` desde la izquierda, `y` desde **arriba**.
  Si el PNG se muestra escalado en pantalla, convertir: `x = clickX · (png_size.width / anchoEnPantalla)`.
- Pasar el **mismo `size`** con el que se pidió el PNG (las URLs devueltas ya lo traen).
- Los PNG se dibujan como el pipeline (`origin='lower'`): la fila 0 del FITS queda **abajo**.
- Ni el L2 (orientación del detector) ni el cubo (proyección HEALPix) tienen el norte arriba: `orientation` da el
  ángulo del norte y del este medido desde "arriba" en sentido horario, para dibujar una brújula.
- Para imágenes alineadas con el norte arriba y recortadas (blink), seguir usando `/api/sequence` y `/api/frame/{id}.png`.

---

## 10. Código TypeScript

```ts
const API = "https://nasaspace-1ebj.onrender.com/api";

async function prepare<T>(url: string, onProgress?: (p: number, stage: string) => void): Promise<T> {
  let r = await fetch(url);
  let d = await r.json();
  if (r.status === 200) return d;
  if (r.status !== 202) throw new Error(d.error.message);
  while (true) {                                           // 202 → sondear el trabajo
    await new Promise(res => setTimeout(res, d.poll_after_ms ?? 1000));
    d = await (await fetch(`${API}/jobs/${d.id}`)).json();
    onProgress?.(d.progress, d.stage);                     // "Descargando tesela hpx5-2858 160/321 MB"
    if (d.status === "done") return d.result;
    if (d.status === "failed") throw new Error(d.error.message);
  }
}

// L2
const l2 = await prepare<any>(`${API}/l2?ra=${ra}&dec=${dec}&date_from=2025-05-01&date_to=2025-06-01&size=1024`);
rawImg.src = API.replace("/api", "") + l2.raw_png_url;
colorImg.src = API.replace("/api", "") + l2.color_png_url;

rawImg.onclick = async (e) => {
  const k = l2.png_size.width / rawImg.clientWidth;
  const x = e.offsetX * k, y = e.offsetY * k;
  const p = await (await fetch(API.replace("/api", "") +
    l2.pixel_url.replace("{x}", x.toFixed(2)).replace("{y}", y.toFixed(2)))).json();
  tooltip(`RA ${p.ra.toFixed(5)}°  Dec ${p.dec.toFixed(5)}°\n` +
          `${p.brightness.value?.toFixed(4)} ${p.brightness.unit}  (λ ${p.wavelength_um} µm)\n` +
          `sin zodiacal: ${p.brightness.value_minus_zodi?.toFixed(4)} ± ${p.brightness.error?.toFixed(4)}`);
};

// Cubo
const cube = await prepare<any>(`${API}/cube?ra=${ra}&dec=${dec}`);
slider.max = "102";
slider.oninput = () => cubeImg.src = API.replace("/api", "") + cube.channels[+slider.value - 1].png_url;
sixGallery(cube.six_channels.map((c: any) => ({ src: c.png_url, label: `${c.wavelength_um.toFixed(3)} µm` })));
plotSpectrum(cube.spectrum.map((s: any) => [s.wavelength_um, s.brightness]));
```

---

## 11. Rendimiento, caché y despliegue

| Operación | Primera vez | Después |
|---|---|---|
| `/api/l2` (descarga 65 MB) | 10–60 s según la red | 200 directo |
| `raw.png` / `color.png` 1024 px | ~5 s | caché |
| `/api/cube` (descarga 320 MB) | 67 s medido (30–90 s) | 200 directo |
| Canal PNG | 0.1 s | caché |
| Clic L2 / clic cubo | < 0.5 s | — |
| `samples.png` / `cube3d.png` | 4 s / 9 s | caché |

- Descarga paralela con rangos HTTP (4 conexiones, `PRODUCT_DOWNLOAD_WORKERS`), como `downloads.py` del pipeline;
  respaldo secuencial; solo se publican archivos completos.
- Los FITS completos se guardan en `CACHE_DIR/products` hasta `PRODUCT_CACHE_GB` (3 GB por defecto); al llenarse se
  borran los menos usados. Si un FITS se borró, el PNG o el clic lo vuelven a descargar.
- Se leen con **memmap** (por planos/píxeles): un cubo de 320 MB no se carga entero en memoria.
- **Render (plan gratuito):** el disco es efímero (se pierde al reiniciar) y el ancho de banda/tiempo de las
  descargas grandes puede acercarse a los límites. Para la demo: preparar las coordenadas antes (llamar a
  `/api/l2` y `/api/cube`), o usar un disco persistente montado en `/data/cache`.
- Varios usuarios pidiendo la misma tesela a la vez → una sola descarga.

---

## 12. Qué se tomó del pipeline y qué cambió

| Pipeline (`SphereX-pipeline`) | Backend |
|---|---|
| `query()` SIA con 1″, orden por `t_min` | `sx.find_frames` (misma consulta SIA, con reintentos, caché e índice local) |
| L2: tercera observación cronológica | Igual, **dentro de las fechas pedidas**; o `frame_id` |
| `normalize()` percentiles 3.5–96.5 | Igual (`products/common.py`) |
| `l2_gray.png` (`gray_r`), `l2_rgb.png` (`cividis`, inválidos en negro) | `raw.png` (opción `invert=false`), `color.png` |
| — | **Clic** → RA/Dec + brillo, zodiacal, error, λ del píxel, banderas (nuevo) |
| `allsky()`: tesela que contiene el punto, 102 planos, BUNIT, λ crecientes | Igual (`products/cube.py`) |
| `spectral.interpret` | Copiado; añadidos los alias `WL_MIN`/`WL_MAX` (el pipeline los dejaba en NaN) |
| `spectrum.ecsv`, `samples.ecsv`, `samples.npy` | `spectrum` y `six_channels` en JSON; PNG por canal |
| `samples.png`, `cube3d.png`, `preview3d.npz` | `samples.png`, `cube3d.png`, `preview3d` (JSON) |
| `downloads.fetch` (rangos HTTP, caché por URL) | Portado a async (httpx) + límite de disco LRU + una descarga por URL |
| `server.py` (1 trabajo a la vez, 409 si ocupado) | Patrón 202 + trabajo de la API (cola, progreso, caché del resultado) |
| `manifest.json`, `outputs/{id}` | El resultado del trabajo (JSON) y la caché |

La carpeta `backend/app/SphereX-pipeline/` se deja como referencia y **no entra en la imagen Docker** (`.dockerignore`),
incluida su `.cache` de 320 MB.
