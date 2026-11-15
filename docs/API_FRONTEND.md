# API de SPHEREx — Guía de contratos para el Frontend

Documento único para conectar el Frontend (BlinkX-Client) al backend. Cada endpoint trae la **URL completa**, los
**parámetros** (request), la **respuesta real** (response), los **errores** y **cómo usarlo** en la interfaz.

Las respuestas de ejemplo son **reales** (datos SPHEREx DR1 de la NASA/IRSA), recortadas con `"…(n más)"` donde
la lista es larga.

---

## 0. Datos de conexión

| | |
|---|---|
| **URL base (producción, Render)** | `https://nasaspace-1ebj.onrender.com` |
| **Swagger (probar cada endpoint desde el navegador)** | https://nasaspace-1ebj.onrender.com/docs |
| **OpenAPI (JSON)** | https://nasaspace-1ebj.onrender.com/openapi.json |
| **Prueba rápida** | https://nasaspace-1ebj.onrender.com/api/status → `"spherex_mode": "irsa"` |
| **Backend local (misma Wi-Fi)** | `http://<IP-de-la-laptop-del-backend>:8000` |

Variable de entorno del Frontend (archivo `.env` en la raíz del proyecto del Frontend):

```bash
# Astro (solo expone al navegador las variables que empiezan por PUBLIC_)
PUBLIC_API_URL=https://nasaspace-1ebj.onrender.com
# Vite puro / Next.js / CRA, si aplica
VITE_API_URL=https://nasaspace-1ebj.onrender.com
```

`src/lib/skyApi.ts` (última versión, en el repo del backend: `docs/frontend/skyApi.ts`) la lee así:

```ts
export const API_BASE = (
  env.PUBLIC_API_URL || env.VITE_API_URL || env.NEXT_PUBLIC_API_URL || env.REACT_APP_API_URL || "http://localhost:8000"
).replace(/\/+$/, "");
```

Después de cambiar el `.env` hay que **reiniciar** `npm run dev`.

> **Render gratuito se duerme** tras ~15 min sin uso: la primera petición tarda **~30–60 s** en despertar.
> Antes de una demo abrir `/api/status` una vez. Además, su disco **no es persistente**: al reiniciarse se pierde la
> caché y las primeras consultas vuelven a ser lentas.

---

## 1. Reglas generales del contrato

| Tema | Regla |
|---|---|
| Métodos | Todo es `GET`, salvo `POST /api/asteroids/predict-from-current` (JSON) |
| Autenticación | Ninguna |
| CORS | Abierto (`*`) para GET/POST/OPTIONS: funciona desde `localhost:4321`, Vercel, ngrok… |
| Coordenadas | Grados decimales ICRS/J2000. `ra` 0 ≤ ra < 360, `dec` −90…90 |
| Ángulos pequeños (`fov`, `radius`, `aperture`) | **Segundos de arco** (″). 0.08° = 288″, 0.15° = 540″ |
| Fechas en query | `YYYY-MM-DD` (`date_from`, `date_to`); `at` acepta también ISO completo |
| Fechas en respuestas | ISO 8601 UTC con milisegundos: `2025-05-05T16:48:02.419Z` |
| Listas en query | `?a=x&a=y` **o** `?a=x,y` |
| URLs de imágenes | **Relativas** (`/api/frame/…png?...`) → anteponer la URL base: `API_BASE + png_url` (en skyApi: `abs(url)`) |
| Imágenes PNG | Cuadradas en `/api/sequence` y `/api/stack` (norte arriba, este a la izquierda). Cabecera `Cache-Control: public, max-age=86400` |
| Caché | Las respuestas traen `"cache": "hit" \| "miss"` en algunos endpoints. La misma consulta (ra/dec redondeados a 1″) se responde desde caché |
| Límite de peticiones | 120 por minuto por IP (ráfaga 40). Si se pasa → `429` con cabecera `Retry-After` (segundos) |

### 1.1 Formato de error (siempre igual)

```json
{ "error": { "code": "bad_request", "message": "ra debe ser menor que 360" } }
```

| HTTP | `code` | Cuándo | Qué hacer en la UI |
|---|---|---|---|
| 400 | `bad_request` | Parámetro inválido o faltante | Mostrar `message` (ya en español) |
| 404 | `not_found` | Nombre/objeto/frame/trabajo inexistente | Mostrar `message` |
| 422 | `identity_mismatch` | Asteroides: el clic no coincide con ese asteroide | Ver §6 |
| 429 | `rate_limited` | Demasiadas peticiones | Esperar `Retry-After` s (skyApi lo hace solo) |
| 502 | `upstream_error` | IRSA/SIMBAD/NED/Horizons (NASA) falló | Botón "Reintentar" (no reintentar en bucle) |
| 504 | `upstream_timeout` | Un servicio externo no respondió a tiempo | Botón "Reintentar" |

Ejemplo real de 404:

```json
{ "error": { "code": "not_found", "message": "No encontramos \"zzzzqqqxx\" en SIMBAD ni en NED." } }
```

### 1.2 Respuestas parciales

Muchos endpoints traen `"partial": true` y `"warnings": [...]` cuando una parte de los datos externos falló
(p. ej. IRSA devolvió 503 en algunas exposiciones). **Los datos que vienen son válidos**: mostrarlos y, si
`partial`, un aviso discreto ("Faltan algunas exposiciones; reintenta más tarde").

---

## 2. Procesos lentos: patrón `202 + trabajo`

Estos endpoints descargan FITS reales de la NASA y pueden tardar de segundos a minutos la primera vez:

`/api/sequence` · `/api/stack` · `/api/spectrum` · `/api/composition` · `/api/predict` · `/api/l2` · `/api/cube`

**Flujo:**

```
GET /api/sequence?ra=…        ──► 200 + resultado                (si ya estaba calculado: instantáneo)
                              └─► 202 + trabajo {id, status…}    (si hay que calcularlo)
GET /api/jobs/{id}  (cada poll_after_ms) ──► status: queued → running → done (result) | failed (error)
```

### 2.1 `GET /api/jobs/{job_id}`

URL: `https://nasaspace-1ebj.onrender.com/api/jobs/seq_37d1ae94c942`

`job_id` cumple `^[a-z0-9]{3}_[0-9a-f]{6,40}$` (prefijos: `seq` sequence, `stk` stack, `spc` spectrum, `cmp`
composition, `prd` predict, `l2p` l2, `cub` cube).

Respuesta (también es el cuerpo del 202 inicial):

```json
{
  "id": "seq_37d1ae94c942",
  "kind": "sequence",
  "status": "queued",
  "progress": 0.0,
  "eta_s": 85,
  "stage": "En cola",
  "queue_position": 1,
  "poll_after_ms": 1500,
  "created_at": "2026-11-15T05:32:08Z",
  "result": null,
  "error": null
}
```

| Campo | Significado | UI |
|---|---|---|
| `status` | `queued` · `running` · `done` · `failed` | |
| `progress` | 0…1 | Barra de progreso |
| `stage` | Texto en español: "En cola", "Descargando frames 3/12", "Fotometría 120/239", "Descargando tesela 160/321 MB" | Texto junto a la barra |
| `eta_s` | Segundos estimados para terminar | "≈ 1 min" |
| `queue_position` | 0 = ejecutándose; ≥ 1 = turno en la cola | "En cola (n)" |
| `poll_after_ms` | Cuánto esperar antes de volver a consultar | **Respetarlo** (mínimo 1 s) |
| `result` | Con `done`: el mismo JSON que devolvería el endpoint con 200 | |
| `error` | Con `failed`: `{code, message}` | Mostrar `message` |

**Reglas importantes:**

