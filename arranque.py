"""Ensamblado de la aplicación: base de datos, modelos, calendario y agente."""

from __future__ import annotations

import logging

from ajustes import RAIZ, Ajustes
from db.repositorio import Repositorio
from ml.clasificador import ClasificadorMaterias
from ml.predictor_tiempo import PredictorTiempo
from skills.contexto import Contexto

registro = logging.getLogger(__name__)


def construir_contexto(ajustes: Ajustes, calendario=None) -> Contexto:
    """Deja todo listo para que los skills trabajen.

    En la primera ejecución crea la base de datos con las materias del pénsum
    y el historial inicial, y entrena los modelos si no están en disco o si
    se guardaron con una versión incompatible de scikit-learn.
    """
    from ml import entrenar

    repo = Repositorio(ajustes.ruta_db)
    if repo.sembrar_pensum(RAIZ / "data" / "pensum.json", ajustes.semestre_actual):
        registro.info("Base de datos creada con el pénsum; semestre activo: %s", ajustes.semestre_actual)

    predictor = PredictorTiempo(ajustes.carpeta_modelos)
    clasificador = ClasificadorMaterias(ajustes.carpeta_modelos)
    if not (predictor.disponible and clasificador.disponible):
        registro.info("Entrenando los modelos por primera vez; tarda menos de un minuto")
        entrenar.entrenar_todo(carpeta=ajustes.carpeta_modelos)
        predictor.recargar()
        clasificador.recargar()
    entrenar.asegurar_datos()
    repo.sembrar_historial(entrenar.RUTA_TIEMPOS)

    if calendario is None:
        from integraciones.google_calendar import crear_calendario

        calendario = crear_calendario(ajustes, repo)
    registro.info("Calendario en uso: %s", calendario.nombre)
    return Contexto(repo, calendario, ajustes, predictor, clasificador)
