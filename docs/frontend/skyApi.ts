/**
 * Cliente del backend "SpaceX Sky API" para el Frontend (sin dependencias; TypeScript/JavaScript).
 * Copiar este archivo al repo del Frontend (p. ej. src/lib/skyApi.ts) y sustituir los datos mock por estas funciones.
 *
 * URL del backend (variable de entorno del Frontend):
 *   Vite:     VITE_API_URL=https://nasaspace-1ebj.onrender.com
 *   Next.js:  NEXT_PUBLIC_API_URL=https://nasaspace-1ebj.onrender.com
 *   Red local (backend en otra laptop):  http://192.168.x.x:8000
 */

// ---------------------------------------------------------------------------------------------------------------
// Configuración
// ---------------------------------------------------------------------------------------------------------------
const env: Record<string, string | undefined> =
  (typeof import.meta !== "undefined" && (import.meta as any).env) ||
  (typeof (globalThis as any).process !== "undefined" ? (globalThis as any).process.env : {}) || {};

export const API_BASE: string = (
  env.VITE_API_URL || env.NEXT_PUBLIC_API_URL || env.REACT_APP_API_URL || "http://localhost:8000"
).replace(/\/+$/, "");

/** Convierte una ruta relativa que devuelve la API ("/api/frame/...png") en URL absoluta para <img src>. */
export const abs = (path: string) => (path.startsWith("http") ? path : `${API_BASE}${path}`);

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

/** Parámetros de query: valores simples o listas (se repiten: ?a=x&a=y). */
type Params = object;

function qs(params: Params = {}): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params as Record<string, unknown>)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => u.append(k, String(x)));
    else u.append(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : "";
}

// ---------------------------------------------------------------------------------------------------------------
// Protección del backend: caché + deduplicación + ritmo de sondeo
// ---------------------------------------------------------------------------------------------------------------
/** Respuestas GET rápidas (200) que se reutilizan sin volver a pedirlas. */
const CACHE_TTL_MS = 5 * 60 * 1000;
/** Nunca sondear /api/jobs/{id} más rápido que esto, aunque el servidor sugiera menos. */
const MIN_POLL_MS = 1000;
/** Con trabajos largos o en cola se pregunta menos: como mucho cada 5 s. */
const MAX_POLL_MS = 5000;
const cache = new Map<string, { at: number; value: { status: number; data: unknown } }>();
const inflight = new Map<string, Promise<{ status: number; data: unknown }>>();

/** Vacía la caché local (p. ej. con un botón "Recargar"). */
export function clearApiCache() { cache.clear(); }

function sleep(ms: number, signal?: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException("Cancelado", "AbortError"));
    const t = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => { clearTimeout(t); reject(new DOMException("Cancelado", "AbortError")); }, { once: true });
  });
}

/** Redondea coordenadas a 1″ (lo mismo que hace el backend): moverse 0.1″ no crea consultas nuevas. */
function roundParams(params: Params = {}): Params {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(params as Record<string, unknown>)) {
    out[k] = (k === "ra" || k === "dec") && typeof v === "number" ? Math.round(v * 3600) / 3600 : v;
  }
  return out;
}

async function request<T>(method: "GET" | "POST", path: string, params?: Params, body?: unknown, signal?: AbortSignal): Promise<{ status: number; data: T }> {
  params = roundParams(params);
  const key = `${method} ${path}${qs(params)} ${body ? JSON.stringify(body) : ""}`;
  const cacheable = method === "GET" && !path.startsWith("/api/jobs/") && path !== "/api/status";
  if (cacheable) {
    const hit = cache.get(key);
    if (hit && Date.now() - hit.at < CACHE_TTL_MS) return hit.value as { status: number; data: T };
  }
  // Misma petición ya en curso (otro componente / otro render) → compartir la respuesta, no repetirla
  const running = inflight.get(key);
  if (running) return running as Promise<{ status: number; data: T }>;
  const p = rawRequest<T>(method, path, params, body, signal).then((r) => {
    if (cacheable && r.status === 200) cache.set(key, { at: Date.now(), value: r });
    return r;
  }).finally(() => inflight.delete(key));
  inflight.set(key, p);
  return p;
}

