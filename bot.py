"""Bot de Telegram - Marcapáginas. Versión 3: conversa con Gemini y recuerda el historial."""

import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import analisis
import flujo
import gemini
import memoria
from config import TELEGRAM_TOKEN

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
# httpx avisa de cada pedido HTTP; con polling eso son varios por segundo
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("marcapaginas")

MENSAJE_DE_ERROR = (
    "Uf, ahora mismo no puedo pensar una respuesta. "
    "Probá de nuevo en un ratito, ¿sí?"
)


# Comando /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Saluda al usuario por su nombre."""
    nombre = update.effective_user.first_name
    logger.info("Nuevo /start de %s (id %s)", nombre, update.effective_user.id)
    await update.message.reply_text(
        f"¡Hola, {nombre}! Soy Marcapáginas, un bot sobre libros.\n"
        "Puedo charlar sobre libros y recomendarte lecturas. Usá /help para ver mis comandos."
    )


# Comando /help
async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lista los comandos disponibles."""
    logger.info("Nuevo /help de id %s", update.effective_user.id)
    await update.message.reply_text(
        "Estos son mis comandos:\n\n"
        "/start - Presentación\n"
        "/help - Esta ayuda\n"
        "/lista - Tu lista de lectura\n"
        "/queleo - Te hago tres preguntas y te recomiendo qué leer\n"
        "/cancelar - Salgo de /queleo en cualquier momento\n"
        "/analizar - Analizo una reseña: /analizar seguido del texto\n"
        "/olvidar - Borro lo que recuerdo de nuestra conversación\n\n"
        "También podés escribirme cualquier mensaje y charlamos sobre libros."
    )


# Comando /lista
async def lista(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra la lista de lectura leyendo la base directamente, sin pasar por el modelo."""
    libros = memoria.leer_lista(update.effective_chat.id)
    if not libros:
        await update.message.reply_text(
            "Tu lista de lectura está vacía. Pedime una recomendación y guardala."
        )
        return
    renglones = [
        f"{n}. {titulo} - {autor}" + (f" ({anio})" if anio else "")
        for n, (titulo, autor, anio) in enumerate(libros, start=1)
    ]
    await update.message.reply_text("Tu lista de lectura:\n\n" + "\n".join(renglones))


