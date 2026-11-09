"""Compara dos rutas de Open Library para recomendar libros por tema.

A) /subjects/<tema>.json   B) search.json con q=subject:"<tema>" (con distintos sort).
Muestra los 5 primeros títulos de cada una para science fiction, horror y mystery.
Es la evidencia de por qué recomendar_por_tema usa search.json (ver docs/decisiones.md).
No usa Gemini, así que no gasta cuota. Uso:  python comparar_rutas.py
"""
import httpx, time
UA = {"User-Agent": "Marcapaginas/1.0 (proyecto academico ITU-UNCUYO)"}
TEMAS = ["science fiction", "horror", "mystery"]
CAMPOS = "title,author_name,first_publish_year"

def get(url, params):
    time.sleep(1)
    r = httpx.get(url, params=params, headers=UA, timeout=20)
    return r.status_code, (r.json() if r.status_code == 200 else {})

def fila(t, a, y):
    return f"{t} - {a or '?'} ({y or '?'})"

def ruta_a(tema):
    s, d = get(f"https://openlibrary.org/subjects/{tema.replace(' ', '_')}.json", {"limit": 5})
    return [fila(w["title"], ", ".join(x["name"] for x in w.get("authors", []))[:30], w.get("first_publish_year")) for w in d.get("works", [])]

def ruta_b(tema, sort=None):
    p = {"q": f'subject:"{tema}"', "limit": 5, "fields": CAMPOS}
    if sort: p["sort"] = sort
    s, d = get("https://openlibrary.org/search.json", p)
    if s != 200: return [f"HTTP {s}"]
    return [fila(w.get("title"), ", ".join(w.get("author_name", []))[:30], w.get("first_publish_year")) for w in d.get("docs", [])]

for tema in TEMAS:
    print(f"\n######## {tema}")
    print("A) /subjects (orden por defecto)"); [print("   ", x) for x in ruta_a(tema)]
    for sort in [None, "rating", "readinglog", "want_to_read", "already_read", "editions", "new"]:
        print(f"B) search.json subject, sort={sort}"); [print("   ", x) for x in ruta_b(tema, sort)]
