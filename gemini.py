"""Cliente del modelo Gemini (SDK google-genai, versión asíncrona)."""

import asyncio
import json
import logging

from pydantic import BaseModel, Field

import herramientas
import memoria
from config import GEMINI_MODELO, SEGUNDOS_MAXIMOS_DEL_MODELO, cliente
from prompts import PROMPT_DE_RECOMENDACION, PROMPT_DE_SISTEMA

logger = logging.getLogger("marcapaginas.gemini")

# En la base los roles son "usuario" y "bot"; la Interactions API los llama por tipo de paso.
TIPO_DE_PASO = {"usuario": "user_input", "bot": "model_output"}

# Tope de idas y vueltas con el modelo por mensaje del usuario (cada vuelta que pide funciones
# cuenta una). Evita un bucle infinito si el modelo siguiera pidiendo herramientas.
MAXIMO_DE_VUELTAS = 5


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

    declaraciones, funciones = herramientas.construir_herramientas(chat_id)
    try:
        # El tope de tiempo envuelve el ciclo entero (todas las vueltas y las herramientas),
        # no cada llamada: así el usuario espera como máximo SEGUNDOS_MAXIMOS_DEL_MODELO.
        return await asyncio.wait_for(
            _ciclo_de_herramientas(
                chat_id, armar_entrada(historial, texto_usuario), declaraciones, funciones
            ),
            timeout=SEGUNDOS_MAXIMOS_DEL_MODELO,
        )
    except asyncio.TimeoutError as error:
        logger.error(
            "Gemini no respondió en %d segundos (chat %s): se corta por tiempo",
            SEGUNDOS_MAXIMOS_DEL_MODELO, chat_id,
        )
        raise ErrorDelModelo("El modelo tardó demasiado en responder") from error
    except ErrorDelModelo:
        raise  # ya registrado donde se levantó
    except Exception as error:
        logger.error("Falló la llamada a Gemini (chat %s)", chat_id, exc_info=True)
        raise _como_error_del_modelo(error) from error


def _resultado_en_json(resultado) -> str:
    return json.dumps(resultado, ensure_ascii=False, default=str)


async def _ejecutar_llamado(paso, funciones: dict) -> dict:
    """Ejecuta un function_call y devuelve el paso function_result que corresponde.

    Nunca levanta: si la función no existe o falla, el modelo recibe un resultado de error y
    puede explicárselo al usuario.
    """
    resultado = {"type": "function_result", "call_id": paso.id, "name": paso.name}
    funcion = funciones.get(paso.name)
    if funcion is None:
        logger.error("El modelo pidió una herramienta que no existe: %r", paso.name)
        resultado["is_error"] = True
        resultado["result"] = _resultado_en_json(
            {"estado": "error", "mensaje": f"La herramienta {paso.name} no existe."}
        )
        return resultado
    try:
        resultado["result"] = _resultado_en_json(await funcion(**(paso.arguments or {})))
    except Exception:
        logger.error("Falló la herramienta %s", paso.name, exc_info=True)
        resultado["is_error"] = True
        resultado["result"] = _resultado_en_json(
            {"estado": "error", "mensaje": f"La herramienta {paso.name} falló."}
        )
    return resultado


async def _ciclo_de_herramientas(
    chat_id: int, entrada: list, declaraciones: list[dict], funciones: dict
) -> str:
    """Llama al modelo y, mientras pida funciones, las ejecuta y le devuelve los resultados.

    Con store=False no hay previous_interaction_id: en cada vuelta reenviamos el input completo.
    Lo verificado (prueba_herramientas.py) es que los pasos que devuelve el modelo hay que
    reenviarlos tal cual vinieron, con el thought y su signature; armarlos a mano da 400.
    """
    for vuelta in range(1, MAXIMO_DE_VUELTAS + 1):
        interaccion = await cliente.aio.interactions.create(
            model=GEMINI_MODELO,
            system_instruction=PROMPT_DE_SISTEMA,
            input=entrada,
            tools=declaraciones,
            # store=False: la memoria la administramos nosotros en SQLite, no Google.
            store=False,
        )
        if interaccion.status != "requires_action":
            return _texto_de(interaccion)

        llamados = [paso for paso in interaccion.steps if paso.type == "function_call"]
        if not llamados:
            raise ValueError("El modelo pidió una acción pero no trae ninguna función")
        logger.info(
            "Vuelta %d (chat %s): el modelo pide %s",
            vuelta, chat_id, ", ".join(paso.name for paso in llamados),
        )
        # Los pasos del modelo vuelven al input sin reconstruirlos.
        entrada.extend(
            paso.model_dump(mode="json", exclude_none=True, by_alias=True)
            for paso in interaccion.steps
        )
        # Puede pedir varias funciones en la misma vuelta: un function_result por cada una.
        entrada.extend([await _ejecutar_llamado(paso, funciones) for paso in llamados])

    logger.error(
        "El modelo siguió pidiendo funciones después de %d vueltas (chat %s)",
        MAXIMO_DE_VUELTAS, chat_id,
    )
    raise ErrorDelModelo(f"El modelo no terminó después de {MAXIMO_DE_VUELTAS} vueltas")


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


