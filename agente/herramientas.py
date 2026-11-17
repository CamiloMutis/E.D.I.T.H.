"""Registro de las herramientas que el LLM puede invocar.

Cada herramienta tiene su esquema JSON (nombre, descripción y parámetros), que
es lo que el LLM lee para decidir cuándo usarla, y la función de Python que la
ejecuta. La descripción sale del docstring del skill.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Callable

from skills import soporte
from skills.consultar_moodle import consultar_moodle
from skills.estimar_tiempo import estimar_tiempo
from skills.organizar_archivos import organizar_archivos
from skills.planificar_entrega import planificar_entrega
from skills.replanificar import replanificar

TIPOS = ["informe", "taller", "lectura", "codigo", "estudio", "presentacion"]


@dataclass(frozen=True)
class Herramienta:
    nombre: str
    funcion: Callable
    parametros: dict

    @property
    def descripcion(self) -> str:
        """Primer párrafo del docstring: lo que hace el skill, en una o dos frases."""
        return " ".join((inspect.getdoc(self.funcion) or "").split("\n\n")[0].split())

    def esquema(self) -> dict:
        return {"nombre": self.nombre, "descripcion": self.descripcion, "parametros": self.parametros}


def _objeto(propiedades: dict, requeridos: list[str]) -> dict:
    return {"type": "object", "properties": propiedades, "required": requeridos}


HERRAMIENTAS: list[Herramienta] = [
    Herramienta(
        "registrar_entrega",
        soporte.registrar_entrega,
        _objeto(
            {
                "materia": {"type": "string", "description": "Nombre de la materia"},
                "nombre": {"type": "string", "description": "Nombre corto de la entrega, p. ej. 'Taller 3'"},
                "fecha_limite": {"type": "string", "description": "AAAA-MM-DD o AAAA-MM-DDTHH:MM"},
                "peso_nota": {"type": "number", "description": "Porcentaje de la nota, p. ej. 15"},
                "descripcion": {"type": "string", "description": "Tema o enunciado"},
            },
            ["materia", "nombre", "fecha_limite"],
        ),
    ),
    Herramienta(
        "planificar_entrega",
        planificar_entrega,
        _objeto(
            {
                "entrega_id": {"type": "integer", "description": "Identificador devuelto por registrar_entrega"},
                "subtareas": {
                    "type": "array",
                    "description": "De 3 a 6 pasos concretos, en el orden en que se deben hacer",
                    "items": _objeto(
                        {
                            "nombre": {"type": "string"},
                            "tipo_tarea": {"type": "string", "enum": TIPOS},
                            "dificultad": {"type": "integer", "description": "De 1 (fácil) a 5 (difícil)"},
                            "horas_usuario": {
                                "type": "number",
                                "description": "Horas que el estudiante cree que tardará; omitir si no lo dijo",
                            },
                        },
                        ["nombre", "tipo_tarea", "dificultad"],
                    ),
                },
            },
            ["entrega_id", "subtareas"],
        ),
    ),
    Herramienta(
        "estimar_tiempo",
        estimar_tiempo,
        _objeto(
            {
                "materia": {"type": "string"},
                "tipo_tarea": {"type": "string", "enum": TIPOS},
                "dificultad": {"type": "integer", "description": "De 1 a 5"},
                "estimado_h": {"type": "number", "description": "Horas que el estudiante cree que tardará"},
            },
            ["materia", "tipo_tarea"],
        ),
    ),
    Herramienta(
        "replanificar",
        replanificar,
        _objeto(
            {
                "horas_extra_dia": {
                    "type": "number",
                    "description": "Horas adicionales al tope diario que el estudiante aceptó estudiar",
                }
            },
            [],
        ),
    ),
    Herramienta(
        "organizar_archivos",
        organizar_archivos,
        _objeto(
            {
                "accion": {"type": "string", "enum": ["clasificar", "consultar"]},
                "ruta": {"type": "string", "description": "Ruta del archivo a clasificar"},
                "materia": {"type": "string", "description": "Materia a consultar"},
                "entrega": {"type": "string", "description": "Entrega a consultar, p. ej. 'quiz 2'"},
            },
            ["accion"],
        ),
    ),
    Herramienta("listar_pendientes", soporte.listar_pendientes, _objeto({}, [])),
    Herramienta(
        "consultar_calendario",
        soporte.consultar_calendario,
        _objeto(
            {
                "desde": {"type": "string", "description": "AAAA-MM-DD"},
                "hasta": {"type": "string", "description": "AAAA-MM-DD, incluido"},
            },
            ["desde", "hasta"],
        ),
    ),
    Herramienta(
        "consultar_moodle",
        consultar_moodle,
        _objeto(
            {
                "dias": {"type": "integer", "description": "Días hacia adelante, de 1 a 90; 14 si no lo dice"},
                "curso": {"type": "string", "description": "Parte del nombre del curso para filtrar"},
            },
            [],
        ),
    ),
    Herramienta(
        "registrar_avance",
        soporte.registrar_avance,
        _objeto(
            {
                "subtarea_id": {"type": "integer"},
                "terminada": {"type": "boolean"},
                "horas_reales": {"type": "number", "description": "Horas que tomó en total"},
                "horas_restantes": {"type": "number", "description": "Horas que faltan, si no la terminó"},
            },
            ["subtarea_id", "terminada"],
        ),
    ),
    Herramienta(
        "marcar_entregada",
        soporte.marcar_entregada,
        _objeto({"entrega_id": {"type": "integer"}}, ["entrega_id"]),
    ),
]

POR_NOMBRE = {h.nombre: h for h in HERRAMIENTAS}
