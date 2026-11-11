"""Prueba suelta (no es parte del bot): ciclo manual de herramientas con la Interactions API.

Dos pedidos de cuota si todo anda (tres si hay que probar la segunda variante).
Se corre con:  python prueba_herramientas.py
"""

import asyncio
import json

import herramientas
from config import GEMINI_MODELO, cliente

MENSAJE = "¿Quién escribió Rayuela y en qué año?"

# Declaración armada a mano para esta prueba (en el bot saldría de herramientas.py).
DECLARACION = {
    "type": "function",
    "name": "buscar_libro",
    "description": (
        "Busca libros reales en Open Library por título, autor o ambos. Usala siempre que "
        "el usuario pregunte un dato concreto de un libro (autor, año) en vez de responder "
        "de memoria."
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

SISTEMA = "Sos Marcapáginas, un bot sobre libros. Respondé en español rioplatense y en pocas líneas."


def mostrar(titulo: str, objeto) -> None:
    print(f"\n===== {titulo} =====")
    if hasattr(objeto, "model_dump"):
        objeto = objeto.model_dump(mode="json", exclude_none=True)
    print(json.dumps(objeto, ensure_ascii=False, indent=2, default=str))


async def llamar(entrada: list) -> object:
    return await asyncio.wait_for(
        cliente.aio.interactions.create(
            model=GEMINI_MODELO,
            system_instruction=SISTEMA,
            input=entrada,
            tools=[DECLARACION],
            store=False,  # sin previous_interaction_id: el contexto lo reenviamos nosotros
        ),
        timeout=30,
    )


async def main() -> None:
    paso_usuario = {"type": "user_input", "content": [{"type": "text", "text": MENSAJE}]}

    # --- Pedido 1: el modelo debería pedir la función ---
    primera = await llamar([paso_usuario])
    print("status:", primera.status)
    mostrar("pasos del pedido 1", [paso.model_dump(mode="json", exclude_none=True) for paso in primera.steps])

    llamados = [paso for paso in primera.steps if paso.type == "function_call"]
    if not llamados:
        print("\nEl modelo no pidió ninguna función; texto:", primera.output_text)
        return

    # --- Ejecutar la función de verdad ---
    resultados = []
    for llamado in llamados:
        resultado = await herramientas.buscar_libro(**llamado.arguments)
        print(f"\nbuscar_libro({llamado.arguments}) ->", json.dumps(resultado, ensure_ascii=False))
        resultados.append(
            {
                "type": "function_result",
                "call_id": llamado.id,
                "name": llamado.name,
                "result": json.dumps(resultado, ensure_ascii=False),
            }
        )

    # --- Pedido 2, variante A: input completo armado a mano (mensaje + function_call + function_result) ---
    pasos_llamado = [
        {"type": "function_call", "id": l.id, "name": l.name, "arguments": l.arguments}
        for l in llamados
    ]
    entrada_a = [paso_usuario, *pasos_llamado, *resultados]
    mostrar("input de la variante A", entrada_a)
    try:
        segunda = await llamar(entrada_a)
        print("\nVARIANTE A (pasos armados a mano): FUNCIONA. status:", segunda.status)
    except Exception as error:
        print(f"\nVARIANTE A FALLÓ: {type(error).__name__} status={getattr(error, 'status_code', None)}: {error}")
        # --- Variante B: reenviar los pasos del pedido 1 tal cual vinieron (con todo lo que traigan) ---
        pasos_tal_cual = [p.model_dump(mode="json", exclude_none=True, by_alias=True) for p in primera.steps]
        entrada_b = [paso_usuario, *pasos_tal_cual, *resultados]
        mostrar("input de la variante B", entrada_b)
        try:
            segunda = await llamar(entrada_b)
            print("\nVARIANTE B (pasos tal cual vinieron): FUNCIONA. status:", segunda.status)
        except Exception as error_b:
            print(f"\nVARIANTE B FALLÓ: {type(error_b).__name__} status={getattr(error_b, 'status_code', None)}: {error_b}")
            return

    mostrar("pasos del pedido 2", [paso.model_dump(mode="json", exclude_none=True) for paso in segunda.steps])
    print("\n===== RESPUESTA FINAL =====\n" + (segunda.output_text or "(vacía)"))


asyncio.run(main())