- El `id` es **determinista**: la misma consulta devuelve el mismo trabajo (no se duplica).
- **Si nadie consulta un trabajo** durante 30 s (en cola) o 90 s (corriendo), el backend lo **pausa** para liberar
  su lugar. Si se vuelve a consultar o pedir, **se reanuda solo** con el mismo `id`. No hay que hacer nada especial:
  basta con dejar de consultar lo que el usuario ya no mira.
- El backend procesa **4 trabajos a la vez** (las descargas grandes de cubo/L2, de 1 en 1).
- Un trabajo `done` sigue disponible 1 h en `/api/jobs/{id}`; el resultado queda en caché **7 días** (la misma
  consulta responde 200 directo).

`skyApi.ts` ya implementa todo esto: `api.sequence(params, onProgress, signal)` espera sola y llama a
`onProgress(progress, stage)`. **No escribir bucles propios de sondeo.**

---

## 3. Búsqueda y catálogos (rápidos)

### 3.1 `GET /api/resolve` — nombre → coordenadas

URL: `https://nasaspace-1ebj.onrender.com/api/resolve?name=WISE%200855`

| Parámetro | Tipo | Obligatorio | Default | Descripción |
|---|---|---|---|---|
| `name` | string ≤ 200 | ✔ | | Nombre del objeto (SIMBAD, NED; asteroides: usar `/api/predict`) |
| `at` | fecha | | hoy | Fecha a la que se propaga la posición con movimiento propio |

Respuesta 200:

```json
{
  "query": "WISE 0855",
  "matched_name": "WISEA J085510.74-071442.5",
  "ra": 133.81890825,
  "dec": -7.247048,
  "source": "simbad",
  "epoch": 2000.0,
  "pm": { "ra": -8123.7, "dec": 673.2 },
  "parallax_mas": 439.0,
  "current": { "ra": 133.7578976, "dec": -7.24205807, "epoch": 2026.871 },
  "object_type": "BD*"
}
```

- **Usar `current.ra` / `current.dec`** para centrar la vista (es la posición actual, con movimiento propio).
  `ra`/`dec` son la posición de catálogo (época 2000).
- `pm` en mas/año (`null` si no se conoce). Errores: 404 si no existe.

### 3.2 `GET /api/objects` — qué hay en la región (SIMBAD + NED)

URL: `https://nasaspace-1ebj.onrender.com/api/objects?ra=133.795129&dec=-7.245147&radius=60`

| Parámetro | Tipo | Obligatorio | Default | Descripción |
|---|---|---|---|---|
| `ra`, `dec` | número | ✔ | | Centro |
| `radius` | ″, 0 < r ≤ 600 | | 60 | Radio del cono. Para una imagen de `fov` usar `fov/2` |
| `catalogs` | lista | | ambos | `simbad`, `ned` |
| `types` | lista | | todos | `star`, `galaxy`, `other` |
| `limit` | 1…2000 | | 200 | |

Respuesta 200:

```json
{
  "center": { "ra": 133.795, "dec": -7.245277777777778 },
  "radius_arcsec": 60.0,
  "counts": { "simbad": 1, "ned": 14 },
  "objects": [
    {
      "id": "ned:WISEA J085510.74-071442.5",
      "name": "WISEA J085510.74-071442.5",
      "source": "ned",
      "ra": 133.7947597,
      "dec": -7.2451456,
      "type": "other",
      "type_raw": "",
      "z": null,
      "distance_arcsec": 0.981,
      "pm": null
    },
    "…"
  ],
  "partial": false,
  "warnings": [],
  "cache": "miss"
}
```

- Ordenados por `distance_arcsec` (distancia al centro).
- **Etiquetas:** mostrar nombre solo de `source === "simbad"` o de los 10 más cercanos; el resto en tooltip
  (NED trae muchos nombres largos `WISEA J…`).
- `z` = corrimiento al rojo (galaxias). `pm` en mas/año.

### 3.3 `GET /api/frames` — lista de exposiciones (sin imágenes)

URL: `https://nasaspace-1ebj.onrender.com/api/frames?ra=133.795129&dec=-7.245147&fov=300&limit=200`

| Parámetro | Tipo | Obligatorio | Default | Descripción |
|---|---|---|---|---|
| `ra`, `dec` | número | ✔ | | |
| `fov` | ″, 10…3600 | | 300 | |
| `detector` | 1…6 | | todos | 1: 0.75–1.09 µm · 2: 1.10–1.62 · 3: 1.63–2.41 · 4: 2.42–3.82 · 5: 3.83–4.41 · 6: 4.42–5.00 |
| `date_from`, `date_to` | YYYY-MM-DD | | | |
| `limit` | 1…1000 | | 200 | |
| `offset` | ≥ 0 | | 0 | Paginación |

Respuesta 200:

```json
{
  "center": { "ra": 133.795, "dec": -7.245277777777778 },
  "fov_arcsec": 300.0,
  "total": 258,
  "limit": 2,
  "offset": 0,
  "frames": [
    {
      "id": "sx-d3-1746463682419",
      "date_obs": "2025-05-05T16:48:02.419Z",
      "detector": 3,
      "band_um": 2.0324,
      "band_exact": false,
      "center": { "ra": 134.41457781198824, "dec": -9.004141208277177 },
      "fov_arcsec": 12648.0,
      "exposure_s": 113.5826,
      "flags": [],
      "obs_id": "2025W19_1B_0116_1"
    },
    "…"
  ],
  "partial": false,
  "warnings": [],
  "cache": "hit"
}
```

- `id` = `sx-d{detector}-{milisegundos}`: sirve para `/api/frame/{id}.png`, `/api/stack` y `/api/l2?frame_id=`.
- `band_um` = longitud de onda central (µm). `band_exact: false` = estimada por detector.
- `total` = cuántas exposiciones hay en total (para "258 exposiciones").

### 3.4 `GET /api/coverage` — histograma de fechas

URL: `https://nasaspace-1ebj.onrender.com/api/coverage?ra=133.795129&dec=-7.245147`

| Parámetro | Tipo | Obligatorio | Descripción |
|---|---|---|---|
| `ra`, `dec` | número | ✔ | |
| `detector` | 1…6 | | Filtrar por detector |

Respuesta 200:

```json
{
  "center": { "ra": 133.795, "dec": -7.245277777777778 },
  "first_date": "2025-05-05",
  "last_date": "2026-05-14",
  "weeks": [
    { "week_start": "2025-05-05", "count": 30 },
    { "week_start": "2025-10-27", "count": 24 },
    "…"
  ],
  "total": 258,
  "partial": false,
  "warnings": [],
  "cache": "hit"
}
```

Uso: barras del selector de fechas (`week_start` = lunes de la semana, `count` = exposiciones).

### 3.5 `GET /api/overlay` — estrellas de Gaia / WISE para dibujar encima

URL: `https://nasaspace-1ebj.onrender.com/api/overlay?ra=133.795129&dec=-7.245147&fov=300&catalog=wise&at=2025-05-05`

| Parámetro | Tipo | Obligatorio | Default | Descripción |
|---|---|---|---|---|
| `ra`, `dec` | número | ✔ | | Los **mismos** de la secuencia mostrada |
| `fov` | ″ | | 300 | El **mismo** `fov_arcsec` de la secuencia |
| `catalog` | `gaia` \| `wise` | ✔ | | |
| `at` | fecha | | hoy | Fecha del frame visible (`date_obs`): las posiciones se propagan a esa fecha |

Respuesta 200:

```json
{
  "catalog": "wise",
  "center": { "ra": 133.795, "dec": -7.245277777777778 },
  "fov_arcsec": 300.0,
  "epoch": 2026.871,
  "catalog_epoch": 2010.5589,
  "mag_band": "W1",
  "sources": [
    {
      "id": "AllWISE J085500.97-071607.9",
      "ra": 133.75416399,
      "dec": -7.26865934,
      "mag": 11.902,
      "pm": { "ra": 22.0, "dec": 50.0 },
      "catalog_ra": 133.7540635,
      "catalog_dec": -7.2688859
    },
    "…(114 más)"
  ],
  "partial": false,
  "warnings": [],
  "cache": "miss"
}
```

