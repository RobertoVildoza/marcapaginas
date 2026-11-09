"""Memoria del bot: historial de conversación guardado en SQLite.

El historial es por chat_id, así que dos personas distintas no se mezclan.
"""

import sqlite3
from contextlib import closing

from config import RUTA_DB

# Por qué sqlite3 sincrónico no rompe la regla del await: una escritura o lectura
# en un archivo local tarda microsegundos y no es una espera de red ni de terceros.


def _conectar() -> sqlite3.Connection:
    return sqlite3.connect(RUTA_DB)


def inicializar() -> None:
    """Crea la tabla de mensajes si todavía no existe."""
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
