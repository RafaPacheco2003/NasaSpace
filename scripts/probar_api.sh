#!/usr/bin/env bash
# =============================================================================
#  probar_api.sh — Prueba todos los endpoints de la SPHEREx Sky API
#
#  Uso:
#    ./scripts/probar_api.sh                  # contra http://localhost:8000
#    BASE_URL=https://mi-api.com ./scripts/probar_api.sh
#    ./scripts/probar_api.sh --rapido         # sin los procesos lentos (sequence/stack/spectrum/predict)
#    ./scripts/probar_api.sh --rate-limit     # además prueba el 429 (al final, bloquea ~20 s tu IP)
#
#  Requisitos: curl y jq (macOS ya los trae; Linux: apt install jq).
#  Las imágenes PNG descargadas quedan en ./salida_pruebas/
# =============================================================================
set -u

BASE="${BASE_URL:-http://localhost:8000}"
OUT="${OUT_DIR:-./salida_pruebas}"
RAPIDO=0
RATE=0
for a in "$@"; do
  case "$a" in
    --rapido) RAPIDO=1 ;;
    --rate-limit) RATE=1 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
  esac
done

command -v curl >/dev/null || { echo "Falta curl"; exit 1; }
command -v jq   >/dev/null || { echo "Falta jq (macOS: brew install jq · Linux: apt install jq)"; exit 1; }

mkdir -p "$OUT"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Objetivos de calibración del reto (posiciones actuales, ya propagadas)
WISE_RA=133.757901;  WISE_DEC=-7.242058     # WISE 0855 (enana marrón más fría conocida)
BARN_RA=269.446227;  BARN_DEC=4.769463      # Barnard's Star (10.4″/año)

# --- utilidades ----------------------------------------------------------------
G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; B=$'\e[1m'; D=$'\e[2m'; N=$'\e[0m'
PASS=0; FAIL=0; FAILED_NAMES=()

titulo() { printf "\n${B}━━ %s${N}\n" "$1"; }
ok()     { PASS=$((PASS+1)); printf "  ${G}✔${N} %s\n" "$1"; [ -n "${2:-}" ] && printf "    ${D}%s${N}\n" "$2"; return 0; }
fail()   { FAIL=$((FAIL+1)); FAILED_NAMES+=("$1"); printf "  ${R}✘${N} %s\n" "$1"; [ -n "${2:-}" ] && printf "    ${R}%s${N}\n" "$2"; return 0; }
info()   { printf "    ${D}%s${N}\n" "$1"; }

# GET  → $CODE (HTTP) y $TMP/body (cuerpo). Acepta cabeceras extra.
req() {
  local path="$1"; shift
  CODE=$(curl -s -m 120 -o "$TMP/body" -D "$TMP/headers" -w "%{http_code}" "$@" "$BASE$path")
}
body() { cat "$TMP/body"; }
j()    { jq -r "$1" "$TMP/body" 2>/dev/null; }
err()  { jq -r '"\(.error.code): \(.error.message)"' "$TMP/body" 2>/dev/null | head -c 300; }

# Patrón 202 + trabajo: llama, sondea /api/jobs/{id} hasta done/failed. Resultado en $TMP/result.
run_job() {
  local path="$1" t0=$SECONDS
  req "$path"
  if [ "$CODE" = "200" ]; then cp "$TMP/body" "$TMP/result"; info "200 directo (resultado en caché)"; return 0; fi
  if [ "$CODE" != "202" ]; then info "HTTP $CODE $(err)"; return 1; fi
  local id; id=$(j .id)
  info "202 → trabajo $id (kind=$(j .kind), cola=$(j .queue_position))"
  while :; do
    case "$(j .status)" in
      done)   printf "\r%80s\r" ""; jq '.result // .' "$TMP/body" > "$TMP/result"; info "done en $((SECONDS-t0)) s"; return 0 ;;
      failed) printf "\r%80s\r" ""; info "failed: $(jq -r '"\(.error.code): \(.error.message)"' "$TMP/body")"; return 1 ;;
    esac
    printf "\r    ${Y}… %-38s %3s%%  eta %ss${N}   " "$(j .stage | cut -c1-38)" "$(jq -r '(.progress*100|floor)' "$TMP/body")" "$(j .eta_s)"
    sleep "$(jq -r '(.poll_after_ms // 1000) / 1000' "$TMP/body")"
    [ $((SECONDS-t0)) -gt 900 ] && { echo; info "timeout de 15 min"; return 1; }
    req "/api/jobs/$id"
    [ "$CODE" = "200" ] || { echo; info "jobs → HTTP $CODE"; return 1; }
  done
}
r() { jq -r "$1" "$TMP/result" 2>/dev/null; }

