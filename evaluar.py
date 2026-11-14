"""Evaluación del bot: casos de prueba con dos verificaciones, sin Telegram.

Uso (desde la raíz del repositorio):
    .venv\\Scripts\\python.exe evaluar.py                 # corre todos los casos
    .venv\\Scripts\\python.exe evaluar.py rayuela dune    # corre solo esos casos

Los casos están en casos_prueba.json. Cada uno hace dos verificaciones:
  1. herramienta_ok: con código, comprobamos qué herramienta invocó el modelo (o que no
     invocó ninguna, si herramienta_esperada es null).
  2. cumple_criterio: un segundo modelo, el juez, lee la pregunta, la respuesta y el criterio
     en lenguaje natural y dice si se cumple. Comparar fragmentos de texto no alcanza: el
     modelo nunca responde dos veces igual.

Cuesta pedidos a Gemini: cada caso hace la llamada de la conversación (una a tres, con
herramientas) más una del juez, y entre caso y caso hay una pausa para no chocar con la cuota
por minuto. Conviene correr de a pocos casos.

Deja el resultado en docs/evaluacion/<fecha>_<hhmm>.csv para comparar corridas entre sí.
"""

import os

# La evaluación usa su propia base de datos para no mezclar la lista de lectura de prueba
# con la real. Tiene que estar antes de importar config (load_dotenv no pisa lo ya definido).
os.environ["RUTA_DB"] = "evaluacion.db"

import asyncio
import csv
import json
import logging
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

import gemini
import memoria
from config import (
    GEMINI_MODELO, GEMINI_MODELO_ANALISIS, RUTA_DB, cliente,
)

ID_CHAT_PRUEBA = 999999999  # un chat que no existe en Telegram, solo para las pruebas
RAIZ = Path(__file__).parent
ARCHIVO_DE_CASOS = RAIZ / "casos_prueba.json"
CARPETA_RESULTADOS = RAIZ / "docs" / "evaluacion"

# Pausas para no pasarse de la cuota por minuto. Cada caso dispara dos o tres pedidos seguidos
# (la conversación, con sus vueltas de herramientas, y el juez): la ráfaga es lo que pega contra
# el límite. Si igual se pasa, el SDK reintenta el 429 con backoff por dentro y nuestro
# asyncio.wait_for lo corta antes de que termine: llega como TimeoutError, no como 429.
# Por eso se evita llegar al límite en vez de atrapar el error (y ver SEGUNDOS_DEL_JUEZ).
PAUSA_ENTRE_CASOS = 20
PAUSA_ANTES_DEL_JUEZ = 3

# Tope de espera por pedido al juez. Tiene que ser MAYOR que el tope total de reintentos del
# SDK (30 s en _gaos, con backoff y Retry-After): con 30 el wait_for cortaba justo cuando el
# backoff todavía esperaba y un 429 nunca llegaba a recuperarse. El bot usa
# SEGUNDOS_MAXIMOS_DEL_MODELO (30) a propósito: ahí hay un usuario de Telegram esperando.
SEGUNDOS_DEL_JUEZ = 40

# Tablas de memoria.py que guardan estado por chat_id.
TABLAS_CON_ESTADO = ["mensajes", "lista_lectura", "flujo_activo", "resenas"]

logger = logging.getLogger("marcapaginas.evaluar")


# --- Cómo sabemos qué herramientas invocó el modelo ---------------------------------------
# herramientas.py loguea cada invocación con extra={"herramienta": <nombre>}. Durante cada
# caso colgamos un handler de ese logger y leemos ese campo (no el texto del mensaje, que
# se puede reescribir sin romper nada).

class RegistroDeHerramientas(logging.Handler):
    """Guarda el nombre de cada herramienta invocada, en orden."""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.invocadas: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        nombre = getattr(record, "herramienta", None)
        if nombre:
            self.invocadas.append(nombre)


# --- El juez -------------------------------------------------------------------------------

class Juicio(BaseModel):
    """Veredicto del juez sobre una respuesta."""

    cumple: bool = Field(description="True si la respuesta cumple el criterio indicado.")
    justificacion: str = Field(
        description="Una o dos frases que explican el veredicto, citando lo que dice la respuesta."
    )


# Se le aclara que evalúe SOLO el criterio: sin eso, un juez penaliza respuestas correctas
# pero escuetas (o con otro estilo) por motivos que el caso no pide verificar.
PROMPT_JUEZ = """\
Sos un evaluador estricto de un asistente sobre libros. Recibís una pregunta que le hicieron
al asistente, la respuesta que dio y un criterio. Tu trabajo es decidir si la respuesta
cumple el criterio.

Reglas:
1. Evaluá SOLO el criterio indicado. No evalúes el estilo, el tono ni la extensión de la
   respuesta: una respuesta breve que cumple el criterio cumple.
2. Sé estricto con lo que el criterio exige: si pide un dato, tiene que estar en la respuesta;
   si pide que no se invente algo, cualquier dato inventado es un incumplimiento.
3. Basate únicamente en el texto de la respuesta, sin suponer lo que el asistente "quiso decir".
4. En la justificación citá brevemente lo que dice la respuesta que justifica tu veredicto.
"""

