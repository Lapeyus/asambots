# Arquitectura

Este documento describe cómo está construido `asambots`: actores y sistemas externos, componentes, flujos de
datos, secuencias clave y dónde corre cada pieza. Todo lo descrito sale del código versionado.

## 1. Contexto

```mermaid
flowchart TB
    INV["Persona investigadora (opera la CLI y revisa resultados)"]
    subgraph Local["Computadora local"]
        COL["Colector"]
        AN["Analizadores"]
        FS[("Sistema de archivos: datos y resultados")]
    end
    YT["YouTube (páginas públicas de canal y video)"]
    API["YouTube Data API v3"]
    YTDLP["yt-dlp (proceso externo)"]
    CD["chat-downloader (biblioteca opcional)"]

    INV -->|"ejecuta"| COL
    INV -->|"ejecuta"| AN
    COL -->|"subprocess"| YTDLP --> YT
    COL -->|"HTTPS + clave"| API
    COL -.->|"respaldo de chats"| CD -.-> YT
    COL --> FS
    AN --> FS
    FS -->|"CSV, JSON, Markdown, HTML"| INV
```

| Actor / sistema | Rol |
|---|---|
| Persona investigadora | Ejecuta el colector y los analizadores, revisa los informes y completa la validación humana (`human_validation_sample.csv`, `--labels`). |
| YouTube | Fuente de los datos públicos: catálogo del canal, repeticiones de chat y comentarios. |
| YouTube Data API v3 | Backend opcional (`api`) para catálogo (`channels`, `playlistItems`, `videos`) y comentarios (`commentThreads`, `comments`). |
| `yt-dlp` | Proceso externo invocado con `subprocess.run` para catálogo plano, hidratación de fechas, repetición del chat (`--sub-langs live_chat`) y comentarios. |
| `chat-downloader` | Respaldo opcional si `yt-dlp` no logra descargar un chat (se importa solo si está instalado). |

No hay base de datos, servidor web, nube ni tareas programadas. El intercambio entre componentes es por
archivos JSONL/CSV/JSON en disco.

## 2. Componentes

```mermaid
flowchart LR
    subgraph Cosecha
        COL["youtube_channel_collector.py (v1.1.0)"]
    end
    subgraph Base
        BD["bot_detector.py (v0.1.0): Message, iter_messages, normalize_text, find_coordination, analyze, write_csv"]
    end
    subgraph Analisis["Análisis"]
        ADV["advanced_analysis.py (v0.3.0)"]
        CAMP["campaign_evidence.py (v0.1.0)"]
        DEEP["deep_coordination_analysis.py (v0.2.0)"]
        TXT["text_content_analysis.py (v0.2.0)"]
    end

    COL -->|"extract_run_text"| BD
    ADV -->|"analyze, iter_messages, filtro de catálogo"| BD
    CAMP -->|"find_coordination, write_csv"| BD
    CAMP -->|"temporal_synchrony, shared_script_edges, multiview_edges"| ADV
    DEEP -->|"benjamini_hochberg"| ADV
    DEEP -->|"classify_messages, network_view, LABELS"| CAMP
    DEEP --> BD
    TXT --> BD
```

| Componente | Responsabilidad | Funciones clave |
|---|---|---|
| `youtube_channel_collector.py` | Descubrir el catálogo de un canal dentro de una ventana de fechas, descargar chats y comentarios a JSONL canónico y mantener `inventory.json`. | `discover_channel_api`, `discover_channel_ytdlp`, `hydrate_catalog_dates_ytdlp`, `download_chat`, `download_comments_api`, `download_comments_ytdlp`, `process_comments_parallel`, `Inventory`, `import_legacy_chat` |
| `bot_detector.py` | Capa base compartida: lectura de los tres formatos de entrada a `Message`, normalización de texto, enlaces texto-tiempo entre cuentas, `signal_score` por cuenta, clústeres, CSV seguro e informe HTML. | `iter_messages`, `filter_messages_to_catalog`, `normalize_text`, `find_coordination`, `analyze`, `build_clusters`, `write_csv`, `write_html` |
| `advanced_analysis.py` | Huellas por cuenta (entropía, compresión, periodicidad, forma de sesión), sincronía temporal por pares con prueba hipergeométrica + Fisher + Benjamini–Hochberg, plantillas compartidas ponderadas por IDF, red multivista, componentes y sensibilidad por bin. | `account_fingerprints`, `temporal_synchrony`, `shared_script_edges`, `multiview_edges`, `score_fingerprints`, `network_components`, `sensitivity_analysis`, `write_pair_evidence` |
| `campaign_evidence.py` | Reglas de postura frente al Gobierno, correcciones humanas, resumen por cuenta, plantillas de apoyo y redes multivista sobre subconjuntos estricto y amplio. | `classify_text`, `load_overrides`, `classify_messages`, `account_summary`, `network_view`, `coverage`, `concentration` |
| `deep_coordination_analysis.py` | Etiquetas narrativas, variantes léxicas (MinHash con 24 permutaciones, 6 bandas de 4, Jaccard ≥ 0,68), trigramas de narrativas, recursos compartidos, red de respuestas, permutación circular exacta, integración de vistas y grupos de alta confianza. | `narrative_tags`, `lexical_clusters`, `shared_sequences`, `extract_resources`, `reply_network`, `circular_permutation`, `integrate_temporal`, `components`, `political_controls`, `validation_sample` |
| `text_content_analysis.py` | Clasificación multietiqueta por mensaje (postura, temas, insultos, blancos atacados por proximidad), consignas, perfiles por cuenta y sesión, matriz tema × postura y muestra estratificada de validación. | `classify_stance`, `classify_content`, `slogan_key`, `is_slogan_candidate`, `catalog_metadata` |

