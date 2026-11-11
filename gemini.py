"""Cliente del modelo Gemini (SDK google-genai, versión asíncrona)."""

import asyncio
import logging

from google.genai import types

import herramientas
import memoria
from config import GEMINI_MODELO, SEGUNDOS_MAXIMOS_DEL_MODELO, cliente
from prompts import PROMPT_DE_RECOMENDACION, PROMPT_DE_SISTEMA

logger = logging.getLogger("marcapaginas.gemini")

# Gemini llama "user" y "model" a los roles; en nuestra base son "usuario" y "bot".
ROL_PARA_GEMINI = {"usuario": "user", "bot": "model"}


class ErrorDelModelo(Exception):
    """El modelo no pudo responder (cupo, red, respuesta vacía, etc.)."""


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

    # El historial viene de SQLite; el chat del SDK se siembra con él y vive solo
    # durante este mensaje (el SDK no guarda nada entre mensajes).
    historial_gemini = [
        types.Content(
            role=ROL_PARA_GEMINI[rol], parts=[types.Part.from_text(text=texto)]
        )
        for rol, texto in historial
    ]

    try:
        # cliente.aio es la versión asíncrona del cliente: se espera con await.
        chat = cliente.aio.chats.create(
            model=GEMINI_MODELO,
            history=historial_gemini,
            config=types.GenerateContentConfig(
                system_instruction=PROMPT_DE_SISTEMA,
                # Function calling automático: el SDK llama a nuestras funciones async
                # (con await) cuando el modelo las pide y le devuelve el resultado.
                tools=herramientas.construir_herramientas(chat_id),
            ),
        )
        respuesta = await asyncio.wait_for(
            chat.send_message(texto_usuario), timeout=SEGUNDOS_MAXIMOS_DEL_MODELO
        )

        if not respuesta.text:
            raise ValueError("El modelo devolvió una respuesta vacía")
        return respuesta.text
    except asyncio.TimeoutError as error:
        logger.error(
            "Gemini no respondió en %d segundos (chat %s): se corta por tiempo",
            SEGUNDOS_MAXIMOS_DEL_MODELO, chat_id,
        )
        raise ErrorDelModelo("El modelo tardó demasiado en responder") from error
    except Exception as error:
        logger.error("Falló la llamada a Gemini (chat %s)", chat_id, exc_info=True)
        raise ErrorDelModelo(str(error)) from error


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
        respuesta = await asyncio.wait_for(
            cliente.aio.models.generate_content(
                model=GEMINI_MODELO,
                contents=pedido,
                config=types.GenerateContentConfig(system_instruction=PROMPT_DE_RECOMENDACION),
            ),
            timeout=SEGUNDOS_MAXIMOS_DEL_MODELO,
        )
        if not respuesta.text:
            raise ValueError("El modelo devolvió una respuesta vacía")
        return respuesta.text
    except asyncio.TimeoutError as error:
        logger.error(
            "Gemini no respondió en %d segundos (recomendación): se corta por tiempo",
            SEGUNDOS_MAXIMOS_DEL_MODELO,
        )
        raise ErrorDelModelo("El modelo tardó demasiado en responder") from error
    except Exception as error:
        logger.error("Falló la recomendación con preferencias", exc_info=True)
        raise ErrorDelModelo(str(error)) from error
