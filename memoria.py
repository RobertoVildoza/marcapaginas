"""Memoria del bot: historial, lista de lectura y flujo guiado, guardados en SQLite.

Todo es por chat_id, así que dos personas distintas no se mezclan.
"""

import json
import sqlite3
from contextlib import closing

from config import RUTA_DB

# Por qué sqlite3 sincrónico no rompe la regla del await: una escritura o lectura
# en un archivo local tarda microsegundos y no es una espera de red ni de terceros.


# Los pasos posibles tienen que coincidir con los de flujo.py. candidatos es un texto JSON con
# los libros que se le ofrecieron a la persona en el paso de confirmación (NULL antes).
_CREAR_FLUJO = """
    CREATE TABLE IF NOT EXISTS {nombre} (
        chat_id     INTEGER PRIMARY KEY,
        paso        TEXT NOT NULL
                    CHECK (paso IN ('genero', 'extension', 'animo', 'confirmacion')),
        genero      TEXT,
        extension   TEXT,
        animo       TEXT,
        iniciado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        candidatos  TEXT
    )
"""


def _migrar_flujo_activo(conexion: sqlite3.Connection) -> None:
    """Pone al día la tabla flujo_activo de una base creada antes del paso 'confirmacion'.

    CREATE TABLE IF NOT EXISTS no toca una tabla que ya existe, y ALTER TABLE ADD COLUMN no
    alcanza: el CHECK de la columna paso, que no admite 'confirmacion', está en la definición
    de la tabla y SQLite no deja modificarlo. Así que, si la definición vieja no lo trae, se
    crea la tabla nueva, se copian las filas (candidatos queda en NULL) y se reemplaza la
    vieja, todo en una transacción. Si ya está al día, no hace nada.
    """
    fila = conexion.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'flujo_activo'"
    ).fetchone()
    if fila is None or ("confirmacion" in fila[0] and "candidatos" in fila[0]):
        return
    conexion.execute("BEGIN")
    try:
        conexion.execute(_CREAR_FLUJO.format(nombre="flujo_activo_nueva"))
        conexion.execute(
            "INSERT INTO flujo_activo_nueva (chat_id, paso, genero, extension, animo, iniciado_en) "
            "SELECT chat_id, paso, genero, extension, animo, iniciado_en FROM flujo_activo"
        )
        conexion.execute("DROP TABLE flujo_activo")
        conexion.execute("ALTER TABLE flujo_activo_nueva RENAME TO flujo_activo")
        conexion.execute("COMMIT")
    except Exception:
        conexion.execute("ROLLBACK")
        raise


def _conectar() -> sqlite3.Connection:
    return sqlite3.connect(RUTA_DB)