### Modelo interno común

Todos los analizadores convierten la entrada a `bot_detector.Message` (dataclass inmutable):

| Campo | Significado |
|---|---|
| `session` | `video_id` para chat; `<video_id>__comments` para comentarios (separa ambas fuentes del mismo video). |
| `time_ms` | Chat: desfase dentro de la repetición en milisegundos. Comentarios: época Unix en ms de `published_at`. |
| `author_id`, `author_name` | Identificador del canal del autor y nombre mostrado. |
| `text`, `normalized` | Texto original y normalizado (NFKC, minúsculas, URL → `<url>`, sin puntuación). |
| `source` | `chat` o `comment`. |
| `time_precision` | `exact`, o `day` para comentarios obtenidos con `yt-dlp`. Solo `exact` participa en cadencia y sincronía. |

## 3. Flujos de datos

```mermaid
flowchart TB
    A["Catálogo del canal (API o yt-dlp)"] --> B["Filtro por ventana since..until"]
    B --> C["Hidratación de fecha exacta por video (yt-dlp)"]
    C --> B2["Segundo filtro por ventana"]
    B2 --> D[("catalog.jsonl")]
    D --> E{"¿Transmisión finalizada?"}
    E -->|"sí"| F["Chat: yt-dlp live_chat, rotando clientes; respaldo chat-downloader"]
    E -->|"no"| G["chat = not_applicable o deferred"]
    D --> H["Comentarios + respuestas (API o yt-dlp)"]
    F --> I[("chats/*.jsonl canónico")]
    H --> J[("comments/*.jsonl canónico")]
    F & G & H --> K[("inventory.json: estado, bytes, SHA-256")]
    I & J & D --> L["iter_messages + filtro de catálogo"]
    L --> M["Message[]"]
    M --> N["Analizador elegido"]
    N --> O[("Directorio de resultados")]
```

Formatos de entrada aceptados por `iter_messages` (cualquier `*.jsonl` encontrado recursivamente):

1. **Chat canónico** (`record_type = "chat_message"`), producido por el colector.
2. **Comentario canónico** (`record_type = "comment"`), producido por el colector.
3. **Repetición cruda de YouTube** (`replayChatItemAction`), tal como la exporta `yt-dlp`; la sesión se toma del
   nombre de la carpeta contenedora.

Las líneas de otros tipos (por ejemplo, `catalog.jsonl`) se ignoran sin error; una línea con JSON inválido
detiene el análisis con un mensaje que indica archivo y línea.

## 4. Secuencias clave

### 4.1 Corrida incremental del colector

```mermaid
sequenceDiagram
    autonumber
    actor U as "Investigador"
    participant C as "Colector"
    participant I as "inventory.json"
    participant Y as "yt-dlp"
    participant A as "YouTube Data API"
    U->>C: "python3 youtube_channel_collector.py --limit 10"
    C->>C: "Validar argumentos y YOUTUBE_RASPADO"
    alt "Backend de catálogo = api"
        C->>A: "channels, playlistItems, videos"
    else "Backend de catálogo = ytdlp"
        C->>Y: "--flat-playlist en /videos y /streams"
        C->>Y: "--dump-single-json por candidato (hidratación)"
    end
    C->>C: "Filtrar por ventana y escribir catalog.jsonl (atómico)"
    C->>I: "ensure_video, set_active_video_ids, save"
    loop "Por video hasta --limit"
        C->>I: "should_process(chat)"
        alt "Completo y hash válido"
            I-->>C: "saltar"
        else "Pendiente, error o cambiado"
            C->>Y: "--write-subs live_chat (rota clientes)"
            Y-->>C: "live_chat.json"
            C->>C: "canonical_chat_records y escritura atómica"
            C->>I: "complete o mark(unavailable/error)"
        end
        C->>I: "should_process(comments)"
        C->>A: "commentThreads y comments (o yt-dlp --write-comments)"
        C->>I: "complete o mark y save"
    end
    C-->>U: "Inventario actualizado"
```

