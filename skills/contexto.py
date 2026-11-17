"""Dependencias compartidas por todos los skills."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from ajustes import DIAS_SEMANA, Ajustes
from db.repositorio import Repositorio
from integraciones.calendario import Calendario
from ml.clasificador import ClasificadorMaterias
from ml.predictor_tiempo import PredictorTiempo

MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
DIAS_CORTOS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


@dataclass
class Contexto:
    """Todo lo que un skill necesita para trabajar.

    Reunirlo en un objeto permite probar cada skill con una base de datos
    temporal, un calendario local y un reloj fijo.
    """

    repo: Repositorio
    calendario: Calendario
    ajustes: Ajustes
    predictor: PredictorTiempo
    clasificador: ClasificadorMaterias
    reloj: Callable[[], datetime] | None = None
    moodle: Any = None  # ClienteMoodle; se crea al primer uso en skills.consultar_moodle

    def ahora(self) -> datetime:
        return self.reloj() if self.reloj else datetime.now(self.ajustes.zona)


def dia_semana(momento: datetime) -> str:
    return DIAS_SEMANA[momento.weekday()]


def franja(momento: datetime) -> str:
    if momento.hour < 12:
        return "manana"
    return "tarde" if momento.hour < 18 else "noche"


def formato_hora(momento: datetime) -> str:
    hora = momento.hour % 12 or 12
    sufijo = "a. m." if momento.hour < 12 else "p. m."
    minutos = f":{momento.minute:02d}" if momento.minute else ""
    return f"{hora}{minutos} {sufijo}"


def formato_fecha(momento: datetime) -> str:
    return f"{DIAS_CORTOS[momento.weekday()]} {momento.day} {MESES[momento.month - 1]}"


def formato_bloque(inicio: datetime, fin: datetime) -> str:
    """Ejemplo: ``mar 17 nov, 7 p. m. a 9 p. m.``"""
    return f"{formato_fecha(inicio)}, {formato_hora(inicio)} a {formato_hora(fin)}"


def formato_horas(horas: float) -> str:
    """Ejemplo: 4.5 -> ``4,5 h``; 2.0 -> ``2 h``."""
    texto = f"{horas:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{texto} h"
