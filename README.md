# Marcapáginas

Bot de Telegram sobre libros: busca y recomienda consultando Open Library, guarda una lista de
lectura y analiza reseñas. Proyecto de Sistemas Inteligentes (ITU-UNCUYO).

## Ponerlo a andar

Necesitás Python 3.10 o superior. Desde PowerShell, en la carpeta del repositorio:

1. Crear el entorno virtual:

   ```powershell
   python -m venv .venv
   ```

2. Instalar las dependencias, usando directamente el Python del entorno (no hace falta
   activarlo):

   ```powershell
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

3. Crear tu archivo de configuración y completarlo:

   ```powershell
   copy .env.example .env
   ```

   Abrí `.env` y completá `TELEGRAM_TOKEN` y `GEMINI_API_KEY` (ver abajo). Los otros dos
   campos (`GEMINI_MODELO` y `GEMINI_MODELO_ANALISIS`) podés dejarlos vacíos.

4. Correr el bot, también con el Python del entorno:

   ```powershell
   .venv\Scripts\python.exe bot.py
   ```

   Después abrí tu bot en Telegram y mandale `/start`.

> Nota: si preferís activar el entorno (`.venv\Scripts\Activate.ps1`) y usar `python` a secas,
> PowerShell puede bloquear el script por su política de ejecución. Arreglarlo con
> `Set-ExecutionPolicy` cambia una configuración de seguridad del sistema, y no lo necesitás
> para nada de esto: con los comandos de arriba alcanza.

## De dónde sale cada clave

- **TELEGRAM_TOKEN**: se lo pedís a [@BotFather](https://t.me/BotFather) en Telegram con
  `/newbot`. Cada uno crea su propio bot de prueba.
- **GEMINI_API_KEY**: se saca gratis en [aistudio.google.com](https://aistudio.google.com).

La cuota gratuita de Gemini es de **20 pedidos por día por clave**. Por eso cada integrante
necesita la suya: si compartimos una, se agota enseguida.

Nunca subas tu `.env` al repositorio (ya está ignorado por git).

## Si el modelo da error

Los nombres de modelo de Gemini caducan. Si el bot responde con error y en la consola ves un 404,
corré:

```powershell
.venv\Scripts\python.exe listar_modelos.py
```

Muestra los modelos que habilita tu clave (los marcados con `[*]` sirven para el bot). Poné uno
en `GEMINI_MODELO` (conversación) o `GEMINI_MODELO_ANALISIS` (reseñas) dentro de `.env` y
reiniciá el bot.

## Comandos del bot

| Comando | Qué hace |
|---|---|
| `/start` | Saludo inicial. |
| `/help` | Lista los comandos. |
| `/lista` | Muestra tu lista de lectura. |
| `/resena <texto>` | Analiza una reseña: sentimiento, aspectos elogiados y criticados, síntesis. |
| `/queleo` | Te hace tres preguntas (género, extensión, ánimo) y te recomienda qué leer. Si lo dejás a medias, vence a los 30 minutos. |
| `/cancelar` | Sale de `/queleo` en cualquier momento. |
| `/olvidar` | Borra la conversación (la lista de lectura se conserva). |

Cualquier otro mensaje se conversa con el bot: pedile recomendaciones por género, preguntale por
un libro o pedile que guarde uno en tu lista.

## Más información

Para entender por qué elegimos lo que elegimos, leé [docs/decisiones.md](docs/decisiones.md).