# Comando /analizar
async def resena(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Analiza el texto que viene después de /analizar, sin pasar por el modelo conversacional."""
    partes = update.message.text.split(maxsplit=1)  # [comando, resto del mensaje]
    if len(partes) < 2:
        await update.message.reply_text(
            "Escribí /analizar seguido del texto de la reseña. Por ejemplo:\n"
            "/analizar Me encantó la trama pero el final me pareció apurado."
        )
        return

    chat_id = update.effective_chat.id
    logger.info("Nuevo /analizar (chat %s)", chat_id)
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    resultado = await analisis.analizar_resena(partes[1])
    if resultado["estado"] != "ok":
        await update.message.reply_text(MENSAJE_DE_ERROR)
        return

    elogiados = ", ".join(resultado["aspectos_elogiados"]) or "nada en particular"
    criticados = ", ".join(resultado["aspectos_criticados"]) or "nada en particular"
    await update.message.reply_text(
        f"Sentimiento: {resultado['sentimiento']}\n"
        f"Elogia: {elogiados}\n"
        f"Critica: {criticados}\n"
        f"Síntesis: {resultado['sintesis']}"
    )


# Comando /queleo
async def que_leo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Empieza el flujo guiado: a partir de acá los textos del chat van al flujo."""
    await update.message.reply_text(flujo.iniciar(update.effective_chat.id))


# Comando /cancelar
async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sale del flujo guiado en cualquier momento y lo confirma."""
    if flujo.cancelar(update.effective_chat.id):
        await update.message.reply_text("Listo, cancelé la búsqueda. Cuando quieras, /queleo de nuevo.")
    else:
        await update.message.reply_text("No tenés ninguna búsqueda en curso.")


# Comando /olvidar
async def olvidar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Borra el historial de este chat y lo confirma."""
    chat_id = update.effective_chat.id
    memoria.borrar_historial(chat_id)
    logger.info("Historial borrado en el chat %s", chat_id)
    await update.message.reply_text("Listo, olvidé nuestra conversación. Tu lista de lectura sigue intacta.")


async def responder_con_el_flujo(update: Update, chat_id: int, texto: str) -> None:
    """Le pasa el texto al flujo guiado y envía lo que conteste."""
    try:
        respuesta = await flujo.avanzar(chat_id, texto)
    except gemini.ErrorDelModelo:
        # El flujo queda en su último paso: el usuario puede reintentar escribiendo de nuevo.
        await update.message.reply_text(MENSAJE_DE_ERROR)
        return

    await update.message.reply_text(respuesta.texto)

    if respuesta.intercambio:  # el flujo terminó: la recomendación pasa a ser parte de la charla
        try:
            memoria.guardar_intercambio(chat_id, *respuesta.intercambio)
        except Exception:
            logger.error("Falló el guardado del historial (chat %s)", chat_id, exc_info=True)


# Responder un texto con Gemini
async def conversar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde con Gemini a cualquier mensaje de texto que no sea un comando."""
    chat_id = update.effective_chat.id
    texto = update.message.text
    logger.info("Mensaje recibido (chat %s): %s", chat_id, texto)

    # El modelo tarda unos segundos: mostramos "escribiendo..." para que no parezca colgado
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # Si hay un flujo guiado en curso, el mensaje es la respuesta a su pregunta y no pasa
    # por la conversación general. Si el flujo venció, se descartó y seguimos con la charla.
    estado_del_flujo = flujo.situacion(chat_id)
    if estado_del_flujo == flujo.VENCIDO:
        await update.message.reply_text(
            "Tu búsqueda de /queleo quedó vieja y la descarté. Si querés, empezá otra con /queleo."
        )
    elif estado_del_flujo == flujo.ACTIVO:
        await responder_con_el_flujo(update, chat_id, texto)
        return

    # Primero respondemos (con el historial anterior) y recién después guardamos el par,
    # así el mensaje nuevo no queda duplicado en el pedido al modelo.
    try:
        respuesta = await gemini.responder(chat_id, texto)
    except gemini.ErrorDelModelo:
        # El detalle ya quedó en el log. Al usuario le damos un aviso amable
        # y no guardamos nada: el historial es la conversación, no los errores.
        await update.message.reply_text(MENSAJE_DE_ERROR)
        return

    await update.message.reply_text(respuesta)

    # Se guarda después de enviar: si el envío falla, no guardamos como dicho algo que el usuario nunca vio.
    try:
        memoria.guardar_intercambio(chat_id, texto, respuesta)
    except Exception:
        # Si falla el guardado, el usuario ya recibió la respuesta: no hay nada que avisarle.
        logger.error("Falló el guardado del historial (chat %s)", chat_id, exc_info=True)


# Comandos que no existen
async def comando_desconocido(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Avisa que no conocemos el comando en vez de quedarnos callados."""
    await update.message.reply_text("Ese comando no lo conozco. Probá con /help o con el menú.")


# Manejo de errores
async def manejo_error(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Registra cualquier error para que el bot no se detenga."""
    logger.error("Error al procesar una actualización", exc_info=context.error)


def main() -> None:
    memoria.inicializar()
    descartados = memoria.borrar_flujos_vencidos(flujo.MINUTOS_DE_VIGENCIA)
    if descartados:
        logger.info("Flujos vencidos descartados al arrancar: %d", descartados)

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", ayuda))
    app.add_handler(CommandHandler("lista", lista))
    app.add_handler(CommandHandler("analizar", resena))
    app.add_handler(CommandHandler("queleo", que_leo))
    app.add_handler(CommandHandler("cancelar", cancelar))
    app.add_handler(CommandHandler("olvidar", olvidar))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, conversar))
    # Tiene que ir DESPUÉS de todos los CommandHandler: la Application entrega cada update
    # al primer handler que lo acepta, y filters.COMMAND acepta cualquier comando, así que
    # si estuviera antes se comería los comandos que sí existen.
    app.add_handler(MessageHandler(filters.COMMAND, comando_desconocido))
    app.add_error_handler(manejo_error)

    logger.info("Marcapáginas está en línea. Ctrl+C para detenerlo.")

    # Polling: el bot le pregunta a Telegram, una y otra vez, si hay mensajes nuevos
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()