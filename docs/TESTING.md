# Pruebas

## Estrategia

- **Pruebas unitarias con `unittest`** (biblioteca estándar; no hace falta pytest).
- **Datos sintéticos** construidos en cada prueba (mensajes `Message`, líneas JSONL en directorios temporales).
  Ninguna prueba usa datos reales de canales ni cuentas.
- **Sin red ni credenciales.** Las llamadas a `yt-dlp` se simulan con `unittest.mock.patch` sobre
  `youtube_channel_collector.subprocess.run` (y `time.sleep` para no esperar). La YouTube Data API nunca se llama;
  la prueba del permiso de raspado limpia `YOUTUBE_RASPADO` y `YOUTUBE_API_KEY` del entorno y verifica que el
  colector termine antes de cualquier solicitud.
- **Qué se prueba:** reglas de puntuación y umbrales, estadística (hipergeométrica, Fisher, BH), reglas lingüísticas
  de postura, insultos y blancos (casos de falso positivo conocidos), contrato de lectura de los tres formatos de
  entrada, inventario e idempotencia del colector, contratos de CLI locales.
- **Qué no se prueba:** descargas reales, cambios de comportamiento de YouTube o `yt-dlp`, calidad de las reglas
  sobre un corpus etiquetado.

## Cómo correr las pruebas

Desde la raíz del repositorio (los módulos se importan desde el directorio de trabajo):

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Si no existe `.venv`, cualquier Python ≥ 3.9 sirve, porque las pruebas solo usan la biblioteca estándar:

```bash
python3 -m unittest discover -s tests -v
```

Una sola suite o una sola prueba:

```bash
.venv/bin/python -m unittest tests.test_logica_pura -v
.venv/bin/python -m unittest tests.test_bot_detector.DetectorTests.test_normalize_text
```

## Resultado actual

Ejecutado el 2026-10-25 en la rama `codex/bot-coordination-analysis`:

| Intérprete | Resultado |
|---|---|
| `.venv` (Python 3.9.6) | **78 pruebas, OK** (≈ 0,03 s) |
| Python 3.12 | **78 pruebas, OK** |

Antes de agregar `tests/test_logica_pura.py` había 52 pruebas, todas en verde. Algunas pruebas de argumentos
inválidos imprimen el mensaje de uso de `argparse` en la salida estándar; es esperado.

## Inventario de suites

| Suite | Pruebas | Cubre |
|---|---|---|
| `test_bot_detector.py` | 5 | Normalización de texto; lectura de repetición cruda con emojis; enlace entre cuentas con plantillas sincronizadas; neutralización de fórmulas en CSV; filtro por catálogo. |
| `test_advanced_analysis.py` | 9 | Entropía en extremos; caso conocido de la hipergeométrica; monotonía de BH; huellas de repetición y de modalidad (chat/comentario); comentarios con precisión diaria fuera de la sincronía; par sincronizado por encima de uno no sincronizado; multivista con dos vistas materiales; sensibilidad por bin. |
| `test_campaign_evidence.py` | 6 | Apoyo directo; crítica; ataque a oposición no es apoyo; apoyo con ataque sigue siendo apoyo; sarcasmo → ambiguo; cobertura con fecha exacta de subida. |
| `test_deep_coordination_analysis.py` | 2 | Canonización de entidades de Gobierno; prioridad de la narrativa de continuidad. |
| `test_text_content_analysis.py` | 15 | Postura a favor y en contra; negaciones; condicionales, hipotéticos y discurso referido; títulos de otras instituciones o países; homónimos; crítica de política no es insulto; atribución del insulto al blanco más cercano; normalización de consignas. |
| `test_youtube_channel_collector.py` | 15 | Ventana inclusiva y predeterminada; precedencia de `upload_date`; slug estable; chat crudo → canónico consumible por el detector; comentario sin falsa precisión de segundos; `parent_id` en respuestas; adopción y salto de archivos inventariados; chats heredados; validación de argumentos; catálogo con fechas aproximadas e hidratación (simuladas); rotación de clientes tras error de recarga (simulada). |
| `test_logica_pura.py` | 26 | Umbrales de `risk_label`; percentiles; elegibilidad de enlaces exactos y tokens informativos; `parse_iso_time_ms`; clústeres solo con enlaces fuertes; CLI `bot_detector analyze` de extremo a extremo en directorio temporal y códigos de error; Fisher; bins logarítmicos; plantillas compartidas; componentes de red; regla de `multivista_fuerte`; concentración (HHI); correcciones humanas; MinHash y Union-Find; regla de alta confianza; distancia entre spans; candidatos a consigna; selección de backend; handle de canal; nombre de artefacto; inventario ante archivo modificado y estado `unavailable`; permiso explícito de raspado. |

## Datos y servicios que necesitan

| Necesidad | Cómo se resuelve en las pruebas |
|---|---|
| Archivos JSONL de chat/comentarios/catálogo | Se escriben en `tempfile.TemporaryDirectory()` |
| `yt-dlp` | `mock.patch("youtube_channel_collector.subprocess.run")` con respuestas sintéticas |
| Esperas entre clientes | `mock.patch("youtube_channel_collector.time.sleep")` |
| YouTube Data API | No se usa; la prueba de permiso corta antes de cualquier llamada |
| Variables de entorno | `mock.patch.dict(os.environ, ..., clear=True)` |

## Brechas conocidas

- **Sin pruebas de extremo a extremo** de `advanced_analysis`, `campaign_evidence`, `deep_coordination_analysis` ni
  `text_content_analysis` como CLI (sí de `bot_detector`).
- `circular_permutation`, `lexical_clusters`, `shared_sequences`, `extract_resources`, `reply_network` y
  `temporal_overlap_evidence` no tienen pruebas directas.
- Las muestras de validación humana (`validation_sample` y la de `text_content_analysis`) no se prueban.
- No se prueba `process_comments_parallel` ni el flujo completo de `main` del colector con descargas simuladas.
- `campaign_evidence.run` indexa `review[0]` al escribir `stance_review.csv`; con un corpus en el que ningún mensaje
  recibe etiqueta distinta de `other` fallaría con `IndexError`. No hay prueba que cubra ese caso.
- La calidad de las reglas lingüísticas solo se verifica con casos puntuales; no hay un conjunto etiquetado para
  medir precisión y exhaustividad.
- No hay CI configurado; las pruebas se corren a mano.
