"""Herramientas que Gemini puede llamar (function calling).

Con la Interactions API el ciclo de herramientas es manual (ver gemini.responder) y el SDK ya
no lee los docstrings: lo que el modelo ve de cada herramienta es su DECLARACION, un
diccionario JSON escrito a mano arriba de la función. Esa declaración es el contrato con el
modelo; el docstring queda para quien lee el código (y debería decir lo mismo).
"""

import logging
import unicodedata

import httpx

import analisis
import memoria

# Cada log de invocación lleva extra={"herramienta": nombre}: evaluar.py lee ese campo,
# así que se puede reescribir el texto del mensaje sin romper las pruebas.
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


DECLARACION_BUSCAR_LIBRO = {
    "type": "function",
    "name": "buscar_libro",
    "description": (
        "Busca libros reales en Open Library por título, autor o ambos. Usala siempre que el "
        "usuario pregunte un dato concreto de un libro (autor, año de publicación, si existe) "
        "en vez de responder de memoria. Devuelve un objeto con 'estado' ('ok', "
        "'sin_resultados' o 'error'). Si es 'ok', trae 'libros': hasta tres libros, cada uno "
        "con 'titulo', 'autor', 'anio' y 'paginas' (el año y las páginas pueden ser null si no "
        "se conocen; las páginas son aproximadas, la mediana entre ediciones). Si es "
        "'sin_resultados' o 'error', trae 'mensaje' con lo que pasó, para que se lo expliques "
        "al usuario."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "consulta": {
                "type": "string",
                "description": 'Texto a buscar, por ejemplo "Rayuela Cortázar" o "Dune".',
            }
        },
        "required": ["consulta"],
    },
}


async def buscar_libro(consulta: str) -> dict:
    """Busca libros reales en Open Library por título, autor o ambos.

    Usala siempre que el usuario pregunte un dato concreto de un libro (autor, año de
    publicación, si existe) en vez de responder de memoria.

    Args:
        consulta: texto a buscar, por ejemplo "Rayuela Cortázar" o "Dune".

    Returns:
        Un diccionario con "estado" ("ok", "sin_resultados" o "error"). Si es "ok", trae
        "libros": hasta tres libros, cada uno con "titulo", "autor", "anio" y "paginas"
        (el año y las páginas pueden ser null si no se conocen; las páginas son aproximadas,
        la mediana entre ediciones). Si es "sin_resultados" o "error", trae "mensaje" con lo
        que pasó, para que se lo expliques al usuario.
    """
    logger.info(
        "Herramienta buscar_libro invocada con consulta=%r", consulta,
        extra={"herramienta": "buscar_libro"},
    )
    parametros = {
        "q": consulta,
        "limit": 3,
        "fields": "title,author_name,first_publish_year,number_of_pages_median",
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
            "paginas": doc.get("number_of_pages_median"),
        }
        for doc in datos.get("docs", [])
    ]
    if not libros:
        logger.info("buscar_libro: sin resultados para consulta=%r", consulta)
        return {"estado": "sin_resultados", "mensaje": "Open Library no encontró libros para esa búsqueda."}
    return {"estado": "ok", "libros": libros}


