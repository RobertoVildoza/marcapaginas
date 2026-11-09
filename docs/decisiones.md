# Decisiones de diseño medidas

Tres decisiones que tomamos midiendo en lugar de suponer.

## 1. El nombre del modelo vive en el `.env`

**Qué elegimos:** `GEMINI_MODELO` (y `GEMINI_MODELO_ANALISIS`) se leen del `.env`, con un
valor por defecto en `config.py`. Cambiar de modelo es cambiar una línea del `.env`, sin tocar código.

**Qué medimos:**
- `gemini-2.5-flash`, que usamos al principio, dejó de estar disponible para cuentas nuevas:
  la API respondió 404 con el mensaje "no longer available to new users".
- `gemini-3.1-lite` no existe: la API respondió 404. El nombre real es `gemini-3.1-flash-lite`.

**Por qué:** los nombres de modelo caducan y es fácil equivocarse al escribirlos. Si el nombre
estuviera en el código, cada cambio sería una modificación del programa. Para saber qué nombres
puede usar cada clave se corre `python listar_modelos.py`, que consulta a la API y marca con
`[*]` los que soportan `generateContent`.

## 2. Modelos separados para la conversación y para el análisis

**Qué elegimos:** la conversación usa `GEMINI_MODELO` (por defecto `gemini-3.5-flash`) y el
análisis de reseñas usa `GEMINI_MODELO_ANALISIS` (por defecto `gemini-3.5-flash-lite`).

**Qué medimos:** el 09/11/2026 `gemini-3.5-flash` devolvió 503 por saturación varias veces
seguidas, mientras que `gemini-3.5-flash-lite` respondía en 2 segundos.

**Por qué:** el análisis es clasificación y extracción con un esquema fijo
(sentimiento, aspectos elogiados y criticados, síntesis); no necesita el modelo grande.
Además, así el análisis no compite con la conversación por el mismo modelo ni por su cuota.

## 3. `search.json` con `subject:` en lugar del endpoint `/subjects`

**Qué elegimos:** `recomendar_por_tema` consulta
`https://openlibrary.org/search.json` con `q=subject:"<tema>"`, `limit=5` y sin `sort`.

**Qué medimos:** con `comparar_rutas.py` (en la raíz del repositorio) traje 5 resultados por
cada ruta para tres temas. No usa Gemini, así que no gasta cuota.

| Tema | A) `/subjects/<tema>.json` | B) `search.json` con `subject:` |
|---|---|---|
| science fiction | Alice's Adventures in Wonderland · Frankenstein · The Wonderful Wizard of Oz · Dracula · The Time Machine | Death's End · Roadside Picnic · The Caves of Steel · The Fall of Hyperion · The Foundation Trilogy |
| horror | The Picture of Dorian Gray · Frankenstein · Dracula · The Mayor of Casterbridge · The Jewel of Seven Stars | Misery · The Shining · The Exorcist · Cujo · House of Leaves |
| mystery | The Hound of the Baskervilles · Crimen y castigo · The Adventures of Sherlock Holmes · The Man Who Was Thursday · The Mysterious Affair at Styles | Never Lie · Peril at End House · The Man in the Brown Suit · The Secret of Chimneys · At Bertram's Hotel |

También probamos el parámetro `sort` de `search.json`:

| `sort` | Resultado |
|---|---|
| `rating` | Bien en ciencia ficción (Project Hail Mary, The Martian), pero se sale del género en horror (Maus, The Hobbit) y en mystery (Watchmen, Harry Potter). |
| `readinglog`, `want_to_read`, `already_read` | Libros populares pero fuera de género: Harry Potter en mystery y en ciencia ficción, Twilight y The Hobbit en horror. |
| `editions` | Parecido a la ruta A: clásicos de dominio público. |
| `new` | Fechas imposibles (2027 y hasta 2098), datos mal cargados. |
| (ninguno) | El mejor orden: libros reconocibles como del género. |

**Por qué:** el endpoint `/subjects` devuelve clásicos de dominio público (Alicia en el País de
las Maravillas y Drácula como ciencia ficción) porque prioriza libros con edición digital libre.
`search.json` con `subject:` devolvió Asimov, Strugatski y Simmons, y ningún `sort` mejoró ese
orden por defecto. Los temas de Open Library están en inglés, así que la herramienta le pide al
modelo que traduzca el tema antes de consultar.
