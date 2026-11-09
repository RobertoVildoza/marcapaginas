"""Herramientas que Gemini puede llamar (function calling).

El SDK arma la descripción de cada herramienta leyendo su docstring y sus tipos:
por eso los docstrings de abajo son la interfaz con el modelo, no un simple comentario.
"""

import logging
import unicodedata

import httpx

import analisis
import memoria

logger = logging.getLogger("marcapaginas.herramientas")

URL_OPEN_LIBRARY = "https://openlibrary.org/search.json"

# La documentación de Open Library pide que los clientes se identifiquen.
USER_AGENT = "Marcapaginas/1.0 (proyecto academico ITU-UNCUYO)"


def _es_alfabeto_latino(texto: str) -> bool:
    """True si todas las letras del texto son del alfabeto latino (con o sin tildes)."""
    return all("LATIN" in unicodedata.name(c, "") for c in texto if c.isalpha())


def _elegir_autor(nombres: list[str]) -> str | None:
    """Elige un solo nombre de autor de la lista de author_name.

    Open Library suele traer varias grafías del mismo autor; elegimos la primera en alfabeto
    latino para que no aparezca un autor en chino o en cirílico en el chat.
    Si ninguna lo es, usamos la primera que venga.
    """
    for nombre in nombres:
        if _es_alfabeto_latino(nombre):
            return nombre
    return nombres[0] if nombres else None


async def buscar_libro(consulta: str) -> dict:
    """Busca libros reales en Open Library por título, autor o ambos.

    Usala siempre que el usuario pregunte un dato concreto de un libro (autor, año de
    publicación, si existe) en vez de responder de memoria.

    Args:
        consulta: texto a buscar, por ejemplo "Rayuela Cortázar" o "Dune".

    Returns:
        Un diccionario con "estado" ("ok", "sin_resultados" o "error"). Si es "ok", trae
        "libros": hasta tres libros, cada uno con "titulo", "autor" y "anio" (el año puede
        ser null si no se conoce). Si es "sin_resultados" o "error", trae "mensaje" con lo
        que pasó, para que se lo expliques al usuario.
    """
    logger.info("Herramienta buscar_libro invocada con consulta=%r", consulta)
    parametros = {
        "q": consulta,
        "limit": 3,
        "fields": "title,author_name,first_publish_year",
    }
    try:
        async with httpx.AsyncClient(
            timeout=10, headers={"User-Agent": USER_AGENT}
        ) as cliente_http:
            respuesta = await cliente_http.get(URL_OPEN_LIBRARY, params=parametros)
            respuesta.raise_for_status()
            datos = respuesta.json()
    except httpx.TimeoutException:
        logger.error("buscar_libro: Open Library no respondió a tiempo (consulta=%r)", consulta)
        return {"estado": "error", "mensaje": "Open Library tardó demasiado en responder."}
    except (httpx.HTTPError, ValueError):
        logger.error("buscar_libro: falló la consulta a Open Library (consulta=%r)", consulta, exc_info=True)
        return {"estado": "error", "mensaje": "No se pudo consultar Open Library en este momento."}

    libros = [
        {
            "titulo": doc.get("title"),
            "autor": _elegir_autor(doc.get("author_name", [])),
            "anio": doc.get("first_publish_year"),
        }
        for doc in datos.get("docs", [])
    ]
    if not libros:
        logger.info("buscar_libro: sin resultados para consulta=%r", consulta)
        return {"estado": "sin_resultados", "mensaje": "Open Library no encontró libros para esa búsqueda."}
    return {"estado": "ok", "libros": libros}