async def buscar_por_tema(tema: str, cantidad: int) -> dict:
    """Busca hasta `cantidad` libros de un tema en Open Library.

    La usan la herramienta recomendar_por_tema (que pide 5) y el flujo guiado (que pide más
    para tener de dónde elegir). Devuelve el mismo diccionario que la herramienta.
    """
    tema_limpio = " ".join(tema.replace('"', " ").lower().split())
    if not tema_limpio:
        return {"estado": "sin_resultados", "mensaje": "No se indicó ningún tema."}
    # Usamos search.json con subject: y no el endpoint /subjects. Medido en comparar_rutas.py:
    # /subjects devolvía clásicos de dominio público (Alicia en el País de las Maravillas y
    # Drácula como ciencia ficción) porque prioriza libros con edición digital libre;
    # search.json con subject: devolvió Asimov, Strugatski y Simmons. Sin sort: ninguno mejoró.
    parametros = {
        "q": f'subject:"{tema_limpio}"',
        "limit": cantidad,
        "fields": "title,author_name,first_publish_year,number_of_pages_median",
    }
    try:
        async with httpx.AsyncClient(
            timeout=10, headers={"User-Agent": USER_AGENT}
        ) as cliente_http:
            respuesta = await cliente_http.get(URL_OPEN_LIBRARY, params=parametros)
            respuesta.raise_for_status()
            datos = respuesta.json()
    except httpx.TimeoutException:
        logger.error("buscar_por_tema: Open Library no respondió a tiempo (tema=%r)", tema)
        return {"estado": "error", "mensaje": "Open Library tardó demasiado en responder."}
    except (httpx.HTTPError, ValueError):
        logger.error("buscar_por_tema: falló la consulta a Open Library (tema=%r)", tema, exc_info=True)
        return {"estado": "error", "mensaje": "No se pudo consultar Open Library en este momento."}

    libros = [
        {
            "titulo": doc.get("title"),
            "autor": _elegir_autor(doc.get("author_name", [])),
            "anio": doc.get("first_publish_year"),
            "paginas": doc.get("number_of_pages_median"),
        }
        for doc in datos.get("docs", [])
    ]
    if not libros:
        logger.info("buscar_por_tema: sin resultados para tema=%r", tema)
        return {"estado": "sin_resultados", "mensaje": "Open Library no tiene libros para ese tema."}
    return {"estado": "ok", "libros": libros}


