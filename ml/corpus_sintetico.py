"""Generación del corpus sintético para el clasificador de archivos.

El clasificador aprende a qué materia pertenece un documento a partir de su
texto. Lo ideal es entrenarlo con archivos reales del estudiante; mientras no
los haya, este módulo construye documentos de práctica (talleres, guías,
diapositivas, parciales) combinando el vocabulario propio de cada materia del
pénsum con frases académicas genéricas y algo de ruido de materias afines.

El corpus resultante sirve para dejar funcionando el pipeline y el agente. Las
métricas obtenidas con él son optimistas: los documentos reales son más largos,
más variados y comparten más vocabulario entre materias.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ENCABEZADOS = [
    "Taller {n}", "Guía de laboratorio {n}", "Diapositivas clase {n}", "Parcial {n}",
    "Quiz {n}", "Lectura complementaria {n}", "Enunciado del proyecto", "Actividad en clase {n}",
    "Trabajo independiente {n}", "Unidad {n}", "Resumen de la sesión {n}", "Ejercicios propuestos",
]

PLANTILLAS = [
    "Explique con sus palabras {a}.",
    "Resuelva los ejercicios sobre {a} y {b}.",
    "En esta sesión se estudia {a}.",
    "Tema: {a}. Subtema: {b}.",
    "Describa la relación entre {a} y {b}.",
    "Punto {n}. Aplique {a} al caso propuesto.",
    "Objetivo: comprender {a} y su uso en {b}.",
    "Repaso de {a}.",
    "Desarrolle un ejemplo de {a}.",
    "Compare {a} con {b} y justifique su respuesta.",
    "Se evaluará el manejo de {a}.",
    "{a}: definición, características y ejemplos.",
    "Para la próxima clase, consulte {a}.",
    "Ejercicio {n}: {a}.",
]

GENERICAS = [
    "Entregue el trabajo en formato PDF por la plataforma.",
    "El trabajo se desarrolla en grupos de tres estudiantes.",
    "Fecha de entrega según el cronograma del curso.",
    "Universidad Santiago de Cali, Facultad de Ingeniería.",
    "Lea con atención cada punto antes de responder.",
    "Se tendrá en cuenta la presentación y la ortografía.",
    "Justifique todas sus respuestas.",
    "Bibliografía recomendada al final del documento.",
    "Docente del curso. Periodo académico 2026B.",
    "Valor de la actividad: quince por ciento de la nota del corte.",
    "No se reciben trabajos fuera de la fecha establecida.",
    "Resultados de aprendizaje de la unidad.",
    "Recuerde citar las fuentes consultadas.",
    "Duración de la prueba: noventa minutos.",
]


def cargar_vocabulario(ruta_pensum: Path) -> dict[str, dict]:
    """Materias del pénsum que tienen vocabulario propio (las electivas no lo tienen)."""
    materias = json.loads(Path(ruta_pensum).read_text(encoding="utf-8"))["materias"]
    return {m["nombre"]: m for m in materias if m.get("terminos")}


def generar_documento(rng: np.random.Generator, materia: dict, afines: list[dict]) -> str:
    terminos = materia["terminos"]
    cantidad = int(rng.integers(3, 11))
    propios = list(rng.choice(terminos, size=min(cantidad, len(terminos)), replace=False))
    # Ruido: términos de otra materia de la misma área, como pasa en la práctica
    # (un taller de Cálculo II menciona derivadas; uno de Redes Inalámbricas, el modelo OSI).
    ajenos: list[str] = []
    if afines and rng.random() < 0.6:
        otra = afines[int(rng.integers(len(afines)))]
        ajenos = list(rng.choice(otra["terminos"], size=int(rng.integers(1, 4)), replace=False))

    partes = [str(rng.choice(ENCABEZADOS)).format(n=int(rng.integers(1, 9)))]
    if rng.random() < 0.35:
        partes.append(materia["nombre"])
    frases = []
    bolsa = propios + ajenos
    rng.shuffle(bolsa)
    for termino in bolsa:
        plantilla = str(rng.choice(PLANTILLAS))
        frases.append(plantilla.format(a=termino, b=str(rng.choice(terminos)), n=int(rng.integers(1, 12))))
    for _ in range(int(rng.integers(1, 5))):
        frases.insert(int(rng.integers(0, len(frases) + 1)), str(rng.choice(GENERICAS)))
    return " ".join(partes + frases)


def generar_corpus(ruta_pensum: Path, por_materia: int = 60, semilla: int = 7) -> pd.DataFrame:
    """Devuelve un DataFrame ``texto, materia`` con ``por_materia`` documentos de cada materia."""
    rng = np.random.default_rng(semilla)
    vocabulario = cargar_vocabulario(ruta_pensum)
    filas = []
    for nombre, materia in vocabulario.items():
        afines = [
            m for n, m in vocabulario.items() if n != nombre and m.get("area") == materia.get("area")
        ]
        for _ in range(por_materia):
            filas.append({"texto": generar_documento(rng, materia, afines), "materia": nombre})
    return pd.DataFrame(filas).sample(frac=1.0, random_state=semilla).reset_index(drop=True)