# Igual que run_job pero la primera llamada es POST con cuerpo JSON
run_post_job() {
  local path="$1" body="$2" t0=$SECONDS
  req "$path" -X POST -H "Content-Type: application/json" -d "$body"
  if [ "$CODE" = "200" ]; then cp "$TMP/body" "$TMP/result"; info "200 directo (resultado en caché)"; return 0; fi
  if [ "$CODE" != "202" ]; then info "HTTP $CODE $(err)"; return 1; fi
  local id; id=$(j .id)
  info "202 → trabajo $id (kind=$(j .kind))"
  while :; do
    case "$(j .status)" in
      done)   printf "\r%80s\r" ""; jq '.result // .' "$TMP/body" > "$TMP/result"; info "done en $((SECONDS-t0)) s"; return 0 ;;
      failed) printf "\r%80s\r" ""; jq '.error' "$TMP/body" > "$TMP/result"; info "failed: $(jq -r '"\(.error.code): \(.error.message)"' "$TMP/body" | head -c 200)"; return 2 ;;
    esac
    printf "\r    ${Y}… %-38s %3s%%${N}   " "$(j .stage | cut -c1-38)" "$(jq -r '(.progress*100|floor)' "$TMP/body")"
    sleep 1
    [ $((SECONDS-t0)) -gt 300 ] && { echo; info "timeout de 5 min"; return 1; }
    req "/api/jobs/$id"
  done
}

