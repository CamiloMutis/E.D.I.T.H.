"""Generación del dataset sintético de tiempos de estudio.

Mientras no haya respuestas de la encuesta ni historial de uso, el predictor
necesita datos para arrancar. Este módulo los genera con reglas explícitas,
de modo que cualquiera puede revisar qué supuestos contiene. Todas las filas
llevan ``fuente = "sintetico"`` y nunca se presentan como datos reales.

Supuestos, basados en la falacia de la planificación (las personas subestiman
de forma sistemática lo que tardan):

* Cada tipo de tarea tiene una estimación típica y un factor de subestimación.
* La dificultad percibida alarga el tiempo real un 12 % por punto sobre 3.
* Algunas áreas (programación, matemáticas) se alargan más que otras.
* De noche y en fin de semana se rinde un poco menos.
* Un ruido multiplicativo representa la variación entre personas y días.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

TIPOS_TAREA = ["informe", "taller", "lectura", "codigo", "estudio", "presentacion"]
DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
FRANJAS = ["manana", "tarde", "noche"]

# Estimación típica del estudiante (horas) y cuánto se alarga en la realidad.
ESTIMADO_TIPICO = {"informe": 3.0, "taller": 2.5, "lectura": 1.5, "codigo": 4.0, "estudio": 4.0, "presentacion": 2.5}
FACTOR_TIPO = {"informe": 1.60, "taller": 1.35, "lectura": 1.15, "codigo": 1.80, "estudio": 1.25, "presentacion": 1.40}
FACTOR_AREA = {
    "programacion": 1.20, "matematicas": 1.15, "fisica": 1.10, "software": 1.10, "hardware": 1.05,
    "redes": 1.05, "proyecto": 1.10, "gestion": 1.00, "idiomas": 0.90, "humanidades": 0.90, "electiva": 1.00,
}
FACTOR_FRANJA = {"manana": 0.95, "tarde": 1.00, "noche": 1.12}
FACTOR_FIN_DE_SEMANA = 1.05
PROBABILIDAD_FRANJA = [0.20, 0.35, 0.45]
RUIDO = 0.22  # desviación del ruido log-normal

# Qué tipos de tarea son frecuentes en cada área.
TIPOS_POR_AREA = {
    "programacion": [0.10, 0.20, 0.05, 0.40, 0.20, 0.05],
    "matematicas": [0.02, 0.45, 0.05, 0.00, 0.46, 0.02],
    "fisica": [0.25, 0.30, 0.05, 0.00, 0.35, 0.05],
    "idiomas": [0.10, 0.30, 0.20, 0.00, 0.25, 0.15],
    "humanidades": [0.30, 0.10, 0.35, 0.00, 0.10, 0.15],
}
TIPOS_GENERAL = [0.25, 0.15, 0.15, 0.10, 0.15, 0.20]


def generar_tiempos(ruta_pensum: Path, filas: int = 600, semilla: int = 42) -> pd.DataFrame:
    """Devuelve un DataFrame con ``filas`` registros sintéticos de tiempo estimado y real."""
    rng = np.random.default_rng(semilla)
    materias = json.loads(Path(ruta_pensum).read_text(encoding="utf-8"))["materias"]

    registros = []
    for _ in range(filas):
        materia = materias[rng.integers(len(materias))]
        area = materia.get("area", "gestion")
        tipo = rng.choice(TIPOS_TAREA, p=TIPOS_POR_AREA.get(area, TIPOS_GENERAL))
        dificultad = int(np.clip(round(rng.normal(3.0, 1.0)), 1, 5))
        dia = rng.choice(DIAS)
        franja = rng.choice(FRANJAS, p=PROBABILIDAD_FRANJA)

        estimado = ESTIMADO_TIPICO[tipo] * rng.lognormal(0.0, 0.35) * (1 + 0.08 * (dificultad - 3))
        estimado = float(np.clip(round(estimado * 2) / 2, 0.5, 12.0))

        factor = (
            FACTOR_TIPO[tipo]
            * (1 + 0.12 * (dificultad - 3))
            * FACTOR_AREA.get(area, 1.0)
            * FACTOR_FRANJA[franja]
            * (FACTOR_FIN_DE_SEMANA if dia in ("sabado", "domingo") else 1.0)
        )
        real = estimado * factor * rng.lognormal(0.0, RUIDO)
        real = float(np.clip(round(real * 4) / 4, 0.25, 40.0))

        registros.append(
            {
                "materia": materia["nombre"], "tipo_tarea": tipo, "dificultad": dificultad,
                "estimado_h": estimado, "real_h": real, "dia_semana": dia, "franja": franja,
                "fuente": "sintetico",
            }
        )
    return pd.DataFrame(registros)
