"""Skill ``estimar_tiempo`` (Predecir).

Corrige la estimación del estudiante con el modelo entrenado sobre el
historial de tiempos. Lo usan el agente directamente y los skills
``planificar_entrega`` y ``replanificar``.
"""

from __future__ import annotations

import statistics

from ml.datos_sinteticos import TIPOS_TAREA
from skills.contexto import Contexto, dia_semana, formato_horas, franja

NOMBRES_TIPO = {
    "informe": "los informes", "taller": "los talleres", "lectura": "las lecturas",
    "codigo": "las tareas de código", "estudio": "el estudio para exámenes",
    "presentacion": "las presentaciones",
}
MINIMO_HISTORIAL_PROPIO = 5  # filas de uso real necesarias para hablar de "tus" tiempos
FACTOR_SIN_MODELO = 1.3


def normalizar_tipo(tipo_tarea: str | None) -> str:
    tipo = (tipo_tarea or "").strip().lower().replace("ó", "o").replace("á", "a")
    return tipo if tipo in TIPOS_TAREA else "taller"


def _desviacion_historica(ctx: Contexto, tipo: str) -> tuple[float | None, bool]:
    """Mediana de real/estimado para el tipo de tarea y si proviene del uso propio."""
    historial = [h for h in ctx.repo.historial() if h["tipo_tarea"] == tipo and h["estimado_h"]]
    propio = [h for h in historial if h["fuente"] == "uso"]
    es_propio = len(propio) >= MINIMO_HISTORIAL_PROPIO
    base = propio if es_propio else historial
    if not base:
        return None, False
    return statistics.median(h["real_h"] / h["estimado_h"] for h in base), es_propio


def _estimado_tipico(ctx: Contexto, tipo: str) -> float:
    estimados = [h["estimado_h"] for h in ctx.repo.historial() if h["tipo_tarea"] == tipo]
    return float(statistics.median(estimados)) if estimados else 2.0


def estimar_tiempo(
    ctx: Contexto,
    materia: str,
    tipo_tarea: str,
    dificultad: int = 3,
    estimado_h: float | None = None,
    dia: str | None = None,
    franja_horaria: str | None = None,
) -> dict:
    """Estima cuántas horas reales tomará una subtarea.

    Args:
        materia: nombre de la materia.
        tipo_tarea: informe, taller, lectura, codigo, estudio o presentacion.
        dificultad: de 1 (muy fácil) a 5 (muy difícil).
        estimado_h: horas que el estudiante cree que tardará. Si no la da, se
            usa la mediana histórica de ese tipo de tarea.
        dia: día de la semana previsto; por defecto, hoy.
        franja_horaria: manana, tarde o noche; por defecto, la actual.

    Returns:
        ``horas`` (predicción), ``horas_usuario``, ``mensaje`` explicativo y
        ``fuente`` (modelo o regla de respaldo).
    """
    ahora = ctx.ahora()
    tipo = normalizar_tipo(tipo_tarea)
    dificultad = max(1, min(5, int(dificultad or 3)))
    encontrada = ctx.repo.buscar_materia(materia)
    nombre_materia = encontrada["nombre"] if encontrada else materia
    estimado = float(estimado_h) if estimado_h else _estimado_tipico(ctx, tipo)
    estimado = max(0.25, min(estimado, 40.0))

    desviacion, es_propia = _desviacion_historica(ctx, tipo)
    if ctx.predictor.disponible:
        horas = ctx.predictor.predecir(
            nombre_materia, tipo, dificultad, estimado, dia or dia_semana(ahora),
            franja_horaria or franja(ahora),
        )
        fuente = "modelo"
    else:
        horas = round(estimado * (desviacion or FACTOR_SIN_MODELO) * 4) / 4
        fuente = "regla"

    if estimado_h:
        mensaje = f"Estimaste {formato_horas(estimado)}; lo esperado es {formato_horas(horas)}."
    else:
        mensaje = f"Sin estimación tuya, lo esperado es {formato_horas(horas)}."
    if desviacion and abs(desviacion - 1) >= 0.1:
        quien = "sueles" if es_propia else "los estudiantes suelen"
        verbo = "subestimar" if desviacion > 1 else "sobrestimar"
        mensaje += f" Con {NOMBRES_TIPO[tipo]}, {quien} {verbo} el tiempo en un {abs(desviacion - 1):.0%}."

    return {
        "materia": nombre_materia,
        "tipo_tarea": tipo,
        "dificultad": dificultad,
        "horas_usuario": float(estimado_h) if estimado_h else None,
        "horas": horas,
        "mensaje": mensaje,
        "fuente": fuente,
    }


def reentrenar_predictor(ctx: Contexto) -> dict:
    """Reentrena el predictor con el dataset inicial más el historial de uso real.

    Las filas de uso son las que el estudiante genera al registrar cuánto tardó
    de verdad; con ellas el modelo se va ajustando a su forma de trabajar.
    """
    from ml import predictor_tiempo
    from ml.entrenar import asegurar_datos, cargar_tiempos

    asegurar_datos()
    uso = [
        {k: h[k] for k in ("materia", "tipo_tarea", "dificultad", "estimado_h", "real_h", "dia_semana", "franja", "fuente")}
        for h in ctx.repo.historial(fuente="uso")
    ]
    metricas = predictor_tiempo.entrenar_y_guardar(cargar_tiempos(uso), ctx.ajustes.carpeta_modelos)
    ctx.predictor.recargar()
    metricas["filas_de_uso"] = len(uso)
    return metricas
