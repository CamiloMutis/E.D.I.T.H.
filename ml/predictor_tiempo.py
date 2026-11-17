"""Predictor de tiempo real de una subtarea (regresión).

Entrada: materia, tipo de tarea, dificultad, estimación del estudiante, día y
franja horaria. Salida: horas que la subtarea tomará en realidad.

El módulo tiene dos partes: las funciones de entrenamiento y evaluación, que
usan los notebooks y el reentreno semanal, y la clase ``PredictorTiempo``, que
es lo que el skill ``estimar_tiempo`` carga para predecir.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.compose import ColumnTransformer, TransformedTargetRegressor
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, r2_score, root_mean_squared_error
from sklearn.model_selection import KFold, cross_val_score, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

registro = logging.getLogger(__name__)

CATEGORICAS = ["materia", "tipo_tarea", "dia_semana", "franja"]
NUMERICAS = ["dificultad", "estimado_h"]
VARIABLES = CATEGORICAS + NUMERICAS
OBJETIVO = "real_h"
HORAS_MAXIMAS = 40.0
NOMBRE_MODELO = "predictor_tiempo.joblib"


def limpiar(datos: pd.DataFrame) -> pd.DataFrame:
    """Quita filas incompletas o con valores imposibles (por ejemplo, más de 40 horas)."""
    datos = datos.dropna(subset=VARIABLES + [OBJETIVO]).copy()
    datos["dificultad"] = datos["dificultad"].astype(int)
    validas = (
        datos["estimado_h"].between(0.1, HORAS_MAXIMAS)
        & datos[OBJETIVO].between(0.1, HORAS_MAXIMAS)
        & datos["dificultad"].between(1, 5)
    )
    return datos[validas].reset_index(drop=True)


def _preprocesador() -> ColumnTransformer:
    return ColumnTransformer(
        [
            ("categoricas", OneHotEncoder(handle_unknown="ignore"), CATEGORICAS),
            ("numericas", StandardScaler(), NUMERICAS),
        ]
    )


def construir_modelos(semilla: int = 42) -> dict[str, Pipeline]:
    """Modelos candidatos. Todos predicen el logaritmo de las horas.

    El error al estimar es proporcional al tamaño de la tarea (una tarea larga
    se desvía más horas que una corta), así que entrenar sobre el logaritmo
    estabiliza la varianza y evita predicciones negativas.
    """
    regresores = {
        "Regresión lineal": LinearRegression(),
        "Random Forest": RandomForestRegressor(
            n_estimators=200, min_samples_leaf=3, random_state=semilla
        ),
        "Gradient Boosting": GradientBoostingRegressor(
            n_estimators=200, max_depth=3, learning_rate=0.05, random_state=semilla
        ),
    }
    return {
        nombre: Pipeline(
            [
                ("pre", _preprocesador()),
                (
                    "modelo",
                    TransformedTargetRegressor(regressor=regresor, func=np.log1p, inverse_func=np.expm1),
                ),
            ]
        )
        for nombre, regresor in regresores.items()
    }


def _metricas(reales, predichas) -> dict[str, float]:
    return {
        "MAE": float(mean_absolute_error(reales, predichas)),
        "RMSE": float(root_mean_squared_error(reales, predichas)),
        "R2": float(r2_score(reales, predichas)),
    }


def evaluar(datos: pd.DataFrame, semilla: int = 42) -> dict:
    """Entrena los candidatos y los compara con la línea base ``real = estimado``.

    Returns:
        Diccionario con la tabla de resultados, el nombre del mejor modelo
        (menor MAE en validación cruzada), los modelos ajustados y la
        partición de prueba, para que el notebook pueda graficar.
    """
    datos = limpiar(datos)
    X, y = datos[VARIABLES], datos[OBJETIVO]
    X_ent, X_pru, y_ent, y_pru = train_test_split(X, y, test_size=0.2, random_state=semilla)
    pliegues = KFold(n_splits=5, shuffle=True, random_state=semilla)

    filas = [{"modelo": "Línea base (real = estimado)", "MAE_cv": np.nan, **_metricas(y_pru, X_pru["estimado_h"])}]
    ajustados: dict[str, Pipeline] = {}
    for nombre, modelo in construir_modelos(semilla).items():
        mae_cv = -cross_val_score(
            modelo, X_ent, y_ent, cv=pliegues, scoring="neg_mean_absolute_error"
        ).mean()
        modelo.fit(X_ent, y_ent)
        ajustados[nombre] = modelo
        filas.append({"modelo": nombre, "MAE_cv": float(mae_cv), **_metricas(y_pru, modelo.predict(X_pru))})

    resultados = pd.DataFrame(filas)
    mejor = resultados.dropna(subset=["MAE_cv"]).sort_values("MAE_cv").iloc[0]["modelo"]
    return {
        "resultados": resultados,
        "mejor": mejor,
        "modelos": ajustados,
        "X_prueba": X_pru,
        "y_prueba": y_pru,
        "datos": datos,
    }


def entrenar_y_guardar(datos: pd.DataFrame, carpeta: Path, semilla: int = 42) -> dict:
    """Evalúa, reentrena el mejor modelo con todos los datos y lo guarda.

    Returns:
        Las métricas del modelo elegido y de la línea base sobre el conjunto de prueba.
    """
    evaluacion = evaluar(datos, semilla)
    resultados, mejor = evaluacion["resultados"], evaluacion["mejor"]
    limpios = evaluacion["datos"]

    final = construir_modelos(semilla)[mejor]
    final.fit(limpios[VARIABLES], limpios[OBJETIVO])

    fila_mejor = resultados[resultados["modelo"] == mejor].iloc[0]
    fila_base = resultados.iloc[0]
    metricas = {
        "modelo": mejor,
        "filas": int(len(limpios)),
        "fuentes": limpios["fuente"].value_counts().to_dict() if "fuente" in limpios else {},
        "MAE": float(fila_mejor["MAE"]),
        "RMSE": float(fila_mejor["RMSE"]),
        "R2": float(fila_mejor["R2"]),
        "MAE_linea_base": float(fila_base["MAE"]),
        "reduccion_error": float(1 - fila_mejor["MAE"] / fila_base["MAE"]),
    }
    carpeta.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "modelo": final,
            "metricas": metricas,
            "entrenado_en": datetime.now().isoformat(timespec="seconds"),
            "version_sklearn": sklearn.__version__,
        },
        carpeta / NOMBRE_MODELO,
        compress=3,
    )
    return metricas


class PredictorTiempo:
    """Carga el modelo entrenado y predice las horas reales de una subtarea."""

    def __init__(self, carpeta: Path):
        self.ruta = Path(carpeta) / NOMBRE_MODELO
        self._paquete: dict | None = None
        self.recargar()

    def recargar(self) -> None:
        """Vuelve a leer el modelo del disco (después de un reentreno)."""
        self._paquete = None
        if not self.ruta.exists():
            return
        try:
            self._paquete = joblib.load(self.ruta)
        except Exception as error:  # modelo de otra versión de scikit-learn, archivo dañado
            registro.warning("No se pudo cargar %s: %s", self.ruta.name, error)

    @property
    def disponible(self) -> bool:
        return self._paquete is not None

    @property
    def metricas(self) -> dict:
        return self._paquete["metricas"] if self._paquete else {}

    def predecir(
        self,
        materia: str,
        tipo_tarea: str,
        dificultad: int,
        estimado_h: float,
        dia_semana: str,
        franja: str,
    ) -> float:
        """Horas reales esperadas, redondeadas al cuarto de hora."""
        if not self._paquete:
            raise RuntimeError("El predictor de tiempo no está entrenado")
        fila = pd.DataFrame(
            [
                {
                    "materia": materia, "tipo_tarea": tipo_tarea, "dia_semana": dia_semana,
                    "franja": franja, "dificultad": int(dificultad), "estimado_h": float(estimado_h),
                }
            ]
        )[VARIABLES]
        horas = float(self._paquete["modelo"].predict(fila)[0])
        return float(np.clip(round(horas * 4) / 4, 0.25, HORAS_MAXIMAS))
