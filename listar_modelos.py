"""Lista los modelos que la clave de Gemini del .env puede usar.

Uso:  python listar_modelos.py

Muestra el nombre exacto de cada modelo y los métodos que soporta. Los que soportan
generateContent (los que sirven para el bot) van marcados con [*]. El nombre que va en
GEMINI_MODELO es el que sigue a "models/".
"""

from config import cliente  # la clave se lee en config.py; acá nunca se imprime

MARCA = "[*]"


def main() -> None:
    modelos = sorted(cliente.models.list(), key=lambda modelo: modelo.name)
    for modelo in modelos:
        metodos = modelo.supported_actions or []
        marca = MARCA if "generateContent" in metodos else "   "
        print(f"{marca} {modelo.name}")
        print(f"      métodos: {', '.join(metodos) or '(ninguno informado)'}")

    utiles = [m for m in modelos if "generateContent" in (m.supported_actions or [])]
    print(f"\n{len(modelos)} modelos en total; {len(utiles)} soportan generateContent {MARCA}.")


if __name__ == "__main__":
    main()
