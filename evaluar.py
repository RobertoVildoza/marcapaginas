"""Evaluación del bot: casos de prueba con respuesta esperada, sin Telegram.

Uso (desde la raíz del repositorio):
    .venv\\Scripts\\python.exe evaluar.py                 # corre los ocho casos
    .venv\\Scripts\\python.exe evaluar.py rayuela dune    # corre solo esos casos

Cada caso llama a gemini.responder() igual que lo hace el bot. Cuesta pedidos a Gemini
(un caso con herramientas puede gastar dos o tres), y la cuota gratuita es de 20 por día
por clave: conviene correr de a pocos casos.

Deja el resultado en docs/evaluacion/<fecha>.md para comparar corridas entre sí.
"""

import os

# La evaluación usa su propia base de datos para no mezclar la lista de lectura de prueba
# con la real. Tiene que estar antes de importar config (load_dotenv no pisa lo ya definido).
os.environ["RUTA_DB"] = "evaluacion.db"

import asyncio
import logging
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import gemini
import memoria
from config import GEMINI_MODELO, GEMINI_MODELO_ANALISIS

ID_CHAT_PRUEBA = 999999999  # un chat que no existe en Telegram, solo para las pruebas
CARPETA_RESULTADOS = Path("docs") / "evaluacion"

RESENA = (
    "Leí La casa de los espíritus y me atrapó la saga familiar: los personajes femeninos "
    "son inolvidables y la prosa es preciosa. Lo malo es que la segunda mitad se arrastra "
    "y el final me pareció apurado. Igual lo recomiendo."
)


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


# --- Normalización y verificaciones --------------------------------------------------------

def normalizar(texto: str) -> str:
    """Minúsculas y sin tildes, para comparar 'Cortázar' con 'cortazar'."""
    sin_tildes = unicodedata.normalize("NFD", texto)
    sin_tildes = "".join(c for c in sin_tildes if not unicodedata.combining(c))
    return sin_tildes.lower()


FRASES_DE_NO_ENCONTRADO = [
    "no encontr", "no encuentro", "no pude encontrar", "no aparece", "no figura",
    "no hay ", "no existe", "no tengo registro", "no pude", "no logr", "no halle",
    "no lo conozco", "no tengo", "no registra", "sin resultados",
]
ANIO = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})\b")
# Dos o más palabras seguidas que empiezan con mayúscula: parece un nombre de persona.
NOMBRE_PROPIO = re.compile(
    r"\b[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)+"
)
NOMBRES_PERMITIDOS = ["open library", "marcapaginas"]


def verificar_libro_inexistente(
    mensaje: str, respuesta: str, invocadas: list[str]
) -> tuple[list[str], list[str]]:
    """Devuelve (fallas, advertencias). Solo las fallas deciden si el caso pasa.

    Es el caso difícil: no hay un texto esperado, hay que comprobar que el bot NO inventó.
    Usamos reglas simples y repetibles (no un segundo modelo, que gastaría cuota y sería
    otra fuente de error). Las advertencias las revisa una persona con la respuesta completa
    que queda en el archivo de resultados.
    """
    fallas = []
    advertencias = []
    respuesta_normalizada = normalizar(respuesta)

    # 1. Tiene que haber consultado la herramienta: si respondió sin buscar, no pudo saber.
    if "buscar_libro" not in invocadas:
        fallas.append("no consultó buscar_libro")

    # 2. Tiene que decir que no lo encontró.
    if not any(frase in respuesta_normalizada for frase in FRASES_DE_NO_ENCONTRADO):
        fallas.append("no dice que no lo encontró")

    # 3. No puede aparecer ningún año: el libro no existe, cualquier año es inventado.
    anios = ANIO.findall(respuesta)
    if anios:
        fallas.append(f"menciona un año ({', '.join(anios)})")

    # 4. Un nombre propio nuevo (el único autor de la conversación es el de la pregunta) es
    #    solo una ADVERTENCIA, no una falla: la regla tiene falsos positivos conocidos
    #    ("Hola Roberto" cuenta como nombre) y una prueba que falla por el motivo
    #    equivocado se termina ignorando.
    pregunta_normalizada = normalizar(mensaje)
    inventados = [
        nombre
        for nombre in NOMBRE_PROPIO.findall(respuesta)
        if normalizar(nombre) not in pregunta_normalizada
        and normalizar(nombre) not in NOMBRES_PERMITIDOS
    ]
    if inventados:
        advertencias.append(f"nombra a alguien que no estaba en la pregunta ({', '.join(inventados)})")

    return fallas, advertencias


