"""Análisis de reseñas con salida estructurada (JSON con esquema declarado).

A diferencia del function calling, acá no hay conversación ni herramientas:
es un pedido propio a Gemini que devuelve datos con una forma fija.
"""

import asyncio
import json
import logging
from typing import Literal

from pydantic import BaseModel, Field

from config import GEMINI_MODELO_ANALISIS, SEGUNDOS_MAXIMOS_DEL_MODELO, cliente

# Instrucción propia, distinta de PROMPT_DE_SISTEMA: esto es extracción de datos, no charla.
INSTRUCCION_DE_ANALISIS = """\
Analizás reseñas de libros. Recibís el texto de una reseña y devolvés su análisis
en el formato pedido, usando únicamente lo que dice la reseña, sin agregar opiniones propias.
Los aspectos son frases cortas en español (por ejemplo "ritmo ágil" o "final flojo").
Si la reseña no elogia o no critica nada, dejá esa lista vacía.
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


# Formato de salida de la Interactions API. El esquema sale del modelo pydantic tal cual:
# la documentación del SDK ignora minLength, maxLength, minProperties, maxProperties,
# uniqueItems, contains, minContains y maxContains, y rechaza con 400 los esquemas demasiado
# complejos; este usa solo type, enum, items, properties, required, description y title,
# sin $defs ni referencias, así que no hubo claves que sacar.
FORMATO_DE_RESPUESTA = {
    "type": "text",
    "mime_type": "application/json",
    "schema": AnalisisDeResena.model_json_schema(),
}


async def analizar_resena(texto: str) -> dict:
    """Analiza el texto de una reseña y devuelve un diccionario.

    Si salió bien: {"estado": "ok", "sentimiento": ..., "aspectos_elogiados": [...],
    "aspectos_criticados": [...], "sintesis": ...}.
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
                response_format=FORMATO_DE_RESPUESTA,
                # Según la documentación del SDK, es obligatorio cuando se usa response_format.
                response_mime_type="application/json",
                # Sin tools: es una sola llamada de extracción.
            ),
            timeout=SEGUNDOS_MAXIMOS_DEL_MODELO,
        )
        # Ya no hay respuesta.parsed: parseamos y validamos nosotros. Si el JSON no es válido
        # o no cumple el esquema, levanta y cae en el camino de error de abajo.
        analisis = AnalisisDeResena.model_validate(json.loads(interaccion.output_text or ""))
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