async function rawRequest<T>(method: "GET" | "POST", path: string, params?: Params, body?: unknown, signal?: AbortSignal): Promise<{ status: number; data: T }> {
  const res = await fetch(`${API_BASE}${path}${qs(params)}`, {
    method,
    signal,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 429) {
    const wait = Number(res.headers.get("Retry-After") || 2);
    await sleep(wait * 1000, signal);
    return rawRequest<T>(method, path, params, body, signal);
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok && res.status !== 202) {
    const e = (data as any)?.error ?? {};
    throw new ApiError(res.status, e.code ?? "error", e.message ?? `HTTP ${res.status}`);
  }
  return { status: res.status, data: data as T };
}

// ---------------------------------------------------------------------------------------------------------------
// Patrón 202 + trabajo (procesos lentos)
// ---------------------------------------------------------------------------------------------------------------
export interface Job<R = unknown> {
  id: string;
  kind: string;
  status: "queued" | "running" | "done" | "failed";
  progress: number;          // 0..1
  eta_s: number | null;
  stage: string;             // "Descargando frames 3/6"
  queue_position: number;
  poll_after_ms: number;
  result: R | null;
  error: { code: string; message: string } | null;
}

export type OnProgress = (progress: number, stage: string, job: Job) => void;

const slowRuns = new Map<string, Promise<unknown>>();

/** Llama a un endpoint lento: si responde 200 devuelve el resultado; si 202, sondea /api/jobs/{id} hasta terminar.
 *  - Si ya hay un sondeo del MISMO trabajo en curso, se reutiliza (un solo sondeo por trabajo en toda la app).
 *  - Sondea cada poll_after_ms del servidor (mínimo 1 s).
 *  - Con `signal` (AbortController) se deja de esperar; el backend termina el trabajo y lo guarda en caché. */
async function slow<R>(method: "GET" | "POST", path: string, params?: Params, body?: unknown,
                       onProgress?: OnProgress, signal?: AbortSignal): Promise<R> {
  params = roundParams(params);
  const key = `${method} ${path}${qs(params)} ${body ? JSON.stringify(body) : ""}`;
  const hit = cache.get(key);
  if (hit && Date.now() - hit.at < CACHE_TTL_MS) return hit.value.data as R;
  const listeners = new Set<OnProgress>();
  if (onProgress) listeners.add(onProgress);
  type Run = Promise<R> & { listeners?: Set<OnProgress>; waiting?: number };
  let run = slowRuns.get(key) as Run | undefined;
  if (run) {
    if (onProgress) run.listeners?.add(onProgress);
    run.waiting = (run.waiting ?? 0) + 1;
  } else {
    run = (async () => {
      const first = await rawRequest<any>(method, path, params, body);
      if (first.status === 200) return first.data as R;
      let job = first.data as Job<R>;
      while (true) {
        // Si TODOS los que esperaban cancelaron (cambiaron de objeto), dejar de sondear: el backend lo termina igual
        if ((run!.waiting ?? 0) <= 0) throw new DOMException("Sin interesados", "AbortError");
        run!.listeners?.forEach((fn) => fn(job.progress, job.stage, job));
        if (job.status === "done") return job.result as R;
        if (job.status === "failed") throw new ApiError(422, job.error?.code ?? "failed", job.error?.message ?? "Trabajo fallido");
        // Sondeo adaptativo: ≥ 1 s; si falta mucho (cola o descarga larga), hasta cada 5 s
        const eta = (job.eta_s ?? 0) * 1000;
        await sleep(Math.max(job.poll_after_ms ?? MIN_POLL_MS, MIN_POLL_MS, Math.min(eta / 5, MAX_POLL_MS)));
        job = (await rawRequest<Job<R>>("GET", `/api/jobs/${job.id}`)).data;
      }
    })() as Run;
    run.listeners = listeners;
    run.waiting = 1;
    slowRuns.set(key, run);
    run.then((data) => cache.set(key, { at: Date.now(), value: { status: 200, data } }))
       .catch(() => {})
       .finally(() => slowRuns.delete(key));
  }
  if (!signal) return run;
  // Cancelar solo deja de esperar a ESTE llamador; el sondeo compartido sigue si alguien más lo usa
  return Promise.race([run, new Promise<never>((_, reject) => {
    if (signal.aborted) reject(new DOMException("Cancelado", "AbortError"));
    signal.addEventListener("abort", () => {
      onProgress && run!.listeners?.delete(onProgress);
      run!.waiting = (run!.waiting ?? 1) - 1;
      reject(new DOMException("Cancelado", "AbortError"));
    }, { once: true });
  })]);
}

// ---------------------------------------------------------------------------------------------------------------
// Tipos principales (los campos más usados; las respuestas traen más)
// ---------------------------------------------------------------------------------------------------------------
export interface Coord { ra: number; dec: number }
export interface Frame {
  id: string; date_obs: string; detector: number; band_um: number; band_exact: boolean;
  center: Coord; fov_arcsec: number; exposure_s: number; flags: string[];
  png_url?: string; flags_png_url?: string; vmin?: number; vmax?: number;
}
export interface SequenceResult {
  center: Coord; fov_arcsec: number; size_px: number; vmin: number; vmax: number; unit: string;
  total: number; frames: Frame[]; partial: boolean; warnings: string[];
}
export interface SpectrumPoint { wavelength: number; flux: number; error: number; date_obs: string; detector: number; frame_id: string; flags: string[] }
export interface Pixel { x: number | null; y_top: number | null; inside_image: boolean; image_width: number; image_height: number }

// ---------------------------------------------------------------------------------------------------------------
// Catálogos y sistema (rápidos)
// ---------------------------------------------------------------------------------------------------------------
export const api = {
  status: () => request<any>("GET", "/api/status").then((r) => r.data),

  /** Nombre → coordenadas ("Barnard's star", "WISE 0855", "M101"). `at` = fecha para la posición actual. */
  resolve: (name: string, at?: string) => request<any>("GET", "/api/resolve", { name, at }).then((r) => r.data),

  /** Objetos SIMBAD/NED en un cono (radius en ″). */
  objects: (p: Coord & { radius?: number; catalogs?: string[]; types?: string[]; limit?: number }) =>
    request<any>("GET", "/api/objects", p).then((r) => r.data),

  // ----- Imágenes del blink -----------------------------------------------------------------------------------
  /** Metadatos de las exposiciones (sin imágenes). Fechas YYYY-MM-DD. */
  frames: (p: Coord & { fov?: number; detector?: number; date_from?: string; date_to?: string; limit?: number; offset?: number }) =>
    request<any>("GET", "/api/frames", p).then((r) => r.data),

  /** Histograma de exposiciones por semana (selector de fechas). */
  coverage: (p: Coord & { detector?: number }) => request<any>("GET", "/api/coverage", p).then((r) => r.data),

  /** Secuencia alineada para el blink (LENTO). Usar frames[i].png_url con abs(). */
  sequence: (p: Coord & { fov?: number; size?: number; detector?: number; date_from?: string; date_to?: string;
                          stretch?: "linear" | "log" | "sqrt" | "asinh"; cmap?: "gray" | "viridis" | "magma" | "inferno";
                          scale?: "sequence" | "frame"; limit?: number; offset?: number },
             onProgress?: OnProgress, signal?: AbortSignal) =>
    slow<SequenceResult>("GET", "/api/sequence", p, undefined, onProgress, signal),

  /** Suma (mean/median/sum de a) o resta (diff = media(a) − media(b)) (LENTO). Usar result.png_url. */
  stack: (p: Coord & { op: "mean" | "median" | "sum" | "diff"; a: string[]; b?: string[]; fov?: number; size?: number;
                       stretch?: string; cmap?: string }, onProgress?: OnProgress, signal?: AbortSignal) =>
    slow<any>("GET", "/api/stack", p, undefined, onProgress, signal),

  // ----- Análisis ----------------------------------------------------------------------------------------------
  /** Espectro 0.75–5 µm en una apertura (″) (LENTO). Ordenado por date_obs = curva de luz. */
  spectrum: (p: Coord & { aperture?: number; date_from?: string; date_to?: string }, onProgress?: OnProgress, signal?: AbortSignal) =>
    slow<{ points: SpectrumPoint[]; [k: string]: any }>("GET", "/api/spectrum", p, undefined, onProgress, signal),

  /** Posición predicha en cada fecha con imágenes (LENTO). Fechas obligatorias. */
  predict: (p: Coord & { name?: string; date_from: string; date_to: string }, onProgress?: OnProgress, signal?: AbortSignal) =>
    slow<any>("GET", "/api/predict", p, undefined, onProgress, signal),

  /** Fuentes Gaia o WISE en el campo, propagadas a la fecha `at`. */
  overlay: (p: Coord & { catalog: "gaia" | "wise"; fov?: number; at?: string }) =>
    request<any>("GET", "/api/overlay", p).then((r) => r.data),

  // ----- Asteroides (JPL Horizons) -----------------------------------------------------------------------------
  /** El usuario marca el asteroide (T3) en un frame + su número MPC → posición futura según JPL Horizons,
   *  trayectoria (`track`, 9 puntos) y píxeles en la vista. Responde 200 directo (~1 s; caché después).
   *  horizon_days: 0 < h ≤ 30. Errores: 422 identity_mismatch (el clic no es ese asteroide), 404 número desconocido. */
  asteroidFromCurrent: (body: { asteroid_id: number; current_observation: { mjd_utc: number; ra: number; dec: number };
                                horizon_days: number; image_id?: string; view?: { ra: number; dec: number; fov: number; size: number } },
                        onProgress?: OnProgress, signal?: AbortSignal) =>
    slow<any>("POST", "/api/asteroids/predict-from-current", undefined, body, onProgress, signal),


  // ----- L2 completo (pipeline SphereX) -----------------------------------------------------------------------
  /** Prepara el L2 completo (LENTO la 1ª vez, ~65 MB). Usar raw_png_url / color_png_url / pixel_url. */
  l2: (p: Coord & { date_from?: string; date_to?: string; detector?: number; frame_id?: string; size?: number },
       onProgress?: OnProgress, signal?: AbortSignal) => slow<any>("GET", "/api/l2", p, undefined, onProgress, signal),

  /** Clic en el L2 (x,y del PNG desde arriba-izquierda, mismo size) → RA/Dec + brillo. */
  l2Pixel: (frameId: string, x: number, y: number, size = 1024) =>
    request<any>("GET", `/api/l2/${frameId}/pixel`, { x: x.toFixed(2), y: y.toFixed(2), size }).then((r) => r.data),

  // ----- Sky cube (102 canales) -------------------------------------------------------------------------------
  /** Prepara la tesela del cubo (LENTO la 1ª vez, ~320 MB). Trae channels[102], six_channels[6], spectrum[102]. */
  cube: (p: Coord & { size?: number }, onProgress?: OnProgress, signal?: AbortSignal) =>
    slow<any>("GET", "/api/cube", p, undefined, onProgress, signal),

  cubeChannels: (tileId: string, set: "all" | "six" = "all", cmap = "gray_r", size?: number) =>
    request<any>("GET", `/api/cube/${tileId}/channels`, { set, cmap, size }).then((r) => r.data),

  /** Clic en un plano del cubo → RA/Dec + espectro de 102 canales. */
  cubePixel: (tileId: string, x: number, y: number, size?: number) =>
    request<any>("GET", `/api/cube/${tileId}/pixel`, { x: x.toFixed(2), y: y.toFixed(2), size }).then((r) => r.data),

  cubePreview3d: (tileId: string, n = 32) => request<any>("GET", `/api/cube/${tileId}/preview3d`, { n }).then((r) => r.data),

  // ----- URLs de imágenes (para <img src>) ---------------------------------------------------------------------
  url: {
    l2Raw: (frameId: string, size = 1024, invert = true) => abs(`/api/l2/${frameId}/raw.png${qs({ size, invert })}`),
    l2Color: (frameId: string, size = 1024) => abs(`/api/l2/${frameId}/color.png${qs({ size })}`),
    cubeChannel: (tileId: string, ch: number, cmap = "gray_r", size?: number) => abs(`/api/cube/${tileId}/channel/${ch}.png${qs({ cmap, size })}`),
    cubeSamples: (tileId: string, ra?: number, dec?: number) => abs(`/api/cube/${tileId}/samples.png${qs({ ra, dec })}`),
    cube3d: (tileId: string) => abs(`/api/cube/${tileId}/cube3d.png`),
  },
};

// ---------------------------------------------------------------------------------------------------------------
// Utilidades
// ---------------------------------------------------------------------------------------------------------------
/** Clic sobre un <img> mostrado a otro tamaño → coordenadas del PNG original (las que esperan los /pixel). */
export function clickToPng(ev: MouseEvent, img: HTMLImageElement): { x: number; y: number } {
  const r = img.getBoundingClientRect();
  return { x: (ev.clientX - r.left) * (img.naturalWidth / r.width), y: (ev.clientY - r.top) * (img.naturalHeight / r.height) };
}

/** Fuente (ra, dec) → píxel (x desde la izquierda, y desde arriba) en un PNG de /api/sequence (norte arriba, este a la izquierda). */
export function skyToSequencePixel(ra: number, dec: number, seq: { center: Coord; fov_arcsec: number; size_px: number }) {
  const scale = seq.size_px / seq.fov_arcsec;
  let dra = ra - seq.center.ra;
  if (dra > 180) dra -= 360;
  if (dra < -180) dra += 360;
  return {
    x: seq.size_px / 2 - dra * Math.cos((seq.center.dec * Math.PI) / 180) * 3600 * scale,
    y: seq.size_px / 2 - (dec - seq.center.dec) * 3600 * scale,
  };
}

/** Inversa: píxel (x, y desde arriba) de un PNG de /api/sequence → (ra, dec). Usar con clickToPng(). */
export function sequencePixelToSky(x: number, y: number, seq: { center: Coord; fov_arcsec: number; size_px: number }) {
  const deg = seq.fov_arcsec / seq.size_px / 3600;
  const dec = seq.center.dec + (seq.size_px / 2 - y) * deg;
  let ra = seq.center.ra - ((x - seq.size_px / 2) * deg) / Math.cos((seq.center.dec * Math.PI) / 180);
  ra = ((ra % 360) + 360) % 360;
  return { ra, dec };
}

/** Fecha ISO UTC → MJD UTC (para asteroidFromCurrent con el date_obs del frame). */
export const isoToMjd = (iso: string) => Date.parse(iso) / 86400000 + 40587;

/** Ejecuta `fn` solo cuando dejan de llegar llamadas durante `ms` (pan, zoom, sliders, escribir en un buscador). */
export function debounce<A extends unknown[]>(fn: (...args: A) => void, ms = 500) {
  let t: ReturnType<typeof setTimeout> | undefined;
  return (...args: A) => { if (t) clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

/** Para cargas que se reemplazan (cambiar de objeto, de fecha…): cada llamada cancela la espera de la anterior.
 *  const nueva = latest();  api.sequence(p, onProgress, nueva())  */
export function latest() {
  let ctrl: AbortController | undefined;
  return () => { ctrl?.abort(); ctrl = new AbortController(); return ctrl.signal; };
}

/** true si el error es solo una cancelación (no mostrarlo al usuario). */
export const isAbort = (e: unknown) => (e as any)?.name === "AbortError";