### 4.2 Análisis multivista (`advanced_analysis.py`)

```mermaid
sequenceDiagram
    autonumber
    actor U as "Investigador"
    participant AA as "advanced_analysis"
    participant BD as "bot_detector"
    participant FS as "Resultados"
    U->>AA: "advanced_analysis.py datos --output advanced_results"
    AA->>BD: "discover_jsonl, iter_messages, filter_messages_to_catalog"
    AA->>BD: "analyze (enlaces de contenido y cuentas base)"
    AA->>AA: "account_fingerprints"
    AA->>AA: "temporal_synchrony (hipergeométrica, Fisher, BH)"
    AA->>AA: "shared_script_edges (IDF)"
    AA->>AA: "multiview_edges (contenido + tiempo)"
    AA->>AA: "sensitivity_analysis (20, 30, 45, 60 s)"
    AA->>AA: "score_fingerprints y network_components"
    AA->>FS: "CSV, pair_evidence.jsonl, methodology.json, advanced_summary.json, advanced_report.md"
```

### 4.3 Análisis profundo de coordinación

```mermaid
sequenceDiagram
    autonumber
    participant D as "deep_coordination_analysis"
    participant CE as "campaign_evidence"
    participant FS as "Resultados"
    D->>CE: "classify_messages (+ --labels)"
    D->>D: "narrative_tags y semantic_outputs"
    D->>D: "lexical_clusters (MinHash/LSH)"
    D->>D: "shared_sequences, extract_resources, reply_network"
    D->>D: "combine_candidates (hasta --max-candidate-pairs)"
    D->>D: "circular_permutation (distribución exacta) + BH"
    D->>D: "integrate_temporal: regla de alta confianza"
    D->>CE: "network_view por etiqueta (controles políticos)"
    D->>FS: "CSV, candidate_evidence.jsonl, deep_summary.json, deep_report.md"
```

## 5. Modelo de despliegue

| Pieza | Dónde corre | Cómo se inicia |
|---|---|---|
| Colector | Computadora local con Python ≥ 3.9 y `yt-dlp` | A mano, por lotes (`--limit`), relanzando hasta completar el inventario |
| Analizadores | Misma computadora; solo biblioteca estándar | A mano sobre el directorio de datos |
| Datos y resultados | Directorios locales generados (ignorados por git) | Creados por las herramientas |

No se encontró en el repositorio configuración de servicios, contenedores, CI ni tareas programadas.

## 6. Dependencias externas y cuotas

| Dependencia | Uso | Límite / riesgo |
|---|---|---|
| YouTube Data API v3 | Catálogo y comentarios con `YOUTUBE_API_KEY` | Cuota diaria por proyecto de Google Cloud; cada página de `playlistItems`, `videos`, `commentThreads` y `comments` consume cuota. Un error `commentsDisabled` se registra como `unavailable`. |
| `yt-dlp` | Catálogo, fechas, chat y comentarios | Limitación de tasa de YouTube; el código añade `--sleep-requests 1` en comentarios, `--sleep` entre videos (5 s) y `--client-sleep` al rotar clientes (3 s). Cookies opcionales. |
| `chat-downloader` | Respaldo de chat | Opcional; si no está instalado se omite. |

## 7. Decisiones de diseño visibles en el código

- **Sin dependencias en los analizadores.** Todo el análisis estadístico (hipergeométrica en espacio logarítmico,
  Fisher, BH, MinHash) está implementado con la biblioteca estándar para que sea reproducible y auditable.
- **Contenido político fuera del puntaje base.** `bot_detector` no usa listas de actores, postura ni sentimiento;
  la orientación se trata aparte (`campaign_evidence`, `text_content_analysis`) y nunca se interpreta como
  prueba de coordinación.
- **Evitar doble conteo.** Texto sincronizado y plantillas compartidas forman una sola familia de evidencia en
  `multiview_edges`.
- **Precisión temporal explícita.** `time_precision` impide que fechas diarias se usen como horas exactas.
- **Escrituras atómicas e idempotencia.** `atomic_json` y `atomic_jsonl` escriben a un temporal y reemplazan; el
  inventario valida tamaño y SHA-256 antes de saltar trabajo.
- **Consentimiento explícito para raspar.** `YOUTUBE_RASPADO=1` es obligatorio cuando la corrida usaría `yt-dlp`.
- **Seguridad de CSV.** `write_csv` antepone `'` a celdas que empiezan con `=`, `+`, `-` o `@`.
- **Auditabilidad.** Cada analizador materializa la evidencia de los pares (`pair_evidence.jsonl`,
  `temporal_overlap_evidence.csv`, `candidate_evidence.jsonl`) y una muestra para validación humana.