DECLARACION_RECOMENDAR_POR_TEMA = {
    "type": "function",
    "name": "recomendar_por_tema",
    "description": (
        "Consigue libros reales de un género, tema o materia, para poder recomendarlos. Usala "
        "cuando el usuario pide recomendaciones de un género o tema (por ejemplo ciencia "
        "ficción, terror, historia) y todavía no sabe qué libro quiere. Recomendá solo títulos "
        "que estén en el resultado. Si el usuario nombra un libro concreto, usá buscar_libro. "
        "Devuelve un objeto con 'estado' ('ok', 'sin_resultados' o 'error'). Si es 'ok', trae "
        "'libros': hasta cinco libros, cada uno con 'titulo', 'autor', 'anio' y 'paginas' (el "
        "año y las páginas pueden ser null si no se conocen; las páginas son aproximadas, la "
        "mediana entre ediciones). Si es 'sin_resultados' o 'error', trae 'mensaje' con lo que "
        "pasó, para que se lo expliques al usuario."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tema": {
                "type": "string",
                "description": (
                    "El género o tema en inglés y en minúsculas, por ejemplo 'science "
                    "fiction', 'horror', 'mystery' o 'history'. Open Library organiza sus "
                    "temas en inglés."
                ),
            }
        },
        "required": ["tema"],
    },
}


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
        "libros": hasta cinco libros, cada uno con "titulo", "autor", "anio" y "paginas"
        (el año y las páginas pueden ser null si no se conocen; las páginas son aproximadas,
        la mediana entre ediciones). Si es "sin_resultados" o "error", trae "mensaje" con lo
        que pasó, para que se lo expliques al usuario.
    """
    logger.info(
        "Herramienta recomendar_por_tema invocada con tema=%r", tema,
        extra={"herramienta": "recomendar_por_tema"},
    )
    return await buscar_por_tema(tema, 5)


DECLARACION_ANALIZAR_RESENA = {
    "type": "function",
    "name": "analizar_resena",
    "description": (
        "Analiza una reseña de un libro y devuelve su análisis estructurado. Usala cuando el "
        "usuario pegue una reseña, una crítica o la opinión de alguien sobre un libro y quiera "
        "saber qué dice. No la uses para dar tu propia opinión ni para buscar datos de un "
        "libro. Devuelve un objeto con 'estado' ('ok' o 'error'). Si es 'ok', trae "
        "'sentimiento' ('favorable', 'desfavorable' o 'mixta'), 'aspectos_elogiados' y "
        "'aspectos_criticados' (listas de textos cortos), 'sintesis' (una línea con el "
        "veredicto) y 'puntaje' (satisfacción de quien escribió la reseña, de 1 a 5). El "
        "análisis queda guardado para el usuario. Si es 'error', trae 'mensaje' con lo que pasó, para que se lo expliques "
        "al usuario."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "texto": {
                "type": "string",
                "description": "El texto completo de la reseña, tal como lo pegó el usuario.",
            }
        },
        "required": ["texto"],
    },
}


# Las tres que siguen se definen dentro de construir_herramientas (necesitan el chat_id), pero
# sus declaraciones no dependen de él, así que van acá, justo arriba (la de analizar_resena
# está más arriba, junto a las otras). Ninguna lleva chat_id:
# lo pone el código, no el modelo.
DECLARACION_GUARDAR_EN_LISTA = {
    "type": "function",
    "name": "guardar_en_lista",
    "description": (
        "Guarda un libro en la lista de lectura del usuario. Usala cuando el usuario quiera "
        "guardar un libro para leerlo después, o acepte tu oferta de guardarlo. Devuelve un "
        "objeto con 'estado' ('guardado' o 'ya_estaba') y el 'titulo'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "titulo": {"type": "string", "description": "Título del libro."},
            "autor": {"type": "string", "description": "Autor del libro."},
            "anio": {
                "type": "integer",
                "description": "Año de primera publicación. Si no lo sabés, usá 0.",
            },
        },
        "required": ["titulo", "autor", "anio"],
    },
}

DECLARACION_VER_LISTA = {
    "type": "function",
    "name": "ver_lista",
    "description": (
        "Devuelve la lista de lectura del usuario. Usala cuando el usuario pregunte qué tiene "
        "guardado o pendiente de leer. Devuelve un objeto con 'libros': una lista donde cada "
        "libro tiene 'titulo', 'autor' y 'anio' (0 si no se conoce). La lista está vacía si no "
        "guardó nada todavía."
    ),
    "parameters": {"type": "object", "properties": {}},
}

DECLARACIONES = [
    DECLARACION_BUSCAR_LIBRO,
    DECLARACION_RECOMENDAR_POR_TEMA,
    DECLARACION_ANALIZAR_RESENA,
    DECLARACION_GUARDAR_EN_LISTA,
    DECLARACION_VER_LISTA,
]


def construir_herramientas(chat_id: int) -> tuple[list[dict], dict]:
    """Devuelve (declaraciones, funciones) de las cinco herramientas.

    `declaraciones` es la lista que se le manda al modelo; `funciones` mapea nombre -> función
    ejecutable, con el chat_id de esta conversación ya cerrado adentro donde corresponde.

    El chat_id NO es un parámetro de las herramientas: si lo completara el modelo, podría
    inventarlo y leer o escribir la lista de lectura de otra persona.
    """

    async def analizar_resena(texto: str) -> dict:
        """Analiza una reseña de un libro, devuelve su análisis estructurado y lo guarda.

        Usala cuando el usuario pegue una reseña, una crítica o la opinión de alguien sobre un
        libro y quiera saber qué dice. No la uses para dar tu propia opinión ni para buscar
        datos de un libro.

        Args:
            texto: el texto completo de la reseña, tal como lo pegó el usuario.

        Returns:
            Un diccionario con "estado" ("ok" o "error"). Si es "ok", trae "sentimiento"
            ("favorable", "desfavorable" o "mixta"), "aspectos_elogiados" y
            "aspectos_criticados" (listas de textos cortos), "sintesis" (una línea con el
            veredicto) y "puntaje" (de 1 a 5). Si es "error", trae "mensaje" con lo que pasó,
            para que se lo expliques al usuario.
        """
        logger.info(
            "Herramienta analizar_resena invocada (%d caracteres, chat %s)", len(texto), chat_id,
            extra={"herramienta": "analizar_resena"},
        )
        resultado = await analisis.analizar_resena(texto)
        if resultado["estado"] == "ok":
            analisis.guardar(chat_id, texto, resultado)
        return resultado

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
            extra={"herramienta": "guardar_en_lista"},
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
        logger.info(
            "Herramienta ver_lista invocada (chat %s)", chat_id,
            extra={"herramienta": "ver_lista"},
        )
        libros = [
            {"titulo": titulo, "autor": autor, "anio": anio}
            for titulo, autor, anio in memoria.leer_lista(chat_id)
        ]
        return {"libros": libros}

    funciones = {
        "buscar_libro": buscar_libro,
        "recomendar_por_tema": recomendar_por_tema,
        "analizar_resena": analizar_resena,
        "guardar_en_lista": guardar_en_lista,
        "ver_lista": ver_lista,
    }
    return DECLARACIONES, funciones
