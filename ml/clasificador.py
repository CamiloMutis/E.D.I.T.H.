"""Clasificador de archivos por materia (clasificación de texto).

Recibe el texto de un documento y devuelve la materia más probable con su
nivel de confianza. Es rápido y gratuito, así que atiende todos los archivos;
el LLM solo interviene cuando la confianza es baja.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import joblib
import pandas as pd
import sklearn
from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import StratifiedKFold, cross_val_score, train_test_split
from sklearn.naive_bayes import MultinomialNB
from sklearn.pipeline import Pipeline
from sklearn.svm import LinearSVC

registro = logging.getLogger(__name__)

NOMBRE_MODELO = "clasificador_materias.joblib"
LONGITUD_MINIMA = 20  # con menos texto no hay de dónde clasificar

# Palabras muy frecuentes en español que no distinguen una materia de otra.
# Van sin tildes porque el vectorizador las quita antes de comparar.
PALABRAS_VACIAS = sorted(
    set(
        """a al algo ante antes como con contra cual cuando de del desde donde durante e el ella
        ellas ellos en entre era es esa esas ese eso esos esta estan estas este esto estos fue ha
        han hasta hay la las le les lo los mas me mi muy no nos o otra otro para pero por que
        se segun ser si sin sobre son su sus tambien tiene todo todos un una uno unos y ya cada
        puede debe sera cuales sea""".split()
    )
)


def _vectorizador() -> TfidfVectorizer:
    return TfidfVectorizer(
        lowercase=True,
        strip_accents="unicode",
        stop_words=PALABRAS_VACIAS,
        ngram_range=(1, 2),
        sublinear_tf=True,
        min_df=2,
        token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z]+\b",  # ignora números (páginas, fechas)
    )


def construir_modelos(semilla: int = 42) -> dict[str, Pipeline]:
    """Modelos candidatos sobre la misma representación TF-IDF."""
    clasificadores = {
        "Regresión logística": LogisticRegression(C=10.0, max_iter=2000),
        "SVM lineal": CalibratedClassifierCV(LinearSVC(C=1.0, random_state=semilla), cv=3),
        "Naive Bayes": MultinomialNB(alpha=0.1),
    }
    return {
        nombre: Pipeline([("tfidf", _vectorizador()), ("modelo", clasificador)])
        for nombre, clasificador in clasificadores.items()
    }


def evaluar(corpus: pd.DataFrame, semilla: int = 42) -> dict:
    """Compara los candidatos con validación cruzada y una partición de prueba estratificada."""
    corpus = corpus.dropna(subset=["texto", "materia"])
    X_ent, X_pru, y_ent, y_pru = train_test_split(
        corpus["texto"], corpus["materia"], test_size=0.2, stratify=corpus["materia"], random_state=semilla
    )
    pliegues = StratifiedKFold(n_splits=5, shuffle=True, random_state=semilla)

    filas = []
    ajustados: dict[str, Pipeline] = {}
    for nombre, modelo in construir_modelos(semilla).items():
        f1_cv = cross_val_score(modelo, X_ent, y_ent, cv=pliegues, scoring="f1_macro").mean()
        modelo.fit(X_ent, y_ent)
        ajustados[nombre] = modelo
        predichas = modelo.predict(X_pru)
        filas.append(
            {
                "modelo": nombre,
                "F1_macro_cv": float(f1_cv),
                "accuracy": float(accuracy_score(y_pru, predichas)),
                "F1_macro": float(f1_score(y_pru, predichas, average="macro")),
            }
        )
    resultados = pd.DataFrame(filas)
    mejor = resultados.sort_values("F1_macro_cv", ascending=False).iloc[0]["modelo"]
    return {
        "resultados": resultados,
        "mejor": mejor,
        "modelos": ajustados,
        "X_prueba": X_pru,
        "y_prueba": y_pru,
        "corpus": corpus,
    }


def entrenar_y_guardar(corpus: pd.DataFrame, carpeta: Path, semilla: int = 42) -> dict:
    """Evalúa, reentrena el mejor modelo con todo el corpus y lo guarda."""
    evaluacion = evaluar(corpus, semilla)
    resultados, mejor = evaluacion["resultados"], evaluacion["mejor"]
    limpio = evaluacion["corpus"]

    final = construir_modelos(semilla)[mejor]
    final.fit(limpio["texto"], limpio["materia"])

    fila = resultados[resultados["modelo"] == mejor].iloc[0]
    metricas = {
        "modelo": mejor,
        "documentos": int(len(limpio)),
        "materias": int(limpio["materia"].nunique()),
        "accuracy": float(fila["accuracy"]),
        "F1_macro": float(fila["F1_macro"]),
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


class ClasificadorMaterias:
    """Carga el modelo entrenado y clasifica textos."""

    def __init__(self, carpeta: Path):
        self.ruta = Path(carpeta) / NOMBRE_MODELO
        self._paquete: dict | None = None
        self.recargar()

    def recargar(self) -> None:
        self._paquete = None
        if not self.ruta.exists():
            return
        try:
            self._paquete = joblib.load(self.ruta)
        except Exception as error:
            registro.warning("No se pudo cargar %s: %s", self.ruta.name, error)

    @property
    def disponible(self) -> bool:
        return self._paquete is not None

    @property
    def metricas(self) -> dict:
        return self._paquete["metricas"] if self._paquete else {}

    @property
    def materias(self) -> list[str]:
        return list(self._paquete["modelo"].classes_) if self._paquete else []

    def clasificar(self, texto: str, permitidas: list[str] | None = None) -> list[tuple[str, float]]:
        """Materias candidatas ordenadas de mayor a menor probabilidad.

        Args:
            texto: contenido del documento.
            permitidas: si se indica, solo se consideran estas materias (las
                que el estudiante cursa) y las probabilidades se reparten
                entre ellas. Descartar de entrada las materias de otros
                semestres evita la mayoría de las confusiones.

        Returns:
            Lista de pares ``(materia, probabilidad)``; vacía si el modelo no
            está disponible, el texto es muy corto o no reconoce en él ningún
            término del vocabulario.
        """
        if not self._paquete or len((texto or "").strip()) < LONGITUD_MINIMA:
            return []
        modelo: Pipeline = self._paquete["modelo"]
        if modelo.named_steps["tfidf"].transform([texto]).nnz == 0:
            return []
        probabilidades = modelo.predict_proba([texto])[0]
        clases = list(modelo.classes_)
        if permitidas is not None:
            indices = [i for i, clase in enumerate(clases) if clase in set(permitidas)]
            if not indices:
                return []
            total = float(probabilidades[indices].sum())
            if total <= 0:
                return []
            pares = [(str(clases[i]), float(probabilidades[i] / total)) for i in indices]
        else:
            pares = [(str(clase), float(p)) for clase, p in zip(clases, probabilidades)]
        return sorted(pares, key=lambda par: par[1], reverse=True)

    def confianza_absoluta(self, texto: str, materia: str) -> float:
        """Probabilidad de ``materia`` frente a todas las del pénsum, sin restringir."""
        for clase, probabilidad in self.clasificar(texto):
            if clase == materia:
                return probabilidad
        return 0.0


def combinar_corpus(base: pd.DataFrame, ejemplos: list[dict]) -> pd.DataFrame:
    """Suma al corpus los ejemplos que el usuario etiquetó cuando el modelo dudó."""
    if not ejemplos:
        return base
    extra = pd.DataFrame(ejemplos)[["texto", "materia"]]
    # Cada ejemplo confirmado pesa más que un documento sintético.
    extra = pd.concat([extra] * 5, ignore_index=True)
    return pd.concat([base, extra], ignore_index=True)