- `ra`/`dec` ya están propagados a `at`: dibujarlos directamente con la fórmula de §8.
- `mag` sirve para el tamaño del círculo (menor = más brillante).

---

## 4. Imágenes

### 4.1 `GET /api/sequence` — imágenes del blink (LENTO → 202)

URL: `https://nasaspace-1ebj.onrender.com/api/sequence?ra=133.795129&dec=-7.245147&fov=300&size=512&detector=6&limit=24`

| Parámetro | Tipo | Obligatorio | Default | Descripción |
|---|---|---|---|---|
| `ra`, `dec` | número | ✔ | | Centro de la imagen |
| `fov` | ″, 10…3600 | | 300 | Lado del cuadro. Recomendado ≥ 540 (0.15°): el píxel de SPHEREx mide 6.2″ |
| `size` | 64…1024 | | 512 | Lado del PNG en píxeles |
| `detector` | 1…6 | | todos | |
| `date_from`, `date_to` | YYYY-MM-DD | | | |
| `stretch` | `linear` `log` `sqrt` `asinh` | | `asinh` | |
| `cmap` | `gray` `viridis` `magma` `inferno` | | `gray` | |
| `scale` | `sequence` \| `frame` | | `sequence` | `sequence` = misma escala en todos (correcto para blink) |
| `limit` | 1…200 | | 60 | **Usar ≤ 24** |
| `offset` | ≥ 0 | | 0 | Paginación |

Respuesta (200 o `result` del trabajo):

```json
{
  "center": { "ra": 133.795, "dec": -7.245277777777778 },
  "fov_arcsec": 300.0,
  "size_px": 256,
  "scale": "sequence",
  "vmin": -0.08020277064293622,
  "vmax": 0.2952296209335337,
  "unit": "MJy/sr",
  "total": 45,
  "limit": 6,
  "offset": 0,
  "frames": [
    {
      "id": "sx-d6-1746463682419",
      "date_obs": "2025-05-05T16:48:02.419Z",
      "detector": 6,
      "band_um": 4.9685,
      "band_exact": true,
      "center": { "ra": 134.40997823066974, "dec": -9.023376583067057 },
      "fov_arcsec": 12648.0,
      "exposure_s": 113.5826,
      "flags": [],
      "obs_id": "2025W19_1B_0116_1",
      "vmin": -0.08020277064293622,
      "vmax": 0.2952296209335337,
      "png_url": "/api/frame/sx-d6-1746463682419.png?ra=133.795000&dec=-7.245278&fov=300&size=256&stretch=asinh&cmap=gray&vmin=-0.0802028&vmax=0.29523",
      "flags_png_url": "/api/frame/sx-d6-1746463682419/flags.png?ra=133.795000&dec=-7.245278&fov=300&size=256"
    },
    "…(5 más)"
  ],
  "partial": false,
  "warnings": []
}
```

- Mostrar cada frame con `<img src={API_BASE + frame.png_url} style="image-rendering: pixelated">`.
- **Guardar `center`, `fov_arcsec` y `size_px` de esta respuesta**: son la geometría para dibujar cualquier
  marcador encima (§8). No usar el FoV del botón.
- `flags_png_url` = capa transparente con píxeles contaminados (ámbar), misma geometría: para un checkbox.
- `frames[].center` / `fov_arcsec` (12648″) son de la **exposición completa**, no del recorte: no usarlos para dibujar.
- **Zoom y pan:** escalar en el navegador (`transform: scale`). Pedir una secuencia nueva solo con
  "Actualizar campo" o al salir del área cargada.

### 4.2 `GET /api/frame/{frame_id}.png` — un frame como PNG

URL: `https://nasaspace-1ebj.onrender.com/api/frame/sx-d6-1746463682419.png?ra=133.795&dec=-7.245278&fov=300&size=512&stretch=asinh&cmap=gray`

| Parámetro | Tipo | Obligatorio | Default |
|---|---|---|---|
| `frame_id` (ruta) | `sx-d…` | ✔ | |
| `ra`, `dec` | número | ✔ | |
| `fov`, `size`, `stretch`, `cmap` | | | 300, 512, asinh, gray |
| `vmin`, `vmax` | MJy/sr | | automático |

Respuesta: `image/png`. **Normalmente no se arma a mano: usar `frames[].png_url` de la secuencia.**

### 4.3 `GET /api/frame/{frame_id}/flags.png` — capa de banderas

URL: `https://nasaspace-1ebj.onrender.com/api/frame/sx-d6-1746463682419/flags.png?ra=133.795&dec=-7.245278&fov=300&size=512`

PNG transparente; usar `frames[].flags_png_url`.

### 4.4 `GET /api/stack` — apilar o restar épocas (LENTO → 202)

URL (resta): `https://nasaspace-1ebj.onrender.com/api/stack?ra=133.795129&dec=-7.245147&fov=300&size=512&op=diff&a=sx-d6-1762166205043&b=sx-d6-1746463682419`

| Parámetro | Tipo | Obligatorio | Default | Descripción |
|---|---|---|---|---|
| `ra`, `dec`, `fov`, `size`, `stretch`, `cmap` | | | | Igual que `/api/sequence` (usar los mismos) |
| `op` | `mean` `median` `sum` `diff` | ✔ | | `diff` = A − B |
| `a` | lista de ids | ✔ | | Frames del grupo A (época nueva) |
| `b` | lista de ids | si `op=diff` | | Frames del grupo B (época vieja) |

Respuesta:

```json
{
  "op": "diff",
  "n_frames": 6,
  "center": { "ra": 269.446111, "dec": 4.770556 },
  "fov_arcsec": 120.0,
  "size_px": 256,
  "vmin": -3.2164,
  "vmax": 3.2164,
  "unit": "MJy/sr",
  "png_url": "/api/stack/stk_cc6f162d39c8.png",
  "partial": false,
  "warnings": []
}
```

- En `diff`: **ámbar = apareció o se hizo más brillante; cian = desapareció**. Escala simétrica (`vmin = −vmax`).
- Llamar solo al activar el modo resta.

### 4.5 `GET /api/stack/{stack_id}.png`

URL: `https://nasaspace-1ebj.onrender.com/api/stack/stk_cc6f162d39c8.png` → `image/png` (usar `png_url`).

---

## 5. Análisis

### 5.1 `GET /api/spectrum` — espectro 0.75–5 µm (LENTO → 202)

URL: `https://nasaspace-1ebj.onrender.com/api/spectrum?ra=133.795129&dec=-7.245147&aperture=6&date_from=2025-11-01&date_to=2025-11-30`

| Parámetro | Tipo | Obligatorio | Default | Descripción |
|---|---|---|---|---|
| `ra`, `dec` | número | ✔ | | La fuente clicada |
| `aperture` | ″, 1…60 | | 6 | Radio de la apertura |
| `date_from`, `date_to` | YYYY-MM-DD | | todo | Para un objeto que se mueve, acotar fechas |

Respuesta:

```json
{
  "center": { "ra": 133.795, "dec": -7.245277777777778 },
  "aperture_arcsec": 6.0,
  "wavelength_unit": "um",
  "flux_unit": "uJy",
  "points": [
    {
      "wavelength": 0.7443,
      "flux": -38.7962,
      "error": 29.2112,
      "date_obs": "2025-11-03T10:36:45.043Z",
      "detector": 1,
      "frame_id": "sx-d1-1762166205043",
      "flags": ["halo"],
      "wavelength_exact": true
    },
    "…(81 más)"
  ],
  "n_exposures": 86,
  "partial": true,
  "warnings": ["sx-d4-1762166205043: spherex: HTTP 503", "…"],
  "cache": "miss"
}
```

