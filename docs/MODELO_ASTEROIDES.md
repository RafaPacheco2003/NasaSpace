# Modelo ML de asteroides — `POST /api/asteroids/predict`

Predicción **experimental** de la posición futura de un asteroide con una red **LSTM** entrenada con
observaciones de **Gaia** y exportada a **ONNX**. Implementa la *Guía de integración del modelo ONNX v1.0*
(14 nov 2026), con el mismo preprocesamiento del notebook de entrenamiento.

> **No es una efeméride.** Para saber dónde está de verdad un asteroide en una fecha usar
> `GET /api/predict?name=Ceres&...` (JPL Horizons). Este endpoint es un modelo experimental:
> sirve para demostrar el pronóstico de la red y compararlo con Horizons, no para apuntar un telescopio.

---

## Índice

1. [Resumen](#1-resumen)
2. [Archivos y dónde viven](#2-archivos-y-dónde-viven)
3. [Petición](#3-petición)
4. [Respuesta](#4-respuesta)
5. [Qué hace por dentro](#5-qué-hace-por-dentro-paso-a-paso)
6. [El tiempo: `epoch` de Gaia](#6-el-tiempo-epoch-de-gaia)
7. [Errores](#7-errores)
8. [Ejemplos reales](#8-ejemplos-reales)
9. [Cómo usarlo desde el Frontend](#9-cómo-usarlo-desde-el-frontend)
10. [Pruebas](#10-pruebas)
11. [Límites del modelo](#11-límites-del-modelo)
12. [Cambiar el modelo por uno nuevo](#12-cambiar-el-modelo-por-uno-nuevo)
13. [Diferencias con la guía](#13-diferencias-con-la-guía)

---

## 1. Resumen

| | |
|---|---|
| Endpoint | `POST /api/asteroids/predict` (JSON en el cuerpo) |
| Entrada | 3 observaciones `{epoch \| time, ra, dec}` del mismo asteroide + `horizon_days` |
| Salida | `predicted_ra_deg`, `predicted_dec_deg`, `forecast_epoch`, `forecast_time` |
| Modelo | `asteroid_lstm.onnx` (98 KB) + `preprocessing.json` |
| Motor | `onnxruntime` 1.20.1 en CPU, **sin PyTorch** |
| Velocidad | ~25 ms por petición (incluye transformaciones de Astropy) |
| API externa | Ninguna. No descarga nada ni busca por nombre. |
| Caché / 202 | No hace falta: siempre responde 200 al momento |
| Rate limit | El mismo que el resto de `/api/*` (120/min por IP) |
| Swagger | <http://localhost:8000/docs#/Asteroides%20(ML)> |

---

## 2. Archivos y dónde viven

```
backend/
├── models/
│   ├── asteroid_lstm.onnx          ← el modelo (entradas observations[N,3,3], horizon[N,1])
│   └── preprocessing.json          ← escalas REALES exportadas del notebook
├── app/
│   ├── services/asteroid_forecast.py  ← carga del modelo, preprocesamiento, inferencia, conversión de tiempos
│   ├── routers/asteroids.py           ← el endpoint
│   └── schemas.py                     ← AsteroidForecastRequest / AsteroidForecastResult (Swagger)
└── tests/test_asteroids.py         ← 15 tests (lista de aceptación de la guía)
```

> Estaban en `NasaSpace/models/`. Se movieron a `backend/models/` porque Docker solo puede copiar
> archivos que estén dentro de su contexto de build (`./backend`). Así el modelo va **dentro de la imagen**.

**Valores reales de `preprocessing.json`:**

```json
{
  "position_scale": [0.07369329780340195, 0.006465849233791232],
  "time_scale": 0.10215662978589535,
  "seq_len": 3
}
```

**Nombres del ONNX** (comprobados con `session.get_inputs()`):

| Nombre | Forma | Tipo |
|---|---|---|
| entrada `observations` | `[batch_size, 3, 3]` | float32 |
| entrada `horizon` | `[batch_size, 1]` | float32 |
| salida `predicted_displacement` | `[batch_size, 2]` | float32 |

**Carga:** se carga la primera vez que se usa (o al consultar `/api/status`) y se reutiliza la misma
`InferenceSession` en todo el proceso. Al cargar se valida que `position_scale` tenga 2 valores > 0,
`time_scale > 0`, `seq_len == 3` y que los nombres de entradas/salidas sean los esperados. Si algo falla,
la API **sigue funcionando** y solo este endpoint responde `503 model_unavailable`.

Ruta configurable con `MODEL_DIR` (por defecto `backend/models`, en Docker `/app/models`).

---

## 3. Petición

```http
POST /api/asteroids/predict
Content-Type: application/json
```

```json
{
  "asteroid_id": 1,
  "observations": [
    {"epoch": 1800.0, "ra": 40.0, "dec": 10.0},
    {"epoch": 1800.5, "ra": 40.1, "dec": 10.1},
    {"epoch": 1801.5, "ra": 40.3, "dec": 10.2}
  ],
  "horizon_days": 1.5
}
```

| Campo | Tipo | Reglas | Qué es |
|---|---|---|---|
| `asteroid_id` | entero, opcional | — | Solo para identificar; **no entra al modelo**, se devuelve tal cual |
| `observations` | lista | **exactamente 3** | Posiciones del **mismo** asteroide, **ordenadas** por fecha |
| `observations[].epoch` | número | estrictamente creciente; huecos **≤ 3 días** | Días TCB desde JD 2455197.5 (escala de Gaia). Ver [sección 6](#6-el-tiempo-epoch-de-gaia) |
| `observations[].time` | texto ISO 8601 | **en lugar de** `epoch` | Extra: fecha UTC normal (`2016-03-01T12:00:00Z`); la API la convierte a epoch Gaia |
| `observations[].ra` | grados | 0 ≤ RA < 360 | Ascensión recta (ICRS) |
| `observations[].dec` | grados | −90 ≤ Dec ≤ 90 | Declinación (ICRS) |
| `horizon_days` | número | 0 < h ≤ 3 | Cuántos días después de la **última** observación predecir |

En cada observación va `epoch` **o** `time`, no los dos. Se pueden mezclar entre observaciones.

---

## 4. Respuesta

```json
{
  "asteroid_id": 1,
  "predicted_ra_deg": 40.65200699257059,
  "predicted_dec_deg": 10.217896905462919,
  "forecast_epoch": 1803.0,
  "forecast_time": "2014-12-08T23:58:34.254Z",
  "horizon_days": 1.5,
  "status": "experimental",
  "frame": "ICRS, geometría Gaia-céntrica (la del entrenamiento)",
  "time_scale": "epoch = días TCB desde JD 2455197.5 (Gaia)"
}
```

| Campo | Qué es | Para qué sirve |
|---|---|---|
| `asteroid_id` | El mismo que se mandó | Relacionar la respuesta con la petición |
| `predicted_ra_deg` / `predicted_dec_deg` | Posición pronosticada, grados ICRS | Dibujar el punto predicho (p. ej. círculo punteado) sobre la imagen |
| `forecast_epoch` | Última `epoch` + `horizon_days` | La fecha de la predicción en la escala de Gaia |
| `forecast_time` | La misma fecha en ISO UTC | Mostrarla al usuario / compararla con `date_obs` de los frames |
| `horizon_days` | El horizonte usado | Eco de la petición |
| `status` | Siempre `"experimental"` | Mostrar en la UI que no es una medición |
| `frame` / `time_scale` | Texto fijo | Recordatorio de la convención de coordenadas y tiempo |

Los campos de la guía (`asteroid_id`, `predicted_ra_deg`, `predicted_dec_deg`, `forecast_epoch`,
`horizon_days`, `status`) están todos, con los mismos nombres. `forecast_time`, `frame` y `time_scale` son
extras.

---

## 5. Qué hace por dentro (paso a paso)

Es exactamente el código de la sección 04 de la guía. Un test (`test_reimplementacion_de_la_guia`) corre el
código de la guía tal cual y comprueba que el endpoint da el mismo resultado (diferencia < 1e-9°).

```
3 observaciones (epoch, ra, dec)
        │
        ▼
1. Validar: 3 filas · epochs finitas y estrictamente crecientes · huecos ≤ 3 días · 0 < horizon ≤ 3
        │
        ▼
2. Origen local = la ÚLTIMA observación (T3) → SkyOffsetFrame de Astropy
   Cada (ra, dec) → (lon, lat) en grados respecto a T3.  T3 queda en (0, 0).
   Así el modelo no ve RA absoluta y el salto 359° → 0° no le afecta.
        │
        ▼
3. dt = [0, t2−t1, t3−t2]  (días desde la observación anterior)
        │
        ▼
4. Normalizar (RobustScaler sin centrado → solo dividir):
   X = [ lon / 0.0736933,  lat / 0.00646585,  dt / 0.1021566 ]   → float32[1, 3, 3]
   H = [ horizon_days / 0.1021566 ]                              → float32[1, 1]
        │
        ▼
5. onnxruntime: predicted_displacement → float32[1, 2]
        │
        ▼
6. Desnormalizar: Δlon = out[0] × 0.0736933,  Δlat = out[1] × 0.00646585   (grados respecto a T3)
        │
        ▼
7. (Δlon, Δlat) en el SkyOffsetFrame de T3 → ICRS → predicted_ra_deg, predicted_dec_deg
```

Las escalas se aplican **una sola vez** a la entrada y **una sola vez** a la salida (el error más común según
la guía).

La predicción es **directa**: el horizonte entra como dato al modelo; no se itera día por día.

---

## 6. El tiempo: `epoch` de Gaia

`epoch` **no** es un timestamp Unix, ni un año, ni una fecha ISO. Es la columna `epoch` de las observaciones
de objetos del Sistema Solar de Gaia: **días en escala TCB** (Gaia-céntricos) contados desde
**JD 2455197.5** (= 2010-01-01 00:00 TCB).

```
epoch = JD(TCB) − 2455197.5
```

El modelo solo usa **diferencias** de epoch (cuánto tiempo pasó entre observaciones), pero el endpoint
también devuelve la fecha de la predicción, por eso conviene mandar el valor correcto.

| Fecha UTC | epoch Gaia |
|---|---|
| 2010-01-01T00:00:00Z | 0.000953 (TCB va ~82 s por delante de UTC) |
| 2014-12-08T23:58:34Z | 1803.0 |
| 2016-03-01T00:00:00Z | 2251.5010 |

**Si el Frontend no tiene epochs de Gaia**, que mande `time` en ISO UTC y la API hace la conversión con
Astropy (UTC → TCB, con precisión de microsegundos):

```json
{"time": "2016-03-01T12:00:00Z", "ra": 120.05, "dec": 20.02}
```

> Detalle fino: `horizon_days` se suma en días TCB. Un día TCB dura ~1.3 ms menos que uno UTC, por eso
> `time` + 1 día puede salir como `...11:59:59.999Z`. No es un error.

---

## 7. Errores

Con la forma de error de toda la API: `{"error": {"code", "message"}}`.

| HTTP | `code` | Cuándo | Mensaje real |
|---|---|---|---|
| 400 | `bad_request` | No son 3 observaciones | `Se requieren exactamente 3 observaciones (llegaron 2).` |
| 400 | `bad_request` | Desordenadas o epochs repetidas | `Las observaciones deben venir ordenadas por fecha, con epochs estrictamente crecientes.` |
| 400 | `bad_request` | Hueco > 3 días | `La separación entre observaciones consecutivas debe ser ≤ 3 días (rango del entrenamiento).` |
| 400 | `bad_request` | `horizon_days` > 3 | `horizon_days supera el máximo (3)` |
| 400 | `bad_request` | `horizon_days` ≤ 0 | `horizon_days debe ser mayor que 0` |
| 400 | `bad_request` | RA/Dec fuera de rango | `observations.0.ra debe ser menor que 360` |
| 400 | `bad_request` | Sin `epoch` ni `time`, o los dos | `observations.1: indicar 'epoch' (días Gaia) o 'time' (ISO UTC), solo uno de los dos.` |
| 400 | `bad_request` | `time` ilegible | `Fecha no válida: '...' (usar ISO 8601, p. ej. 2016-03-01T12:00:00Z)` |
| 400 | `bad_request` | Cuerpo no es JSON | `El cuerpo de la petición no es JSON válido.` |
| 429 | `rate_limited` | Demasiadas peticiones | (con cabecera `Retry-After`) |
| 503 | `model_unavailable` | Faltan los archivos o no cargó el ONNX | `El modelo de predicción de asteroides no está disponible (...)` |

> La guía pide **422** para entradas inválidas. Esta API usa **400** en todos los endpoints (es lo que dice
> el contrato SpaceX Sky API), así que aquí también es 400. Ver [sección 13](#13-diferencias-con-la-guía).

---

## 8. Ejemplos reales

Salidas reales del modelo entregado (no son números de ejemplo).

### 8.1 El ejemplo de la guía

```bash
curl -s -X POST http://localhost:8000/api/asteroids/predict \
  -H 'Content-Type: application/json' \
  -d '{"asteroid_id":1,
       "observations":[{"epoch":1800.0,"ra":40.0,"dec":10.0},
                       {"epoch":1800.5,"ra":40.1,"dec":10.1},
                       {"epoch":1801.5,"ra":40.3,"dec":10.2}],
       "horizon_days":1.5}'
```

```json
{"asteroid_id": 1, "predicted_ra_deg": 40.65200699257059, "predicted_dec_deg": 10.217896905462919,
 "forecast_epoch": 1803.0, "forecast_time": "2014-12-08T23:58:34.254Z", "horizon_days": 1.5,
 "status": "experimental", ...}
```

### 8.2 La salida cambia con el horizonte (sin iterar)

Mismas 3 observaciones, distintos `horizon_days`:

| `horizon_days` | `predicted_ra_deg` | `predicted_dec_deg` |
|---|---|---|
| 0.5 | 40.40446 | 10.23478 |
| 1.0 | 40.48756 | 10.23759 |
| 1.5 | 40.65201 | 10.21790 |
| 2.0 | 40.76179 | 10.19636 |
| 3.0 | 40.83618 | 10.17448 |

La RA sigue avanzando como en las observaciones (≈ +0.2°/día).

### 8.3 Con fechas ISO (`time`) en lugar de `epoch`

```bash
curl -s -X POST http://localhost:8000/api/asteroids/predict -H 'Content-Type: application/json' \
  -d '{"asteroid_id":433,
       "observations":[{"time":"2016-03-01T00:00:00Z","ra":120.0, "dec":20.0},
                       {"time":"2016-03-01T12:00:00Z","ra":120.05,"dec":20.02},
                       {"time":"2016-03-02T12:00:00Z","ra":120.15,"dec":20.06}],
       "horizon_days":1}'
```

```json
{"asteroid_id": 433, "predicted_ra_deg": 120.27665120328221, "predicted_dec_deg": 20.09966904266292,
 "forecast_epoch": 2253.501010993422, "forecast_time": "2016-03-03T11:59:59.999Z", "horizon_days": 1.0, ...}
```

### 8.4 Cruzando RA = 0°

```json
"observations": [{"epoch":1800.0,"ra":359.8,"dec":1}, {"epoch":1800.5,"ra":359.95,"dec":1},
                 {"epoch":1801.0,"ra":0.1,"dec":1}], "horizon_days": 0.5
```
→ `predicted_ra_deg: 0.19242`, `predicted_dec_deg: 1.00534`. Sigue avanzando después de pasar por 0°
(el SkyOffsetFrame evita el salto 359.95 → 0.1).

### 8.5 Errores

```bash
# observaciones desordenadas → 400
{"error":{"code":"bad_request","message":"Las observaciones deben venir ordenadas por fecha, con epochs estrictamente crecientes."}}
# horizon_days = 5 → 400
{"error":{"code":"bad_request","message":"horizon_days supera el máximo (3)"}}
```

### 8.6 Estado del modelo en `/api/status`

```bash
curl -s http://localhost:8000/api/status | jq .models
```
```json
{"asteroid_lstm": {"loaded": true, "file": "asteroid_lstm.onnx", "error": null}}
```

---

## 9. Cómo usarlo desde el Frontend

```ts
type Obs = { epoch?: number; time?: string; ra: number; dec: number };

async function forecastAsteroid(observations: Obs[], horizonDays: number, asteroidId?: number) {
  const res = await fetch(`${API}/api/asteroids/predict`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ asteroid_id: asteroidId, observations, horizon_days: horizonDays }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error.message);   // mismo formato de error que el resto
  return data as {
    predicted_ra_deg: number; predicted_dec_deg: number;
    forecast_epoch: number; forecast_time: string; status: "experimental";
  };
}
```

CORS ya permite `POST` (el preflight `OPTIONS` responde 200).

**De dónde sacar las 3 observaciones:** este endpoint **no** busca por nombre. Opciones:

- El usuario las marca en el blink: 3 clics sobre el asteroide en 3 frames → `ra/dec` del clic +
  `date_obs` del frame como `time`. (Ojo: los frames de SPHEREx de un mismo campo suelen estar separados
  por más de 3 días; solo sirven 3 frames dentro de ≤ 3 días entre sí.)
- Datos de Gaia DR3 SSO (`gaiadr3.sso_observation`: columnas `epoch`, `ra`, `dec`), ya en la escala correcta.
- **No inventarlas** ni rellenar con una sola posición: un único RA/Dec no basta para el LSTM.

**Cómo mostrarlo:** marcar el punto con un estilo distinto (círculo punteado) y la etiqueta
"predicción experimental (ML)". Si el objeto tiene nombre, mostrar al lado la posición de `GET /api/predict`
(Horizons) para comparar.

---

## 10. Pruebas

```bash
docker compose run --rm api pytest -q tests/test_asteroids.py     # 15 tests, sin red
./scripts/probar_api.sh --rapido                                  # incluye la sección "Asteroides (ML)"
```

La lista de aceptación de la guía, cubierta por los tests:

| Criterio de la guía | Test |
|---|---|
| 200 con dos coordenadas dentro de rango | `test_ok_dentro_de_rangos` |
| Observaciones desordenadas u horizonte negativo → error | `test_invalidos_400` (9 casos) |
| La salida cambia con `horizon_days` | `test_horizonte_cambia_la_salida` |
| Mismo X/H normalizado que el código de referencia | `test_reimplementacion_de_la_guia` |
| Cruce RA 359° → 0° | `test_cruce_de_ra_cero` |
| `time` ISO ≡ `epoch` | `test_time_iso_equivale_a_epoch` |
| `/api/status` reporta el modelo | `test_status_reporta_modelo` |

**Pendiente del dueño del notebook** (no se puede hacer en el backend porque no hay PyTorch ni el
checkpoint): correr la comprobación *"ONNX replica PyTorch"* de la sección 05 de la guía
(`assert_allclose(..., rtol=1e-4, atol=1e-4)`).

---

## 11. Límites del modelo

Copiados de la guía. Hay que comunicarlos en la UI:

- **Gaia-céntrico:** se entrenó con posiciones vistas desde Gaia (punto L2). No es automáticamente la
  posición vista desde la Tierra o desde SPHEREx. Para asteroides cercanos la diferencia (paralaje) puede
  ser de varios segundos de arco o más.
- **Horizonte ≤ 3 días:** fuera de eso no está validado (por eso la API lo rechaza).
- **Sin incertidumbre:** devuelve un punto, no una región de confianza.
- **Un solo objeto:** las 3 observaciones deben ser del mismo asteroide, con la misma convención de
  coordenadas y tiempo.
- **No son mediciones:** es una predicción experimental, no una observación ni una efeméride validada.

---

## 12. Cambiar el modelo por uno nuevo

1. Reemplazar `backend/models/asteroid_lstm.onnx` y `backend/models/preprocessing.json` (los **dos** juntos:
   las escalas son del mismo entrenamiento).
2. `docker compose up -d --build`.
3. `docker compose run --rm api pytest -q tests/test_asteroids.py`.
4. Comprobar `curl -s localhost:8000/api/status | jq .models`.

Sin reconstruir la imagen: montar la carpeta y apuntar `MODEL_DIR` a ella:

```yaml
# docker-compose.yml → services.api
    volumes:
      - ./mis_modelos:/models:ro
    environment:
      MODEL_DIR: /models
```

Si el nuevo ONNX cambia los nombres de entradas/salidas o `seq_len`, el modelo no carga y el endpoint
responde 503 con el motivo (también visible en `/api/status`).

---

## 13. Diferencias con la guía

| Guía | Aquí | Por qué |
|---|---|---|
| Errores 422 con `HTTPException` | **400** `{"error":{"code":"bad_request",...}}` | Toda la API usa 400 y esa forma de error (contrato). El Frontend maneja un solo formato |
| Archivos en `backend/export/` | `backend/models/` (configurable con `MODEL_DIR`) | Ya venían en una carpeta `models` |
| Modelo cargado al importar (si falta, no arranca) | Carga perezosa; si falta, solo este endpoint da 503 | Que un problema del modelo no tumbe el blink ni el espectro |
| Solo `epoch` | `epoch` **o** `time` (ISO UTC) | Que el Frontend pueda mandar fechas normales sin convertirlas |
| Respuesta de 6 campos | Los mismos 6 + `forecast_time`, `frame`, `time_scale` | Fecha legible y convención explícita |
| `def` síncrono | Inferencia en un hilo (`run_in_threadpool`) | No bloquear el bucle de eventos (los trabajos lentos de imágenes corren en el mismo proceso) |
