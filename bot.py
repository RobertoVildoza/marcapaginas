"""Bot de Telegram - Marcapáginas. Versión 1: comandos básicos y eco."""

import logging
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
# httpx avisa de cada pedido HTTP; con polling eso son varios por segundo
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("marcapaginas")


# Comando /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Saluda al usuario por su nombre."""
    nombre = update.effective_user.first_name
    logger.info("Nuevo /start de %s (id %s)", nombre, update.effective_user.id)
    await update.message.reply_text(
        f"¡Hola, {nombre}! Soy Marcapáginas, un bot sobre libros.\n"
        "Todavía estoy en construcción. Usá /help para ver qué puedo hacer."
    )


# Comando /help
async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lista los comandos disponibles."""
    logger.info("Nuevo /help de id %s", update.effective_user.id)
    await update.message.reply_text(
        "Estos son mis comandos:\n\n"
        "/start - Presentación\n"
        "/help - Esta ayuda\n\n"
        "También podés escribirme cualquier mensaje y te lo repito."
    )


# Responder un texto (función eco)
async def eco(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Repite cualquier mensaje de texto que no sea un comando."""
    texto = update.message.text
    logger.info("Mensaje recibido: %s", texto)
    await update.message.reply_text(f"Me dijiste: {texto}")


# Manejo de errores
async def manejo_error(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Registra cualquier error para que el bot no se detenga."""
    logger.error("Error al procesar una actualización", exc_info=context.error)


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise SystemExit("Falta TELEGRAM_TOKEN. Revisá el archivo .env")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", ayuda))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, eco))
    app.add_error_handler(manejo_error)

    logger.info("Marcapáginas está en línea. Ctrl+C para detenerlo.")

    # Polling: el bot le pregunta a Telegram, una y otra vez, si hay mensajes nuevos
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()