"""Elementos compartidos por las pruebas: base de datos temporal, calendario local y reloj fijo."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from ajustes import RAIZ, Ajustes
from db.repositorio import Repositorio
from integraciones.calendario import CalendarioLocal
from ml.clasificador import ClasificadorMaterias
from ml.predictor_tiempo import PredictorTiempo
from skills.contexto import Contexto

ZONA = ZoneInfo("America/Bogota")
# Lunes 16 de noviembre de 2026, 8:00 a. m.
AHORA = datetime(2026, 11, 16, 8, 0, tzinfo=ZONA)


def momento(dia: int, hora: int, minuto: int = 0) -> datetime:
    """Fecha de noviembre de 2026 en la zona de las pruebas."""
    return datetime(2026, 11, dia, hora, minuto, tzinfo=ZONA)


class Reloj:
    """Reloj que las pruebas pueden adelantar."""

    def __init__(self, inicial: datetime):
        self.actual = inicial

    def __call__(self) -> datetime:
        return self.actual


@pytest.fixture(scope="session")
def modelos():
    """Modelos entrenados; si el repositorio no los trae, se entrenan una vez."""
    from ml import entrenar

    carpeta = RAIZ / "ml" / "modelos"
    predictor, clasificador = PredictorTiempo(carpeta), ClasificadorMaterias(carpeta)
    if not (predictor.disponible and clasificador.disponible):
        entrenar.entrenar_todo(carpeta=carpeta)
        predictor.recargar()
        clasificador.recargar()
    return predictor, clasificador


@pytest.fixture
def ctx(tmp_path, modelos) -> Contexto:
    ajustes = Ajustes(
        zona=ZONA,
        carpeta_vigilada=tmp_path / "Descargas",
        carpeta_destino=tmp_path / "Universidad",
        ruta_db=tmp_path / "prueba.db",
        hora_resumen=time(7, 0),
    )
    ajustes.carpeta_vigilada.mkdir()
    repo = Repositorio(ajustes.ruta_db)
    repo.sembrar_pensum(RAIZ / "data" / "pensum.json", semestre_actual=7)
    repo.sembrar_historial(RAIZ / "data" / "tiempos_sinteticos.csv")
    contexto = Contexto(repo, CalendarioLocal(repo, ZONA), ajustes, *modelos, reloj=Reloj(AHORA))
    yield contexto
    repo.cerrar()
