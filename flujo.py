"""Flujo guiado "¿qué leo ahora?": pregunta género, extensión y ánimo antes de recomendar.

Es una máquina de estados explícita: el paso actual vive en la tabla flujo_activo de SQLite
y las transiciones están escritas acá abajo, en SIGUIENTE_PASO y en avanzar().

Por qué no usamos ConversationHandler de python-telegram-bot: guarda el estado en memoria,
así que se perdería al reiniciar el bot salvo que sumáramos la persistencia de la librería,
y ya tenemos la nuestra (SQLite, igual que el historial). Además así la máquina de estados
se lee en nuestro código y no adentro de la librería.
"""

import logging
import re
import unicodedata
from typing import NamedTuple

import gemini
import herramientas
import memoria

logger = logging.getLogger("marcapaginas.flujo")

# Un flujo abandonado se descarta a los 30 minutos de haber empezado (vencimiento perezoso:
# se revisa cuando el usuario escribe, sin timers, así que sobrevive a los reinicios).
MINUTOS_DE_VIGENCIA = 30

# Umbrales de extensión, en páginas (Open Library da la mediana entre ediciones).
UMBRAL_CORTA = 250   # menos de esto: corta
UMBRAL_LARGA = 450   # más de esto: larga; en el medio: mediana
DESCRIPCION_DE_EXTENSION = {
    "corta": f"menos de {UMBRAL_CORTA} páginas",
    "mediana": f"entre {UMBRAL_CORTA} y {UMBRAL_LARGA} páginas",
    "larga": f"más de {UMBRAL_LARGA} páginas",
}

# Cuántos libros pedimos a Open Library: más que los 5 del chat, para tener de dónde elegir.
LIBROS_A_PEDIR = 10

# --- La máquina de estados ----------------------------------------------------------------
GENERO, EXTENSION, ANIMO, CONFIRMACION = "genero", "extension", "animo", "confirmacion"

# Cada paso: qué pregunta el bot cuando se entra a él.
PREGUNTAS = {
    GENERO: "¿De qué género tenés ganas? Por ejemplo terror, ciencia ficción, misterio o romance.",
    EXTENSION: "¿Qué extensión preferís: corta, mediana o larga?",
    ANIMO: "Última: ¿cómo andás de ánimo? Contame en una frase, por ejemplo "
           "\"cansado\" o \"con ganas de reírme\".",
    CONFIRMACION: "¿Querés guardar alguno en tu lista de lectura? Respondé con el número, "
                  "o decime que no.",
}

# Cada paso: a cuál se pasa cuando la respuesta es válida. Al responder ANIMO se hace la
# recomendación y se pasa a CONFIRMACION, que es el último (None): al responderlo, el flujo termina.
SIGUIENTE_PASO = {GENERO: EXTENSION, EXTENSION: ANIMO, ANIMO: CONFIRMACION, CONFIRMACION: None}

# Respuestas que en CONFIRMACION significan "no quiero guardar ninguno".
PALABRAS_DE_RECHAZO = {"no", "nop", "nope", "ninguno", "ninguna", "nada", "gracias"}
NUMEROS_EN_PALABRAS = {
    "uno": 1, "primero": 1, "primera": 1, "dos": 2, "segundo": 2, "segunda": 2,
}

# Qué hay en la base para un chat: nada, un flujo en curso, o uno que ya venció.
NINGUNO, ACTIVO, VENCIDO = "ninguno", "activo", "vencido"

# Los géneros que la gente escribe en español, en el inglés que usa Open Library.
# Lo que no esté acá se pasa tal cual (sirve si lo escribió en inglés).
GENEROS_EN_INGLES = {
    "terror": "horror",
    "miedo": "horror",
    "ciencia ficcion": "science fiction",
    "misterio": "mystery",
    "policial": "detective",
    "romance": "romance",
    "fantasia": "fantasy",
    "historia": "history",
    "aventura": "adventure",
    "biografia": "biography",
    "poesia": "poetry",
    "filosofia": "philosophy",
}


class RespuestaDelFlujo(NamedTuple):
    texto: str
    # Si el flujo terminó con una recomendación: (mensaje del usuario, respuesta del bot) para
    # que bot.py los guarde en el historial después de enviar. None en los pasos intermedios.
    intercambio: tuple[str, str] | None = None


def _normalizar(texto: str) -> str:
    """Minúsculas y sin tildes, para comparar 'Ciencia ficción' con 'ciencia ficcion'."""
    sin_tildes = unicodedata.normalize("NFD", texto)
    return "".join(c for c in sin_tildes if not unicodedata.combining(c)).lower().strip()


def _interpretar_extension(texto: str) -> str | None:
    """Devuelve "corta", "mediana" o "larga" según lo que escribió, o None si no se entiende."""
    palabras = _normalizar(texto).split()
    for palabra in palabras:
        if palabra in ("corta", "corto", "cortita", "breve"):
            return "corta"
        if palabra in ("mediana", "mediano", "media", "medio"):
            return "mediana"
        if palabra in ("larga", "largo", "extensa", "extenso"):
            return "larga"
    return None