FORMATO_DEL_JUEZ = {
    "type": "text",
    "mime_type": "application/json",
    "schema": Juicio.model_json_schema(),
}


async def juzgar(pregunta: str, respuesta: str, criterio: str) -> Juicio:
    """Le pide al juez que evalúe la respuesta contra el criterio. Levanta si algo falla.

    Usa GEMINI_MODELO_ANALISIS: es una clasificación con esquema fijo, como el análisis de
    reseñas, y no necesita el modelo grande. Va sin herramientas, con response_format.

    El juez también se equivoca: por eso se guarda su justificación en el reporte, para que una
    persona pueda revisar los veredictos dudosos en vez de tomarlos como verdad.
    """
    pedido = (
        f"PREGUNTA:\n{pregunta}\n\n"
        f"RESPUESTA DEL ASISTENTE:\n{respuesta}\n\n"
        f"CRITERIO A VERIFICAR:\n{criterio}"
    )
    interaccion = await asyncio.wait_for(
        cliente.aio.interactions.create(
            model=GEMINI_MODELO_ANALISIS,
            system_instruction=PROMPT_JUEZ,
            input=pedido,
            store=False,
            response_format=FORMATO_DEL_JUEZ,
        ),
        timeout=SEGUNDOS_DEL_JUEZ,
    )
    return Juicio.model_validate_json(interaccion.output_text)


# --- Casos ---------------------------------------------------------------------------------

@dataclass
class Caso:
    nombre: str
    pregunta: str
    herramienta_esperada: str | None  # None: no debería usar ninguna
    criterio: str


def cargar_casos() -> list[Caso]:
    datos = json.loads(ARCHIVO_DE_CASOS.read_text(encoding="utf-8"))
    return [
        Caso(d["nombre"], d["pregunta"], d["herramienta_esperada"], d["criterio"]) for d in datos
    ]


# --- Ejecución -----------------------------------------------------------------------------

def limpiar_estado_de_prueba() -> None:
    """Borra el estado que dejaron corridas anteriores en el chat de la evaluación.

    evaluacion.db no se reinicia: sin esto, "guardar" pasa la primera vez y falla las
    siguientes porque Dune ya estaba en la lista. El chat_id es fijo a propósito, para que los
    reportes de distintas corridas sean comparables.
    """
    with closing(sqlite3.connect(RUTA_DB)) as conexion, conexion:
        for tabla in TABLAS_CON_ESTADO:  # los nombres salen de la lista de arriba, no de afuera
            cursor = conexion.execute(f"DELETE FROM {tabla} WHERE chat_id = ?", (ID_CHAT_PRUEBA,))
            logger.info("Estado de prueba: %d fila(s) borradas de %s", cursor.rowcount, tabla)


@dataclass
class Resultado:
    caso: Caso
    invocadas: list[str]
    respuesta: str
    # None en herramienta_ok y cumple_criterio significa ERROR: no se pudo verificar (falló el
    # modelo o el juez). No es lo mismo que False, que es una falla real del bot.
    herramienta_ok: bool | None
    cumple_criterio: bool | None
    justificacion: str  # la del juez, o el motivo del error


def herramienta_correcta(esperada: str | None, invocadas: list[str]) -> bool:
    """Con esperada=None, correcto es no haber usado ninguna herramienta."""
    return not invocadas if esperada is None else esperada in invocadas


async def correr_caso(caso: Caso) -> Resultado:
    """Corre un caso con el historial vacío y lo verifica de las dos maneras."""
    memoria.borrar_historial(ID_CHAT_PRUEBA)  # que un caso no contamine al siguiente
    registro = RegistroDeHerramientas()
    logger_herramientas = logging.getLogger("marcapaginas.herramientas")
    logger_herramientas.addHandler(registro)
    try:
        respuesta = await gemini.responder(ID_CHAT_PRUEBA, caso.pregunta)
    except gemini.ErrorDelModelo as error:
        return Resultado(
            caso, registro.invocadas, "", None, None, f"El modelo no respondió: {error}"
        )
    finally:
        logger_herramientas.removeHandler(registro)

    herramienta_ok = herramienta_correcta(caso.herramienta_esperada, registro.invocadas)
    await asyncio.sleep(PAUSA_ANTES_DEL_JUEZ)
    try:
        juicio = await juzgar(caso.pregunta, respuesta, caso.criterio)
    except Exception as error:
        logger.error("Falló el juez en el caso %s", caso.nombre, exc_info=True)
        return Resultado(
            caso, registro.invocadas, respuesta, herramienta_ok, None,
            # str() de un TimeoutError es vacío: con el nombre del tipo se ve qué pasó.
            f"El juez no pudo evaluar: {type(error).__name__} {error}".rstrip(),
        )
    return Resultado(
        caso, registro.invocadas, respuesta, herramienta_ok, juicio.cumple, juicio.justificacion
    )


