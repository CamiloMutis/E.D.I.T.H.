"""Genera los datos de arranque y entrena los dos modelos.

Uso:
    python -m ml.entrenar                # entrena con los datos que haya en data/
    python -m ml.entrenar --regenerar    # vuelve a generar los datos sintéticos

El bot ejecuta este mismo proceso al arrancar si no encuentra los modelos.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from ajustes import RAIZ
from ml import clasificador, predictor_tiempo
from ml.corpus_sintetico import generar_corpus
from ml.datos_sinteticos import generar_tiempos

RUTA_PENSUM = RAIZ / "data" / "pensum.json"
RUTA_TIEMPOS = RAIZ / "data" / "tiempos_sinteticos.csv"
RUTA_ENCUESTA = RAIZ / "data" / "encuesta_tiempos.csv"
RUTA_CORPUS = RAIZ / "data" / "corpus_materias.csv"
CARPETA_MODELOS = RAIZ / "ml" / "modelos"


def asegurar_datos(regenerar: bool = False) -> None:
    """Crea los datasets sintéticos si no existen (o si se pide regenerarlos)."""
    if regenerar or not RUTA_TIEMPOS.exists():
        generar_tiempos(RUTA_PENSUM).to_csv(RUTA_TIEMPOS, index=False)
    if regenerar or not RUTA_CORPUS.exists():
        generar_corpus(RUTA_PENSUM).to_csv(RUTA_CORPUS, index=False)


def cargar_tiempos(extra: list[dict] | None = None) -> pd.DataFrame:
    """Une los datos sintéticos, la encuesta (si existe) y el historial de uso."""
    partes = [pd.read_csv(RUTA_TIEMPOS)]
    if RUTA_ENCUESTA.exists():
        encuesta = pd.read_csv(RUTA_ENCUESTA)
        encuesta["fuente"] = "encuesta"
        partes.append(encuesta)
    if extra:
        partes.append(pd.DataFrame(extra))
    return pd.concat(partes, ignore_index=True)


def entrenar_todo(regenerar: bool = False, carpeta: Path = CARPETA_MODELOS) -> dict:
    """Entrena el predictor y el clasificador y guarda sus métricas en ``metricas.json``."""
    asegurar_datos(regenerar)
    metricas = {
        "predictor": predictor_tiempo.entrenar_y_guardar(cargar_tiempos(), carpeta),
        "clasificador": clasificador.entrenar_y_guardar(pd.read_csv(RUTA_CORPUS), carpeta),
    }
    (carpeta / "metricas.json").write_text(
        json.dumps(metricas, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return metricas


def main() -> None:
    analizador = argparse.ArgumentParser(description="Entrena los modelos de E.D.I.T.H.")
    analizador.add_argument("--regenerar", action="store_true", help="regenera los datos sintéticos")
    argumentos = analizador.parse_args()

    metricas = entrenar_todo(argumentos.regenerar)
    p, c = metricas["predictor"], metricas["clasificador"]
    print(f"Predictor de tiempo: {p['modelo']} con {p['filas']} filas")
    print(f"  MAE {p['MAE']:.2f} h (línea base {p['MAE_linea_base']:.2f} h), R² {p['R2']:.2f}")
    print(f"  Reduce el error un {p['reduccion_error']:.0%} frente a creerle al estimado")
    print(f"Clasificador: {c['modelo']} con {c['documentos']} documentos de {c['materias']} materias")
    print(f"  Accuracy {c['accuracy']:.3f}, F1 macro {c['F1_macro']:.3f}")


if __name__ == "__main__":
    main()
