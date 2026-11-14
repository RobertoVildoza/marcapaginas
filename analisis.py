"""Análisis de reseñas con salida estructurada (JSON con esquema declarado).

A diferencia del function calling, acá no hay conversación ni herramientas:
es un pedido propio a Gemini que devuelve datos con una forma fija.
"""

import asyncio
import logging
from typing import Literal

from pydantic import BaseModel, Field

import memoria
from config import GEMINI_MODELO_ANALISIS, SEGUNDOS_MAXIMOS_DEL_MODELO, cliente

# Instrucción propia, distinta de PROMPT_DE_SISTEMA: esto es extracción de datos, no charla.
INSTRUCCION_DE_ANALISIS = """\
Analizás reseñas de libros. Recibís el texto de una reseña y devolvés su análisis
en el formato pedido, usando únicamente lo que dice la reseña, sin agregar opiniones propias.
Los aspectos son frases cortas en español (por ejemplo "ritmo ágil" o "final flojo").
Si la reseña no elogia o no critica nada, dejá esa lista vacía.
El puntaje es la satisfacción de quien escribió la reseña con el libro, de 1 (muy mala) a 5
(excelente), según lo que expresa el texto y no según tu propio gusto.
"""

logger = logging.getLogger("marcapaginas.analisis")


class AnalisisDeResena(BaseModel):
    """Análisis de una reseña de un libro."""  # va como descripción en el esquema JSON

    sentimiento: Literal["favorable", "desfavorable", "mixta"] = Field(
        description="Valoración general de la reseña."
    )
    aspectos_elogiados: list[str] = Field(
        description="Aspectos que la reseña elogia, como textos cortos."
    )
    aspectos_criticados: list[str] = Field(
        description="Aspectos que la reseña critica, como textos cortos."
    )
    sintesis: str = Field(description="Una sola línea que resume el veredicto.")
    puntaje: int = Field(
        ge=1,
        le=5,
        description=(
            "Satisfacción de quien escribió la reseña, de 1 (muy mala) a 5 (excelente)."
        ),
    )


# Formato de salida de la Interactions API. El esquema sale del modelo pydantic tal cual:
# la documentación del SDK ignora minLength, maxLength, minProperties, maxProperties,
# uniqueItems, contains, minContains y maxContains, y rechaza con 400 los esquemas demasiado
# complejos; este usa type, enum, items, properties, required, description, title y, por el
# puntaje, minimum y maximum, sin $defs ni referencias, así que no hubo claves que sacar.
FORMATO_DE_RESPUESTA = {
    "type": "text",
    "mime_type": "application/json",
    "schema": AnalisisDeResena.model_json_schema(),
}


async def analizar_resena(texto: str) -> dict:
    """Analiza el texto de una reseña y devuelve un diccionario.

    Si salió bien: {"estado": "ok", "sentimiento": ..., "aspectos_elogiados": [...],
    "aspectos_criticados": [...], "sintesis": ..., "puntaje": ...}.
    Si falló: {"estado": "error", "mensaje": ...}. No levanta excepción; el error queda en el log.
    """
    try:
        interaccion = await asyncio.wait_for(
            cliente.aio.interactions.create(
                # Clasificación y extracción con esquema fijo: no necesita el modelo más grande,
                # y así no compite con la conversación.
                model=GEMINI_MODELO_ANALISIS,
                system_instruction=INSTRUCCION_DE_ANALISIS,
                input=texto,
                # store=False: no guardamos nada del lado de Google.
                store=False,
                # El mime_type ya va dentro de response_format. NO pasar response_mime_type
                # suelto: aunque la documentación del SDK dice que es obligatorio, la API
                # estable lo rechaza con 400 ("only available in v1beta").
                response_format=FORMATO_DE_RESPUESTA,
                # Sin tools: es una sola llamada de extracción.
            ),
            timeout=SEGUNDOS_MAXIMOS_DEL_MODELO,
        )
        # Ya no hay respuesta.parsed: parseamos y validamos nosotros, en una sola llamada.
        # Validamos aunque el esquema ya viajó al modelo, porque algunas restricciones no se
        # aplican del lado del modelo: si devolviera un puntaje de 9, esto lo rechaza antes
        # de que lo guardemos. Si el JSON no es válido o no cumple el esquema, levanta y cae
        # en el camino de error de abajo.
        analisis = AnalisisDeResena.model_validate_json(interaccion.output_text)
        return {"estado": "ok", **analisis.model_dump()}
    except asyncio.TimeoutError:
        logger.error(
            "Gemini no respondió en %d segundos (análisis): se corta por tiempo",
            SEGUNDOS_MAXIMOS_DEL_MODELO,
        )
        return {
            "estado": "error",
            "mensaje": "El análisis tardó demasiado en responder.",
        }
    except Exception:
        logger.error("Falló el análisis de la reseña", exc_info=True)
        return {
            "estado": "error",
            "mensaje": "No se pudo analizar la reseña en este momento.",
        }


def guardar(chat_id: int, texto: str, resultado: dict) -> None:
    """Guarda en la base un análisis con estado ok. Lo usan el comando y la herramienta.

    Si el guardado falla, queda en el log y no se propaga: el usuario ya tiene su análisis.
    """
    try:
        memoria.guardar_resena(
            chat_id,
            resultado["sentimiento"],
            resultado["puntaje"],
            resultado["aspectos_elogiados"],
            resultado["aspectos_criticados"],
            resultado["sintesis"],
            texto,
        )
    except Exception:
        logger.error("Falló el guardado de la reseña (chat %s)", chat_id, exc_info=True)
