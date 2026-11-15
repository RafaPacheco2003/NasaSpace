# Conectar el Frontend (otro repo / otra laptop) al backend real

Objetivo: que el Frontend deje de usar datos **mock** y consuma esta API con datos reales de SPHEREx.

---

## 1. Elegir a qué backend apuntar

| Opción | URL | Cuándo |
|---|---|---|
| **A. Render (internet)** | `https://nasaspace-1ebj.onrender.com` | Lo más fácil; funciona desde cualquier laptop. Requiere que el último código esté subido (`git push`) |
| **B. Esta laptop en la misma red Wi-Fi** | `http://<IP-de-esta-laptop>:8000` (p. ej. `http://192.168.72.12:8000`) | Pruebas rápidas sin subir nada; ambas laptops en la misma red |
| C. Backend en la otra laptop | `http://localhost:8000` | Clonar este repo allí y `docker compose up -d --build` |

Comprobar desde la laptop del Frontend (navegador o terminal):

```bash
curl https://nasaspace-1ebj.onrender.com/api/status      # o http://192.168.72.12:8000/api/status
```

Debe responder `"spherex_mode": "irsa"` (datos reales). `"mock"` = sintéticos.

**Opción B — en esta laptop:**

```bash
cd NasaSpace && docker compose up -d --build     # ya escucha en todas las interfaces, puerto 8000
ipconfig getifaddr en0                           # la IP que va en la URL (cambia si cambia la red)
```

Si el Frontend corre en **https** (p. ej. desplegado en Vercel), solo puede llamar a un backend **https** → usar Render.

---

## 2. En el repo del Frontend

1. **Copiar el cliente**: `docs/frontend/skyApi.ts` → `src/lib/skyApi.ts` (sin dependencias; TypeScript o JS moderno).
2. **Variable de entorno** (archivo `.env` / `.env.local` del Frontend):

   ```bash
   # Vite
   VITE_API_URL=https://nasaspace-1ebj.onrender.com
   # Next.js
   NEXT_PUBLIC_API_URL=https://nasaspace-1ebj.onrender.com
   # Create React App
   REACT_APP_API_URL=https://nasaspace-1ebj.onrender.com
   ```

   Reiniciar el servidor de desarrollo después de cambiar el `.env`.
3. **Quitar los mocks**: buscar dónde se importan (`grep -rn "mock" src/`) y cambiar cada uno por la llamada real:

| Mock (lo que el Frontend inventaba) | Llamada real |
|---|---|
| Buscar objeto por nombre | `api.resolve("WISE 0855")` → `current.ra/dec` |
| Lista de fechas / línea de tiempo | `api.coverage({ra, dec})` y `api.frames({ra, dec, date_from, date_to})` |
| Imágenes del blink | `api.sequence({ra, dec, fov: 300, size: 512}, onProgress)` → `frames[i].png_url` con `abs()` |
| Resta / suma de épocas | `api.stack({ra, dec, op: "diff", a: [idA], b: [idB]})` → `png_url` |
| Estrellas / galaxias marcadas | `api.objects({ra, dec, radius: 120})`, `api.overlay({ra, dec, catalog: "gaia", at})` |
| Gráfico del espectro | `api.spectrum({ra, dec, aperture: 6}, onProgress)` → `points[]` |
| Predicción de posición | `api.predict({ra, dec, name, date_from, date_to})` |
| Imagen raw / color | `api.l2({ra, dec, date_from, date_to})` → `api.url.l2Raw(id)`, `api.url.l2Color(id)` |
| Clic → coordenadas + brillo | `api.l2Pixel(frameId, x, y, 1024)` (con `clickToPng(evento, img)`) |
| Sky cube 102 / 6 canales | `api.cube({ra, dec})` → `channels[]`, `six_channels[]`, `spectrum[]`; `api.url.cubeChannel(tile, n)` |
| Asteroide (dónde estará, JPL Horizons) | `api.asteroidFromCurrent({asteroid_id, current_observation, horizon_days, image_id, view})` → `pixel`, `track[]` |
| Indicador de estado | `api.status()` |

4. **Estados de carga**: las funciones lentas (`sequence`, `stack`, `spectrum`, `predict`, `l2`, `cube`) aceptan `onProgress(progress, stage)` → barra de progreso con texto
   ("Descargando frames 3/6", "Descargando tesela 160/321 MB"). Ya gestionan el 202 + sondeo de `/api/jobs/{id}`.
5. **Errores**: todas lanzan `ApiError` con `status`, `code` y `message` en español, listo para mostrar.

Ejemplo mínimo (React):

```tsx
import { api, abs, clickToPng } from "./lib/skyApi";

const [frames, setFrames] = useState<any[]>([]);
const [progress, setProgress] = useState<string>("");

async function load(name: string) {
  const obj = await api.resolve(name);
  const seq = await api.sequence({ ra: obj.current.ra, dec: obj.current.dec, fov: 300, size: 512 },
                                 (p, stage) => setProgress(`${Math.round(p * 100)}% · ${stage}`));
  setFrames(seq.frames);
}

// <img src={abs(frames[i].png_url)} />
```

---

## 3. Probar que ya no hay mocks

- Abrir las herramientas del navegador → **Red (Network)**: deben verse peticiones a `…/api/…` con respuestas 200/202.
- `GET /api/status` → `"spherex_mode": "irsa"`.
- WISE 0855 (`ra=133.757901, dec=-7.242058`) debe dar **258** exposiciones en `/api/frames` (datos reales DR1).
- M101 (`ra=210.80227, dec=54.34895`): clic en el núcleo del L2 ≈ **13.1 MJy/sr**.

---

## 4. Problemas típicos

| Síntoma | Causa / solución |
|---|---|
| `Failed to fetch` / CORS en la consola | URL mal escrita, backend apagado o página https llamando a http. CORS ya está abierto (`*`) |
| Primera petición a Render tarda ~1 min | Plan gratuito dormido: se despierta solo; abrir `/api/status` antes de la demo |
| `404` en `/api/l2` o `/api/cube` en Render | Render tiene código viejo: hacer `git push` del backend |
| `429 rate_limited` | Demasiadas peticiones; el cliente ya espera y reintenta. En Render poner `TRUST_PROXY=true` |
| Imágenes no cargan | Usar `abs(png_url)` (las URLs de la API son relativas) |
| Opción B deja de funcionar | Cambió la IP de esta laptop (`ipconfig getifaddr en0`) o está apagado Docker |
| `502 upstream_error` | IRSA/SIMBAD caído momentáneamente: reintentar en unos minutos |
