"""Dependencias compartidas por todos los skills."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
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
    # Crea el cliente de Moodle del estudiante (con su token). Sin ella se usan
    # las credenciales de .env, como en el modo consola.
    fabrica_moodle: Callable[[], Any] | None = None

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


def formato_vence(momento: datetime) -> str:
    """Fecha límite legible. Moodle pone muchas a las 00:00: eso es la medianoche del día anterior.

    Ejemplo: ``dom 29 nov, 11:59 p. m.``; ``sáb 28 nov, 00:00`` -> ``vie 27 nov a medianoche``.
    """
    if momento.hour == 0 and momento.minute == 0:
        return f"{formato_fecha(momento - timedelta(minutes=1))} a medianoche"
    return f"{formato_fecha(momento)}, {formato_hora(momento)}"


def formato_faltan(vence: datetime, ahora: datetime) -> str:
    """Tiempo que queda hasta la fecha límite: ``faltan 2 días``, ``faltan 5 h``, ``faltan 40 min``."""
    segundos = (vence - ahora).total_seconds()
    if segundos <= 0:
        return "ya venció"
    horas = segundos / 3600
    if horas < 1:
        return f"faltan {max(1, int(segundos // 60))} min"
    if horas < 48:
        return "falta 1 h" if int(horas) == 1 else f"faltan {int(horas)} h"
    return f"faltan {int(horas // 24)} días"


def lista_por_materia(entregas: list[dict], ahora: datetime) -> str:
    """Entregas agrupadas por materia, una línea cada una: nombre, fecha y lo que falta.

    Cada entrega trae ``materia``, ``nombre``, ``vence`` (datetime) y, si se conoce, ``riesgo``.
    Las materias salen en el orden de su entrega más próxima.
    """
    grupos: dict[str, list[dict]] = {}
    for e in sorted(entregas, key=lambda e: e["vence"]):
        grupos.setdefault(e["materia"], []).append(e)
    bloques = []
    for materia, lista in grupos.items():
        lineas = [f"{materia}:"]
        for e in lista:
            marca = " ⚠️" if e.get("riesgo") == "en_riesgo" else ""
            lineas.append(f"• {e['nombre']} — {formato_vence(e['vence'])} — {formato_faltan(e['vence'], ahora)}{marca}")
        bloques.append("\n".join(lineas))
    texto = "\n\n".join(bloques)
    if any(e.get("riesgo") == "en_riesgo" for e in entregas):
        texto += "\n\n⚠️ = no alcanza el tiempo de estudio agendado. Escríbeme y lo reorganizamos."
    return texto
