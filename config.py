"""Configuración del bot. Es el ÚNICO módulo que lee el archivo .env."""

import os

from dotenv import load_dotenv
from google import genai

load_dotenv()


def _obligatoria(nombre: str) -> str:
    """Devuelve la variable de entorno o corta el arranque diciendo cuál falta."""
    valor = os.getenv(nombre)
    if not valor:
        raise SystemExit(f"Falta {nombre}. Revisá el archivo .env")
    return valor


TELEGRAM_TOKEN = _obligatoria("TELEGRAM_TOKEN")
GEMINI_API_KEY = _obligatoria("GEMINI_API_KEY")

# Único cliente de Gemini: lo comparten gemini.py y analisis.py, y la clave se usa solo acá.
cliente = genai.Client(api_key=GEMINI_API_KEY)

# Opcionales: tienen valor por defecto, así que no hace falta definirlas.
# Para cambiar de modelo alcanza con poner GEMINI_MODELO=... en el .env.
GEMINI_MODELO = os.getenv("GEMINI_MODELO") or "gemini-3.5-flash"
# Modelo del análisis de reseñas (analisis.py), separado del de la conversación.
GEMINI_MODELO_ANALISIS = os.getenv("GEMINI_MODELO_ANALISIS") or "gemini-3.5-flash-lite"
RUTA_DB = os.getenv("RUTA_DB") or "marcapaginas.db"

# Tope de espera por llamada al modelo. Con un 503 el SDK reintenta por dentro y el usuario
# esperaba más de 60 segundos; 30 da margen para una llamada con dos idas y vueltas de
# herramientas, que es lo más lento que vimos funcionando bien.
SEGUNDOS_MAXIMOS_DEL_MODELO = 30
