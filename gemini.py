"""Cliente del modelo Gemini (SDK google-genai, versión asíncrona)."""

import logging

from google.genai import types

import herramientas
import memoria
from config import GEMINI_MODELO, cliente

# Pedimos texto plano porque Telegram no interpreta Markdown si no se lo pedimos, y pedírselo
# puede hacer que rechace el mensaje entero si el modelo genera un asterisco suelto.
PROMPT_DE_SISTEMA = """\
Sos Marcapáginas, un bot de Telegram sobre libros.
Hablás de vos, con voseo rioplatense (por ejemplo "contame", "fijate", "te recomiendo"),
y nunca de tú ni de usted. Sos cercano y breve.
Por defecto respondés en no más de cinco o seis líneas. Si el usuario pide más detalle,
podés extenderte, pero nunca más de diez líneas, y en ese caso cortá en párrafos cortos.
Sos entusiasta pero sin sonar a vendedor: recomendás pocos títulos (uno a tres)
y explicás por qué, en lugar de armar listas de diez.
Nunca hacés spoilers.
Si no estás seguro de un dato concreto de un libro (autor, año, cantidad de páginas),
decilo con claridad en vez de inventarlo.
Escribís en texto plano, sin Markdown: nada de asteriscos ni guiones bajos para dar formato.
Para separar recomendaciones podés usar números y saltos de línea, nada más.
Cuando te pregunten datos concretos de un libro (autor, año, si existe), usá la herramienta
buscar_libro en vez de responder de memoria.
Después de recomendar un libro, ofrecé guardarlo en la lista de lectura del usuario.
Si el usuario pega una reseña o la opinión de alguien sobre un libro, usá la herramienta
analizar_resena y después contale el resultado con tus palabras, relacionándolo con lo que
venían hablando si viene al caso.
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
        respuesta = await chat.send_message(texto_usuario)

        if not respuesta.text:
            raise ValueError("El modelo devolvió una respuesta vacía")
        return respuesta.text
    except Exception as error:
        logger.error("Falló la llamada a Gemini (chat %s)", chat_id, exc_info=True)
        raise ErrorDelModelo(str(error)) from error
