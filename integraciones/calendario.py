"""Interfaz común de calendario y su versión local.

Los skills hablan con un ``Calendario`` sin saber si detrás está Google
Calendar o la tabla ``eventos_locales`` de SQLite. El calendario local permite
usar y probar todo el sistema sin credenciales de Google.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from db.repositorio import Repositorio


@dataclass(frozen=True)
class Evento:
    id: str
    titulo: str
    inicio: datetime
    fin: datetime


class Calendario(Protocol):
    """Operaciones que el agente necesita de cualquier calendario."""

    nombre: str

    def listar_eventos(self, desde: datetime, hasta: datetime) -> list[Evento]: ...

    def crear_evento(self, titulo: str, inicio: datetime, fin: datetime, descripcion: str = "") -> str: ...

    def mover_evento(self, evento_id: str, inicio: datetime, fin: datetime) -> None: ...

    def eliminar_evento(self, evento_id: str) -> None: ...


class CalendarioLocal:
    """Calendario guardado en la base de datos del proyecto."""

    nombre = "local"

    def __init__(self, repo: Repositorio, zona: ZoneInfo):
        self.repo = repo
        self.zona = zona

    def listar_eventos(self, desde: datetime, hasta: datetime) -> list[Evento]:
        filas = self.repo.eventos_locales_entre(desde.astimezone(self.zona), hasta.astimezone(self.zona))
        return [
            Evento(
                id=str(f["id"]),
                titulo=f["titulo"],
                inicio=datetime.fromisoformat(f["inicio"]),
                fin=datetime.fromisoformat(f["fin"]),
            )
            for f in filas
        ]

    def crear_evento(self, titulo: str, inicio: datetime, fin: datetime, descripcion: str = "") -> str:
        return str(
            self.repo.crear_evento_local(
                titulo, inicio.astimezone(self.zona), fin.astimezone(self.zona), descripcion
            )
        )

    def mover_evento(self, evento_id: str, inicio: datetime, fin: datetime) -> None:
        self.repo.mover_evento_local(
            int(evento_id), inicio.astimezone(self.zona), fin.astimezone(self.zona)
        )

    def eliminar_evento(self, evento_id: str) -> None:
        self.repo.eliminar_evento_local(int(evento_id))
