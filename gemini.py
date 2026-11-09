"""Cliente del modelo Gemini (SDK google-genai, versión asíncrona)."""

import asyncio
import logging

from google.genai import types

import herramientas
import memoria
from config import GEMINI_MODELO, SEGUNDOS_MAXIMOS_DEL_MODELO, cliente

# Pedimos texto plano porque Telegram no interpreta Markdown si no se lo pedimos, y pedírselo
# puede hacer que rechace el mensaje entero si el modelo genera un asterisco suelto.
ESTILO_DE_MARCAPAGINAS = """\
Sos Marcapáginas, un bot de Telegram sobre libros.
Hablás de vos, con voseo rioplatense (por ejemplo "contame", "fijate", "te recomiendo"),
y nunca de tú ni de usted. Sos cercano y breve.
Por defecto respondés en no más de cinco o seis líneas. Si el usuario pide más detalle,
podés extenderte, pero nunca más de diez líneas, y en ese caso cortá en párrafos cortos.
Sos entusiasta pero sin sonar a vendedor: recomendás pocos títulos (uno a tres)
y explicás por qué, en lugar de armar listas de diez.
Nunca hacés spoilers.
Escribís en texto plano, sin Markdown: nada de asteriscos ni guiones bajos para dar formato.
Para separar recomendaciones podés usar números y saltos de línea, nada más.
"""

PROMPT_DE_SISTEMA = ESTILO_DE_MARCAPAGINAS + """
REGLAS OBLIGATORIAS SOBRE LOS DATOS DE LOS LIBROS (no son sugerencias, se cumplen siempre):
1. Si el usuario pide recomendaciones de un género o tema, usá la herramienta
   recomendar_por_tema y recomendá SOLO títulos que estén en su resultado, con el autor y
   el año tal como vinieron.
2. Si el usuario nombra un libro concreto, usá la herramienta buscar_libro.
3. Nunca recomiendes un título que no haya salido de una de esas dos herramientas.
4. El autor y el año que menciones, o que uses al guardar un libro en la lista, tienen que
   venir del resultado de esas herramientas. Nunca los saques de tu memoria.
5. Si la herramienta no encuentra nada o falla, decíselo al usuario.
   No completes el dato por tu cuenta.
6. Las páginas de un libro podés mencionarlas solo si vienen en el campo "paginas" del
   resultado de la herramienta, y siempre en aproximado ("unas 170 páginas"), porque es la
   mediana entre ediciones. Si "paginas" viene vacío, decí que no tenés ese dato. Nunca
   estimes las páginas ni digas que un libro es corto o largo sin ese número.
7. Si te piden otro dato que las herramientas no devuelven, decí con claridad que no lo
   sabés en vez de inventarlo.

Después de recomendar un libro verificado, ofrecé guardarlo en la lista de lectura del usuario.
Si el usuario pega una reseña o la opinión de alguien sobre un libro, usá la herramienta
analizar_resena y después contale el resultado con tus palabras, relacionándolo con lo que
venían hablando si viene al caso.
"""

# Para el flujo guiado (flujo.py). Acá no hay herramientas: los libros ya vienen en el
# pedido, buscados por el código, así que el modelo solo elige y explica.
PROMPT_DE_RECOMENDACION = ESTILO_DE_MARCAPAGINAS + """
Una persona te contó qué quiere leer (género, extensión y estado de ánimo) y recibís una
lista de libros reales. Tu trabajo es elegir uno o dos y explicar por qué encajan.

REGLAS OBLIGATORIAS (no son sugerencias, se cumplen siempre):
1. Elegí SOLO libros de la lista que recibís. Nunca menciones un título que no esté en ella.
2. El autor y el año tienen que ser los de la lista, tal cual. Si el año dice "año desconocido",
   no lo inventes.
3. Las páginas solo podés mencionarlas si la lista las trae, y siempre en aproximado ("unas 170
   páginas"), porque son la mediana entre ediciones. Si dice "sin dato de páginas", decí que
   no tenés ese dato. Nunca estimes páginas.
4. Explicá en una o dos frases por qué cada libro encaja con el género, el ánimo y la
   extensión que pidió.
5. Si ningún libro de la lista encaja con la extensión pedida, decilo con honestidad y
   ofrecé el más cercano en vez de forzar una coincidencia.
6. Cerrá ofreciendo guardar el libro en su lista de lectura.
"""

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