- Gráfico: eje X `wavelength` (µm), eje Y `flux` (µJy), barras de error `error`.
- Cuadros D1–D6: agrupar `points` por `detector` y promediar `flux`.
- "Exposiciones" = `n_exposures`.
- Mientras corre (202) mostrar "Calculando… X %" con `stage`, **no** "Sin puntos espectrales".
- Puntos con `flags` (p. ej. `halo`) se pueden dibujar más tenues.
- Llamar **solo al hacer clic** en una fuente.

### 5.2 `GET /api/predict` — dónde estará un objeto en cada fecha (LENTO → 202)

URL: `https://nasaspace-1ebj.onrender.com/api/predict?ra=269.452&dec=4.6933&name=Barnard%27s%20star&date_from=2025-05-01&date_to=2026-06-01`

| Parámetro | Tipo | Obligatorio | Descripción |
|---|---|---|---|
| `ra`, `dec` | número | ✔ | |
| `name` | string | | Si falta, usa el objeto con movimiento conocido más cercano al centro |
| `date_from`, `date_to` | YYYY-MM-DD | ✔ | Máximo 30 años |

Respuesta (estrella con movimiento propio):

```json
{
  "target": "Barnard's star",
  "method": "proper_motion",
  "pm_mas_yr": { "ra": -801.551, "dec": 10362.394 },
  "parallax_mas": 546.9759,
  "catalog_epoch": 2000.0,
  "catalog_position": { "ra": 269.4520769586187, "dec": 4.69336496657667 },
  "positions": [
    { "date": "2025-08-15T20:45:11.376Z", "ra": 269.446229, "dec": 4.7671599, "sigma_arcsec": 0.001 },
    "…(120 más)"
  ],
  "epoch_range": [2025.622, 2026.34],
  "warnings": []
}
```

- `method`: `proper_motion` (estrellas, enanas marrones), `ephemeris` (asteroides/cometas por nombre, JPL
  Horizons) o `static`.
- `positions[]` = una posición por cada fecha con imagen: dibujar un círculo de radio `sigma_arcsec` sobre el frame
  con la misma `date`.

### 5.3 `GET /api/composition` — qué compuestos hay en un punto y en qué cantidad (LENTO → 202)

URL: `https://nasaspace-1ebj.onrender.com/api/composition?ra=354.05882932&dec=2.15506703&aperture=8`

El usuario elige un punto en el mapa (clic → `sequencePixelToSky`) y el backend analiza el espectro SPHEREx de
0.75–5 µm en ese punto. Mide **18 bandas conocidas** de hielos, moléculas, minerales e hidrógeno ionizado y devuelve:

- **qué se detecta** (con significancia en σ),
- **cuánto**: para los **hielos**, la densidad de columna en moléculas/cm², el % respecto al agua y los átomos H/C/O que contienen;
  para el resto, la profundidad de la banda (%) como **indicador relativo**,
- **qué elementos** respalda cada banda,
- un **resumen en español** listo para mostrar.

Usa el mismo espectro que `/api/spectrum`: si ya se pidió el espectro de ese punto, solo tarda el análisis (~1 s).
La primera vez tarda como un espectro (1–15 min según IRSA).

| Parámetro | Tipo | Obligatorio | Default | Descripción |
|---|---|---|---|---|
| `ra`, `dec` | número | ✔ | | El punto elegido |
| `aperture` | ″, 1…60 | | 6 | Radio de la apertura. Fuente puntual: 6; galaxia/nebulosa: 8–15 |
| `date_from`, `date_to` | YYYY-MM-DD | | todo | Acotar fechas (útil si el objeto se mueve) |
| `context` | `auto` `brown_dwarf` `yso` `cloud` `hii` `pn` `galaxy` `star` `solar_system` `unknown` | | `auto` | Tipo de objeto para interpretar las bandas. `auto` = el de SIMBAD en ese punto o, si no hay, deducido del espectro. Para asteroides/cometas mandar `solar_system` |

Cliente: `api.composition({ ra, dec, aperture: 8 }, (p, stage) => barra(p, stage), signal)`.

Respuesta real (galaxia con brote estelar NGC 7714; listas recortadas):

```json
{
  "center": { "ra": 354.0588888888889, "dec": 2.155 },
  "aperture_arcsec": 8.0,
  "simbad_match": { "name": "NGC  7714", "otype": "AGN", "distance_arcsec": 0.228 },
  "n_exposures": 226,
  "spectrum_url": "/api/spectrum?ra=354.0588888888889&dec=2.155&aperture=8.0",
  "context": { "type": "galaxy", "label": "galaxia", "source": "simbad" },
  "summary": "Detectado: Monóxido de carbono gaseoso (4.6 µm) en absorción (22σ, 18%) → CO gaseoso (supergigantes rojas y gas de la galaxia); Hidrógeno ionizado Brackett α (4.05 µm) en emisión (16σ, 32%) → H II (Brα); Hidrógeno ionizado Paschen α (1.875 µm) en emisión (11σ, 23%) → H II (Paα); Metano / CO (2.2–2.45 µm) en absorción (6σ, 10%) → CO bandhead 2.29 µm (estrellas frías); Hielo de agua (H₂O) en absorción (5σ, 4%) → H₂O sólido (hielo). Columna de hielo de agua ≈ 1.5e+16 moléculas/cm². Contexto SIMBAD: galaxia.",
  "detected": ["co_gas_4_6", "br_alpha", "pa_alpha", "ch4_2_3", "h2o_ice"],
  "tentative": ["pah_3_3", "aliphatic_3_4"],
  "attributed_to_other": ["hydrated_2_7um"],
  "ices": [
    { "formula": "H2O", "name": "Hielo de agua (H₂O)", "column_density_cm-2": 1.476e+16, "relative_to_h2o_percent": 100.0 }
  ],
  "elements_in_ices": [
    { "element": "H", "column_density_cm-2": 2.952e+16, "fraction_percent": 66.7 },
    { "element": "O", "column_density_cm-2": 1.476e+16, "fraction_percent": 33.3 }
  ],
  "elements_supported": [
    { "element": "C", "evidence": ["Monóxido de carbono gaseoso (4.6 µm)", "Metano / CO (2.2–2.45 µm)"], "quantified": false },
    { "element": "H", "evidence": ["Hidrógeno ionizado Brackett α (4.05 µm)", "Hidrógeno ionizado Paschen α (1.875 µm)", "…"], "quantified": true },
    { "element": "O", "evidence": ["Monóxido de carbono gaseoso (4.6 µm)", "Hielo de agua (H₂O)", "…"], "quantified": true }
  ],
  "indicators": [
    { "id": "co_gas_4_6", "name": "Monóxido de carbono gaseoso (4.6 µm)", "kind": "absorption", "depth_percent": 18.1,
      "significance_sigma": 22.46, "carriers": ["CO gaseoso (supergigantes rojas y gas de la galaxia)"] },
    { "id": "br_alpha", "name": "Hidrógeno ionizado Brackett α (4.05 µm)", "kind": "emission", "depth_percent": -31.56,
      "significance_sigma": 15.96, "carriers": ["H II (Brα)"] },
    "…"
  ],
  "features": [
    { "id": "br_alpha", "name": "Hidrógeno ionizado Brackett α (4.05 µm)", "group": "emission_line", "center_um": 4.052,
      "band_um": [4.025, 4.08], "status": "detected", "kind": "emission", "significance_sigma": 15.96,
      "depth_percent": -31.56, "depth_error_percent": 1.98, "equivalent_width_um": -0.02269, "continuum_snr": 80.8,
      "channels_in_band": 3, "carriers": ["H II (Brα)"], "elements": ["H"], "line_flux_W_m2": 9.348e-17,
      "expected_in_context": true },
    { "id": "h2o_ice", "name": "Hielo de agua (H₂O)", "group": "ice", "center_um": 3.05, "band_um": [2.88, 3.3],
      "status": "detected", "kind": "absorption", "significance_sigma": 5.42, "depth_percent": 4.37,
      "depth_error_percent": 0.81, "carriers": ["H₂O sólido (hielo)", "minerales hidratados (OH/H₂O) en superficies"],
      "elements": ["H", "O"],
      "ice": { "formula": "H2O", "peak_optical_depth": 0.1879, "integrated_optical_depth_cm-1": 2.951,
               "band_strength_cm_per_molecule": 2e-16, "column_density_cm-2": 1.476e+16,
               "column_density_error_cm-2": 2.725e+15 } },
    { "id": "hydrated_2_7um", "name": "Minerales hidratados (OH, 2.7 µm)", "group": "mineral", "status": "attributed_to_other",
      "reason": "Rasgo presente pero no físico en un objeto de tipo «galaxia»: probablemente es parte de otra banda vecina.", "…": "…" },
    { "id": "ch3oh_ice", "name": "Hielo de metanol (CH₃OH)", "status": "not_detected", "significance_sigma": 1.83,
      "upper_limit_depth_percent": 16.41, "…": "…" },
    "… (18 en total)"
  ],
  "spectrum_binned": [
    { "wavelength_um": 0.7442, "flux_uJy": 19374.88, "error_uJy": 422.39, "n": 1 },
    "…"
  ],
  "quality": { "level": "good", "median_snr_per_channel": 45.8, "channels": 147, "features_measurable": 18,
               "reduced_chi2": 35.44, "input_points": 202, "used_points": 195, "flagged_points_removed": 7,
               "saturated_fraction": 0.0 },
  "method": "Bandas medidas contra un continuo lineal local; detección ≥ 3σ. Hielos: N = ∫τ dν / A …",
  "limitations": ["SPHEREx tiene baja resolución (R≈40 hasta 3.8 µm): bandas vecinas pueden mezclarse.", "…"],
  "warnings": ["Dispersión entre exposiciones 35.4× mayor que el error: …"],
  "partial": true
}
```

