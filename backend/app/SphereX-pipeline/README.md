# SPHEREx: RA/DEC → L2 y all-sky

Implementación local basada en `ingestion_test.ipynb` y `m101_allsky_102_canales.ipynb`. No necesita ejecutar notebooks. RA y DEC son grados ICRS; RA [0,360), DEC [-90,90].

## Ejecutar

Con el entorno científico que ya existe en SphereX:

```bash
cd /home/arjona/SphereX-pipeline
/run/media/arjona/aplicaciones/SphereX/.venv/bin/python server.py
```

Abrir http://127.0.0.1:8081. El formulario envía `POST /api/jobs` con `{"ra":210.80227,"dec":54.34895}`; consultar `GET /api/jobs/{id}` hasta estado `complete`, `partial` o `failed`. Cada solicitud guarda sus resultados en `outputs/{id}`. El servidor limita a un trabajo en curso; devuelve 409 si está ocupado. La descarga y generación se ejecutan fuera del hilo HTTP.

En un entorno nuevo: `python -m pip install -r requirements.txt`.

También admite CLI:

```bash
python pipeline.py --ra 210.80227 --dec 54.34895 --output outputs/m101
python pipeline.py --ra 210.80227 --dec 54.34895 --stage l2 --output outputs/l2
python pipeline.py --ra 210.80227 --dec 54.34895 --stage allsky --output outputs/cube
```

## Productos

- L2: colección `spherex_dr1`, consulta de 1 arcsec, URLs y metadatos completos. Se selecciona la tercera observación cronológica, como en el notebook (o la última disponible si hay menos de tres). `l2.fits` conserva IMAGE y su cabecera, no todas las extensiones del producto de origen. Se exporta la normalización float32 [0,1], gris invertido y pseudocolor cividis RGB con percentiles 3.5–96.5. Los píxeles inválidos quedan negros en RGB. No se crea una calibración L2 desde datos crudos: se usa el producto L2 publicado por IRSA.
- All-sky: colección `spherex_dr1_cube`; WCS verifica que la coordenada esté dentro de IMAGE, y se exigen 102 planos, BUNIT y longitudes crecientes. `allsky_cube.fits` conserva todas las extensiones originales, incluida calidad. El ECSV incluye las 102 longitudes, bordes y anchos disponibles y el brillo, FLAGS y NHITS del píxel más cercano a la coordenada. `samples.npy` tiene seis planos originales, canales 1,20,40,60,80,102. La figura incluye los seis planos, longitudes y anchos de banda.
- `cube3d.png` dibuja los 102 planos con eje Z espectral; muestreo espacial hasta 32×32 y color común por percentiles. `preview3d.npz` exporta esa malla con sus ejes. Los valores originales permanecen en FITS. Es una vista estática; el explorador WebGL previo permanece separado.
- `manifest.json` informa URLs seleccionadas, formas, coordenada y errores por producto. Si falla un producto se conserva el otro. CLI termina con código 1 ante resultados parciales.

`spectral.py` reutiliza los alias y la conversión de unidades del notebook; asume micrómetros únicamente en columnas espectrales sin unidad FITS. Anchos ausentes quedan NaN en ECSV. No se interpolan canales, no se aplican máscaras de calidad ni se confunden detectores L2 con los 102 canales del cubo. La coordenada localiza una tesela completa, no genera un mosaico entre teselas.

Esta carpeta es autónoma: puede copiarse dentro del proyecto como `pipeline/` y ejecutarse allí. El formulario es adicional al explorador estático de M101; no reemplaza sus datos ni sus teselas precalculadas.

## Rendimiento de NPIX

Los cubos se descargan con cuatro conexiones HTTP por rangos, comprobando los
límites y el tamaño de cada bloque. Si el servidor no soporta rangos o falla un
bloque, se intenta una descarga secuencial. La caché existente de Astropy se
reutiliza; nuevas descargas completas se guardan en `.cache/downloads` por URL.
No se publican archivos parciales. Se conserva una copia exacta del FITS original
en cada resultado, sin reconstruir sus extensiones.

`SPHEREX_DOWNLOAD_WORKERS=1` permite comparar con descarga secuencial; se admiten
entre 1 y 8 conexiones. `SPHEREX_CACHE_DIR` cambia la ubicación de nuevas descargas.
Para comparar descargas frías, usar URLs sin caché previa de Astropy y directorios
de caché vacíos distintos. `manifest.json`, dentro de `allsky`, registra
`download_seconds` (incluye acceso a caché) y `total_seconds` (consulta, descarga,
procesamiento y figuras); la diferencia permite identificar el coste restante.

El objetivo de 120 segundos requiere medir con IRSA y la red reales. Una tesela
de 320.909.760 bytes necesita al menos 2,67 MB/s (21,4 Mbit/s) solo para transferirla;
el proceso completo necesita margen adicional para consultas, L2 y figuras.
La concurrencia no garantiza esa velocidad si el límite es el ancho de banda total.

Medición del 14 de noviembre de 2026: `Npix3039_cube.fits`, 320.909.760 bytes,
cuatro conexiones y ambas cachés omitidas: **46,04 segundos** de descarga.
Es una medición de transferencia, no del trabajo completo ni de otras teselas.

## Pruebas

`python -m unittest discover -s tests -v`

Las pruebas usan FITS sintéticos y simulan IRSA: verifican coordenadas, normalización, WCS, selección de tesela, salidas de ambos productos y errores parciales, sin depender de la disponibilidad del archivo remoto.
