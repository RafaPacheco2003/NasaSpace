# SPHEREx Sky API — Documentación completa

Versión **0.3.0** · Datos: **SPHEREx DR1** (IRSA, colección `spherex_dr1`)

Este documento explica **qué APIs externas consume el backend**, **qué devuelve cada endpoint y para qué
sirve**, y **cómo probarlo** (Swagger, script bash y curl). Para instalación, Docker y arquitectura interna,
ver el [`README.md`](../README.md) de la raíz.

---

## Índice

1. [Cómo encaja todo](#1-cómo-encaja-todo)
2. [Cómo probar la API](#2-cómo-probar-la-api)
3. [APIs externas que usamos](#3-apis-externas-que-usamos)
4. [Reglas generales del contrato](#4-reglas-generales-del-contrato)
5. [Referencia de endpoints](#5-referencia-de-endpoints)
6. [Flujos típicos del Frontend](#6-flujos-típicos-del-frontend)
7. [Glosario](#7-glosario)

---

## 1. Cómo encaja todo

```
┌──────────────┐      solo GET       ┌────────────────────────┐        ┌──────────────────────────────┐
│   Frontend   │ ──────────────────▶ │  SPHEREx Sky API       │ ─────▶ │ IRSA (NASA)   imágenes SPHEREx│
│  (navegador) │ ◀────────────────── │  FastAPI · Docker      │ ─────▶ │ SIMBAD (CDS)  nombres/estrellas│
└──────────────┘   JSON + PNG        │  localhost:8000        │ ─────▶ │ NED (NASA)    galaxias        │
                                     │                        │ ─────▶ │ Gaia (ESA)    estrellas+pm    │
                                     │  caché en disco 24 h   │ ─────▶ │ WISE (NASA)   infrarrojo      │
                                     └────────────────────────┘ ─────▶ │ JPL Horizons  asteroides      │
                                                                       └──────────────────────────────┘
```

- El **navegador nunca llama a las APIs externas**: solo a nuestra API (así evitamos CORS, unificamos formatos
  y cacheamos).
- Nuestra API **no tiene base de datos propia** de astronomía: todo se consulta en vivo a las fuentes oficiales y
  se guarda 24 h en caché. Cuando sale un data release nuevo, basta con cambiar una variable de entorno.
- Lo que sí hace nuestra API (valor añadido): **recortar, alinear, limpiar y renderizar** las imágenes de
  SPHEREx, **restar épocas**, **medir espectros**, **propagar movimientos propios** y **unificar** los catálogos.

---

## 2. Cómo probar la API

Primero levanta la API: `docker compose up -d --build` (desde la carpeta `NasaSpace/`).

### 2.1 Swagger (interactivo)

| Qué | URL |
|---|---|
| **Swagger UI** (probar cada endpoint desde el navegador) | <http://localhost:8000/docs> |
| **ReDoc** (documentación de lectura) | <http://localhost:8000/redoc> |
| OpenAPI en vivo (JSON) | <http://localhost:8000/openapi.json> |
| OpenAPI exportado (JSON / YAML) | [`docs/openapi.json`](openapi.json) · [`docs/openapi.yaml`](openapi.yaml) |

En Swagger UI: abre un endpoint → **Try it out** → rellena los parámetros (ya vienen ejemplos) → **Execute**.
Cada endpoint muestra **qué API externa usa**, el **esquema de la respuesta** campo por campo y un **ejemplo
real** de SPHEREx DR1.

Los archivos `openapi.json` / `openapi.yaml` se pueden importar en **Postman**, **Insomnia**,
<https://editor.swagger.io> o en generadores de cliente (`openapi-typescript`, `orval`…) para el Frontend.

Para regenerarlos tras cambiar la API:

```bash
curl -s localhost:8000/openapi.json | jq . > docs/openapi.json
docker compose exec -T api python -c "import json,sys,yaml; print(yaml.safe_dump(json.load(sys.stdin), allow_unicode=True, sort_keys=False))" < docs/openapi.json > docs/openapi.yaml
```

### 2.2 Script bash (prueba todo de una vez)

```bash
./scripts/probar_api.sh                 # todo (≈ 2 min la primera vez; luego segundos por la caché)
./scripts/probar_api.sh --rapido        # solo endpoints rápidos (sin sequence/stack/spectrum/predict)
./scripts/probar_api.sh --rate-limit    # añade la prueba del 429 al final
BASE_URL=https://mi-servidor.com ./scripts/probar_api.sh   # contra otro servidor
```

Requiere `curl` y `jq` (macOS ya los trae). Ejecuta **49 comprobaciones** con los 5 objetivos del reto, muestra
qué devolvió cada endpoint, sondea los trabajos con barra de progreso y guarda los PNG en `./salida_pruebas/`.
Sale con código 0 si todo pasa (sirve para CI).

Salida real (resumida):

```
━━ Catálogos — /api/resolve  (SIMBAD → NED)
  ✔ Barnard's star → Barnard's star
    catálogo J2000.0: 269.4520769, 4.6933649 · pm -801.551,10362.394 mas/año · current(2026.413): 269.44622695, 4.76946318
  ✔ WISE 0855 → WISEA J085510.74-071442.5 (BD*)
  ✔ RW Aur → V* RW Aur [simbad]
  ✔ R Dra → V* R Dra [simbad]
  ✔ epsilon indi b → * eps Ind B [simbad]
  ✔ Nombre inexistente → 404 not_found
━━ Imágenes — /api/frames y /api/coverage  (IRSA SIA v2, SPHEREx DR1)
  ✔ Frames del detector 6 en WISE 0855 → total 45
  ✔ Cobertura → 258 imágenes de 2025-05-05 a 2026-05-14 en 9 semanas
━━ Imágenes — /api/sequence  (202 + trabajo · recortes FITS de IRSA)
  ✔ Secuencia → 6 de 45 frames, 256px, vmin/vmax -0.0804/0.44773 MJy/sr
━━ Análisis — /api/spectrum  (fotometría en cada exposición)
  ✔ Espectro de WISE 0855 (nov 2025) → 84 puntos de 86 exposiciones
      0.7451 µm  755 ± 43 µJy  D1  2025-11-03
      4.8715 µm  373 ± 109 µJy  D6  2025-11-14
━━ Análisis — /api/predict  (SIMBAD pm+paralaje · JPL Horizons)
  ✔ Barnard → method=proper_motion, 57 posiciones
  ✔ Ceres → method=ephemeris, 28 posiciones
Resultado: 32 correctas, 0 fallidas
```

### 2.3 curl a mano

```bash
B=http://localhost:8000/api
curl -s "$B/status" | jq
curl -s "$B/resolve?name=WISE%200855" | jq
curl -s "$B/frames?ra=133.757901&dec=-7.242058&detector=6&limit=3" | jq
```

Proceso lento (202 + trabajo) a mano:

```bash
curl -s "$B/sequence?ra=133.757901&dec=-7.242058&fov=300&size=256&detector=6&limit=6" | jq '{id,status}'
# → {"id": "seq_b88275e26582", "status": "queued"}
curl -s "$B/jobs/seq_b88275e26582" | jq '{status, progress, stage, eta_s}'   # repetir hasta status=done
curl -s "$B/jobs/seq_b88275e26582" | jq '.result.frames[0].png_url'
curl -s -o frame.png "http://localhost:8000$(curl -s "$B/jobs/seq_b88275e26582" | jq -r '.result.frames[0].png_url')"
```

### 2.4 Tests automáticos (sin red)

```bash
docker compose run --rm api pytest -q     # 10 tests con datos sintéticos (modo mock)
```

---

## 3. APIs externas que usamos

Todas son **públicas, gratuitas y sin clave**. Las URLs se configuran en `backend/app/config.py` (variables de
entorno). Para todas: **timeout 10 s + 1 reintento**, caché 24 h, y su estado aparece en `/api/status`.

### 3.1 IRSA — imágenes de SPHEREx  ⭐ (la principal)

| | |
|---|---|
| **Qué es** | NASA/IPAC Infrared Science Archive (Caltech): el archivo oficial donde la NASA publica los datos de SPHEREx |
| **Qué nos da** | Las **imágenes reales de SPHEREx** (FITS): lista de exposiciones, píxeles en MJy/sr, máscara de calidad, varianza, luz zodiacal y la longitud de onda de cada píxel |
| **Servicios** | **SIA v2** (búsqueda): `https://irsa.ipac.caltech.edu/SIA` · **Cutouts** (recortes): `<access_url del FITS>?center=ra,dec&size=grados` |
| **Usado en** | `/api/frames`, `/api/coverage`, `/api/sequence`, `/api/frame/{id}.png`, `/api/stack`, `/api/spectrum`, `/api/predict` (fechas) |
| **Variables** | `IRSA_SIA_URL`, `SPHEREX_COLLECTION=spherex_dr1`, `SPHEREX_SEARCH_TIMEOUT_S=20`, `DOWNLOAD_TIMEOUT_S=60` |

**Paso 1 — Buscar exposiciones** que contienen el punto:

```
GET https://irsa.ipac.caltech.edu/SIA?COLLECTION=spherex_dr1&POS=CIRCLE 133.7579 -7.2420 0.000278
```

Devuelve una tabla VOTable (una fila por exposición y detector). Columnas que usamos:

| Columna IRSA | Ejemplo real | Lo usamos para |
|---|---|---|
| `access_url` | `https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/2025W19_1B/…/level2_2025W19_1B_0116_1D6_spx_l2b-v27-2026-197.fits` | Descargar el recorte |
| `t_min` | `60800.700028` (MJD) | `date_obs` y el `id` del frame |
| `t_exptime` | `113.5826` | `exposure_s` |
| `em_min` / `em_max` | `4.41e-06` / `5.01e-06` (m) | Saber el detector (1–6) y `band_um` aproximado |
| `energy_bandpassname` | `SPHEREx-D6` | Respaldo para el detector |
| `obs_id` | `2025W19_1B_0116_1` | `obs_id` |
| `s_ra` / `s_dec` | `134.41` / `-9.02` | `center` de la exposición |
| `dataproduct_type` / `obs_collection` | `image` / `spherex_dr1` | Descartar los cubos (`spherex_dr1_cube`) |

> **Si el buscador de IRSA cae** (502, frecuente): se reintenta 2 veces con espera creciente y, si sigue
> caído, se usa el **índice local de huellas**: las exposiciones ya conocidas cuya huella (`s_region`) contiene
> el punto. La respuesta lleva `partial: true` y el aviso
> `IRSA no responde; se usan N exposiciones del índice local…`. Solo se devuelve 502 si no hay ninguna
> exposición conocida cerca. El servicio de recortes es otro servidor de IRSA y suele seguir funcionando.

**Paso 2 — Descargar un recorte** del FITS alrededor del objetivo:

```
GET <access_url>?center=133.7579,-7.2420&size=0.125&gzip=false
```

Devuelve un FITS multi-extensión ya recortado. Extensiones:

| Extensión | Contenido | Lo usamos para |
|---|---|---|
| `IMAGE` | Brillo en **MJy/sr** + WCS (con distorsión SIP) | La imagen |
| `FLAGS` | Máscara de bits por píxel + definición de bits en la cabecera (`MSKN0000 = 'TRANSIENT'`…) | `flags`, capa de flags, limpieza de píxeles |
| `VARIANCE` | Varianza por píxel | Error del espectro |
| `ZODI` | Modelo de luz zodiacal | Se resta para que el fondo no "parpadee" |
| `WCS-WAVE` | Tabla de longitud de onda por posición en el detector | `band_um` / `wavelength` **exactos** |
| `EPSF` | Modelo de PSF (~3.8 MB) | No se usa: se descarta antes de cachear |

> SPHEREx usa filtros lineales variables: **la longitud de onda depende de la posición en el detector**. Por eso
> cada exposición aporta un punto distinto del espectro, y por eso leemos `WCS-WAVE` en el píxel del objetivo.

### 3.2 SIMBAD — nombres, estrellas y movimiento propio

| | |
|---|---|
| **Qué es** | Base de datos astronómica de referencia del CDS (Estrasburgo, Francia): ~18 millones de objetos fuera del Sistema Solar |
| **Qué nos da** | Traducir **nombre → coordenadas**, tipo de objeto, **movimiento propio**, **paralaje** y los objetos conocidos en una zona |
| **Servicios** | **Sesame** (resolver nombres): `https://cds.unistra.fr/cgi-bin/nph-sesame/-oxp/S?<nombre>` · **TAP/ADQL**: `https://simbad.cds.unistra.fr/simbad/sim-tap/sync` · **Espejo** (Harvard/CfA): `https://simbad.harvard.edu/simbad/sim-tap/sync` |
| **Usado en** | `/api/resolve`, `/api/objects`, `/api/predict` |
| **Variables** | `SESAME_URL`, `SIMBAD_TAP_URL`, `SIMBAD_TAP_MIRROR` |

Sesame tolera variantes de escritura (`Barnard's star`, `barnard star`…). Devuelve XML; tomamos:
`oname` (nombre), `jradeg`/`jdedeg` (RA/Dec J2000), `pm/pmRA`, `pm/pmDE` (mas/año), `plx/v` (paralaje, mas),
`otype` (tipo, p. ej. `BD*` = enana marrón), y los errores de posición y de pm.

Búsqueda de cono (ADQL):

```sql
SELECT TOP 2000 main_id, ra, dec, otype, rvz_redshift, pmra, pmdec, plx_value, coo_err_maj, pm_err_maj
FROM basic
WHERE CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', 133.7579, -7.2420, 0.0333)) = 1
```

Si el servidor principal falla, **el reintento va al espejo de Harvard** (durante el desarrollo, el principal
estuvo en mantenimiento).

### 3.3 NED — galaxias y cuásares

| | |
|---|---|
| **Qué es** | NASA/IPAC Extragalactic Database: catálogo de objetos fuera de la Vía Láctea |
| **Qué nos da** | **Galaxias, cuásares y fuentes extragalácticas fijas** (para descartarlas como falsos positivos) y su corrimiento al rojo `z`; segunda opción para resolver nombres |
| **Servicios** | **ObjectLookup**: `https://ned.ipac.caltech.edu/srs/ObjectLookup` (POST `json={"name":{"v":"M31"}}`) · **TAP**: `https://ned.ipac.caltech.edu/tap/sync` |
| **Usado en** | `/api/resolve` (si SIMBAD no lo encuentra), `/api/objects` |
| **Variables** | `NED_LOOKUP_URL`, `NED_TAP_URL` |

```sql
SELECT TOP 2000 prefname, ra, dec, prefphytype, z FROM NEDTAP.objdir
WHERE CONTAINS(POINT('J2000', ra, dec), CIRCLE('J2000', 133.7579, -7.2420, 0.0333)) = 1
```

`prefphytype` (tipo: `G` galaxia, `QSO` cuásar, `*` estrella…) se convierte en `type` = `galaxy`/`star`/`other`.

### 3.4 Gaia DR3 — estrellas con movimiento propio

| | |
|---|---|
| **Qué es** | Tercer catálogo de la misión Gaia de la ESA: ~1 800 millones de estrellas con posición, **movimiento propio** y paralaje de altísima precisión |
| **Qué nos da** | Las estrellas del campo para **dibujarlas sobre la imagen** con su vector de movimiento |
| **Servicio** | TAP de ESA: `https://gea.esac.esa.int/tap-server/tap/sync` |
| **Usado en** | `/api/overlay?catalog=gaia` |
| **Variable** | `GAIA_TAP_URL` |

```sql
SELECT TOP 5000 source_id, ra, dec, pmra, pmdec, parallax, phot_g_mean_mag
FROM gaiadr3.gaia_source
WHERE 1 = CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', 269.4462, 4.7695, 0.0601))
ORDER BY phot_g_mean_mag ASC
```

Las posiciones de Gaia son de la **época 2016.0**; las propagamos a la fecha de la imagen (`at`).

### 3.5 WISE (AllWISE) — infrarrojo de referencia

| | |
|---|---|
| **Qué es** | Catálogo del telescopio espacial infrarrojo WISE de la NASA (3.4–22 µm), el que descubrió WISE 0855 |
| **Qué nos da** | Fuentes infrarrojas del campo (magnitud W1 = 3.4 µm) para comparar con SPHEREx |
| **Servicios** | TAP de IRSA: `https://irsa.ipac.caltech.edu/TAP/sync` (tabla `allwise_p3as_psd`) · **Respaldo Gator**: `https://irsa.ipac.caltech.edu/cgi-bin/Gator/nph-query` |
| **Usado en** | `/api/overlay?catalog=wise` |
| **Variables** | `IRSA_TAP_URL`, `IRSA_GATOR_URL`, `WISE_CATALOG=allwise` (o `catwise`) |

Posiciones de AllWISE en época **2010.56** (CatWISE2020: 2015.4). Si el TAP de IRSA falla, el reintento va a Gator.

### 3.6 JPL Horizons — asteroides y cometas

| | |
|---|---|
| **Qué es** | Servicio de efemérides del Jet Propulsion Laboratory de la NASA: posición de cualquier cuerpo del Sistema Solar en cualquier fecha |
| **Qué nos da** | Dónde estaba un **asteroide o cometa** en cada fecha con imágenes (+ su incertidumbre) |
| **Servicio** | `https://ssd.jpl.nasa.gov/api/horizons.api` |
| **Usado en** | `/api/predict` cuando el nombre no está en SIMBAD con movimiento propio |
| **Variable** | `HORIZONS_URL` |

Consulta: observador geocéntrico (`CENTER=500@399`, SPHEREx está en órbita baja), lista de fechas (`TLIST`),
cantidades `1` (RA/Dec astrométricas) y `36` (incertidumbre 3σ).

### 3.7 Resumen: qué API externa alimenta cada endpoint

| Endpoint | IRSA | SIMBAD | NED | Gaia | WISE | Horizons |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| `/api/resolve` | | ✔ | ✔ (si no está en SIMBAD) | | | |
| `/api/objects` | | ✔ | ✔ | | | |
| `/api/frames` | ✔ | | | | | |
| `/api/coverage` | ✔ | | | | | |
| `/api/sequence` | ✔ | | | | | |
| `/api/frame/{id}.png` | ✔ (si no está en caché) | | | | | |
| `/api/stack` | ✔ | | | | | |
| `/api/stack/{id}.png` | (caché) | | | | | |
| `/api/spectrum` | ✔ | | | | | |
| `/api/predict` | ✔ (fechas) | ✔ | | | | ✔ |
| `/api/overlay` | | | | ✔ (gaia) | ✔ (wise) | |
| `/api/jobs/{id}` | | | | | | |
| `POST /api/asteroids/predict-from-current` | (solo si `image_id` no está en caché) | | | | | ✔ (desde SPHEREx) |
| `/api/status` | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |

---

## 4. Reglas generales del contrato

- **Solo GET**, sin autenticación, **CORS abierto** (`Access-Control-Allow-Origin: *`).
- **Coordenadas** en grados decimales (ICRS/J2000). **Radio, fov, apertura** en segundos de arco (″).
- **Listas** en query: `?a=x&a=y` o `?a=x,y`.
- **Caché**: por (consulta, fuente), coordenadas redondeadas a 1″, 24 h. Campo `cache: "hit" | "miss"`.
- **Fuentes parciales**: si falla solo una de las pedidas → `200` con `partial: true` y el motivo en `warnings`.
- **Procesos lentos** (`sequence`, `stack`, `spectrum`, `predict`): `200` si está en caché; si no `202` + Job.
- **Errores** (todos con la misma forma):

```json
{"error": {"code": "bad_request", "message": "radius supera el máximo (600)"}}
```

| HTTP | `code` | Cuándo |
|---|---|---|
| 400 | `bad_request` | Falta un parámetro, valor fuera de rango, opción inválida |
| 404 | `not_found` | Nombre, frame, trabajo o apilado no encontrado / expirado |
| 429 | `rate_limited` | Más de 120 peticiones/min desde la IP (cabecera `Retry-After`) |
| 502 | `upstream_error` | Fallaron todas las APIs externas pedidas |
| 504 | `upstream_timeout` | Todas las APIs externas pedidas tardaron más de 10 s |

**Límites**: `ra` ∈ [0, 360) · `dec` ∈ [−90, 90] · `radius` ≤ 600″ · `fov` ∈ [10, 3600]″ · `size` ∈ [64, 1024] px ·
`aperture` ∈ [1, 60]″ · `a`/`b` ≤ 100 ids · `limit` de sequence ≤ 200, de frames ≤ 1000, de objects ≤ 2000.

---

## 5. Referencia de endpoints

Ejemplos con datos **reales** (SPHEREx DR1). Coordenadas de prueba: **WISE 0855** `133.757901, -7.242058` ·
**Barnard** `269.446227, 4.769463`.

### 5.1 `GET /api/resolve` — nombre → coordenadas

**Para qué sirve:** el buscador. El usuario escribe un nombre y obtenemos dónde está **hoy** en el cielo
(o en la fecha `at`). **API externa:** SIMBAD (Sesame), luego NED.

| Parámetro | Tipo | Oblig. | Descripción |
|---|---|:-:|---|
| `name` | string | ✔ | Nombre del objeto (`Barnard's star`, `WISE 0855`, `RW Aur`, `M31`…) |
| `at` | fecha | | Fecha a la que propagar la posición. Sin valor = hoy |

```bash
curl "http://localhost:8000/api/resolve?name=WISE%200855"
```

```json
{
  "query": "WISE 0855",
  "matched_name": "WISEA J085510.74-071442.5",
  "ra": 133.81890825, "dec": -7.247048,
  "source": "simbad",
  "epoch": 2000.0,
  "pm": {"ra": -8123.7, "dec": 673.2},
  "parallax_mas": 439.0,
  "current": {"ra": 133.75790575, "dec": -7.24205776, "epoch": 2026.868},
  "object_type": "BD*"
}
```

| Campo | Significado | Uso en el Frontend |
|---|---|---|
| `matched_name` | Nombre oficial en el catálogo | Título de la ficha |
| `ra`, `dec`, `epoch` | Posición **del catálogo** en su época (J2000) | Informativo |
| `source` | `simbad` o `ned` | Mostrar la fuente |
| `pm` | Movimiento propio (mas/año); `null` si no se conoce | Mostrar velocidad, dibujar vector |
| `parallax_mas` | Paralaje (mas) → distancia = 1000/paralaje pársecs | Ficha (WISE 0855: 2.3 pc) |
| `current` | Posición **propagada a hoy/`at`** (pm + paralaje) | **Centrar la vista aquí** |
| `object_type` | (extra) Tipo SIMBAD (`BD*` enana marrón, `V*` variable…) | Ficha |

> WISE 0855 se movió **~3.5′** desde la posición J2000: si centras en `ra/dec` en lugar de `current`, no la verás.

Errores: `404` si no está en SIMBAD ni NED (`No encontramos "X" en SIMBAD ni en NED.`) · `502/504` si ambos caídos.

---

### 5.2 `GET /api/objects` — objetos conocidos en una zona

**Para qué sirve:** marcar sobre la imagen qué fuentes **ya son conocidas**: estrellas (SIMBAD) y galaxias de
fondo (NED). Una fuente que se mueve y **no aparece aquí es candidata a descubrimiento**.
**API externa:** SIMBAD TAP + NED TAP.

| Parámetro | Tipo | Defecto | Descripción |
|---|---|---|---|
| `ra`, `dec` | número | — | Centro (grados) |
| `radius` | número | 60 | Radio en ″ (máx. 600) |
| `catalogs` | lista | `simbad,ned` | Qué catálogos consultar |
| `types` | lista | `star,galaxy,other` | Qué tipos devolver |
| `limit` | entero | 200 | Máximo de objetos |

```bash
curl "http://localhost:8000/api/objects?ra=133.757901&dec=-7.242058&radius=120&limit=2"
```

```json
{
  "center": {"ra": 133.757778, "dec": -7.241944},
  "radius_arcsec": 120.0,
  "counts": {"simbad": 1, "ned": 55},
  "objects": [
    {"id": "ned:WISEA J085501.95-071428.1", "name": "WISEA J085501.95-071428.1", "source": "ned",
     "ra": 133.758195, "dec": -7.241165, "type": "other", "type_raw": "", "z": null,
     "distance_arcsec": 3.177, "pm": null}
  ],
  "partial": false, "warnings": [], "cache": "hit"
}
```

| Campo | Significado |
|---|---|
| `counts` | Cuántos objetos encontró cada catálogo |
| `objects[].type` | `star` (anillo cian en el visor), `galaxy` (marcador tenue: ignorar), `other` |
| `objects[].type_raw` | Tipo original (`BD*`, `G`, `QSO`…) |
| `objects[].z` | Corrimiento al rojo (galaxias) |
| `objects[].distance_arcsec` | Distancia al centro; la lista viene ordenada por esto |
| `objects[].pm` | Movimiento propio (solo SIMBAD) |
| `partial` / `warnings` | `true` si uno de los dos catálogos falló |

> Las posiciones son las **del catálogo** (sin propagar). Para dibujar sobre la imagen de una fecha concreta usa
> `/api/overlay`, que sí propaga.

---

### 5.3 `GET /api/frames` — qué imágenes hay (rápido)

**Para qué sirve:** listar las exposiciones de SPHEREx de ese punto **sin descargarlas**: para la línea de
tiempo, para elegir épocas y para obtener los `id` que pide `/api/stack`. **API externa:** IRSA SIA v2.

| Parámetro | Tipo | Defecto | Descripción |
|---|---|---|---|
| `ra`, `dec` | número | — | Punto (grados) |
| `fov` | número | 300 | Campo de visión (″), informativo |
| `detector` | 1–6 | todos | 1: 0.75–1.09 µm · 2: 1.10–1.62 · 3: 1.63–2.41 · 4: 2.42–3.82 · 5: 3.83–4.41 · 6: 4.42–5.00 |
| `date_from`, `date_to` | fecha | | Rango de fechas (inclusive) |
| `limit`, `offset` | entero | 200, 0 | Paginación |

```bash
curl "http://localhost:8000/api/frames?ra=133.757901&dec=-7.242058&detector=6&limit=1"
```

```json
{
  "center": {"ra": 133.757778, "dec": -7.241944}, "fov_arcsec": 300.0,
  "total": 45, "limit": 1, "offset": 0,
  "frames": [{
    "id": "sx-d6-1746463682419",
    "date_obs": "2025-05-05T16:48:02.419Z",
    "detector": 6,
    "band_um": 4.9674, "band_exact": true,
    "center": {"ra": 134.40997823, "dec": -9.02337658},
    "fov_arcsec": 12648.0,
    "exposure_s": 113.5826,
    "flags": [],
    "obs_id": "2025W19_1B_0116_1"
  }],
  "partial": false, "warnings": [], "cache": "hit"
}
```

| Campo | Significado |
|---|---|
| `total` | Total de exposiciones (todas, no solo esta página) |
| `frames[].id` | Identificador del frame: `sx-d<detector>-<milisegundos Unix>` |
| `frames[].date_obs` | Fecha/hora de la exposición (UTC) |
| `frames[].band_um` | Longitud de onda en ese punto (µm). Aproximada (`band_exact: false`) hasta que el frame se procesa |
| `frames[].center`, `fov_arcsec` | Centro y tamaño (~3.5°) de la **exposición completa** |
| `frames[].flags` | Banderas de calidad en el objetivo (se llenan al procesar el frame) |

---

### 5.4 `GET /api/coverage` — histograma de fechas

**Para qué sirve:** el histograma del selector de fechas (cuántas imágenes hay cada semana). SPHEREx observa
cada punto en **tandas cada ~6 meses**. **API externa:** IRSA SIA v2.

```bash
curl "http://localhost:8000/api/coverage?ra=133.757901&dec=-7.242058"
```

```json
{
  "center": {"ra": 133.757778, "dec": -7.241944},
  "first_date": "2025-05-05", "last_date": "2026-05-14",
  "weeks": [
    {"week_start": "2025-05-05", "count": 30},
    {"week_start": "2025-10-27", "count": 24},
    {"week_start": "2025-11-03", "count": 48}
  ],
  "total": 258, "cache": "hit"
}
```

Parámetros: `ra`, `dec`, `detector` (opcional). `week_start` es el lunes de cada semana.

---

### 5.5 `GET /api/sequence` — la secuencia para el blink (lento → 202)

**Para qué sirve:** el **"video"**. Descarga los recortes, los **alinea píxel a píxel**, resta la luz zodiacal,
limpia rayos cósmicos y prepara los PNG. Al reproducirlos en orden, lo que se mueve "salta".
**API externa:** IRSA (SIA + cutouts).

| Parámetro | Tipo | Defecto | Descripción |
|---|---|---|---|
| `ra`, `dec` | número | — | Centro (usar `current` de resolve) |
| `fov` | número | 300 | Lado del cuadro (″). Mayor = más datos y más tiempo |
| `size` | entero | 512 | Lado del PNG (px) |
| `detector` | 1–6 | todos | Detector |
| `date_from`, `date_to` | fecha | | Rango |
| `stretch` | `linear`/`log`/`sqrt`/`asinh` | `asinh` | Curva de brillo |
| `cmap` | `gray`/`viridis`/`magma`/`inferno` | `gray` | Colores |
| `scale` | `sequence`/`frame` | `sequence` | `sequence`: mismo brillo en todos (cambios reales, para variables) · `frame`: cada uno ajustado |
| `limit`, `offset` | entero | 60, 0 | Paginación de frames |

Primera llamada → **202**:

```json
{"id": "seq_03900b21fbf0", "kind": "sequence", "status": "queued", "progress": 0.0, "eta_s": 45,
 "stage": "En cola", "queue_position": 1, "poll_after_ms": 1500, "created_at": "2026-11-14T17:34:13Z",
 "result": null, "error": null}
```

Cuando termina (en `/api/jobs/{id}` → `result`, o 200 directo si ya está en caché):

```json
{
  "center": {"ra": 133.757778, "dec": -7.241944}, "fov_arcsec": 300.0, "size_px": 256,
  "scale": "sequence", "vmin": -0.1146, "vmax": 0.7778, "unit": "MJy/sr",
  "total": 45, "limit": 6, "offset": 0,
  "frames": [{
    "id": "sx-d6-1746463682419", "date_obs": "2025-05-05T16:48:02.419Z", "detector": 6,
    "band_um": 4.9674, "band_exact": true, "flags": [], "exposure_s": 113.5826,
    "center": {"ra": 134.40997823, "dec": -9.02337658}, "fov_arcsec": 12648.0,
    "vmin": -0.1146, "vmax": 0.7778,
    "png_url": "/api/frame/sx-d6-1746463682419.png?ra=133.757778&dec=-7.241944&fov=300&size=256&stretch=asinh&cmap=gray&vmin=-0.114633&vmax=0.777781",
    "flags_png_url": "/api/frame/sx-d6-1746463682419/flags.png?ra=133.757778&dec=-7.241944&fov=300&size=256"
  }],
  "partial": false, "warnings": []
}
```

| Campo | Significado |
|---|---|
| `center`, `fov_arcsec`, `size_px` | Geometría **común a todos los PNG** (ver fórmula abajo) |
| `vmin`, `vmax`, `unit` | Escala de brillo compartida (MJy/sr) |
| `total` | Frames que hay en total (para paginar con `offset`) |
| `frames[].png_url` | **URL del PNG lista para usar** (no construirla) |
| `frames[].flags_png_url` | (extra) Capa transparente con los píxeles con banderas en ámbar |
| `frames[].band_um` | λ exacta medida en el FITS |

**Geometría** (todos los PNG): TAN, norte arriba, este a la izquierda, centrado en `center`:

```
escala = size / fov                                   (px por ″)
x = size/2 − (ra − ra0) · cos(dec0) · 3600 · escala   (desde la izquierda)
y = size/2 − (dec − dec0) · 3600 · escala             (desde arriba)
```

---

### 5.6 `GET /api/frame/{id}.png` — imagen de un frame

**Para qué sirve:** la imagen PNG de un frame. **Usar las `png_url` de la secuencia tal cual.** Se pueden
cambiar `stretch`, `cmap`, `vmin`, `vmax` en la URL para re-renderizar al instante (el recorte ya está en caché).
**API externa:** IRSA, solo si el recorte no está en caché (hasta ~10 s).

Parámetros: `ra`, `dec`, `fov`, `size`, `stretch`, `cmap`, `vmin`, `vmax` (MJy/sr; sin valor = automático).
Devuelve `image/png` de `size × size`; transparente donde el detector no cubre.

**Extra:** `GET /api/frame/{id}/flags.png?ra&dec&fov&size` → misma geometría, píxeles con banderas en ámbar
(para el checkbox "Banderas de contaminación").

---

### 5.7 `GET /api/stack` — apilar o restar épocas (lento → 202)

**Para qué sirve:**
- `mean` / `median` / `sum`: **sumar** varias imágenes para ver objetos débiles (más señal/ruido).
- `diff`: **resta T2 − T1**. Las estrellas fijas se anulan; lo que se movió aparece como pareja
  **cian (posición vieja) + ámbar (posición nueva)**; lo que cambió de brillo, en un solo color.

**API externa:** IRSA.

| Parámetro | Tipo | Descripción |
|---|---|---|
| `ra`, `dec`, `fov`, `size`, `stretch`, `cmap` | | Igual que sequence (`cmap` se ignora en diff) |
| `op` | `mean`/`median`/`sum`/`diff` | Operación (obligatorio) |
| `a` | lista de ids | Frames a combinar (o época nueva en diff). Máx. 100 |
| `b` | lista de ids | Época vieja (obligatorio en diff). Máx. 100 |

```bash
curl "http://localhost:8000/api/stack?ra=269.446227&dec=4.769463&fov=120&size=256&op=diff&a=sx-d2-...,sx-d2-...&b=sx-d2-..."
```

Resultado:

```json
{"op": "diff", "n_frames": 6, "center": {"ra": 269.446111, "dec": 4.770556}, "fov_arcsec": 120.0,
 "size_px": 256, "vmin": -3.2164, "vmax": 3.2164, "unit": "MJy/sr",
 "png_url": "/api/stack/stk_cc6f162d39c8.png", "partial": false, "warnings": []}
```

| Campo | Significado |
|---|---|
| `n_frames` | Frames usados de verdad |
| `vmin`, `vmax` | En `diff` es simétrico (±) alrededor de 0 |
| `png_url` | Imagen resultado → `GET /api/stack/{id}.png` |

> Probado con Barnard (agosto 2025 vs marzo 2026): dipolo **ámbar al norte / cian al sur** = la estrella se
> movió hacia el norte, como predice su movimiento propio.

### 5.8 `GET /api/stack/{id}.png`

Imagen del apilado/resta (la `png_url` anterior). Sin API externa (sale de la caché). `404` si expiró.

---

### 5.9 `GET /api/jobs/{id}` — estado de un proceso lento

**Para qué sirve:** la tarjeta de progreso. Consultar cada `poll_after_ms` hasta `done` o `failed`.

```json
{"id": "seq_03900b21fbf0", "kind": "sequence", "status": "running", "progress": 0.485, "eta_s": 9,
 "stage": "Descargando frames 3/6", "queue_position": 0, "poll_after_ms": 800,
 "created_at": "2026-11-14T17:34:13Z", "result": null, "error": null}
```

| Campo | Significado |
|---|---|
| `id` | Determinista: la misma consulta siempre da el mismo id |
| `kind` | `sequence`, `stack`, `spectrum`, `predict` |
| `status` | `queued` → `running` → `done` / `failed` |
| `progress` | 0–1 (para la barra) |
| `eta_s` | Segundos estimados restantes |
| `stage` | Texto para mostrar (`Descargando frames 18/43`, `Fotometría 120/239`…) |
| `queue_position` | 0 = ejecutándose; ≥ 1 = esperando turno. Un trabajo que nadie consulta en 30 s (en cola) / 90 s (corriendo) se pausa y se reanuda solo al volver a consultarlo |
| `poll_after_ms` | Cuánto esperar antes de volver a preguntar |
| `result` | Con `done`: el resultado completo (el mismo que daría el 200) |
| `error` | Con `failed`: `{code, message}` |

---

### 5.10 `GET /api/spectrum` — espectro y curva de luz (lento → 202)

**Para qué sirve:** el **gráfico espectral** (0.75–5 µm) de la fuente clicada, para identificar moléculas
(H₂O, CH₄, CO₂, CO). Cada exposición aporta un punto. Como cada punto trae su fecha, **ordenados por
`date_obs` forman una curva de luz** (estrellas variables como RW Aur y R Dra).
**API externa:** IRSA (un recorte pequeño por exposición).

| Parámetro | Tipo | Defecto | Descripción |
|---|---|---|---|
| `ra`, `dec` | número | — | Punto a medir |
| `aperture` | número | 6 | Radio de la apertura (″), 1–60 |
| `date_from`, `date_to` | fecha | | Acotar (útil para objetos que se mueven) |

```bash
curl "http://localhost:8000/api/spectrum?ra=133.757901&dec=-7.242058&aperture=6&date_from=2025-11-01&date_to=2025-11-30"
```

```json
{
  "center": {"ra": 133.757778, "dec": -7.241944}, "aperture_arcsec": 6.0,
  "wavelength_unit": "um", "flux_unit": "uJy",
  "points": [
    {"wavelength": 0.7451, "flux": 755.58, "error": 43.79, "date_obs": "2025-11-03T…Z",
     "detector": 1, "frame_id": "sx-d1-…", "flags": [], "wavelength_exact": true},
    {"wavelength": 4.8715, "flux": 373.1, "error": 109.2, "date_obs": "2025-11-14T…Z",
     "detector": 6, "frame_id": "sx-d6-…", "flags": [], "wavelength_exact": true}
  ],
  "n_exposures": 86, "partial": false, "warnings": [], "cache": "miss"
}
```

| Campo | Significado |
|---|---|
| `points[].wavelength` | Longitud de onda (µm), exacta (de `WCS-WAVE`) |
| `points[].flux` / `error` | Flujo en **microjanskys** (fondo restado) y su error |
| `points[].date_obs` | Fecha → curva de luz |
| `points[].flags` | Banderas en la apertura: si trae `saturation`, `cosmic_ray`… **desconfiar del punto** |
| `n_exposures` | Exposiciones intentadas |
| `partial` / `warnings` | Exposiciones que IRSA no pudo servir |

Bandas moleculares para sombrear: **H₂O** ~1.4, 1.9, 2.7–3.0 µm · **CH₄** ~1.65, 2.2, 3.3 µm ·
**CO₂** ~4.27 µm · **CO** ~4.6–4.7 µm.

Tiempo: ~1 s por exposición la primera vez (WISE 0855 completo: ~240 exposiciones ≈ 3–4 min; con un mes ≈ 1 min).

---

### 5.11 `GET /api/predict` — dónde estará el objeto (lento → 202)

**Para qué sirve:** dibujar en cada frame el **círculo donde debería estar** un objeto que se mueve, para
confirmarlo. **APIs externas:** SIMBAD (pm + paralaje), JPL Horizons (asteroides/cometas), IRSA (fechas).

| Parámetro | Tipo | Oblig. | Descripción |
|---|---|:-:|---|
| `ra`, `dec` | número | ✔ | Campo |
| `name` | string | | Objeto. Sin valor = el objeto con movimiento conocido más cercano (5′) |
| `date_from`, `date_to` | fecha | ✔ | Rango |

```bash
curl "http://localhost:8000/api/predict?ra=269.446227&dec=4.769463&name=Barnard%27s%20star&date_from=2025-05-01&date_to=2026-06-01"
```

```json
{
  "target": "Barnard's star", "method": "proper_motion",
  "pm_mas_yr": {"ra": -801.551, "dec": 10362.394}, "parallax_mas": 546.9759, "catalog_epoch": 2000.0,
  "positions": [
    {"date": "2025-08-15T20:45:11.376Z", "ra": 269.446229, "dec": 4.7671599, "sigma_arcsec": 0.001},
    {"date": "2026-05-05T01:59:28.780Z", "ra": 269.4463015, "dec": 4.7692332, "sigma_arcsec": 0.001}
  ],
  "warnings": []
}
```

| Campo | Significado |
|---|---|
| `method` | `proper_motion` (SIMBAD), `ephemeris` (JPL Horizons), `static` (sin movimiento conocido) |
| `pm_mas_yr` | Movimiento propio usado |
| `positions[]` | Una por fecha con imágenes de SPHEREx (o semanal si no hay) |
| `positions[].sigma_arcsec` | Radio de incertidumbre (1σ) → tamaño del círculo |

Ejemplo asteroide: `name=Ceres` → `method: "ephemeris"`.

---

### 5.12 `GET /api/overlay` — Gaia o WISE sobre la imagen

**Para qué sirve:** capa "Estrellas y movimiento propio": dibujar las fuentes de otro telescopio en la posición
que tenían **en la fecha del frame mostrado** (`at`), con su vector de movimiento.
**API externa:** Gaia DR3 (ESA) o AllWISE (IRSA).

| Parámetro | Tipo | Oblig. | Descripción |
|---|---|:-:|---|
| `ra`, `dec`, `fov` | número | ✔ (fov=300) | Cuadro |
| `catalog` | `gaia` / `wise` | ✔ | Catálogo |
| `at` | fecha | | Fecha del frame (sin valor = hoy) |

```bash
curl "http://localhost:8000/api/overlay?ra=269.446227&dec=4.769463&fov=300&catalog=gaia&at=2026-06-01"
```

```json
{
  "catalog": "gaia", "center": {"ra": 269.446111, "dec": 4.769444}, "fov_arcsec": 300.0,
  "epoch": 2026.413, "catalog_epoch": 2016.0, "mag_band": "G",
  "sources": [{
    "id": "Gaia DR3 4472832130942575872",
    "ra": 269.44622734, "dec": 4.76946331, "mag": 8.194,
    "pm": {"ra": -801.551, "dec": 10362.394},
    "catalog_ra": 269.44850253, "catalog_dec": 4.73942005
  }],
  "partial": false, "warnings": [], "cache": "hit"
}
```

| Campo | Significado |
|---|---|
| `epoch` | Fecha de las posiciones devueltas (= `at`) |
| `catalog_epoch` | (extra) Época original (Gaia 2016.0, AllWISE 2010.56) |
| `sources[].ra/dec` | Posición **propagada** a `at` → dibujar aquí |
| `sources[].mag` | Magnitud (G o W1): menor = más brillante → tamaño del marcador |
| `sources[].pm` | Vector de movimiento (mas/año) → flecha |
| `sources[].catalog_ra/dec` | (extra) Posición original sin propagar |

> Barnard: en el catálogo de Gaia (2016) estaba 108″ al sur; propagada a 2026 coincide al 0.001″ con
> `/api/resolve`.

---

### 5.13 `GET /api/status` — salud del sistema

**Para qué sirve:** indicador de estado en el Frontend y ver qué data release se usa. Comprueba cada API
externa (como mucho cada 5 min).

```json
{
  "status": "ok", "version": "0.3.0",
  "data_release": "SPHEREx DR1 (IRSA, colección spherex_dr1)", "spherex_mode": "irsa",
  "sources": {
    "spherex":  {"ok": true, "latency_ms": 383, "last_error": null},
    "simbad":   {"ok": true, "latency_ms": 749, "last_error": null},
    "ned":      {"ok": true, "latency_ms": 298, "last_error": null},
    "gaia":     {"ok": true, "latency_ms": 979, "last_error": null},
    "wise":     {"ok": true, "latency_ms": 512, "last_error": null},
    "horizons": {"ok": true, "latency_ms": 566, "last_error": null}
  },
  "jobs": {"running": 0, "queued": 0, "paused_now": 0, "paused_total": 0, "max_concurrent": 4, "max_heavy": 1}
}
```

`status: "degraded"` si alguna fuente falla (la API sigue funcionando con lo demás).

---

### 5.14 Banderas de calidad (`flags`)

Vienen de la extensión `FLAGS` de cada FITS de SPHEREx (se leen de su cabecera, así que un release nuevo con
banderas nuevas funciona sin cambiar código).

| Publicado como | SPHEREx | Qué significa | ¿Se limpia en la imagen? |
|---|---|---|:-:|
| `cosmic_ray` | TRANSIENT | Rayo cósmico / transitorio | ✔ |
| `saturation` | OVERFLOW | Píxel saturado | ✔ |
| `fit_error` | SUR_ERROR | Error en el procesado a bordo | ✔ |
| `nonfunctional` | NONFUNC | Píxel inutilizable | ✔ |
| `missing_data` | MISSING_DATA | Datos perdidos | ✔ |
| `hot_pixel` / `cold_pixel` | HOT / COLD | Píxel caliente / frío | ✔ |
| `outlier` | OUTLIER | Valor atípico | ✔ |
| `dichroic` | DICHROIC | Baja eficiencia (dicroico) | |
| `phantom_uncorrected` | PHANMISS | Corrección de phantom no aplicada | |
| `nonlinear` | NONLINEAR | No linealidad | |
| `persistence` | PERSIST | Carga residual de una fuente brillante anterior | |
| `crosstalk` | CROSSTALK | Diafonía | |
| `ghost`, `ghost_fpa`, `ghost_ext` | GHOST… | Fantasma de una fuente brillante | |
| `streak` | STREAK | Estela (satélite…) | |
| `bloom`, `snowball`, `halo` | BLOOM… | Artefactos del detector | |

"Se limpia" = el píxel se rellena con la media de sus vecinos antes de mostrar o restar (`CLEAN_BAD_PIXELS`).

---

### 5.15 Asteroides — JPL Horizons

Un solo endpoint `POST` (el único de la API). Sin modelo de IA: la posición futura es la efeméride oficial de
JPL Horizons, vista desde SPHEREx (`500@-163182`; si no hay órbita de SPHEREx para esas fechas, geocentro).

| Endpoint | Entrada | Respuesta | APIs externas |
|---|---|---|---|
| `/api/asteroids/predict-from-current` | `asteroid_id` (número MPC) + `current_observation {mjd_utc, ra, dec}` + `horizon_days` (0 < h ≤ 30) + `view` + `image_id` | **200 directo** (~0.5–2 s; caché después) | JPL Horizons (e IRSA solo si `image_id` no está en caché) |

**Para qué sirve:** el usuario marca un asteroide en el blink (T3, con la fecha del frame) y da su número MPC.
El backend comprueba con Horizons que el clic sea ese asteroide (≤ 60″), devuelve dónde estará `horizon_days`
después, la trayectoria (`track`, 9 puntos) y el píxel de cada posición sobre el PNG mostrado (`view` = los
`ra, dec, fov, size` de la secuencia).

Respuesta real (3000 Leonardo, `view` de 1200″ y 512 px), resumida:

```json
{"asteroid_id": 3000, "target": "3000 Leonardo (1981 EG19)", "method": "horizons", "observer": "500@-163182",
 "current_observation": {"mjd_utc": 61280.27836, "time": "2026-08-28T06:40:50.153Z", "ra": 60.39039, "dec": 20.06447,
                         "ephemeris_ra": 60.39042, "ephemeris_dec": 20.06450, "offset_arcsec": 0.35,
                         "pixel": {"x": 256.009, "y_top": 256.046, "inside_image": true}},
 "predicted_ra_deg": 60.5815245, "predicted_dec_deg": 20.0863899, "sigma_arcsec": 0.02,
 "forecast_mjd_utc": 61280.77836, "forecast_time": "2026-08-28T18:40:50.153Z", "horizon_days": 0.5,
 "motion": {"arcsec_per_hour": 54.282, "position_angle_deg": 83.03},
 "pixel": {"x": null, "y_top": null, "inside_image": false},
 "track": [{"time": "2026-08-28T06:40:50.153Z", "ra": 60.39042, "dec": 20.0645, "pixel": {"x": 256.136, "y_top": 256.124, "inside_image": true}},
           {"time": "2026-08-28T08:10:50.153Z", "pixel": {"x": 221.776, "y_top": 250.494, "inside_image": true}}, "…"],
 "warnings": [], "status": "ephemeris"}
```

Errores: 400 (validación; `image_id` sin `view`), 404 (número MPC desconocido), 422 `identity_mismatch` (el clic
está a más de 60″ de Horizons: otro asteroide o fecha equivocada), 502 (Horizons no respondió).

---

## 6. Flujos típicos del Frontend

**A. Buscar un objeto y ver si se mueve**

```
1. GET /api/resolve?name=WISE 0855                       → current.ra/dec
2. GET /api/coverage?ra&dec                              → histograma de fechas
3. GET /api/sequence?ra&dec&fov=300&detector=6           → 202 → /api/jobs/{id} … → frames[].png_url
4. Reproducir los png_url en orden (blink)
5. GET /api/overlay?ra&dec&fov=300&catalog=gaia&at=<date_obs del frame visible>   → marcadores
6. GET /api/objects?ra&dec&radius=150                    → qué es conocido (star/galaxy)
```

**B. Resta entre épocas**

```
1. GET /api/frames?ra&dec&detector=6                     → ids agrupados por fecha
2. GET /api/stack?ra&dec&op=diff&a=<ids época nueva>&b=<ids época vieja>   → 202 → png_url
```

**C. Clic en una fuente**

```
1. GET /api/spectrum?ra=<clic>&dec=<clic>&aperture=6     → 202 → points (gráfico λ vs flujo)
2. Ordenar points por date_obs                           → curva de luz
3. GET /api/predict?ra&dec&date_from&date_to             → ¿coincide con un objeto conocido en movimiento?
```

**Convertir el clic (x, y en el PNG) a coordenadas:**

```ts
const escala = size_px / fov_arcsec;
const dec = center.dec + (size_px / 2 - y) / escala / 3600;
const ra  = center.ra  + (size_px / 2 - x) / escala / 3600 / Math.cos(center.dec * Math.PI / 180);
```

---

## 7. Glosario

| Término | Significado |
|---|---|
| **RA / Dec** | Ascensión recta y declinación: "longitud" y "latitud" en el cielo, en grados |
| **″ (arcsec)** | Segundo de arco = 1/3600 de grado. Un píxel de SPHEREx ≈ 6.2″ |
| **FOV** | Campo de visión: lado del cuadro que se muestra (300″ = 5′) |
| **ICRS / J2000** | Sistema de referencia de coordenadas estándar |
| **Época** | Fecha a la que corresponde una posición de catálogo (J2000.0, Gaia 2016.0…) |
| **Movimiento propio (pm)** | Velocidad aparente de una estrella en el cielo, en mas/año (Barnard: 10 400 mas/año = 10.4″/año) |
| **Paralaje** | Pequeña oscilación anual por la órbita de la Tierra; 1000/paralaje(mas) = distancia en pársecs |
| **mas** | Milisegundo de arco = 0.001″ |
| **Detector (1–6)** | SPHEREx tiene 6 detectores; cada uno cubre un rango de longitudes de onda y aporta 17 de las 102 bandas |
| **µm** | Micra: unidad de longitud de onda (SPHEREx: 0.75–5 µm, infrarrojo cercano) |
| **MJy/sr** | Unidad de brillo superficial de las imágenes (megajanskys por estereorradián) |
| **µJy** | Microjansky: unidad de flujo de una fuente (espectro) |
| **FITS** | Formato estándar de imágenes astronómicas |
| **WCS** | Información que relaciona cada píxel con su RA/Dec |
| **Luz zodiacal** | Brillo difuso del polvo del Sistema Solar; varía con la fecha (se resta) |
| **Blink comparator** | Alternar imágenes de distintas fechas para ver lo que cambia (así se descubrió Plutón) |
| **SIA / TAP / ADQL** | Protocolos estándar del Observatorio Virtual: buscar imágenes (SIA), consultar catálogos con un SQL astronómico (TAP/ADQL) |
| **Enana marrón** | Objeto entre planeta y estrella; frío, brilla más en el infrarrojo largo (detectores 5–6). WISE 0855 es la más fría conocida |