**Campos clave para la UI:**

| Campo | Mostrar como |
|---|---|
| `summary` | Texto principal del panel |
| `context.label` (+ `context.source`: `simbad` / `spectrum` / `user` / `none`) | "Tipo de objeto: galaxia (SIMBAD)" |
| `ices[]` | Tabla "Hielos": fórmula, `column_density_cm-2` (moléculas/cm², notación científica), `relative_to_h2o_percent` |
| `elements_in_ices[]` | Gráfico de barras/pastel de átomos en los hielos (`fraction_percent`) |
| `elements_supported[]` | Chips de elementos (H, C, O, Fe, Mg, Si…); `quantified: true` = tiene cantidad en `elements_in_ices` |
| `indicators[]` | Lista de moléculas/minerales/líneas con barra de `|depth_percent|` y `significance_sigma` |
| `features[]` | Detalle de las 18 bandas. `status`: `detected` (≥ 3σ) · `tentative` (2–3σ o datos de baja calidad) · `not_detected` (trae `upper_limit_depth_percent`) · `attributed_to_other` (rasgo no físico en ese tipo de objeto; trae `reason`) · `no_coverage` / `continuum_too_faint` |
| `spectrum_binned[]` | Gráfico del espectro (λ µm vs µJy) con las bandas `features[].band_um` sombreadas: verde detectada, gris no detectada |
| `quality.level` | `good` / `fair` / `poor`. Con `poor` mostrar aviso: "Datos insuficientes o saturados" |
| `warnings[]`, `limitations[]` | Avisos en letra pequeña |

**Cómo leer las cantidades:**

- Hielos: `column_density_cm-2` = moléculas por cm² a lo largo de la línea de visión (p. ej. `1.5e+16`). Es la
  magnitud estándar en astronomía; no es una masa ni un porcentaje del objeto.
- `relative_to_h2o_percent` = abundancia respecto al hielo de agua (en nubes típicas: CO₂ ≈ 15–35 %, CO ≈ 5–40 %).
- Minerales, gases y líneas: `depth_percent` = cuánto baja (o sube, negativo = emisión) el espectro respecto al continuo.
  Más profundo = más abundante, pero **no** se puede convertir en masa.
- La composición elemental total (% de Fe, Si…) **no** es medible con SPHEREx; solo se indica qué elementos respalda cada banda.

**Verificación con objetos reales (backend local, datos SPHEREx DR1):**

| Objeto | Literatura | Resultado |
|---|---|---|
| Enana marrón T4.5 (2MASS J0559−1404) | H₂O, CH₄, CO en la atmósfera | Contexto deducido del espectro: enana marrón · H₂O 1.4 µm (92 %), CH₄ 3.3 µm (89 %), CH₄ 2.3 µm, CO 4.6 µm; 8 rasgos falsos descartados |
| Arp 220 (galaxia ultraluminosa) | hielo de agua, PAH, CO | Hielo H₂O 6.4×10¹⁷ cm⁻² (25σ), PAH 3.3 µm en emisión, CO gaseoso |
| NGC 7714 (brote estelar) | hidrógeno ionizado, PAH | Brα (16σ), Paα (11σ), CO de estrellas frías, PAH (posible) |
| Elias 2-29 (protoestrella brillante) | satura SPHEREx | `quality: poor`, aviso de saturación, nada confirmado |

**Uso recomendado en la app:** botón "Analizar composición" en el panel de la fuente clicada (no automático).
Mientras corre, mostrar `stage` ("Fotometría 40/86", "Analizando bandas de composición").

---

## 6. Asteroides — `POST /api/asteroids/predict-from-current` (JPL Horizons)

URL: `https://nasaspace-1ebj.onrender.com/api/asteroids/predict-from-current`

El usuario marca el asteroide en el frame que está viendo y da su **número MPC** (433 = Eros, 3000 = Leonardo…).
El backend comprueba con **JPL Horizons** (observador = SPHEREx) que el clic sea ese asteroide y devuelve dónde
estará, la trayectoria y los píxeles para dibujarla. **Responde 200 directo** (~0.5–2 s; instantáneo con caché). No
usa IA.

Request (`Content-Type: application/json`):

```json
{
  "asteroid_id": 3000,
  "current_observation": { "mjd_utc": 61280.27835825572, "ra": 60.39039387599737, "dec": 20.06447024678794 },
  "horizon_days": 0.5,
  "image_id": "sx-d6-1787899193362",
  "view": { "ra": 60.3904, "dec": 20.0645, "fov": 1200, "size": 512 }
}
```

| Campo | Tipo | Obligatorio | Descripción |
|---|---|---|---|
| `asteroid_id` | entero > 0 | ✔ | Número MPC |
| `current_observation.mjd_utc` | número | ✔ | Fecha del frame visible en MJD UTC: `isoToMjd(frame.date_obs)` |
| `current_observation.ra`, `.dec` | número | ✔ | Donde hizo clic el usuario (`sequencePixelToSky`) |
| `horizon_days` | 0 < h ≤ 30 | ✔ | Días hacia adelante |
| `view` | `{ra, dec, fov, size}` | recomendado | Geometría del PNG mostrado: `{ra: seq.center.ra, dec: seq.center.dec, fov: seq.fov_arcsec, size: seq.size_px}`. Sin `view` no hay píxeles |
| `image_id` | string | | Frame visible (`frame.id`); requiere `view` |

Respuesta 200 (real):

