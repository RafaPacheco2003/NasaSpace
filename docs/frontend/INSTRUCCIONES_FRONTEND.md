# Instrucciones para el Frontend: usar el backend sin saturarlo

> Texto pensado para pasarlo tal cual al equipo del Frontend **o a su asistente de IA** (Claude, Copilot, Cursor…).

---

## Contexto

El Frontend (Astro, `http://localhost:4321`) ya consume el backend real de SPHEREx. Funciona sin errores, pero en
20 minutos hizo **594 peticiones**, y en el pico ~390 en un minuto. Por eso los espectros esperaban hasta **7 minutos
en cola**. El backend procesa **2 trabajos lentos a la vez**; el resto espera en fila.

Se vieron tres problemas:

1. **Sondeo demasiado rápido.** `/api/jobs/{id}` se consultaba cada ~0.5 s, de varios trabajos a la vez.
2. **Consultas repetidas.** `coverage`, `objects` y `frames` se pedían **13 veces** con las mismas coordenadas: se
   re-ejecutan en cada render.
3. **Un trabajo nuevo por cada movimiento o zoom.** RA 269.47643 → 269.47819 → 269.49043 y FOV 338/540/670 crean
   `spectrum`, `sequence` y `predict` nuevos que se quedan en cola aunque el usuario ya no los mire.

---

## Instrucción (copiar y pegar)

```
Estamos conectando el Frontend al backend real de SPHEREx (FastAPI). Todas las llamadas a la API deben pasar por
`src/lib/skyApi.ts` (versión nueva adjunta: reemplaza la anterior). No usar fetch directo contra la API.

Reglas obligatorias:

1. UN SOLO CLIENTE. Importar siempre `api` desde `src/lib/skyApi.ts`. Si hay otro cliente propio o fetch sueltos
   contra /api/*, sustituirlos. skyApi ya hace: caché de 5 min de las respuestas, deduplicación de peticiones
   idénticas simultáneas, redondeo de ra/dec a 1″, un solo sondeo por trabajo y sondeo cada 1–5 s
   según poll_after_ms y eta_s. NO escribir bucles propios de sondeo de /api/jobs/{id}.

2. NADA DE LLAMADAS EN CADA RENDER. Las llamadas a la API solo se hacen en respuesta a una acción del usuario
   (buscar, elegir fecha, clic) o cuando cambian de verdad sus parámetros. En React/Preact: dentro de useEffect con
   dependencias primitivas (ra, dec, fov, fechas), nunca con objetos/arrays recreados en cada render. En Astro: en
   islas con client:load/client:visible, no en el frontmatter que se re-ejecuta.

3. DEBOUNCE EN PAN, ZOOM, SLIDERS Y BUSCADOR. Usar `debounce(fn, 500)` de skyApi: solo se pide cuando el usuario deja
   de mover 500 ms. Durante el movimiento, reescalar la imagen que ya está cargada en el cliente.

4. LOS PROCESOS LENTOS SOLO BAJO DEMANDA:
   - api.sequence (blink): al buscar un objeto o cambiar el rango de fechas. NO al hacer zoom: el zoom se hace en el
     navegador escalando los PNG ya descargados. Pedir una secuencia nueva solo si el usuario pulsa "Actualizar
     campo" o sale del área cargada.
   - api.spectrum y api.predict: SOLO al hacer clic en una fuente concreta, no automáticamente para todas las
     fuentes ni en cada movimiento.
   - api.stack (resta): solo al activar el modo resta.
   - api.l2 y api.cube: solo al abrir esas vistas (descargan 65 MB / 320 MB la primera vez).

5. CANCELAR LO QUE YA NO SE MIRA. Para cada panel usar `const nueva = latest()` y pasar `nueva()` como signal en
   cada llamada lenta: al cambiar de objeto o de fuente se deja de esperar la anterior. Ignorar los errores de
   cancelación con `isAbort(e)` (no mostrarlos).

6. LÍMITES:
   - sequence: limit ≤ 24 frames por petición (paginar con offset si el usuario quiere más) y size 512.
   - Como máximo 2 procesos lentos activos a la vez desde toda la app (el backend solo procesa 2).
   - api.status: una vez al cargar y luego cada ≥ 60 s, no en cada render.

7. MOSTRAR EL PROGRESO. Toda llamada lenta recibe onProgress(progress, stage) → barra con el texto de stage
   ("Descargando frames 3/12", "Fotometría 120/239", "En cola"). Si queue_position > 0, mostrar "En cola (n)".

8. ERRORES: capturar ApiError y mostrar e.message (ya viene en español). 502/504 = la NASA no respondió:
   ofrecer "Reintentar". No reintentar automáticamente en bucle.

Criterio de aceptación: con el Frontend abierto y usándolo normal (buscar, mover, zoom, clic en 2 fuentes),
en la pestaña Network hay menos de 30 peticiones por minuto a /api/* y ninguna petición idéntica repetida
dentro de 5 minutos.
```

---

## Ejemplos de código

### Llamadas al buscar (no en cada render)

