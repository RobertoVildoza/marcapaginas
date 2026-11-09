# Decisiones de diseño medidas

Tres decisiones que tomamos midiendo en lugar de suponer.

## 1. El nombre del modelo vive en el `.env`

**Qué elegimos:** `GEMINI_MODELO` (y `GEMINI_MODELO_ANALISIS`) se leen del `.env`, con un
valor por defecto en `config.py`. Cambiar de modelo es cambiar una línea del `.env`, sin tocar código.

**Qué medimos:**
- `gemini-2.5-flash`, que usamos al principio: el 08/11/2026 Claude lo llamó desde el bot y la API
  respondió 404 con el mensaje "no longer available to new users".
- `gemini-3.1-lite`: Roberto reportó que la API responde 404 con ese nombre (lo vio en su
  `.env`, Claude no lo probó). El 08/11/2026 Claude corrió `listar_modelos.py` con la clave de
  Roberto: devolvió 26 modelos, entre ellos `gemini-3.1-flash-lite`, y ninguno llamado
  `gemini-3.1-lite`.

**Por qué:** los nombres de modelo caducan y es fácil equivocarse al escribirlos. Si el nombre
estuviera en el código, cada cambio sería una modificación del programa. Para saber qué nombres
puede usar cada clave se corre `python listar_modelos.py`, que consulta a la API y marca con
`[*]` los que soportan `generateContent`.

## 2. Modelos separados para la conversación y para el análisis

**Qué elegimos:** la conversación usa `GEMINI_MODELO` (por defecto `gemini-3.5-flash`) y el
análisis de reseñas usa `GEMINI_MODELO_ANALISIS` (por defecto `gemini-3.5-flash-lite`).

**Qué medimos:** estos datos salen de los logs del bot, en corridas en la máquina de Roberto
(no son pruebas de Claude), así que se pueden verificar ahí:
- El 09/11/2026 `gemini-3.5-flash` devolvió 503 a las 10:25, 10:29 y 11:22.
- El 09/11/2026 `gemini-3.5-flash-lite` respondió 200 OK a las 11:20:55, dos segundos después
  del pedido.

Antecedente, medido por Claude el 08/11/2026: `gemini-3.8-flash` dio 503 en cuatro intentos
seguidos y `gemini-3.7-flash` se colgó sin responder.

**Por qué:** el análisis es clasificación y extracción con un esquema fijo
(sentimiento, aspectos elogiados y criticados, síntesis); no necesita el modelo grande.
Además, así el análisis no compite con la conversación por el mismo modelo ni por su cuota.

## 3. `search.json` con `subject:` en lugar del endpoint `/subjects`

**Qué elegimos:** `recomendar_por_tema` consulta
`https://openlibrary.org/search.json` con `q=subject:"<tema>"`, `limit=5` y sin `sort`.

**Qué medimos:** Claude corrió `comparar_rutas.py` (en la raíz del repositorio) el 09/11/2026 y
trajo 5 resultados por cada ruta para tres temas, consultando Open Library en ese momento. Los
resultados pueden cambiar si Open Library cambia sus datos; el script permite repetir la
medición. No usa Gemini, así que no gasta cuota.

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
| `new` | Años futuros (2027 y hasta 2098). |
| (ninguno) | El mejor orden: libros reconocibles como del género. |

**Por qué:** en la medición, el endpoint `/subjects` devolvió clásicos de dominio público
(Alicia en el País de las Maravillas y Drácula como ciencia ficción), mientras que `search.json`
con `subject:` devolvió Asimov, Strugatski y Simmons, y ningún `sort` mejoró ese orden por
defecto. La explicación de por qué `/subjects` favorece esos clásicos (que prioriza libros con
edición digital libre) es una hipótesis nuestra: no la verificamos en la documentación de Open
Library. Por diseño, la herramienta pide el tema en inglés porque los temas que probamos
estaban en inglés.

## 4. Las páginas de los libros salen de Open Library, en aproximado

**Qué asumimos:** que Open Library no devolvía la cantidad de páginas. Lo declaramos como una
limitación conocida y lo escribimos en el prompt ("no sabés la cantidad de páginas"), pero nunca
lo habíamos medido: nuestras consultas simplemente no pedían ese campo.

**Qué medimos:** el 09/11/2026 Claude consultó `search.json` pidiendo el campo
`number_of_pages_median`, con 5 libros por tema en 5 temas (ciencia ficción, terror, misterio,
romance e historia). Vino con valor en 24 de los 25 libros; solo "Married at first sight" vino
sin dato. Ejemplos: Roadside Picnic 170, The Exorcist 400, The Shining 506. Es la mediana entre
ediciones, no el dato de una edición concreta, y puede traer valores raros: un libro de historia
en indonesio dio 94.

**Qué cambió:**
- `buscar_libro` y `recomendar_por_tema` devuelven `paginas` (null si no hay dato).
- La regla 6 del prompt ahora permite mencionar las páginas solo si vienen en el resultado, siempre
  en aproximado ("unas 170 páginas"), a propósito porque son una mediana. Si el campo viene vacío,
  el bot dice que no tiene el dato y nunca lo estima.
- El flujo guiado (`/queleo`) clasifica cada libro como corto (menos de 250 páginas), mediano
  (de 250 a 450) o largo (más de 450) con esos datos reales, y se los pasa al modelo junto con
  la extensión que pidió el usuario. Los umbrales son una elección nuestra, no una medición.
  La clasificación la hace el código; el modelo elige entre los libros y explica por qué encajan.