# --- Salida --------------------------------------------------------------------------------

def marca(valor: bool | None) -> str:
    return "?" if valor is None else ("✓" if valor else "✗")


def imprimir_resultado(resultado: Resultado) -> None:
    """Una línea por caso: marca de herramienta, marca de criterio y la pregunta."""
    print(
        f"  herramienta {marca(resultado.herramienta_ok)}   "
        f"criterio {marca(resultado.cumple_criterio)}   "
        f"{resultado.caso.nombre}: {resultado.caso.pregunta}"
    )
    if resultado.cumple_criterio is not True:  # falla (False) o error (None): se explica
        print(f"      juez: {resultado.justificacion}")
    if resultado.herramienta_ok is False:
        esperada = resultado.caso.herramienta_esperada or "ninguna"
        usadas = ", ".join(resultado.invocadas) or "ninguna"
        print(f"      herramienta: se esperaba {esperada}, usó {usadas}")


def porcentaje(aciertos: int, total: int) -> str:
    return f"{aciertos}/{total} ({round(100 * aciertos / total)}%)" if total else "0/0"


def imprimir_resumen(resultados: list[Resultado]) -> None:
    total = len(resultados)
    print()
    print(f"Herramienta correcta: {porcentaje(sum(r.herramienta_ok is True for r in resultados), total)}")
    print(f"Criterio cumplido:    {porcentaje(sum(r.cumple_criterio is True for r in resultados), total)}")
    sin_verificar = sum(r.herramienta_ok is None or r.cumple_criterio is None for r in resultados)
    if sin_verificar:
        print(
            f"({sin_verificar} caso(s) con ERROR, marcados con ?: no cuentan como aciertos pero "
            "tampoco son fallas del bot; conviene repetirlos)"
        )


def si_no(valor: bool | None) -> str:
    return "ERROR" if valor is None else ("sí" if valor else "no")


COLUMNAS = [
    "caso", "pregunta", "herramienta_esperada", "herramientas_usadas", "herramienta_ok",
    "cumple_criterio", "justificacion", "respuesta", "modelo", "modelo_juez",
]


def guardar_resultado(resultados: list[Resultado], ahora: datetime) -> Path:
    CARPETA_RESULTADOS.mkdir(parents=True, exist_ok=True)
    # Con hora y minutos para que dos corridas del mismo día no se pisen.
    ruta = CARPETA_RESULTADOS / f"{ahora:%Y-%m-%d_%H%M}.csv"
    # utf-8-sig: el BOM hace que Excel muestre bien las tildes.
    with ruta.open("w", encoding="utf-8-sig", newline="") as archivo:
        escritor = csv.writer(archivo)
        escritor.writerow(COLUMNAS)
        for r in resultados:
            escritor.writerow([
                r.caso.nombre,
                r.caso.pregunta,
                r.caso.herramienta_esperada or "ninguna",
                ", ".join(r.invocadas) or "ninguna",
                si_no(r.herramienta_ok),
                si_no(r.cumple_criterio),
                r.justificacion,
                r.respuesta,
                # Sin el modelo en el reporte, dos corridas no se pueden comparar entre sí.
                GEMINI_MODELO,
                # Ídem el juez: si cambia entre corridas, los criterios no son comparables.
                GEMINI_MODELO_ANALISIS,
            ])
    return ruta


async def main() -> None:
    todos = cargar_casos()
    nombres_pedidos = sys.argv[1:]
    por_nombre = {caso.nombre: caso for caso in todos}
    desconocidos = [n for n in nombres_pedidos if n not in por_nombre]
    if desconocidos:
        raise SystemExit(
            f"No existe el caso: {', '.join(desconocidos)}. Casos disponibles: {', '.join(por_nombre)}"
        )
    casos = [por_nombre[n] for n in nombres_pedidos] or todos

    memoria.inicializar()
    limpiar_estado_de_prueba()
    print(f"Modelo: {GEMINI_MODELO} | juez: {GEMINI_MODELO_ANALISIS} | casos: {len(casos)}")
    resultados = []
    for numero, caso in enumerate(casos, start=1):
        if numero > 1:
            await asyncio.sleep(PAUSA_ENTRE_CASOS)
        print(f"[{numero}/{len(casos)}] Corriendo {caso.nombre}...", flush=True)
        resultado = await correr_caso(caso)
        resultados.append(resultado)
        imprimir_resultado(resultado)

    imprimir_resumen(resultados)
    print(f"\nGuardado en {guardar_resultado(resultados, datetime.now())}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING)
    # Solo queremos leer los logs de herramientas, no verlos en la consola.
    logging.getLogger("marcapaginas.herramientas").setLevel(logging.INFO)
    logging.getLogger("marcapaginas.herramientas").propagate = False
    logging.getLogger("marcapaginas.evaluar").setLevel(logging.INFO)
    asyncio.run(main())