```json
{
  "asteroid_id": 3000,
  "target": "3000 Leonardo (1981 EG19)",
  "method": "horizons",
  "observer": "500@-163182",
  "current_observation": {
    "mjd_utc": 61280.27835825572,
    "time": "2026-08-28T06:40:50.153Z",
    "ra": 60.39039387599737,
    "dec": 20.06447024678794,
    "ephemeris_ra": 60.3903057,
    "ephemeris_dec": 20.0644192,
    "offset_arcsec": 0.35,
    "pixel": { "x": 256.009, "y_top": 256.046, "inside_image": true, "image_width": 512, "image_height": 512,
               "image_id": null, "image_date_obs": null, "in_frame_footprint": null }
  },
  "predicted_ra_deg": 60.5815245,
  "predicted_dec_deg": 20.0863899,
  "sigma_arcsec": 0.0013743685418725535,
  "forecast_mjd_utc": 61280.778358,
  "forecast_time": "2026-08-28T18:40:50.153Z",
  "horizon_days": 0.5,
  "motion": { "arcsec_per_hour": 54.282, "position_angle_deg": 83.03 },
  "image_id": null,
  "pixel": { "x": null, "y_top": null, "inside_image": false, "image_width": 512, "image_height": 512,
             "image_id": null, "image_date_obs": null, "in_frame_footprint": null },
  "track": [
    { "mjd_utc": 61280.278358, "time": "2026-08-28T06:40:50.153Z", "ra": 60.3903057, "dec": 20.0644192,
      "sigma_arcsec": 0.00137, "pixel": { "x": 256.136, "y_top": 256.124, "inside_image": true, "…": "…" } },
    { "mjd_utc": 61280.340858, "time": "2026-08-28T08:10:50.153Z", "ra": 60.4141218, "dec": 20.0680829,
      "sigma_arcsec": 0.00137, "pixel": { "x": 221.776, "y_top": 250.494, "inside_image": true, "…": "…" } },
    "…(7 más)"
  ],
  "warnings": [],
  "status": "ephemeris"
}
```

| Campo | Uso |
|---|---|
| `current_observation.pixel` | Punto donde marcó el usuario |
| `track[]` (9 puntos, de ahora a la predicción) | Línea de trayectoria: unir los puntos con `pixel.inside_image === true` |
| `pixel` | Marcador de la posición futura. Si `inside_image: false` → "La posición predicha sale de la imagen" |
| `predicted_ra_deg`, `predicted_dec_deg`, `forecast_time` | Panel de texto |
| `motion.arcsec_per_hour`, `motion.position_angle_deg` | Velocidad y dirección (0° = norte, 90° = este) |
| `pixel.x`, `pixel.y_top` | En píxeles del PNG de `view.size`; en pantalla multiplicar por `k = img.clientWidth / view.size` |
| `observer` | `500@-163182` = visto desde SPHEREx; `500@399` = geocentro (aparece un aviso en `warnings`) |

Errores:

| HTTP | `code` | Mensaje para el usuario |
|---|---|---|
| 422 | `identity_mismatch` | "Ese punto no coincide con el asteroide indicado. Revisa el número o haz clic sobre el punto que se mueve." |
| 404 | `not_found` | "No existe un asteroide con ese número." |
| 400 | `bad_request` | `horizon_days` fuera de rango, `image_id` sin `view`, etc. |
| 502 | `upstream_error` | "JPL Horizons no respondió; reintentar." |

---

## 7. L2 completo y cubo all-sky

### 7.1 `GET /api/l2` — imagen L2 entera (LENTO → 202, ~65 MB la primera vez)

URL: `https://nasaspace-1ebj.onrender.com/api/l2?ra=210.80227&dec=54.34895&size=1024`

| Parámetro | Tipo | Obligatorio | Default | Descripción |
|---|---|---|---|---|
| `ra`, `dec` | número | ✔ | | Objetivo |
| `date_from`, `date_to` | YYYY-MM-DD | | | Rango de fechas |
| `detector` | 1…6 | | | |
| `frame_id` | `sx-d…` | | | Elegir una exposición concreta (de `candidates`) |
| `size` | 64…2040 | | 1024 | Lado mayor del PNG. **Usar el mismo en `/pixel`** |

Respuesta (real, M101):

```json
{
  "frame": {
    "id": "sx-d3-1746761764061",
    "date_obs": "2025-05-09T03:36:04.060Z",
    "detector": 3,
    "band_um": 2.0324,
    "band_exact": false,
    "center": { "ra": 209.19139660733282, "dec": 53.828651866469706 },
    "fov_arcsec": 12648.0,
    "exposure_s": 113.5826,
    "flags": [],
    "obs_id": "2025W19_2B_0101_2",
    "access_url": "https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/…/level2_2025W19_2B_0101_2D3_spx_l2b-v27-2026-198.fits",
    "mjd": 60804.150047
  },
  "selection": {
    "rule": "tercera exposición en orden cronológico dentro del rango (o la última si hay menos de tres), como el pipeline",
    "index": 2,
    "candidates_total": 246
  },
  "candidates": [
    { "id": "sx-d3-1746761635152", "date_obs": "2025-05-09T03:33:55.152Z", "detector": 3, "band_um": 2.0324 },
    "…(245 más)"
  ],
  "native_size": { "width": 2040, "height": 2040 },
  "png_size": { "width": 1024, "height": 1024 },
  "unit": "MJy / sr",
  "normalization": { "low": 0.08028469637036324, "high": 0.2089474156498907, "percentiles": [3.5, 96.5] },
  "orientation": {
    "north_deg_from_up": 230.08,
    "east_deg_from_up": 139.75,
    "pixel_scale_arcsec": 6.2731,
    "center": { "ra": 209.19139660733282, "dec": 53.828651866469706 }
  },
  "target": {
    "ra": 210.8022222222222,
    "dec": 54.34888888888889,
    "x": 576.304,
    "y": 825.098,
    "inside": true,
    "brightness": { "value": 13.102856636047363, "unit": "MJy / sr", "zodi": 0.08183363080024719,
                    "value_minus_zodi": 13.021023005247116, "error": 0.12004563286002266 },
    "wavelength_um": 2.24207,
    "flags": ["source"]
  },
  "raw_png_url": "/api/l2/sx-d3-1746761764061/raw.png?size=1024",
  "color_png_url": "/api/l2/sx-d3-1746761764061/color.png?size=1024",
  "pixel_url": "/api/l2/sx-d3-1746761764061/pixel?x={x}&y={y}&size=1024",
  "fits_url": "/api/l2/sx-d3-1746761764061/fits",
  "colormaps": { "raw": "gray_r (gris invertido, como el pipeline)", "color": "cividis (pseudocolor, no RGB visible)" },
  "partial": false,
  "warnings": []
}
```

- **La imagen L2 NO tiene el norte arriba**: está en la orientación del detector (`orientation` da los ángulos).
  Para saber qué hay en un punto, usar `/pixel` (no calcular RA/Dec a mano).
- `target.x`, `target.y` = dónde está el objetivo en el PNG: dibujar una cruz ahí.
- Selector de exposición: listar `candidates` y volver a pedir con `frame_id=`.

### 7.2 `GET /api/l2/{frame_id}/raw.png` y `/color.png`

- `https://nasaspace-1ebj.onrender.com/api/l2/sx-d3-1746761764061/raw.png?size=1024` (`invert=true` por defecto = gris invertido; `invert=false` = gris normal)
- `https://nasaspace-1ebj.onrender.com/api/l2/sx-d3-1746761764061/color.png?size=1024` (cividis)

Usar `raw_png_url` / `color_png_url` de la respuesta.

### 7.3 `GET /api/l2/{frame_id}/pixel` — clic → RA/Dec + brillo

URL: `https://nasaspace-1ebj.onrender.com/api/l2/sx-d3-1746761764061/pixel?x=576&y=825&size=1024`

| Parámetro | Descripción |
|---|---|
| `x`, `y` | Píxel del PNG: x desde el borde **izquierdo**, y desde el borde **superior** (decimales permitidos) |
| `size` | El **mismo** `size` del PNG |
| `ra`, `dec` | Alternativa a x/y: coordenada → píxel y valores |