png_ok() {  # comprueba que el archivo es un PNG de NxN
  local f="$1" n="$2"
  [ "$(head -c 8 "$f" | xxd -p)" = "89504e470d0a1a0a" ] || return 1
  local w h; w=$(xxd -s 16 -l 4 -p "$f"); h=$(xxd -s 20 -l 4 -p "$f")
  [ $((16#$w)) -eq "$n" ] && [ $((16#$h)) -eq "$n" ]
}

printf "${B}SPHEREx Sky API — pruebas contra %s${N}\n" "$BASE"

# =============================================================================
titulo "Sistema"
# =============================================================================
req "/api/status"
if [ "$CODE" = "200" ]; then
  ok "GET /api/status → $(j .status) · v$(j .version)" "data_release: $(j .data_release)"
  jq -r '.sources | to_entries[] | "    \(if .value.ok then "🟢" else "🔴" end) \(.key) \(.value.latency_ms // "-") ms \(.value.last_error // "" | .[0:60])"' "$TMP/body"
else
  fail "GET /api/status" "HTTP $CODE — ¿está la API levantada? (docker compose up)"; exit 1
fi

req "/api/status" -H "Origin: http://ejemplo.com"
grep -qi '^access-control-allow-origin: \*' "$TMP/headers" && ok "CORS abierto (Access-Control-Allow-Origin: *)" || fail "CORS" "falta la cabecera"

# =============================================================================
titulo "Catálogos — /api/resolve  (SIMBAD → NED)"
# =============================================================================
req "/api/resolve?name=Barnard%27s%20star&at=2026-06-01"
if [ "$CODE" = "200" ] && [ "$(j .source)" = "simbad" ]; then
  ok "Barnard's star → $(j .matched_name)" "catálogo J$(j .epoch): $(j .ra), $(j .dec) · pm $(j .pm.ra),$(j .pm.dec) mas/año · current($(j .current.epoch)): $(j .current.ra), $(j .current.dec)"
else fail "resolve Barnard" "HTTP $CODE $(err)"; fi

req "/api/resolve?name=WISE%200855"
[ "$CODE" = "200" ] && ok "WISE 0855 → $(j .matched_name) ($(j .object_type))" "current: $(j .current.ra), $(j .current.dec)" || fail "resolve WISE 0855" "HTTP $CODE $(err)"

for name in "RW%20Aur" "R%20Dra" "epsilon%20indi%20b"; do
  req "/api/resolve?name=$name"
  [ "$CODE" = "200" ] && ok "${name//%20/ } → $(j .matched_name) [$(j .source)]" || fail "resolve ${name//%20/ }" "HTTP $CODE $(err)"
done

req "/api/resolve?name=Estrella%20Falsa%20XYZ"
[ "$CODE" = "404" ] && [ "$(j .error.code)" = "not_found" ] && ok "Nombre inexistente → 404 not_found" "$(j .error.message)" || fail "resolve 404" "HTTP $CODE"

# =============================================================================
titulo "Catálogos — /api/objects  (SIMBAD TAP + NED TAP)"
# =============================================================================
req "/api/objects?ra=$WISE_RA&dec=$WISE_DEC&radius=120&limit=5"
if [ "$CODE" = "200" ]; then
  ok "Cono de 2′ en WISE 0855 → $(j '.counts | to_entries | map("\(.key)=\(.value)") | join(" ")')" "partial=$(j .partial) cache=$(j .cache)"
  jq -r '.objects[:3][] | "      · \(.name) [\(.source)/\(.type)] a \(.distance_arcsec)″"' "$TMP/body"
else fail "objects" "HTTP $CODE $(err)"; fi

req "/api/objects?ra=$BARN_RA&dec=$BARN_DEC&radius=300&catalogs=simbad&types=star"
[ "$CODE" = "200" ] && ok "Solo SIMBAD, solo estrellas, 5′ en Barnard → $(j '.objects | length') (más cercana: $(j '.objects[0].name'))" || fail "objects filtros" "HTTP $CODE $(err)"

req "/api/objects?ra=10&dec=5&radius=601"
[ "$CODE" = "400" ] && ok "radius=601 → 400" "$(j .error.message)" || fail "validación radius" "HTTP $CODE"
req "/api/objects?ra=400&dec=5"
[ "$CODE" = "400" ] && ok "ra=400 → 400" "$(j .error.message)" || fail "validación ra" "HTTP $CODE"
req "/api/objects?dec=5"
[ "$CODE" = "400" ] && ok "sin ra → 400" "$(j .error.message)" || fail "validación ra obligatoria" "HTTP $CODE"

# =============================================================================
titulo "Imágenes — /api/frames y /api/coverage  (IRSA SIA v2, SPHEREx DR1)"
# =============================================================================
req "/api/frames?ra=$WISE_RA&dec=$WISE_DEC&detector=6&limit=200"
if [ "$CODE" = "200" ] && [ "$(j .total)" -gt 0 ] 2>/dev/null; then
  cp "$TMP/body" "$TMP/frames.json"
  ok "Frames del detector 6 en WISE 0855 → total $(j .total)" "primero: $(j '.frames[0].id') $(j '.frames[0].date_obs') λ≈$(j '.frames[0].band_um') µm · último: $(j '.frames[-1].date_obs')"
else fail "frames" "HTTP $CODE $(err)"; fi

req "/api/frames?ra=$WISE_RA&dec=$WISE_DEC&limit=5&offset=10&date_from=2025-11-01&date_to=2025-12-31"
[ "$CODE" = "200" ] && ok "Paginación + fechas (nov–dic 2025) → total $(j .total), página de $(j '.frames|length')" || fail "frames filtros" "HTTP $CODE $(err)"

req "/api/coverage?ra=$WISE_RA&dec=$WISE_DEC"
if [ "$CODE" = "200" ]; then
  ok "Cobertura → $(j .total) imágenes de $(j .first_date) a $(j .last_date) en $(j '.weeks|length') semanas"
  jq -r '.weeks[] | "      \(.week_start)  \("█" * ((.count/4)|ceil)) \(.count)"' "$TMP/body" | head -8
else fail "coverage" "HTTP $CODE $(err)"; fi

# =============================================================================
titulo "Análisis — /api/overlay  (Gaia DR3 · AllWISE)"
# =============================================================================
req "/api/overlay?ra=$BARN_RA&dec=$BARN_DEC&fov=300&catalog=gaia&at=2026-06-01"
if [ "$CODE" = "200" ]; then
  ok "Gaia en Barnard → $(j '.sources|length') fuentes propagadas de $(j .catalog_epoch) a $(j .epoch)"
  jq -r '[.sources[] | select(.pm != null and ((.pm.dec // 0) | fabs) > 5000)][0] // empty | "      · \(.id): catálogo (\(.catalog_ra), \(.catalog_dec)) → ahora (\(.ra), \(.dec))"' "$TMP/body"
else fail "overlay gaia" "HTTP $CODE $(err)"; fi

req "/api/overlay?ra=$WISE_RA&dec=$WISE_DEC&fov=300&catalog=wise"
[ "$CODE" = "200" ] && ok "AllWISE en WISE 0855 → $(j '.sources|length') fuentes (época $(j .catalog_epoch) → $(j .epoch))" || fail "overlay wise" "HTTP $CODE $(err)"

req "/api/overlay?ra=$WISE_RA&dec=$WISE_DEC&catalog=hubble"
[ "$CODE" = "400" ] && ok "catalog=hubble → 400" "$(j .error.message)" || fail "validación catalog" "HTTP $CODE"

req "/api/jobs/seq_ffffffffffff"
[ "$CODE" = "404" ] && ok "Trabajo inexistente → 404" "$(j .error.message)" || fail "jobs 404" "HTTP $CODE"

# =============================================================================
titulo "Asteroides — POST /api/asteroids/predict-from-current  (JPL Horizons, observador SPHEREx)"
# =============================================================================
LEO='{"asteroid_id":3000,"current_observation":{"mjd_utc":61280.27835825572,"ra":60.39039387599737,"dec":20.06447024678794},"horizon_days":0.5,"view":{"ra":60.3904,"dec":20.0645,"fov":1200,"size":512}}'
astp() { req "/api/asteroids/predict-from-current" -X POST -H "Content-Type: application/json" -d "$1"; }
astp "$LEO"
if [ "$CODE" = "200" ] && [ "$(j .method)" = "horizons" ]; then
  ok "3000 Leonardo → $(j .forecast_time | cut -c1-16): RA $(j .predicted_ra_deg | cut -c1-9), Dec $(j .predicted_dec_deg | cut -c1-9)" "clic a $(j .current_observation.offset_arcsec)″ de Horizons · $(j .motion.arcsec_per_hour)″/h · $(j '.track|length') puntos de trayectoria · píxel actual ($(j .current_observation.pixel.x), $(j .current_observation.pixel.y_top))"
else fail "predict-from-current Leonardo" "HTTP $CODE $(err)"; fi
astp '{"asteroid_id":3000,"current_observation":{"mjd_utc":61280.27835825572,"ra":61.0,"dec":20.06},"horizon_days":0.5}'
[ "$CODE" = "422" ] && [ "$(j .error.code)" = "identity_mismatch" ] && ok "Clic que no es Leonardo → 422 identity_mismatch" "$(err | cut -c1-110)" || fail "identity_mismatch" "HTTP $CODE"
astp '{"asteroid_id":3000,"current_observation":{"mjd_utc":61280.27,"ra":60.39,"dec":20.06},"horizon_days":31}'
[ "$CODE" = "400" ] && ok "horizon_days > 30 → 400" "$(err | cut -c1-110)" || fail "horizonte" "HTTP $CODE"

# =============================================================================
if [ "$RAPIDO" = "0" ]; then
# =============================================================================
titulo "Imágenes — /api/sequence  (202 + trabajo · recortes FITS de IRSA)"
Q="ra=$WISE_RA&dec=$WISE_DEC&fov=300&size=256"
if run_job "/api/sequence?$Q&detector=6&limit=6&stretch=asinh&cmap=gray"; then
  cp "$TMP/result" "$TMP/seq.json"
  ok "Secuencia → $(r '.frames|length') de $(r .total) frames, $(r .size_px)px, vmin/vmax $(r .vmin | cut -c1-7)/$(r .vmax | cut -c1-7) $(r .unit)"
  jq -r '.frames[] | "      · \(.id)  \(.date_obs[0:10])  λ=\(.band_um) µm  flags=\(.flags|join(",")|if .=="" then "-" else . end)"' "$TMP/result"
  req "/api/sequence?$Q&detector=6&limit=6&stretch=asinh&cmap=gray"
  [ "$CODE" = "200" ] && ok "Misma consulta otra vez → 200 desde caché" || fail "sequence caché" "HTTP $CODE"
else fail "sequence"; fi

titulo "Imágenes — /api/frame/{id}.png"
if [ -f "$TMP/seq.json" ]; then
  i=0
  for url in $(jq -r '.frames[0,-1].png_url' "$TMP/seq.json"); do
    curl -s -m 60 -o "$OUT/frame_$i.png" "$BASE$url"
    png_ok "$OUT/frame_$i.png" 256 && ok "PNG 256×256 → $OUT/frame_$i.png" "${url:0:90}…" || fail "frame png $i"
    i=$((i+1))
  done
  url=$(jq -r '.frames[0].flags_png_url' "$TMP/seq.json")
  curl -s -m 60 -o "$OUT/flags_0.png" "$BASE$url"
  png_ok "$OUT/flags_0.png" 256 && ok "Capa de flags → $OUT/flags_0.png" || fail "flags png"
  fid=$(jq -r '.frames[0].id' "$TMP/seq.json")
  curl -s -m 60 -o "$OUT/frame_viridis.png" "$BASE/api/frame/$fid.png?$Q&stretch=sqrt&cmap=viridis"
  png_ok "$OUT/frame_viridis.png" 256 && ok "Mismo frame con sqrt + viridis → $OUT/frame_viridis.png" || fail "frame png cmap"
fi

titulo "Imágenes — /api/stack  (apilado y resta de épocas)"
if [ -f "$TMP/frames.json" ]; then
  A=$(jq -r '[.frames[] | select(.date_obs >= "2025-10")][:3] | map(.id) | join(",")' "$TMP/frames.json")
  Bb=$(jq -r '[.frames[] | select(.date_obs < "2025-10")][:3] | map(.id) | join(",")' "$TMP/frames.json")
  if run_job "/api/stack?$Q&op=mean&a=$Bb"; then
    curl -s -m 60 -o "$OUT/stack_mean.png" "$BASE$(r .png_url)"
    png_ok "$OUT/stack_mean.png" 256 && ok "mean de $(r .n_frames) frames → $OUT/stack_mean.png" || fail "stack mean png"
  else fail "stack mean"; fi
  if [ -n "$A" ] && [ -n "$Bb" ] && run_job "/api/stack?$Q&op=diff&a=$A&b=$Bb"; then
    curl -s -m 60 -o "$OUT/stack_diff.png" "$BASE$(r .png_url)"
    png_ok "$OUT/stack_diff.png" 256 && ok "diff (época nueva − vieja) → $OUT/stack_diff.png" "escala simétrica ±$(r .vmax | cut -c1-7) MJy/sr: ámbar = apareció/brilló, cian = desapareció" || fail "stack diff png"
  else fail "stack diff"; fi
  req "/api/stack?$Q&op=diff&a=${A%%,*}"
  [ "$CODE" = "400" ] && ok "diff sin b → 400" "$(j .error.message)" || fail "validación diff" "HTTP $CODE"
fi

titulo "Análisis — /api/spectrum  (fotometría en cada exposición)"
if run_job "/api/spectrum?ra=$WISE_RA&dec=$WISE_DEC&aperture=6&date_from=2025-11-01&date_to=2025-11-30"; then
  ok "Espectro de WISE 0855 (nov 2025) → $(r '.points|length') puntos de $(r '.n_exposures') exposiciones" "unidades: $(r .wavelength_unit) / $(r .flux_unit) · partial=$(r .partial)"
  jq -r '.points as $p | ($p|length) as $n | [range(0; $n; ([($n/8|floor), 1] | max))][] | $p[.]
         | "      \(.wavelength) µm  \(.flux|floor) ± \(.error|floor) µJy  D\(.detector)  \(.date_obs[0:10])  \(.flags|join(","))"' "$TMP/result"
else fail "spectrum"; fi

titulo "Análisis — /api/predict  (SIMBAD pm+paralaje · JPL Horizons)"
if run_job "/api/predict?ra=$BARN_RA&dec=$BARN_DEC&name=Barnard%27s%20star&date_from=2025-05-01&date_to=2026-06-01"; then
  ok "Barnard → method=$(r .method), $(r '.positions|length') posiciones" "$(r '.positions[0].date[0:10]'): $(r '.positions[0].ra'), $(r '.positions[0].dec') → $(r '.positions[-1].date[0:10]'): $(r '.positions[-1].ra'), $(r '.positions[-1].dec')"
else fail "predict Barnard"; fi
if run_job "/api/predict?ra=0&dec=0&name=Ceres&date_from=2025-06-01&date_to=2025-07-01"; then
  ok "Ceres → method=$(r .method), $(r '.positions|length') posiciones" "primera: $(r '.positions[0].date[0:10]') $(r '.positions[0].ra'), $(r '.positions[0].dec')"
else fail "predict Ceres"; fi
fi

if [ "$RAPIDO" = "0" ]; then
titulo "L2 completo y cubo all-sky  (pipeline SphereX · M101)"
if run_job "/api/l2?ra=210.80227&dec=54.34895&size=1024"; then
  F=$(r .frame.id)
  ok "L2 $F ($(r .frame.date_obs | cut -c1-10), D$(r .frame.detector)) · $(r .selection.candidates_total) candidatas" "target x=$(r .target.x) y=$(r .target.y) → $(r .target.brightness.value) $(r .unit) a $(r .target.wavelength_um) µm"
  for k in raw color; do
    curl -s -m 120 -o "$OUT/l2_$k.png" -w "%{http_code}" "$BASE/api/l2/$F/$k.png?size=1024" | grep -q 200 \
      && png_ok "$OUT/l2_$k.png" 1024 && ok "L2 $k.png 1024×1024" || fail "L2 $k.png"
  done
  req "/api/l2/$F/pixel?x=$(r .target.x)&y=$(r .target.y)&size=1024"
  [ "$CODE" = "200" ] && ok "Clic L2 → RA $(j .ra | cut -c1-10), Dec $(j .dec | cut -c1-9)" "$(j .brightness.value) MJy/sr (−zodi $(j .brightness.value_minus_zodi), ±$(j .brightness.error)) λ=$(j .wavelength_um) µm flags=$(j '.flags|join(",")')" || fail "clic L2" "HTTP $CODE"
else fail "L2 completo"; fi
if run_job "/api/cube?ra=210.80227&dec=54.34895"; then
  T=$(r .tile.id)
  ok "Cubo $T $(r '.shape|join("×")') ($(r .tile.size_mb) MB) · $(r '.channels|length') canales" "seis: $(r '[.six_channels[]|"\(.channel):\(.wavelength_um*1000|round/1000)µm"]|join(" ")')"
  curl -s -m 60 -o "$OUT/cube_c40.png" -w "%{http_code}" "$BASE/api/cube/$T/channel/40.png" | grep -q 200 && png_ok "$OUT/cube_c40.png" 512 \
    && ok "Canal 40 PNG 512×512" || fail "canal PNG"
  req "/api/cube/$T/channels?set=six"; [ "$(j .count)" = "6" ] && ok "channels?set=six → 6" || fail "six"
  req "/api/cube/$T/pixel?x=$(r .target.x)&y=$(r .target.y)"
  [ "$CODE" = "200" ] && ok "Clic cubo → espectro de $(j '.spectrum|length') canales ($(j .valid_channels) válidos)" "canal 40: $(j '.spectrum[39].brightness') $(j .unit)" || fail "clic cubo"
  curl -s -m 120 -o "$OUT/cube_samples.png" -w "%{http_code}" "$BASE/api/cube/$T/samples.png?ra=210.80227&dec=54.34895" | grep -q 200 && ok "samples.png (6 canales del script)" || fail "samples.png"
else fail "cubo all-sky"; fi
fi

# =============================================================================
if [ "$RATE" = "1" ]; then
titulo "Límite de peticiones (429)"
got=""
for i in $(seq 1 80); do
  req "/api/resolve?name=Barnard%27s%20star"
  [ "$CODE" = "429" ] && { got=$i; break; }
done
[ -n "$got" ] && ok "429 tras $got peticiones seguidas" "Retry-After: $(grep -i '^retry-after' "$TMP/headers" | cut -d' ' -f2 | tr -d '\r') s · $(j .error.message)" || fail "rate limit" "no hubo 429"
fi

# =============================================================================
printf "\n${B}Resultado: ${G}%d correctas${N}${B}, ${R}%d fallidas${N}\n" "$PASS" "$FAIL"
[ "$FAIL" -gt 0 ] && printf "  ${R}Fallaron:${N} %s\n  ${D}(si son 502/504, suele ser una caída temporal de IRSA/SIMBAD: repite en unos minutos)${N}\n" "${FAILED_NAMES[*]}"
[ -d "$OUT" ] && ls "$OUT"/*.png >/dev/null 2>&1 && printf "  Imágenes en: %s\n" "$OUT"
exit $(( FAIL > 0 ))