def inicializar() -> None:
    """Crea las tablas si todavía no existen."""
    with closing(_conectar()) as conexion, conexion:
        conexion.execute(
            """
            CREATE TABLE IF NOT EXISTS mensajes (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id   INTEGER NOT NULL,
                rol       TEXT NOT NULL CHECK (rol IN ('usuario', 'bot')),
                texto     TEXT NOT NULL,
                creado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conexion.execute(
            "CREATE INDEX IF NOT EXISTS idx_mensajes_chat ON mensajes (chat_id, id)"
        )
        # UNIQUE (chat_id, titulo): la base misma impide el mismo título dos veces en un chat,
        # sin distinguir mayúsculas (COLLATE NOCASE): "Dune" y "dune" son el mismo libro.
        conexion.execute(
            """
            CREATE TABLE IF NOT EXISTS lista_lectura (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                titulo      TEXT NOT NULL COLLATE NOCASE,
                autor      TEXT NOT NULL,
                anio        INTEGER NOT NULL,
                agregado_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (chat_id, titulo)
            )
            """
        )
        # Estado del flujo guiado "¿qué leo ahora?": una fila por chat (PRIMARY KEY), así
        # que empezar de nuevo reemplaza el flujo anterior en vez de apilarlo.
        _migrar_flujo_activo(conexion)
        conexion.execute(_CREAR_FLUJO.format(nombre="flujo_activo"))
        # Reseñas analizadas. Las dos listas de aspectos van como texto JSON (json.dumps):
        # es lo más simple que no se rompe si un aspecto lleva comas o saltos de línea.
        # El CHECK repite el rango del esquema de analisis.py: la base no acepta un puntaje de 9.
        conexion.execute(
            """
            CREATE TABLE IF NOT EXISTS resenas (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id             INTEGER NOT NULL,
                sentimiento         TEXT NOT NULL,
                puntaje             INTEGER NOT NULL CHECK (puntaje BETWEEN 1 AND 5),
                aspectos_elogiados  TEXT NOT NULL,
                aspectos_criticados TEXT NOT NULL,
                sintesis            TEXT NOT NULL,
                texto_resena        TEXT NOT NULL,
                creado_en           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conexion.execute(
            "CREATE INDEX IF NOT EXISTS idx_resenas_chat ON resenas (chat_id, id)"
        )


def guardar_intercambio(chat_id: int, texto_usuario: str, texto_bot: str) -> None:
    """Guarda el mensaje del usuario y la respuesta del bot en una sola transacción:
    se guardan las dos filas o ninguna."""
    with closing(_conectar()) as conexion, conexion:
        conexion.executemany(
            "INSERT INTO mensajes (chat_id, rol, texto) VALUES (?, ?, ?)",
            [(chat_id, "usuario", texto_usuario), (chat_id, "bot", texto_bot)],
        )


def ultimos_mensajes(chat_id: int, limite: int = 20) -> list[tuple[str, str]]:
    """Devuelve los últimos `limite` mensajes del chat como (rol, texto),
    del más viejo al más nuevo."""
    with closing(_conectar()) as conexion:
        filas = conexion.execute(
            "SELECT rol, texto FROM mensajes WHERE chat_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (chat_id, limite),
        ).fetchall()
    # Los pedimos del más nuevo al más viejo (para que LIMIT corte bien) y los damos vuelta.
    return list(reversed(filas))


def borrar_historial(chat_id: int) -> None:
    """Borra todo el historial de un chat (lo usa /olvidar)."""
    with closing(_conectar()) as conexion, conexion:
        conexion.execute("DELETE FROM mensajes WHERE chat_id = ?", (chat_id,))


def agregar_a_lista(chat_id: int, titulo: str, autor: str, anio: int) -> bool:
    """Agrega un libro a la lista de lectura del chat.

    Devuelve True si lo agregó, o False si ese título ya estaba en la lista de ese chat.
    """
    with closing(_conectar()) as conexion, conexion:
        cursor = conexion.execute(
            "INSERT OR IGNORE INTO lista_lectura (chat_id, titulo, autor, anio) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, titulo, autor, anio),
        )
        return cursor.rowcount == 1


def leer_lista(chat_id: int) -> list[tuple[str, str, int]]:
    """Devuelve la lista de lectura del chat como (titulo, autor, anio), en el orden en que se agregó."""
    with closing(_conectar()) as conexion:
        return conexion.execute(
            "SELECT titulo, autor, anio FROM lista_lectura WHERE chat_id = ? ORDER BY id",
            (chat_id,),
        ).fetchall()


def iniciar_flujo(chat_id: int) -> None:
    """Empieza un flujo guiado en el primer paso, reemplazando el que hubiera en ese chat."""
    with closing(_conectar()) as conexion, conexion:
        conexion.execute(
            "INSERT OR REPLACE INTO flujo_activo (chat_id, paso) VALUES (?, 'genero')",
            (chat_id,),
        )


def leer_flujo(chat_id: int, minutos_de_vigencia: int) -> dict | None:
    """Devuelve el flujo activo del chat, o None si no hay.

    El diccionario trae paso, genero, extension, animo, candidatos (lista de libros ofrecidos o None) y "vencido" (True si pasaron más de
    `minutos_de_vigencia` desde que empezó). La comparación la hace SQLite, que guarda
    iniciado_en en UTC con el mismo formato que datetime('now').
    """
    with closing(_conectar()) as conexion:
        fila = conexion.execute(
            "SELECT paso, genero, extension, animo, iniciado_en < datetime('now', ?), candidatos "
            "FROM flujo_activo WHERE chat_id = ?",
            (f"-{minutos_de_vigencia} minutes", chat_id),
        ).fetchone()
    if fila is None:
        return None
    paso, genero, extension, animo, vencido, candidatos = fila
    return {
        "paso": paso, "genero": genero, "extension": extension, "animo": animo,
        "vencido": bool(vencido),
        "candidatos": json.loads(candidatos) if candidatos else None,
    }


def guardar_dato_de_flujo(chat_id: int, campo: str, valor: str | None, paso: str) -> None:
    """Guarda un dato del flujo (genero, extension o animo) y deja el flujo en `paso`."""
    if campo not in ("genero", "extension", "animo"):
        raise ValueError(f"Campo de flujo desconocido: {campo}")
    with closing(_conectar()) as conexion, conexion:
        # `campo` sale de la lista de arriba, nunca del usuario, así que es seguro en el SQL.
        conexion.execute(
            f"UPDATE flujo_activo SET {campo} = ?, paso = ? WHERE chat_id = ?",
            (valor, paso, chat_id),
        )


def pasar_a_confirmacion(chat_id: int, candidatos: list[dict]) -> None:
    """Deja el flujo en el paso 'confirmacion' con los libros que se le ofrecieron a la persona."""
    with closing(_conectar()) as conexion, conexion:
        conexion.execute(
            "UPDATE flujo_activo SET candidatos = ?, paso = 'confirmacion' WHERE chat_id = ?",
            (json.dumps(candidatos, ensure_ascii=False), chat_id),
        )


def borrar_flujo(chat_id: int) -> bool:
    """Borra el flujo del chat. Devuelve True si había uno."""
    with closing(_conectar()) as conexion, conexion:
        cursor = conexion.execute("DELETE FROM flujo_activo WHERE chat_id = ?", (chat_id,))
        return cursor.rowcount == 1


def borrar_flujos_vencidos(minutos_de_vigencia: int) -> int:
    """Borra los flujos de todos los chats que ya vencieron. Devuelve cuántos."""
    with closing(_conectar()) as conexion, conexion:
        cursor = conexion.execute(
            "DELETE FROM flujo_activo WHERE iniciado_en < datetime('now', ?)",
            (f"-{minutos_de_vigencia} minutes",),
        )
        return cursor.rowcount


def guardar_resena(
    chat_id: int,
    sentimiento: str,
    puntaje: int,
    aspectos_elogiados: list[str],
    aspectos_criticados: list[str],
    sintesis: str,
    texto_resena: str,
) -> None:
    """Guarda el análisis de una reseña (ya validado) junto con el texto original."""
    with closing(_conectar()) as conexion, conexion:
        conexion.execute(
            "INSERT INTO resenas (chat_id, sentimiento, puntaje, aspectos_elogiados, "
            "aspectos_criticados, sintesis, texto_resena) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                chat_id, sentimiento, puntaje,
                json.dumps(aspectos_elogiados, ensure_ascii=False),
                json.dumps(aspectos_criticados, ensure_ascii=False),
                sintesis, texto_resena,
            ),
        )


def leer_resenas(chat_id: int) -> list[dict]:
    """Devuelve las reseñas analizadas del chat, de la más vieja a la más nueva.

    Cada una es un diccionario con sentimiento, puntaje, aspectos_elogiados y
    aspectos_criticados (listas), sintesis, texto_resena y creado_en. El promedio de puntaje
    se calcula sobre esta lista.
    """
    with closing(_conectar()) as conexion:
        filas = conexion.execute(
            "SELECT sentimiento, puntaje, aspectos_elogiados, aspectos_criticados, "
            "sintesis, texto_resena, creado_en FROM resenas WHERE chat_id = ? ORDER BY id",
            (chat_id,),
        ).fetchall()
    return [
        {
            "sentimiento": sentimiento, "puntaje": puntaje,
            "aspectos_elogiados": json.loads(elogiados),
            "aspectos_criticados": json.loads(criticados),
            "sintesis": sintesis, "texto_resena": texto, "creado_en": creado_en,
        }
        for sentimiento, puntaje, elogiados, criticados, sintesis, texto, creado_en in filas
    ]