Respuesta:

```json
{
  "frame_id": "sx-d3-1746761764061",
  "date_obs": "2025-05-09T03:36:04.060Z",
  "detector": 3,
  "x": 576.0,
  "y": 825.0,
  "size": 1024,
  "ra": 210.80070193276995,
  "dec": 54.349494543159274,
  "inside": true,
  "fits_pixel": { "col": 1147.0, "row": 395.945, "i": 1147, "j": 396 },
  "brightness": { "value": 11.574589729309082, "unit": "MJy / sr", "zodi": 0.08183559030294418,
                  "value_minus_zodi": 11.492754139006138, "error": 0.11263680898108125 },
  "wavelength_um": 2.24207,
  "flags": ["source"],
  "flags_raw": 2097152,
  "valid": true
}
```

Clic → `const {x, y} = clickToPng(evento, img)` → esta llamada → mostrar RA/Dec, brillo ± error, λ y banderas.
Si `inside: false` → "fuera de la imagen".

### 7.4 `GET /api/l2/{frame_id}/fits` → 307 a IRSA (descarga del FITS original, ~65 MB)

### 7.5 `GET /api/cube` — cubo all-sky de 102 canales (LENTO → 202, ~320 MB la primera vez)

URL: `https://nasaspace-1ebj.onrender.com/api/cube?ra=210.80227&dec=54.34895`

| Parámetro | Tipo | Obligatorio | Default | Descripción |
|---|---|---|---|---|
| `ra`, `dec` | número | ✔ | | |
| `size` | entero | | nativo 512 | Lado mayor del PNG. **Usar el mismo en `/pixel`** |

Respuesta (real):

```json
{
  "tile": {
    "id": "hpx5-2858",
    "obs_id": "hips5_cube-hips_20261026",
    "access_url": "https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/hips5_cube/hips_20261026/Norder5/Dir0/Npix2858_cube.fits",
    "hpx_order": 5,
    "hpx_npix": 2858,
    "projection": "HPX",
    "size_mb": 320.9
  },
  "shape": [102, 512, 512],
  "unit": "MJy/sr",
  "native_size": { "width": 512, "height": 512 },
  "png_size": { "width": 512, "height": 512 },
  "orientation": { "north_deg_from_up": 151.26, "east_deg_from_up": 225.0, "pixel_scale_arcsec": 14.6594,
                   "center": { "ra": 211.875, "dec": 54.34091230386124 } },
  "target": { "ra": 210.8022222222222, "dec": 54.34888888888889, "x": 403.38, "y": 111.279,
              "fits_pixel": { "col": 402.88, "row": 400.221, "i": 403, "j": 400 } },
  "spectrum": [
    { "channel": 1, "wavelength_um": 0.753, "bandwidth_um": 0.018, "brightness": 5.618226051330566,
      "flags_raw": 2097152, "flags": ["source"], "nhits": 5 },
    "…(101 más)"
  ],
  "channels": [
    { "channel": 1, "wavelength_um": 0.753, "lower_um": 0.743, "upper_um": 0.762, "bandwidth_um": 0.018,
      "bandwidth_source": "columna de ancho", "detector": 1, "resolving_power": 42.0,
      "png_url": "/api/cube/hpx5-2858/channel/1.png?cmap=gray_r" },
    "…(101 más)"
  ],
  "six_channels": [ "canales 1, 20, 40, 60, 80, 102 (mismo formato que channels)" ],
  "urls": {
    "channels_all": "/api/cube/hpx5-2858/channels?set=all",
    "channels_six": "/api/cube/hpx5-2858/channels?set=six",
    "samples_png": "/api/cube/hpx5-2858/samples.png?ra=210.8022222222222&dec=54.34888888888889",
    "pixel": "/api/cube/hpx5-2858/pixel?x={x}&y={y}",
    "preview3d": "/api/cube/hpx5-2858/preview3d",
    "cube3d_png": "/api/cube/hpx5-2858/cube3d.png",
    "fits": "/api/cube/hpx5-2858/fits"
  },
  "partial": false,
  "warnings": []
}
```

- `spectrum[]` = espectro de 102 canales **en el objetivo** (gráfico λ vs brillo).
- `six_channels[]` = galería de 6 imágenes. `channels[]` = slider de 102 canales (cada PNG se genera al pedirlo:
  ~0.3 s la primera vez, instantáneo después).
- Proyección HEALPix: **no** tiene el norte arriba.
- ⚠️ En Render gratuito la descarga de 320 MB es pesada: probarla antes de la demo.

### 7.6 Endpoints del cubo (usar las URLs de `urls` / `png_url`)

| Endpoint | URL completa de ejemplo | Respuesta |
|---|---|---|
| Canales | `https://nasaspace-1ebj.onrender.com/api/cube/hpx5-2858/channels?set=six` (`set=all\|six`, `cmap`, `size`) | `{tile_id, set, unit, count, channels:[…]}` |
| Un canal | `https://nasaspace-1ebj.onrender.com/api/cube/hpx5-2858/channel/20.png?cmap=gray_r` (`channel` 1…102; `cmap` = `gray_r` `gray` `inferno` `magma` `viridis` `cividis`; `size`) | PNG (transparente sin datos) |
| Clic | `https://nasaspace-1ebj.onrender.com/api/cube/hpx5-2858/pixel?x=403&y=111` (o `?ra&dec`; `size` igual al PNG) | `{tile_id, x, y, ra, dec, inside, fits_pixel, unit, spectrum:[102], valid_channels}` |
| Figura 6 canales | `https://nasaspace-1ebj.onrender.com/api/cube/hpx5-2858/samples.png?ra=210.80227&dec=54.34895` | PNG |
| Malla 3D (WebGL) | `https://nasaspace-1ebj.onrender.com/api/cube/hpx5-2858/preview3d?n=32` (`n` 8…64) | `{unit, shape:[102,n,n], x, y, wavelength_um, color_range, brightness:[102][n][n]}` |
| Vista 3D estática | `https://nasaspace-1ebj.onrender.com/api/cube/hpx5-2858/cube3d.png` | PNG |
| FITS | `https://nasaspace-1ebj.onrender.com/api/cube/hpx5-2858/fits` | 307 a IRSA |

Respuesta real de `/pixel`:

```json
{
  "tile_id": "hpx5-2858",
  "x": 403.381, "y": 111.299, "size": null,
  "ra": 210.80226999999994, "dec": 54.34895000000004,
  "inside": true,
  "fits_pixel": { "col": 402.881, "row": 400.201, "i": 403, "j": 400 },
  "unit": "MJy/sr",
  "spectrum": [
    { "channel": 1, "wavelength_um": 0.753, "bandwidth_um": 0.018, "brightness": 5.618226051330566,
      "flags_raw": 2097152, "flags": ["source"], "nhits": 5 },
    "…(101 más)"
  ],
  "valid_channels": 100
}
```

---

## 8. Sistema

### `GET /api/status`

URL: `https://nasaspace-1ebj.onrender.com/api/status`

```json
{
  "status": "degraded",
  "version": "0.3.0",
  "data_release": "SPHEREx DR1 (IRSA, colección spherex_dr1)",
  "spherex_mode": "irsa",
  "sources": {
    "spherex":  { "ok": true,  "latency_ms": 288,  "last_error": null },
    "simbad":   { "ok": true,  "latency_ms": 942,  "last_error": null },
    "ned":      { "ok": true,  "latency_ms": 137,  "last_error": null },
    "gaia":     { "ok": false, "latency_ms": null, "last_error": "sin respuesta en 5 s" },
    "wise":     { "ok": true,  "latency_ms": 523,  "last_error": null },
    "horizons": { "ok": true,  "latency_ms": 342,  "last_error": null }
  },
  "jobs": { "running": 0, "queued": 0, "paused_now": 0, "paused_total": 0, "max_concurrent": 4, "max_heavy": 1 }
}
```