async def recomendar_por_tema(tema: str) -> dict:
    """Consigue libros reales de un género, tema o materia, para poder recomendarlos.

    Usala cuando el usuario pide recomendaciones de un género o tema (por ejemplo ciencia
    ficción, terror, historia) y todavía no sabe qué libro quiere. Recomendá solo títulos que
    estén en el resultado. Si el usuario nombra un libro concreto, usá buscar_libro.

    Args:
        tema: el género o tema en inglés y en minúsculas, por ejemplo "science fiction",
            "horror", "mystery" o "history". Open Library organiza sus temas en inglés.

    Returns:
        Un diccionario con "estado" ("ok", "sin_resultados" o "error"). Si es "ok", trae
        "libros": hasta cinco libros, cada uno con "titulo", "autor" y "anio" (el año puede
        ser null si no se conoce). Si es "sin_resultados" o "error", trae "mensaje" con lo
        que pasó, para que se lo expliques al usuario.
    """
    logger.info("Herramienta recomendar_por_tema invocada con tema=%r", tema)
    tema_limpio = " ".join(tema.replace('"', " ").lower().split())
    if not tema_limpio:
        return {"estado": "sin_resultados", "mensaje": "No se indicó ningún tema."}
    # Usamos search.json con subject: y no el endpoint /subjects. Medido en comparar_rutas.py:
    # /subjects devolvía clásicos de dominio público (Alicia en el País de las Maravillas y
    # Drácula como ciencia ficción) porque prioriza libros con edición digital libre;
    # search.json con subject: devolvió Asimov, Strugatski y Simmons. Sin sort: ninguno mejoró.
    parametros = {
        "q": f'subject:"{tema_limpio}"',
        "limit": 5,
        "fields": "title,author_name,first_publish_year",
    }
    try:
        async with httpx.AsyncClient(
            timeout=10, headers={"User-Agent": USER_AGENT}
        ) as cliente_http:
            respuesta = await cliente_http.get(URL_OPEN_LIBRARY, params=parametros)
            respuesta.raise_for_status()
            datos = respuesta.json()
    except httpx.TimeoutException:
        logger.error("recomendar_por_tema: Open Library no respondió a tiempo (tema=%r)", tema)
        return {"estado": "error", "mensaje": "Open Library tardó demasiado en responder."}
    except (httpx.HTTPError, ValueError):
        logger.error("recomendar_por_tema: falló la consulta a Open Library (tema=%r)", tema, exc_info=True)
        return {"estado": "error", "mensaje": "No se pudo consultar Open Library en este momento."}

    libros = [
        {
            "titulo": doc.get("title"),
            "autor": _elegir_autor(doc.get("author_name", [])),
            "anio": doc.get("first_publish_year"),
        }
        for doc in datos.get("docs", [])
    ]
    if not libros:
        logger.info("recomendar_por_tema: sin resultados para tema=%r", tema)
        return {"estado": "sin_resultados", "mensaje": "Open Library no tiene libros para ese tema."}
    return {"estado": "ok", "libros": libros}


async def analizar_resena(texto: str) -> dict:
    """Analiza una reseña de un libro y devuelve su análisis estructurado.

    Usala cuando el usuario pegue una reseña, una crítica o la opinión de alguien sobre un
    libro y quiera saber qué dice. No la uses para dar tu propia opinión ni para buscar datos
    de un libro.

    Args:
        texto: el texto completo de la reseña, tal como lo pegó el usuario.

    Returns:
        Un diccionario con "estado" ("ok" o "error"). Si es "ok", trae "sentimiento"
        ("favorable", "desfavorable" o "mixta"), "aspectos_elogiados" y "aspectos_criticados"
        (listas de textos cortos) y "sintesis" (una línea con el veredicto). Si es "error",
        trae "mensaje" con lo que pasó, para que se lo expliques al usuario.
    """
    logger.info("Herramienta analizar_resena invocada (%d caracteres)", len(texto))
    return await analisis.analizar_resena(texto)


def construir_herramientas(chat_id: int) -> list:
    """Devuelve las cinco herramientas, con el chat_id de esta conversación ya cerrado adentro.

    El chat_id NO es un parámetro de las herramientas: si lo completara el modelo, podría
    inventarlo y leer o escribir la lista de lectura de otra persona.
    """

    async def guardar_en_lista(titulo: str, autor: str, anio: int) -> dict:
        """Guarda un libro en la lista de lectura del usuario.

        Usala cuando el usuario quiera guardar un libro para leerlo después, o acepte tu
        oferta de guardarlo.

        Args:
            titulo: título del libro.
            autor: autor del libro.
            anio: año de primera publicación. Si no lo sabés, usá 0.

        Returns:
            Un diccionario con "estado" ("guardado" o "ya_estaba") y el "titulo".
        """
        logger.info(
            "Herramienta guardar_en_lista invocada con titulo=%r, autor=%r, anio=%r (chat %s)",
            titulo, autor, anio, chat_id,
        )
        agregado = memoria.agregar_a_lista(chat_id, titulo, autor, anio)
        return {"estado": "guardado" if agregado else "ya_estaba", "titulo": titulo}

    async def ver_lista() -> dict:
        """Devuelve la lista de lectura del usuario.

        Usala cuando el usuario pregunte qué tiene guardado o pendiente de leer.

        Returns:
            Un diccionario con "libros": una lista donde cada libro tiene "titulo", "autor"
            y "anio" (0 si no se conoce). La lista está vacía si no guardó nada todavía.
        """
        logger.info("Herramienta ver_lista invocada (chat %s)", chat_id)
        libros = [
            {"titulo": titulo, "autor": autor, "anio": anio}
            for titulo, autor, anio in memoria.leer_lista(chat_id)
        ]
        return {"libros": libros}

    return [buscar_libro, recomendar_por_tema, analizar_resena, guardar_en_lista, ver_lista]
