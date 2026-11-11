"""Cliente del modelo Gemini (SDK google-genai, versión asíncrona)."""

import asyncio
import logging

import memoria
from config import GEMINI_MODELO, SEGUNDOS_MAXIMOS_DEL_MODELO, cliente
from prompts import PROMPT_DE_RECOMENDACION, PROMPT_DE_SISTEMA

logger = logging.getLogger("marcapaginas.gemini")

# En la base los roles son "usuario" y "bot"; la Interactions API los llama por tipo de paso.
TIPO_DE_PASO = {"usuario": "user_input", "bot": "model_output"}


class ErrorDelModelo(Exception):
    """El modelo no pudo responder (cupo, red, respuesta vacía, etc.).

    `codigo` es el status HTTP del error original (429, 401...) o None si no lo hubo
    (falla de red, tiempo agotado, respuesta vacía).
    """

    def __init__(self, mensaje: str, codigo: int | None = None):
        super().__init__(mensaje)
        self.codigo = codigo


def _paso(tipo: str, texto: str) -> dict:
    return {"type": tipo, "content": [{"type": "text", "text": texto}]}


def armar_entrada(historial: list[tuple[str, str]], mensaje_nuevo: str) -> list[dict]:
    """Convierte las filas del historial (rol, texto) en pasos de la Interactions API.

    Al final agrega el mensaje nuevo del usuario como user_input.
    """
    pasos = [_paso(TIPO_DE_PASO[rol], texto) for rol, texto in historial]
    pasos.append(_paso("user_input", mensaje_nuevo))
    return pasos


def _texto_de(interaccion) -> str:
    texto = interaccion.output_text
    if not texto:
        raise ValueError("El modelo devolvió una respuesta vacía")
    return texto


def _como_error_del_modelo(error: Exception) -> ErrorDelModelo:
    # Una falla de red no trae status_code: por eso getattr con None por defecto.
    return ErrorDelModelo(str(error), getattr(error, "status_code", None))


async def responder(chat_id: int, texto_usuario: str) -> str:
    """Arma el pedido (historial + mensaje nuevo), llama al modelo y devuelve el texto.

    Si algo falla registra el detalle en el log y levanta ErrorDelModelo.
    Qué decirle al usuario en ese caso lo decide quien llama (bot.py).
    """
    try:
        historial = memoria.ultimos_mensajes(chat_id)
    except Exception:
        # Sin memoria el bot sirve, sin modelo no: seguimos con historial vacío.
        logger.error("Falló la lectura del historial (chat %s)", chat_id, exc_info=True)
        historial = []

    try:
        # cliente.aio es la versión asíncrona del cliente: se espera con await.
        interaccion = await asyncio.wait_for(
            cliente.aio.interactions.create(
                model=GEMINI_MODELO,
                system_instruction=PROMPT_DE_SISTEMA,
                input=armar_entrada(historial, texto_usuario),
                # store=False: la memoria la administramos nosotros en SQLite, no Google.
                store=False,
            ),
            timeout=SEGUNDOS_MAXIMOS_DEL_MODELO,
        )
        return _texto_de(interaccion)
    except asyncio.TimeoutError as error:
        logger.error(
            "Gemini no respondió en %d segundos (chat %s): se corta por tiempo",
            SEGUNDOS_MAXIMOS_DEL_MODELO, chat_id,
        )
        raise ErrorDelModelo("El modelo tardó demasiado en responder") from error
    except Exception as error:
        logger.error("Falló la llamada a Gemini (chat %s)", chat_id, exc_info=True)
        raise _como_error_del_modelo(error) from error


def _renglon_de_libro(numero: int, libro: dict) -> str:
    """Un libro de la lista como línea de texto para el pedido al modelo."""
    anio = libro.get("anio") or "año desconocido"
    paginas = libro.get("paginas")
    if paginas:
        # "unas" va acá, en el dato, para que la aproximación llegue siempre al usuario.
        dato_de_paginas = f"unas {paginas} páginas ({libro['extension']})"
    else:
        dato_de_paginas = "sin dato de páginas"
    return f"{numero}. {libro['titulo']} - {libro.get('autor') or 'autor desconocido'}, {anio}, {dato_de_paginas}"


async def recomendar_con_preferencias(
    genero: str, extension: str, animo: str, libros: list[dict], descripcion_de_extension: str
) -> str:
    """Última pieza del flujo guiado: arma el pedido y le pide al modelo que elija y explique.

    `libros` son los resultados de Open Library; cada uno con titulo, autor, anio, paginas y
    extension (corta, mediana o larga, calculada por el código a partir de las páginas).
    El pedido es una sola llamada sin herramientas: el modelo solo puede elegir de la lista.
    Si falla levanta ErrorDelModelo, igual que responder().
    """
    renglones = "\n".join(_renglon_de_libro(n, libro) for n, libro in enumerate(libros, start=1))
    pedido = (
        "Preferencias de la persona:\n"
        f"- Género: {genero}\n"
        f"- Extensión: {extension} ({descripcion_de_extension})\n"
        f"- Estado de ánimo: {animo}\n\n"
        f"Libros disponibles:\n{renglones}"
    )
    try:
        interaccion = await asyncio.wait_for(
            cliente.aio.interactions.create(
                model=GEMINI_MODELO,
                system_instruction=PROMPT_DE_RECOMENDACION,
                input=pedido,
                store=False,  # la memoria es nuestra (SQLite), no de Google
            ),
            timeout=SEGUNDOS_MAXIMOS_DEL_MODELO,
        )
        return _texto_de(interaccion)
    except asyncio.TimeoutError as error:
        logger.error(
            "Gemini no respondió en %d segundos (recomendación): se corta por tiempo",
            SEGUNDOS_MAXIMOS_DEL_MODELO,
        )
        raise ErrorDelModelo("El modelo tardó demasiado en responder") from error
    except Exception as error:
        logger.error("Falló la recomendación con preferencias", exc_info=True)
        raise _como_error_del_modelo(error) from error