- `status: "degraded"` = alguna fuente externa falla (aquí Gaia): el resto funciona. Pintar 🟢/🔴 por fuente.
- Pedir **una vez al cargar y luego cada ≥ 60 s**.
- `GET /healthz` → `{"ok": true}` (para monitoreo).

---

## 9. Dibujar sobre las imágenes (fórmulas)

Las imágenes de `/api/sequence` y `/api/stack` son una proyección tangente con **norte arriba y este a la
izquierda**, centradas en `seq.center`, de lado `seq.fov_arcsec` segundos de arco y `seq.size_px` píxeles.

```ts
type Seq = { center: { ra: number; dec: number }; fov_arcsec: number; size_px: number };

// (ra, dec) → píxel del PNG (x desde la izquierda, y desde arriba)
export function skyToSequencePixel(ra: number, dec: number, seq: Seq) {
  const scale = seq.size_px / seq.fov_arcsec;          // píxeles por segundo de arco
  let dra = ra - seq.center.ra;
  if (dra > 180) dra -= 360;
  if (dra < -180) dra += 360;
  return {
    x: seq.size_px / 2 - dra * Math.cos((seq.center.dec * Math.PI) / 180) * 3600 * scale,
    y: seq.size_px / 2 - (dec - seq.center.dec) * 3600 * scale,
  };
}

// píxel del PNG → (ra, dec)   (inversa, para el clic)
export function sequencePixelToSky(x: number, y: number, seq: Seq) {
  const deg = seq.fov_arcsec / seq.size_px / 3600;
  const dec = seq.center.dec + (seq.size_px / 2 - y) * deg;
  let ra = seq.center.ra - ((x - seq.size_px / 2) * deg) / Math.cos((seq.center.dec * Math.PI) / 180);
  ra = ((ra % 360) + 360) % 360;
  return { ra, dec };
}

// clic en un <img> mostrado a otro tamaño → píxel del PNG original
export function clickToPng(ev: MouseEvent, img: HTMLImageElement) {
  const r = img.getBoundingClientRect();
  return { x: (ev.clientX - r.left) * (img.naturalWidth / r.width), y: (ev.clientY - r.top) * (img.naturalHeight / r.height) };
}

// fecha ISO → MJD UTC (para asteroides)
export const isoToMjd = (iso: string) => Date.parse(iso) / 86400000 + 40587;
```

**Escala a pantalla:** el PNG mide `size_px` (p. ej. 512) pero se muestra a otro tamaño (p. ej. 760 px):

```ts
const k = img.clientWidth / seq.size_px;
const p = skyToSequencePixel(obj.ra, obj.dec, seq);
dibujarPunto(p.x * k, p.y * k);
```

**Errores típicos:**

- Calcular con el FoV del **botón** en vez del `seq.fov_arcsec` de la secuencia mostrada → los puntos quedan
  amontonados en el centro. Recalcular los marcadores **solo cuando llega la secuencia nueva**.
- Usar `frames[i].center` (centro de la exposición completa) en vez de `seq.center`.
- Olvidar `k` (PNG de 512 mostrado más grande).
- En L2 y cubo **no** usar estas fórmulas (no tienen el norte arriba): usar sus endpoints `/pixel`.

---

## 10. Flujos de la aplicación

**A. Buscar un objeto y hacer blink**

```
1. GET  /api/resolve?name=WISE 0855                      → current.ra, current.dec
2. GET  /api/coverage?ra&dec                             → histograma de fechas
3. GET  /api/sequence?ra&dec&fov=540&size=512&limit=24   → 202 → jobs … → frames[].png_url
4. Reproducir los PNG en orden (blink) con image-rendering: pixelated
5. GET  /api/objects?ra&dec&radius=270                   → etiquetas (radius = fov/2)
6. GET  /api/overlay?ra&dec&fov=540&catalog=gaia&at=<date_obs del frame visible>
```

**B. Resta entre épocas:** elegir ids de dos fechas de `seq.frames` → `GET /api/stack?…&op=diff&a=<nuevos>&b=<viejos>` → `png_url`.

**C. Clic en una fuente → espectro:** `GET /api/spectrum?ra&dec&aperture=6` (solo al hacer clic).

**C2. Composición del punto elegido:** botón "Analizar composición" → `GET /api/composition?ra&dec&aperture=6` → panel con `summary`, `ices`, `elements_supported` e `indicators`.

**D. Asteroide:** modo "🎯 Asteroide" → número MPC + clic → `POST /api/asteroids/predict-from-current` → dibujar
`current_observation.pixel`, la línea de `track[]` y `pixel`.

**E. L2:** `GET /api/l2?ra&dec&size=1024` → `<img src={raw_png_url | color_png_url}>` → clic →
`GET pixel_url` con x, y.

**F. Cubo:** `GET /api/cube?ra&dec` → galería `six_channels`, slider con `channels[]`, gráfico `spectrum[]`, clic →
`/pixel`, 3D con `preview3d`.

---

## 11. Reglas para no saturar el backend (obligatorias)

1. **Un solo cliente:** todas las llamadas por `src/lib/skyApi.ts` (caché 5 min, deduplicación, sondeo adaptativo,
   reintento ante 429). Nada de `fetch` sueltos ni bucles propios de sondeo.
2. **Nada en cada render:** solo al buscar, elegir fecha o hacer clic. En React/Preact, `useEffect` con dependencias
   primitivas (`ra`, `dec`, `fov`), nunca objetos recreados.
3. **Zoom y pan en el navegador** (`transform: scale`); una secuencia nueva solo con "Actualizar campo" o al salir
   del área cargada. `debounce(fn, 500)` en sliders y buscador.
4. **Procesos lentos bajo demanda:** `spectrum`/`predict`/asteroides solo al hacer clic; `stack` al activar la
   resta; `l2`/`cube` al abrir esa vista.
5. **Cancelar lo que ya no se mira:** `const nueva = latest()` por panel y pasar `nueva()` como `signal`; ignorar
   errores con `isAbort(e)`. El backend pausa solo lo que nadie consulta.
6. **Límites:** `sequence` con `limit ≤ 24` y `size` 512; `api.status()` cada ≥ 60 s.
7. **Mostrar el progreso** (`onProgress(progress, stage)`) y "En cola (n)" si `queue_position > 0`.
8. **Errores:** mostrar `e.message` (ya en español); 502/504 → botón "Reintentar", sin reintentos en bucle.

**Criterio de aceptación:** usando la app con normalidad, la pestaña Network muestra **menos de 30 peticiones por
minuto** a `/api/*` y ninguna petición idéntica repetida en 5 minutos.

---

## 12. Lista de comprobación

- [ ] `GET https://nasaspace-1ebj.onrender.com/api/status` responde `"spherex_mode": "irsa"`.
- [ ] WISE 0855 → `/api/frames` da `total: 258`.
- [ ] La secuencia se ve nítida (`image-rendering: pixelated`) y los marcadores de `objects`/`overlay` caen
      sobre las fuentes (no amontonados en el centro).
- [ ] El espectro muestra "Calculando… X %" mientras corre y luego los puntos.
- [ ] M101 L2 (`ra 210.80227, dec 54.34895`): clic cerca de `target.x, target.y` ≈ **11–13 MJy/sr** a 2.24 µm.
- [ ] Asteroide 3000 con el ejemplo de §6 → `target: "3000 Leonardo (1981 EG19)"`, `offset_arcsec ≈ 0.35`.
- [ ] Ninguna petición a `/api/jobs/*` más seguida que `poll_after_ms`; `/api/status` como mucho 1 por minuto.
