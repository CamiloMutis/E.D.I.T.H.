"""Skill ``planificar_entrega`` (Planear).

Recibe la entrega dividida en subtareas (la división la hace el LLM, que es
quien entiende el enunciado), estima cada una con el modelo y reserva los
bloques en el calendario, de la fecha límite hacia atrás.
"""

from __future__ import annotations

from planificador.scheduler import Tarea, asignar_hacia_atras, horas_a_minutos, redondear_arriba
from skills.agenda import (
    ErrorSkill,
    evaluar_entrega,
    fecha,
    guardar_bloques,
    minutos_estudio_por_dia,
    ocupados,
)
from skills.contexto import Contexto, formato_horas
from skills.estimar_tiempo import estimar_tiempo

MAX_SUBTAREAS = 8


def planificar_entrega(ctx: Contexto, entrega_id: int, subtareas: list[dict]) -> dict:
    """Divide una entrega en subtareas y las agenda en los huecos libres del calendario.

    Args:
        entrega_id: entrega creada antes con ``registrar_entrega``.
        subtareas: entre 1 y 8 pasos concretos, en el orden en que se deben
            hacer. Cada uno con ``nombre``, ``tipo_tarea``, ``dificultad``
            (1 a 5) y, si el estudiante la dio, ``horas_usuario``.

    Returns:
        Las subtareas con su estimación corregida, los bloques creados y el
        nivel de riesgo de la entrega.
    """
    entrega = ctx.repo.obtener_entrega(int(entrega_id))
    if not entrega:
        raise ErrorSkill(f"No existe la entrega {entrega_id}. Regístrala primero.")
    if entrega["estado"] != "abierta":
        raise ErrorSkill(f"La entrega '{entrega['nombre']}' ya fue entregada.")
    if ctx.repo.subtareas_de_entrega(entrega["id"]):
        raise ErrorSkill(
            f"La entrega '{entrega['nombre']}' ya está planificada. Usa replanificar para reacomodarla."
        )
    if not subtareas or len(subtareas) > MAX_SUBTAREAS:
        raise ErrorSkill(f"Se necesitan entre 1 y {MAX_SUBTAREAS} subtareas.")

    ahora = ctx.ahora()
    limite = fecha(entrega["fecha_limite"])
    if limite <= ahora:
        raise ErrorSkill(f"La fecha límite de '{entrega['nombre']}' ya pasó.")

    detalle, por_id = [], {}
    for paso in subtareas:
        nombre = str(paso.get("nombre") or "").strip()
        if not nombre:
            raise ErrorSkill("Cada subtarea necesita un nombre.")
        estimacion = estimar_tiempo(
            ctx, entrega["materia"], paso.get("tipo_tarea"), paso.get("dificultad") or 3,
            paso.get("horas_usuario"),
        )
        subtarea_id = ctx.repo.crear_subtarea(
            entrega["id"], nombre, estimacion["tipo_tarea"], estimacion["dificultad"],
            estimacion["horas_usuario"], estimacion["horas"],
        )
        por_id[subtarea_id] = ctx.repo.obtener_subtarea(subtarea_id)
        detalle.append(
            {
                "subtarea_id": subtarea_id,
                "nombre": nombre,
                "tipo_tarea": estimacion["tipo_tarea"],
                "horas_usuario": estimacion["horas_usuario"],
                "horas_estimadas": estimacion["horas"],
                "nota": estimacion["mensaje"],
            }
        )

    desde = redondear_arriba(ahora)
    tareas = [
        Tarea(s["id"], horas_a_minutos(s["horas_predichas"]), limite, entrega["id"])
        for s in por_id.values()
    ]
    plan = asignar_hacia_atras(
        tareas, ocupados(ctx, desde, limite), desde, ctx.ajustes.reglas,
        minutos_estudio_por_dia(ctx, desde, limite),
    )
    bloques = guardar_bloques(ctx, plan, por_id)
    estado = evaluar_entrega(ctx, entrega, desde, sum(plan.sin_asignar.values()))

    total_usuario = sum(d["horas_usuario"] or 0 for d in detalle)
    total = sum(d["horas_estimadas"] for d in detalle)
    resumen = f"{len(bloques)} bloques reservados, {formato_horas(total)} en total."
    if total_usuario:
        resumen += f" El estudiante había estimado {formato_horas(total_usuario)}."
    if estado["horas_sin_agendar"]:
        resumen += (
            f" No cupieron {formato_horas(estado['horas_sin_agendar'])} antes de la fecha límite: "
            "la entrega queda en riesgo."
        )
    return {
        "entrega": entrega["nombre"],
        "materia": entrega["materia"],
        "fecha_limite": entrega["fecha_limite"],
        "subtareas": detalle,
        "bloques": bloques,
        "riesgo": estado["riesgo"],
        "holgura": estado["holgura"],
        "horas_sin_agendar": estado["horas_sin_agendar"],
        "resumen": resumen,
    }