# Cuántos libros como máximo se le ofrecen a la persona, aunque el modelo devuelva más.
MAXIMO_DE_ELEGIDOS = 2


class EleccionDeLibro(BaseModel):
    """Un libro elegido de la lista, con el motivo."""

    # Se devuelve el NÚMERO y no los datos del libro: el título, el autor, el año y las
    # páginas los toma el código de la lista que armó él mismo. Así el modelo no puede alterar
    # ni inventar un dato que ya tenemos, igual que con el chat_id de las herramientas.
    numero: int = Field(
        description="Posición del libro elegido en la lista recibida, empezando en 1."
    )
    porque: str = Field(
        description="Una o dos frases que explican por qué encaja con lo que pidió la persona."
    )


class Recomendacion(BaseModel):
    """Recomendación del flujo guiado: una introducción y uno o dos libros elegidos."""

    introduccion: str = Field(description="Una línea de introducción, en el tono de Marcapáginas.")
    elegidos: list[EleccionDeLibro] = Field(
        description="Uno o dos libros elegidos de la lista recibida."
    )


def _sin_referencias(esquema: dict) -> dict:
    """Devuelve el esquema con los $ref reemplazados por la definición que nombran.

    pydantic saca las clases anidadas a $defs y las cita con $ref; el esquema que mandamos al
    modelo tiene que ser autocontenido y simple (ver analisis.py), así que lo aplanamos acá.
    """
    definiciones = esquema.get("$defs", {})

    def resolver(nodo):
        if isinstance(nodo, dict):
            if "$ref" in nodo:
                return resolver(definiciones[nodo["$ref"].rsplit("/", 1)[-1]])
            return {clave: resolver(valor) for clave, valor in nodo.items() if clave != "$defs"}
        if isinstance(nodo, list):
            return [resolver(item) for item in nodo]
        return nodo

    return resolver(esquema)


FORMATO_DE_RECOMENDACION = {
    "type": "text",
    "mime_type": "application/json",
    "schema": _sin_referencias(Recomendacion.model_json_schema()),
}


async def recomendar_con_preferencias(
    genero: str, extension: str, animo: str, libros: list[dict], descripcion_de_extension: str
) -> Recomendacion:
    """Última pieza del flujo guiado: arma el pedido y le pide al modelo que elija y explique.

    `libros` son los resultados de Open Library; cada uno con titulo, autor, anio, paginas y
    extension (corta, mediana o larga, calculada por el código a partir de las páginas).
    Devuelve una Recomendacion ya validada, solo con elegidos cuyo número existe en `libros`
    (sin repetidos y hasta MAXIMO_DE_ELEGIDOS); el que arma el mensaje busca cada número en
    su lista. Si falla, o si no queda ningún elegido válido, levanta ErrorDelModelo.
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
        # Sin tools: esta llamada puede usar response_format sin problema.
        interaccion = await asyncio.wait_for(
            cliente.aio.interactions.create(
                model=GEMINI_MODELO,
                system_instruction=PROMPT_DE_RECOMENDACION,
                input=pedido,
                store=False,  # la memoria es nuestra (SQLite), no de Google
                response_format=FORMATO_DE_RECOMENDACION,
            ),
            timeout=SEGUNDOS_MAXIMOS_DEL_MODELO,
        )
        # Validamos del lado nuestro aunque el esquema ya viajó: no todo se aplica en el modelo.
        recomendacion = Recomendacion.model_validate_json(interaccion.output_text)
    except asyncio.TimeoutError as error:
        logger.error(
            "Gemini no respondió en %d segundos (recomendación): se corta por tiempo",
            SEGUNDOS_MAXIMOS_DEL_MODELO,
        )
        raise ErrorDelModelo("El modelo tardó demasiado en responder") from error
    except Exception as error:
        logger.error("Falló la recomendación con preferencias", exc_info=True)
        raise _como_error_del_modelo(error) from error

    validos, vistos = [], set()
    for eleccion in recomendacion.elegidos:
        if not 1 <= eleccion.numero <= len(libros):
            logger.warning(
                "La recomendación trae un número fuera de rango (%d de %d): se descarta",
                eleccion.numero, len(libros),
            )
        elif eleccion.numero not in vistos:
            vistos.add(eleccion.numero)
            validos.append(eleccion)
    if not validos:
        logger.error("La recomendación no trae ningún libro válido de la lista")
        raise ErrorDelModelo("El modelo no eligió ningún libro de la lista")
    recomendacion.elegidos = validos[:MAXIMO_DE_ELEGIDOS]
    return recomendacion
