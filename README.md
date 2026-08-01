# Detector exploratorio de automatización y coordinación

Esta herramienta analiza exportaciones JSONL del chat de repeticiones de YouTube. Genera una lista priorizada de cuentas y enlaces entre cuentas a partir de señales de comportamiento observables.

No declara que una persona sea un bot. Un puntaje alto significa **“vale la pena revisar la evidencia”**, no “automatización comprobada”. Personas muy activas, consignas populares y respuestas a un mismo evento pueden parecer coordinación.

## Uso

Requiere Python 3.9 o posterior y no instala dependencias.

```bash
python3 bot_detector.py analyze chats --output results
```

El directorio de salida contiene:

- `report.html`: informe legible con las primeras 100 cuentas y relaciones.
- `accounts.csv`: todas las cuentas y cada señal que compone la priorización.
- `coordination.csv`: pares de cuentas con evidencia textual-temporal.
- `clusters.csv`: componentes conectados por enlaces fuertes.
- `summary.json`: parámetros, conteos y versión del método.

Para cambiar la ventana de sincronización o la similitud textual:

```bash
python3 bot_detector.py analyze chats --output results \
  --sync-window 90 --near-threshold 0.88
```

## Qué mide

El `signal_score` combina:

- repetición dentro de la cuenta (25%);
- reutilización de los mismos textos en sesiones diferentes (15%);
- intervalos muy cortos y densidad por minuto activo (20%);
- volumen relativo dentro de este corpus (10%);
- mensajes iguales o casi iguales de cuentas distintas dentro de la ventana temporal (30%).

En `accounts.csv`, las columnas terminadas en `_component` muestran cada señal en escala 0–100; `review_reasons` enumera las señales dominantes y `dominant_text` permite comprobar rápidamente si la repetición es real. Las celdas de texto se neutralizan para que contenido del chat no se ejecute como fórmula al abrir un CSV.

La similitud “casi igual” usa Jaccard sobre palabras informativas. La puntuación no contiene listas de actores, partidos, consignas, sentimiento ni posición política. Los mensajes breves y genéricos se excluyen de los enlaces exactos para reducir falsos positivos.

## Lectura responsable

1. Empiece por `report.html`, pero confirme cada hallazgo en los CSV y el chat original.
2. Dé más peso a evidencia repetida en varias sesiones y a varios textos distintivos; una sola frase viral es evidencia débil.
3. Compare cuentas con actividad equivalente. El volumen por sí solo tiene peso limitado.
4. Documente hipótesis alternativas: audiencia organizada, citas del debate, campaña legítima, moderación o una noticia que provoque respuestas simultáneas.
5. Para afirmar automatización harían falta datos adicionales, por ejemplo metadatos públicos de los canales, patrones durante más tiempo y validación manual ciega.

## Pruebas

```bash
python3 -m unittest discover -s tests -v
```

## Análisis avanzado

`advanced_analysis.py` agrega huellas de entropía y compresibilidad, periodicidad, una prueba de sincronización temporal por pares con corrección Benjamini–Hochberg, plantillas compartidas ponderadas por rareza y una red multivista. Se ejecuta sin dependencias externas:

```bash
python3 advanced_analysis.py chats --output advanced_results
```

Produce:

- `advanced_report.md`: resumen regenerado en cada ejecución;
- `account_fingerprints.csv`: huellas y componentes de cada cuenta;
- `most_active_users.csv`: cuentas ordenadas por volumen total, separando chat y comentarios;
- `temporal_coordination.csv`: sincronización, efecto, p-value y q-value;
- `shared_scripts.csv`: plantillas largas compartidas entre cuentas;
- `multiview_edges.csv`: integración de evidencia textual, temporal y de plantillas;
- `pair_evidence.jsonl`: mensajes y tiempos originales para auditar los principales pares;
- `network_components.csv`: grupos sostenidos por al menos dos vistas;
- `methodology.json`: método, parámetros, limitaciones y bibliografía con URL;
- `advanced_summary.json`: conteos y configuración exacta de la ejecución.
- `sensitivity.csv` y `sensitivity_edges.csv`: estabilidad al usar bins de 20, 30, 45 y 60 segundos.

La prueba de sensibilidad se ejecuta de forma predeterminada. Para una corrida más rápida puede limitarla al bin principal:

```bash
python3 advanced_analysis.py chats --output advanced_results --sensitivity-bins 30
```

El modelo nulo temporal trabaja por sesión y únicamente sobre intervalos donde hubo actividad en el chat. Esto reduce el efecto de recesos, pero no elimina el posible falso positivo de varias personas reaccionando orgánicamente al mismo momento del debate. Texto sincronizado y plantillas compartidas se consideran una sola familia de contenido, no dos señales independientes. La categoría `multivista_fuerte` exige además sincronización recurrente en al menos dos sesiones.

## Limitaciones actuales

- La coordinación se infiere únicamente por proximidad temporal y similitud textual.
- No se consultan perfiles de YouTube ni información externa.
- No distingue una herramienta de publicación de una persona que copia y pega manualmente.
- Los umbrales son transparentes y ajustables, pero deben validarse con ejemplos etiquetados antes de usarse para decisiones de impacto.

## Cosecha del canal de Presidencia

`youtube_channel_collector.py` inventaría y descarga contenidos públicos de `@PresidenciadelaRepublicaCR` publicados desde el **1 de mayo de 2026 hasta la fecha de ejecución**, ambas fechas inclusive. Produce chats canónicos de transmisiones finalizadas y comentarios con todas sus respuestas.

