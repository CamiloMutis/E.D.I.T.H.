"""Convierte lo que hay en Moodle en entregas planificadas, sin esperar a que el estudiante las dicte.

Lo llama ``revisar_moodle`` en cada revisión. Cada tarea o quiz de Moodle sin
entregar queda como una entrega con subtareas y bloques reservados; si Moodle
cambia la fecha se vuelve a agendar, y si el estudiante entrega se cierra y se
liberan sus bloques. La división en subtareas es una plantilla según el tipo de
actividad: no gasta LLM y funciona aunque no haya llave.
"""

from __future__ import annotations

import logging
from datetime import datetime

from db.repositorio import normalizar
from integraciones.moodle import EventoMoodle
from skills.agenda import ErrorSkill, eliminar_bloque, fecha
from skills.contexto import Contexto
from skills.planificar_entrega import agendar_entrega, planificar_entrega

registro = logging.getLogger(__name__)
SIN_CURSO = "Otros cursos de Moodle"  # eventos que Moodle no asocia a un curso

# Palabras del nombre o la descripción que delatan el tipo de trabajo, en orden de prioridad.
PALABRAS_TIPO = [
    ("estudio", ("quiz", "parcial", "examen", "evaluacion", "prueba escrita")),
    ("presentacion", ("video", "presentacion", "exposicion", "storytelling", "diapositiva", "pitch", "sustentacion")),
    ("codigo", ("laboratorio", "codigo", "programa", "github", "implementa", "syscall", "script", "repositorio")),
    ("lectura", ("lectura", "leer", "resumen de", "articulo")),
    ("informe", ("informe", "documentacion", "documento", "acta", "manual", "plan de", "reporte", "ensayo", "formato", "plantilla")),
]


# Punto de partida de cada plantilla. El modelo lo corrige; el estudiante puede ajustarlo por chat.
HORAS_DESARROLLO = {"informe": 2.0, "presentacion": 2.0, "codigo": 3.0, "lectura": 1.0, "taller": 2.0}


def clave_moodle(e: EventoMoodle) -> str:
    """Identifica la actividad, no el evento: el vencimiento y el cierre de una misma tarea son una sola entrega."""
    return f"{e.modulo}:{e.instancia}" if e.modulo and e.instancia else f"evento:{e.id}"


def tipo_de_actividad(e: EventoMoodle) -> str:
    if e.modulo == "quiz":
        return "estudio"
    texto = normalizar(f"{e.nombre} {e.descripcion}")
    for tipo, palabras in PALABRAS_TIPO:
        if any(p in texto for p in palabras):
            return tipo
    return "taller"


def subtareas_para(e: EventoMoodle, nombre: str) -> list[dict]:
    tipo = tipo_de_actividad(e)
    if tipo == "estudio":
        return [
            {"nombre": f"Repasar los temas de {nombre}", "tipo_tarea": "estudio", "dificultad": 3, "horas_base": 1.5},
            {"nombre": "Practicar con ejercicios", "tipo_tarea": "estudio", "dificultad": 3, "horas_base": 1.0},
        ]
    return [
        {"nombre": "Leer el enunciado y reunir el material", "tipo_tarea": "lectura", "dificultad": 1, "horas_base": 0.5},
        {"nombre": f"Desarrollar {nombre}", "tipo_tarea": tipo, "dificultad": 3, "horas_base": HORAS_DESARROLLO[tipo]},
        {"nombre": "Revisar y subir a Moodle", "tipo_tarea": tipo, "dificultad": 1, "horas_base": 0.5},
    ]


def _materia(ctx: Contexto, curso_corto: str) -> dict:
    """La materia del pénsum que corresponde al curso; si no hay, se crea con el nombre del curso."""
    curso_corto = (curso_corto or "").strip() or SIN_CURSO
    encontrada = ctx.repo.buscar_materia(curso_corto)
    if encontrada:
        if not encontrada["activa"]:
            ctx.repo.activar_materia(encontrada["id"])
        return encontrada
    return ctx.repo.crear_materia(curso_corto if curso_corto == SIN_CURSO else curso_corto.title())


