"""Análisis de reseñas con salida estructurada (JSON con esquema declarado).

A diferencia del function calling, acá no hay conversación ni herramientas:
es un pedido propio a Gemini que devuelve datos con una forma fija.
"""

import logging
from typing import Literal

from google.genai import types
from pydantic import BaseModel, Field

from config import GEMINI_MODELO_ANALISIS, cliente

# Instrucción propia, distinta de PROMPT_DE_SISTEMA: esto es extracción de datos, no charla.
INSTRUCCION_DE_ANALISIS = """\
Analizás reseñas de libros. Recibís el texto de una reseña y devolvés su análisis
en el formato pedido, usando únicamente lo que dice la reseña, sin agregar opiniones propias.
Los aspectos son frases cortas en español (por ejemplo "ritmo ágil" o "final flojo").
Si la reseña no elogia o no critica nada, dejá esa lista vacía.
"""

logger = logging.getLogger("marcapaginas.analisis")


class AnalisisDeResena(BaseModel):
    """Esquema de la salida. El SDK lo convierte en el response_schema del pedido."""

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


async def analizar_resena(texto: str) -> dict:
    """Analiza el texto de una reseña y devuelve un diccionario.

    Si salió bien: {"estado": "ok", "sentimiento": ..., "aspectos_elogiados": [...],
    "aspectos_criticados": [...], "sintesis": ...}.
    Si falló: {"estado": "error", "mensaje": ...}. No levanta excepción; el error queda en el log.
    """
    try:
        respuesta = await cliente.aio.models.generate_content(
            # Clasificación y extracción con esquema fijo: no necesita el modelo más grande,
            # y así no compite con la conversación.
            model=GEMINI_MODELO_ANALISIS,
            contents=texto,
            config=types.GenerateContentConfig(
                system_instruction=INSTRUCCION_DE_ANALISIS,
                response_mime_type="application/json",
                response_schema=AnalisisDeResena,
                # Sin tools: no se pueden combinar con response_schema.
            ),
        )
        analisis = respuesta.parsed
        if not isinstance(analisis, AnalisisDeResena):
            raise ValueError("El modelo no devolvió un JSON que respete el esquema")
        return {"estado": "ok", **analisis.model_dump()}
    except Exception:
        logger.error("Falló el análisis de la reseña", exc_info=True)
        return {
            "estado": "error",
            "mensaje": "No se pudo analizar la reseña en este momento.",
        }