# --- Casos ---------------------------------------------------------------------------------

@dataclass
class Caso:
    nombre: str
    mensaje: str
    fragmentos: list[str] = field(default_factory=list)  # deben aparecer en la respuesta
    herramienta: str | None = None                       # debe haberse invocado
    verificacion: Callable[[str, str, list[str]], tuple[list[str], list[str]]] | None = None

    def esperado(self) -> str:
        partes = []
        if self.herramienta:
            partes.append(f"herramienta {self.herramienta}")
        if self.fragmentos:
            partes.append("texto: " + ", ".join(self.fragmentos))
        if self.verificacion:
            partes.append("dice que no lo encontró, sin año inventado")
        return "; ".join(partes)


CASOS = [
    Caso("rayuela", "¿Quién escribió Rayuela y en qué año salió?",
         fragmentos=["Cortázar", "1963"], herramienta="buscar_libro"),
    Caso("dune", "Contame de Dune",
         fragmentos=["Herbert", "1965"], herramienta="buscar_libro"),
    Caso("recomendacion_scifi", "Recomendame ciencia ficción",
         herramienta="recomendar_por_tema"),
    Caso("recomendacion_terror", "Recomendame terror",
         herramienta="recomendar_por_tema"),
    Caso("resena", f'Mirá esta reseña de un libro: "{RESENA}" ¿Qué dice?',
         herramienta="analizar_resena"),
    Caso("guardar", "Guardá Dune en mi lista",
         herramienta="guardar_en_lista"),
    Caso("ver_lista", "¿Qué tengo en mi lista?",
         herramienta="ver_lista"),
    Caso("libro_inexistente", "Contame del libro Zzzqq de Mariana Quintil",
         verificacion=verificar_libro_inexistente),
]


# --- Ejecución -----------------------------------------------------------------------------

@dataclass
class Resultado:
    caso: Caso
    estado: str          # "OK", "FALLA" o "ERROR" (el modelo no pudo responder)
    fallas: list[str]
    invocadas: list[str]
    respuesta: str
    advertencias: list[str] = field(default_factory=list)  # se informan, no cambian el veredicto

    def obtenido(self) -> str:
        herramientas = ", ".join(self.invocadas) or "ninguna"
        if self.estado == "ERROR":
            return f"herramientas: {herramientas}; el modelo no respondió"
        return f"herramientas: {herramientas}; respuesta: {' '.join(self.respuesta.split())}"


async def correr_caso(caso: Caso) -> Resultado:
    """Corre un caso con el historial vacío y devuelve su resultado."""
    memoria.borrar_historial(ID_CHAT_PRUEBA)  # que un caso no contamine al siguiente
    registro = RegistroDeHerramientas()
    logger_herramientas = logging.getLogger("marcapaginas.herramientas")
    logger_herramientas.addHandler(registro)
    try:
        respuesta = await gemini.responder(ID_CHAT_PRUEBA, caso.mensaje)
    except gemini.ErrorDelModelo as error:
        return Resultado(caso, "ERROR", [str(error)], registro.invocadas, "")
    finally:
        logger_herramientas.removeHandler(registro)

    fallas = []
    advertencias = []
    if caso.herramienta and caso.herramienta not in registro.invocadas:
        fallas.append(f"no invocó {caso.herramienta}")
    respuesta_normalizada = normalizar(respuesta)
    for fragmento in caso.fragmentos:
        if normalizar(fragmento) not in respuesta_normalizada:
            fallas.append(f"falta '{fragmento}' en la respuesta")
    if caso.verificacion:
        fallas_propias, advertencias = caso.verificacion(caso.mensaje, respuesta, registro.invocadas)
        fallas += fallas_propias

    return Resultado(
        caso, "FALLA" if fallas else "OK", fallas, registro.invocadas, respuesta, advertencias
    )