```tsx
import { api, debounce, latest, isAbort } from "@/lib/skyApi";

const nuevaSecuencia = latest();

export function Explorador() {
  const [obj, setObj] = useState<{ ra: number; dec: number } | null>(null);
  const [seq, setSeq] = useState<any>(null);
  const [progreso, setProgreso] = useState("");

  // Solo cuando cambia el OBJETO (no el zoom)
  useEffect(() => {
    if (!obj) return;
    setProgreso("Buscando…");
    api.coverage(obj).then(setCobertura);                              // rápido; queda en caché
    api.sequence({ ...obj, fov: 540, size: 512, limit: 24 },
                 (p, stage) => setProgreso(`${Math.round(p * 100)}% · ${stage}`),
                 nuevaSecuencia())                                      // cancela la espera de la anterior
      .then(setSeq)
      .catch((e) => { if (!isAbort(e)) setProgreso(e.message); });
  }, [obj?.ra, obj?.dec]);                                              // dependencias primitivas
  // …
}
```

### Zoom y pan: escalar en el navegador, pedir al backend solo al terminar

```tsx
const [zoom, setZoom] = useState(1);
const pedirCampoNuevo = useMemo(() => debounce((ra: number, dec: number) => setObj({ ra, dec }), 500), []);

function onZoom(z: number) { setZoom(z); }                  // solo CSS: transform: scale(z)
function onPanTermina(ra: number, dec: number) {           // si sale del área cargada
  pedirCampoNuevo(ra, dec);
}
```

### Espectro solo al hacer clic

```tsx
const nuevoEspectro = latest();

async function onClickFuente(f: { ra: number; dec: number }) {
  try {
    const s = await api.spectrum({ ra: f.ra, dec: f.dec, aperture: 6 },
                                 (p, st) => setBarra(p, st), nuevoEspectro());
    pintarEspectro(s.points);
  } catch (e) {
    if (!isAbort(e)) mostrarError((e as Error).message);
  }
}
```

### Estado del sistema sin saturar

```ts
api.status().then(pintarEstado);
setInterval(() => api.status().then(pintarEstado), 60_000);
```

---

## Qué hace la versión nueva de `skyApi.ts` por sí sola

| Protección | Efecto medido |
|---|---|
| Deduplicación de peticiones idénticas simultáneas | 10 componentes pidiendo `frames` a la vez → **1 petición** |
| Caché local de 5 min | Repetir la misma consulta → **0 peticiones** |
| Redondeo de ra/dec a 1″ | Mover el mapa 0.1″ no crea consultas nuevas |
| Un solo sondeo por trabajo | 5 llamadas al mismo `sequence` → **1 petición de inicio + 1 sondeo compartido** (no 5) |
| Sondeo adaptativo: ≥ 1 s; hasta 5 s si el trabajo va para largo (`eta_s`) | Intervalo mínimo medido 1002 ms; en cola larga, ~1 consulta cada 5 s |
| `latest()` + `signal` | Cambiar de objeto deja de esperar el trabajo anterior (verificado) |
| `debounce()` | 20 eventos de zoom → **1 llamada** (verificado) |
| Espera automática ante 429 | Respeta `Retry-After` |

Lo que el cliente **no** puede arreglar solo, y lo tiene que hacer el Frontend: no lanzar trabajos en cada render,
zoom o pan (reglas 2–4).

---

## Revisión 2 (después de aplicar las reglas)

Medido en el log del backend:

| | Antes | Ahora |
|---|---|---|
| Peticiones en el pico | ~570 / min | ~90 / min |
| Consultas idénticas repetidas | 13× `coverage`/`objects`/`frames` | **0** (cada coordenada una vez, redondeada a 1″) ✅ |
| Intervalo de sondeo | ~0.5 s | 1–2 s, uno por trabajo ✅ |
| Espectros lanzados solos | muchos | 1 (al hacer clic) ✅ |

Falta:

1. **El zoom y el pan siguen lanzando un `sequence` nuevo.** En 2 minutos hubo 5 secuencias: el FOV pasó de 540 a 288
   y de vuelta a 540, y la RA se movió de 269.4992 a 269.5117 (unos 45″). Regla 4: el zoom se hace en el navegador
   (CSS `transform: scale`) con los PNG ya cargados. Un `sequence` nuevo solo con el botón "Actualizar campo" o al
   salir del área cargada (más de ~1/3 del FOV).
2. **Las secuencias viejas se seguían sondeando** después de pedir otras. Usar `const nueva = latest()` y pasar
   `nueva()` en cada `api.sequence(...)`. Con la versión nueva de `skyApi.ts`, si nadie espera ya un trabajo, se deja
   de sondear.
3. **Actualizar `skyApi.ts`** a la última versión: el sondeo de 2.0 s fijo con `eta_s` de 1116 s indica una versión
   anterior. La nueva espera hasta 5 s cuando el trabajo va para largo.

Además, **IRSA (NASA) está limitando las descargas**: 556 reintentos por HTTP 503 en 30 min, y una secuencia de 24
frames lleva varios minutos en "Descargando frames 0/24". No es un fallo del Frontend, pero cada `sequence` innecesario
empeora la espera.