La opción recomendada usa la API oficial para catálogo y comentarios, y `yt-dlp` únicamente para la repetición del chat:

```bash
export YOUTUBE_API_KEY="su_clave"
export YOUTUBE_RASPADO=1
python3 youtube_channel_collector.py --mode all --limit 10
```

Sin clave API, `yt-dlp` puede encargarse también del catálogo y comentarios:

```bash
python3 -m pip install -U yt-dlp
export YOUTUBE_RASPADO=1
python3 youtube_channel_collector.py \
  --catalog-backend ytdlp --comments-backend ytdlp --limit 10
```

Salidas en `presidencia_data/`:

- `catalog.jsonl`: videos dentro de la ventana temporal;
- `inventory.json`: estado independiente de chat y comentarios por video, intentos, ruta, conteo, bytes y SHA-256;
- `chats/*.jsonl`: un mensaje canónico por línea, compatible con `bot_detector.py` y `advanced_analysis.py`;
- `comments/*.jsonl`: comentarios superiores y todas las respuestas, un registro por línea.

El inventario evita duplicados. Antes de saltar un archivo completo comprueba que exista y coincidan tamaño y SHA-256. También adopta archivos preexistentes que todavía no estén inventariados. Los estados `unavailable` y `not_applicable` no se vuelven a solicitar salvo que se use `--retry-unavailable`; los errores transitorios sí se reintentan en otra corrida.

En modo `yt-dlp`, el catálogo activa `youtubetab:approximate_date` porque las entradas `--flat-playlist` normales suelen devolver `timestamp: null`. Si todas las fechas vuelven a faltar, el programa termina con error y conserva el catálogo anterior en vez de reemplazarlo silenciosamente por uno vacío.

Después del descubrimiento plano, el colector hidrata los candidatos uno por uno para obtener su fecha exacta antes de aplicar la ventana. Esto evita que videos de abril redondeados por YouTube a `2026-05-01` entren como falsos positivos. `--approximate-catalog-dates` omite esta verificación únicamente cuando se prefiera velocidad sobre precisión.

Si quedaron artefactos descargados antes de corregir una fecha aproximada, no se borran. Al recibir la raíz `presidencia_data`, ambos analizadores usan `catalog.jsonl` como lista permitida y excluyen automáticamente esos archivos fuera de ventana; el resumen registra cuántos mensajes fueron descartados.

Tanto chat como comentarios rotan clientes de YouTube (`web/ios`, `android`, `tv`, `mweb`, `web_embedded`). Esto permite recuperarse del error transitorio `The page needs to be reloaded` sin volver a descargar artefactos que el inventario ya marcó completos.

Para inspeccionar el catálogo sin descargar artefactos:

```bash
python3 youtube_channel_collector.py --mode catalog --dry-run
```

Para analizar únicamente los chats descargados:

```bash
python3 advanced_analysis.py presidencia_data/chats --output presidencia_analysis
```

Para combinar chats, comentarios y respuestas en un solo corpus reproducible:

```bash
python3 advanced_analysis.py presidencia_data --output presidencia_analysis
```

Los comentarios participan en las señales de texto, repetición, plantillas compartidas y actividad entre videos. Cuando fueron obtenidos con `yt-dlp`, su fecha tiene precisión diaria; por seguridad metodológica no se usa como si fuera una hora exacta para medir cadencia o sincronización de segundos. Los chats sí conservan esa dimensión temporal.

## Hipótesis de campaña progobierno

`campaign_evidence.py` separa orientación y coordinación. Ejecuta una prueba estricta sobre apoyo directo al Gobierno y otra más amplia que agrega ataques a oposición o instituciones, sin asumir que estos últimos equivalgan a apoyo:

```bash
python3 campaign_evidence.py presidencia_data --output campaign_analysis
```

Las etiquetas políticas iniciales provienen de reglas auditables y pueden confundir ironía, citas o lenguaje ambiguo. Revise `campaign_analysis/stance_review.csv`; para corregirlas, prepare un CSV con columnas `normalized_text,label` y vuelva a ejecutar:

```bash
python3 campaign_evidence.py presidencia_data --output campaign_analysis \
  --labels etiquetas_revisadas.csv
```

Las etiquetas aceptadas son `direct_support`, `ambiguous_support`, `possible_criticism`, `opposition_or_institution_attack` y `other`. El informe nunca interpreta afinidad política por sí sola como prueba de coordinación.

## Análisis profundo de coincidencias

`deep_coordination_analysis.py` ejecuta conjuntamente familias narrativas, variantes léxicas con MinHash/LSH, secuencias, red cuenta–narrativa–sesión, URLs/hashtags/emojis compartidos, red de respuestas, controles políticos, permutaciones temporales y grupos de alta confianza:

```bash
python3 deep_coordination_analysis.py presidencia_data --output deep_analysis
```

La regla de alta confianza exige sincronía significativa bajo la distribución exacta de todos los desplazamientos circulares, otra vista independiente y recurrencia en al menos tres sesiones. `human_validation_sample.csv` contiene una muestra estratificada para validar manualmente las etiquetas políticas antes de publicar conclusiones.

Los bins que sostienen cualquier resultado temporal con `q <= 0,05` se materializan en `temporal_overlap_evidence.csv`, con tiempos y textos de ambas cuentas para distinguir coordinación de una reacción orgánica al mismo evento. `deep_summary.json` también declara si la red de respuestas estaba disponible; si el descargador no entregó `parent_id`, un resultado de cero relaciones se reporta como dato no disponible y no como ausencia de interacción.