def recortar(texto: str, largo: int) -> str:
    return texto if len(texto) <= largo else texto[: largo - 1] + "…"


def imprimir_tabla(resultados: list[Resultado]) -> None:
    print()
    print(f"{'caso':<22}{'resultado':<11}{'esperado':<45}obtenido")
    print("-" * 140)
    for r in resultados:
        print(f"{r.caso.nombre:<22}{r.estado:<11}{recortar(r.caso.esperado(), 43):<45}"
              f"{recortar(r.obtenido(), 80)}")
        for falla in r.fallas:
            print(f"{'':<22}  -> {falla}")
        for advertencia in r.advertencias:
            print(f"{'':<22}  ADVERTENCIA: {advertencia}")


def puntaje(resultados: list[Resultado]) -> str:
    return f"{sum(r.estado == 'OK' for r in resultados)}/{len(resultados)}"


def celda(texto: str) -> str:
    """Escapa un texto para ponerlo en una celda de tabla Markdown."""
    return " ".join(texto.split()).replace("|", "\\|")


def guardar_resultado(resultados: list[Resultado], ahora: datetime) -> Path:
    CARPETA_RESULTADOS.mkdir(parents=True, exist_ok=True)
    # Con hora y minutos para que dos corridas del mismo día no se pisen.
    ruta = CARPETA_RESULTADOS / f"{ahora:%Y-%m-%d_%H%M}.md"
    lineas = [
        f"# Evaluación del {ahora:%d/%m/%Y %H:%M}",
        "",
        f"- Modelo de conversación: `{GEMINI_MODELO}`",
        f"- Modelo de análisis: `{GEMINI_MODELO_ANALISIS}`",
        f"- Casos corridos: {', '.join(r.caso.nombre for r in resultados)}",
        f"- **Puntaje: {puntaje(resultados)}**",
        "",
        "| Caso | Resultado | Esperado | Obtenido |",
        "|---|---|---|---|",
    ]
    for r in resultados:
        obtenido = r.obtenido()
        if r.fallas:
            obtenido += " — FALLAS: " + "; ".join(r.fallas)
        if r.advertencias:
            obtenido += " — ADVERTENCIAS: " + "; ".join(r.advertencias)
        lineas.append(f"| {r.caso.nombre} | {r.estado} | {celda(r.caso.esperado())} | {celda(obtenido)} |")
    lineas += ["", "## Respuestas completas", ""]
    for r in resultados:
        lineas += [f"### {r.caso.nombre}", "", f"Mensaje: {r.caso.mensaje}", "",
                   r.respuesta or "(sin respuesta)", ""]
    ruta.write_text("\n".join(lineas), encoding="utf-8")
    return ruta


async def main() -> None:
    nombres_pedidos = sys.argv[1:]
    por_nombre = {caso.nombre: caso for caso in CASOS}
    desconocidos = [n for n in nombres_pedidos if n not in por_nombre]
    if desconocidos:
        raise SystemExit(
            f"No existe el caso: {', '.join(desconocidos)}. Casos disponibles: {', '.join(por_nombre)}"
        )
    casos = [por_nombre[n] for n in nombres_pedidos] or CASOS

    memoria.inicializar()
    resultados = []
    for caso in casos:
        print(f"Corriendo {caso.nombre}...", flush=True)
        resultados.append(await correr_caso(caso))

    imprimir_tabla(resultados)
    print(f"\nPuntaje: {puntaje(resultados)}")
    print(f"Guardado en {guardar_resultado(resultados, datetime.now())}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING)
    # Solo queremos leer los logs de herramientas, no verlos en la consola.
    logging.getLogger("marcapaginas.herramientas").setLevel(logging.INFO)
    logging.getLogger("marcapaginas.herramientas").propagate = False
    asyncio.run(main())