def clasificar_extension(paginas: int | None) -> str | None:
    """corta / mediana / larga según las páginas, o None si no se conocen."""
    if not paginas:
        return None
    if paginas < UMBRAL_CORTA:
        return "corta"
    if paginas > UMBRAL_LARGA:
        return "larga"
    return "mediana"


# --- Entradas que usa bot.py ----------------------------------------------------------------

def iniciar(chat_id: int) -> str:
    """/queleo: arranca el flujo en el primer paso (reemplaza uno anterior) y hace la primera pregunta."""
    memoria.iniciar_flujo(chat_id)
    logger.info("Flujo iniciado (chat %s)", chat_id)
    return "¡Dale! Te hago tres preguntas y te recomiendo algo. Podés salir con /cancelar.\n\n" + PREGUNTAS[GENERO]


def cancelar(chat_id: int) -> bool:
    """/cancelar: descarta el flujo. Devuelve False si no había ninguno."""
    return memoria.borrar_flujo(chat_id)


def situacion(chat_id: int) -> str:
    """Devuelve NINGUNO, ACTIVO o VENCIDO. Si venció, lo borra acá mismo."""
    try:
        flujo = memoria.leer_flujo(chat_id, MINUTOS_DE_VIGENCIA)
        if flujo is None:
            return NINGUNO
        if flujo["vencido"]:
            memoria.borrar_flujo(chat_id)
            logger.info("Flujo vencido descartado (chat %s)", chat_id)
            return VENCIDO
        return ACTIVO
    except Exception:
        # La memoria no es esencial: si falla, el mensaje va a la conversación normal.
        logger.error("Falló la lectura del flujo (chat %s)", chat_id, exc_info=True)
        return NINGUNO


async def avanzar(chat_id: int, texto: str) -> RespuestaDelFlujo:
    """Procesa la respuesta del usuario según el paso en que está el flujo.

    Levanta gemini.ErrorDelModelo si falla la recomendación final; en ese caso el flujo queda
    en el último paso y el usuario puede reintentar escribiendo de nuevo su ánimo.
    """
    flujo = memoria.leer_flujo(chat_id, MINUTOS_DE_VIGENCIA)
    paso = flujo["paso"]
    texto = texto.strip()

    if paso == GENERO:
        return _guardar_y_pasar_al_siguiente(chat_id, GENERO, texto)

    if paso == EXTENSION:
        extension = _interpretar_extension(texto)
        if extension is None:
            # No avanzamos: repetimos la pregunta del mismo paso.
            return RespuestaDelFlujo("No te entendí. Elegí una: corta, mediana o larga.")
        return _guardar_y_pasar_al_siguiente(chat_id, EXTENSION, extension)

    if paso == CONFIRMACION:
        return _confirmar(chat_id, texto, flujo["candidatos"])

    # paso == ANIMO: guardamos el dato y recomendamos (queda en CONFIRMACION).
    memoria.guardar_dato_de_flujo(chat_id, ANIMO, texto, ANIMO)
    return await _recomendar(chat_id, flujo["genero"], flujo["extension"], texto)


def _interpretar_numero(texto: str) -> int | None:
    """Devuelve el número que eligió ("2", "el 2", "número 2", "segundo"), o None si no hay."""
    limpio = _normalizar(texto)
    coincidencia = re.fullmatch(r"(?:el |la |numero |opcion |#)?\s*(\d+)\W*", limpio)
    if coincidencia:
        return int(coincidencia.group(1))
    return NUMEROS_EN_PALABRAS.get(limpio.strip(" .!"))


def _es_rechazo(texto: str) -> bool:
    """True si dice que no quiere guardar ninguno ("no", "ninguno", "no, gracias"...)."""
    palabras = re.findall(r"\w+", _normalizar(texto))
    return bool(palabras) and palabras[0] in PALABRAS_DE_RECHAZO and not any(
        p.isdigit() for p in palabras
    )