def _liberar_bloques_futuros(ctx: Contexto, entrega_id: int, desde: datetime) -> None:
    for bloque in ctx.repo.bloques_pendientes_desde(desde):
        if bloque["entrega_id"] == entrega_id:
            eliminar_bloque(ctx, bloque)


def _resumen_plan(nombre: str, materia: str, e: EventoMoodle, plan: dict) -> dict:
    return {
        "nombre": nombre,
        "materia": materia,
        "vence": e.inicio,
        "bloques": len(plan["bloques"]),
        "horas": round(sum(s["horas_estimadas"] for s in plan["subtareas"]), 2),
        "primer_bloque": plan["bloques"][0]["cuando"] if plan["bloques"] else None,
        "riesgo": plan["riesgo"],
        "horas_sin_agendar": plan["horas_sin_agendar"],
    }


def sincronizar_entregas(
    ctx: Contexto, actividades: list[tuple[EventoMoodle, bool]], nombre_de, curso_de
) -> dict:
    """Deja una entrega planificada por cada actividad de Moodle pendiente.

    ``actividades`` son los vencimientos de la revisión con su estado de entrega.
    ``nombre_de`` (recibe el evento) y ``curso_de`` (recibe el nombre del curso) limpian ambos como en los avisos.
    """
    ahora = ctx.ahora()
    resultado: dict = {"planeadas": [], "reagendadas": [], "cerradas": []}

    # Una actividad puede traer vencimiento y cierre: cuenta la fecha más próxima.
    por_clave: dict[str, tuple[EventoMoodle, bool]] = {}
    for e, entregado in sorted(actividades, key=lambda par: par[0].inicio):
        clave = clave_moodle(e)
        if clave in por_clave:
            por_clave[clave] = (por_clave[clave][0], por_clave[clave][1] or entregado)
        else:
            por_clave[clave] = (e, entregado)

    for clave, (e, entregado) in por_clave.items():
        nombre = nombre_de(e)
        try:
            existente = ctx.repo.entrega_de_moodle(clave)
            if existente:
                if existente["estado"] != "abierta":
                    continue
                if entregado:
                    ctx.repo.marcar_entregada(existente["id"])
                    _liberar_bloques_futuros(ctx, existente["id"], ahora)
                    resultado["cerradas"].append(nombre)
                elif not ctx.repo.subtareas_de_entrega(existente["id"]) and e.inicio > ahora:
                    # Quedó registrada pero sin plan (falló a mitad de camino): se planea ahora.
                    ctx.repo.actualizar_fecha_entrega(existente["id"], e.inicio)
                    plan = planificar_entrega(ctx, existente["id"], subtareas_para(e, nombre))
                    resultado["planeadas"].append(_resumen_plan(nombre, existente["materia"], e, plan))
                elif fecha(existente["fecha_limite"]) != e.inicio and e.inicio > ahora:
                    ctx.repo.actualizar_fecha_entrega(existente["id"], e.inicio)
                    _liberar_bloques_futuros(ctx, existente["id"], ahora)
                    bloques, estado = agendar_entrega(ctx, ctx.repo.obtener_entrega(existente["id"]))
                    resultado["reagendadas"].append(
                        {"nombre": nombre, "materia": existente["materia"], "vence": e.inicio, "bloques": len(bloques), "riesgo": estado["riesgo"]}
                    )
                continue
            if entregado or e.inicio <= ahora:
                continue
            materia = _materia(ctx, curso_de(e.curso))
            entrega_id = ctx.repo.crear_entrega(
                materia["id"], nombre, e.inicio, None, (e.descripcion or "")[:2000], moodle_id=clave
            )
            plan = planificar_entrega(ctx, entrega_id, subtareas_para(e, nombre))
            resultado["planeadas"].append(_resumen_plan(nombre, materia["nombre"], e, plan))
        except ErrorSkill as error:
            registro.warning("No se pudo planear '%s' de Moodle: %s", nombre, error)
        except Exception:
            registro.exception("Falló la planeación automática de '%s'", nombre)

    resultado["planeadas"].sort(key=lambda p: p["vence"])
    return resultado