def _confirmar(chat_id: int, texto: str, candidatos: list[dict] | None) -> RespuestaDelFlujo:
    """Paso CONFIRMACION: guarda el libro elegido, cierra si dice que no, o repite la pregunta."""
    if not candidatos:
        # No debería pasar (se guardan al entrar al paso); si pasa, no dejamos al chat trabado.
        memoria.borrar_flujo(chat_id)
        logger.error("Flujo en confirmación sin candidatos (chat %s)", chat_id)
        return RespuestaDelFlujo("Se me perdieron los libros que te ofrecí. Probá de nuevo con /queleo.")

    numero = _interpretar_numero(texto)
    if numero is not None and 1 <= numero <= len(candidatos):
        libro = candidatos[numero - 1]
        agregado = memoria.agregar_a_lista(
            chat_id, libro["titulo"], libro["autor"] or "autor desconocido", libro["anio"] or 0
        )
        memoria.borrar_flujo(chat_id)
        logger.info("Flujo terminado con libro guardado (chat %s)", chat_id)
        respuesta = (
            f"Listo, guardé \"{libro['titulo']}\" en tu lista de lectura. Podés verla con /lista."
            if agregado
            else f"\"{libro['titulo']}\" ya estaba en tu lista de lectura. Podés verla con /lista."
        )
        return RespuestaDelFlujo(respuesta, intercambio=(texto, respuesta))

    if _es_rechazo(texto):
        memoria.borrar_flujo(chat_id)
        logger.info("Flujo terminado sin guardar (chat %s)", chat_id)
        respuesta = "Está bien, no guardo nada. Cuando quieras otra recomendación, /queleo."
        return RespuestaDelFlujo(respuesta, intercambio=(texto, respuesta))

    # Cualquier otra cosa (incluido un número fuera de rango): repetimos sin avanzar.
    return RespuestaDelFlujo(
        f"No te entendí. Respondé con un número del 1 al {len(candidatos)}, decime que no, "
        "o salí con /cancelar."
    )


def _guardar_y_pasar_al_siguiente(chat_id: int, paso: str, valor: str) -> RespuestaDelFlujo:
    siguiente = SIGUIENTE_PASO[paso]
    memoria.guardar_dato_de_flujo(chat_id, paso, valor, siguiente)
    return RespuestaDelFlujo(PREGUNTAS[siguiente])


async def _recomendar(chat_id: int, genero: str, extension: str, animo: str) -> RespuestaDelFlujo:
    """Fin del flujo: busca libros del género (código) y el modelo elige y explica (IA)."""
    tema = GENEROS_EN_INGLES.get(_normalizar(genero), genero)
    resultado = await herramientas.buscar_por_tema(tema, LIBROS_A_PEDIR)

    if resultado["estado"] == "sin_resultados":
        # El género no existe en Open Library: volvemos a preguntarlo en vez de terminar.
        memoria.guardar_dato_de_flujo(chat_id, GENERO, None, GENERO)
        return RespuestaDelFlujo(
            f"No encontré libros de \"{genero}\". Probemos con otro género.\n\n" + PREGUNTAS[GENERO]
        )
    if resultado["estado"] == "error":
        # Nos quedamos en el último paso: escribir de nuevo el ánimo reintenta la búsqueda.
        return RespuestaDelFlujo(
            "No pude consultar la base de libros ahora. Escribime de nuevo cómo andás de ánimo "
            "y lo intento otra vez, o salí con /cancelar."
        )

    libros = [
        {**libro, "extension": clasificar_extension(libro["paginas"])}
        for libro in resultado["libros"]
    ]
    recomendacion = await gemini.recomendar_con_preferencias(
        genero, extension, animo, libros, DESCRIPCION_DE_EXTENSION[extension]
    )

    # Los datos de cada libro salen de nuestra lista, no del modelo: él solo dio el número.
    elegidos = [libros[eleccion.numero - 1] for eleccion in recomendacion.elegidos]
    texto = armar_mensaje(recomendacion, elegidos)
    candidatos = [
        {k: libro[k] for k in ("titulo", "autor", "anio", "paginas")} for libro in elegidos
    ]
    memoria.pasar_a_confirmacion(chat_id, candidatos)
    logger.info("Recomendación hecha, esperando confirmación (chat %s)", chat_id)
    pedido = (
        "Quiero que me recomiendes algo para leer. "
        f"Género: {genero}. Extensión: {extension}. Ánimo: {animo}."
    )
    return RespuestaDelFlujo(texto, intercambio=(pedido, texto))


def armar_mensaje(recomendacion: gemini.Recomendacion, elegidos: list[dict]) -> str:
    """Arma el mensaje: introducción, libros numerados con sus datos y el porqué, y la pregunta.

    `elegidos` son los libros (de la lista del código) en el mismo orden que
    recomendacion.elegidos; los números del mensaje son los que la persona va a responder.
    """
    renglones = []
    for n, (libro, eleccion) in enumerate(zip(elegidos, recomendacion.elegidos), start=1):
        anio = f" ({libro['anio']})" if libro["anio"] else ""
        # "unas": las páginas son la mediana entre ediciones, una aproximación.
        paginas = f"unas {libro['paginas']} páginas" if libro["paginas"] else "sin dato de páginas"
        renglones.append(
            f"{n}. {libro['titulo']} - {libro['autor'] or 'autor desconocido'}{anio}, {paginas}\n"
            f"   {eleccion.porque.strip()}"
        )
    return (
        f"{recomendacion.introduccion.strip()}\n\n" + "\n\n".join(renglones)
        + f"\n\n{PREGUNTAS[CONFIRMACION]}"
    )
