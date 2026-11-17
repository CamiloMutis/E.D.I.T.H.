"""Skill ``replanificar`` (Corregir el rumbo).

Encadena en un solo ciclo: detectar lo que no se hizo, recalcular lo
pendiente, reasignar los bloques y clasificar el riesgo de cada entrega.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from planificador.scheduler import Tarea, asignar, horas_a_minutos, redondear_arriba
from skills.agenda import (
    eliminar_bloque,
    evaluar_entrega,
    fecha,
    guardar_bloques,
    minutos_en_curso,
    minutos_estudio_por_dia,
    ocupados,
)
from skills.contexto import Contexto, formato_fecha, formato_horas

HORA_DE_CIERRE = time(20, 0)  # a partir de aquí el día ya no se replanifica


def _inicio_de_replanificacion(ctx: Contexto, ahora: datetime) -> datetime:
    """De noche se reorganiza desde mañana; durante el día, desde este momento."""
    if ahora.time() >= HORA_DE_CIERRE:
        manana = (ahora + timedelta(days=1)).date()
        return datetime.combine(manana, ctx.ajustes.reglas.franja_inicio, tzinfo=ahora.tzinfo)
    return redondear_arriba(ahora)


def replanificar(ctx: Contexto, horas_extra_dia: float = 0.0) -> dict:
    """Reacomoda todo el trabajo pendiente y dice qué entregas quedan en riesgo.

    Args:
        horas_extra_dia: horas adicionales al tope diario que el estudiante
            acepta estudiar para salvar una entrega en riesgo (por defecto 0).

    Returns:
        Bloques perdidos y reubicados, el estado de cada entrega y, si alguna
        queda en riesgo, cuántas horas faltan por ubicar.
    """
    ahora = ctx.ahora()
    horas_extra_dia = max(0.0, min(float(horas_extra_dia or 0), 6.0))

    # 1. Detectar: un bloque que ya pasó y nadie marcó cuenta como no hecho.
    perdidos = ctx.repo.bloques_vencidos_sin_marcar(ahora)
    for bloque in perdidos:
        ctx.repo.actualizar_estado_bloque(bloque["id"], "perdido")

    # 2. Liberar los bloques futuros: se vuelven a ubicar todos en orden de urgencia.
    desde = _inicio_de_replanificacion(ctx, ahora)
    anteriores = ctx.repo.bloques_pendientes_desde(ahora)
    for bloque in anteriores:
        eliminar_bloque(ctx, bloque)

    # 3. Recalcular lo pendiente, ya ordenado por fecha límite y peso en la nota.
    pendientes = [s for s in ctx.repo.subtareas_abiertas() if (s["horas_restantes"] or 0) > 0]
    por_id = {s["id"]: s for s in pendientes}
    resultado = {"bloques": [], "sin_asignar": {}}
    if pendientes:
        ultima = max(fecha(s["fecha_limite"]) for s in pendientes)
        en_curso = minutos_en_curso(ctx, ahora)
        tareas = [
            Tarea(
                s["id"],
                max(0, horas_a_minutos(s["horas_restantes"]) - en_curso.get(s["id"], 0)),
                fecha(s["fecha_limite"]),
                s["entrega_id"],
            )
            for s in pendientes
        ]
        # 4. Reasignar con el algoritmo voraz.
        plan = asignar(
            tareas, ocupados(ctx, desde, ultima), desde, ctx.ajustes.reglas, direccion="adelante",
            minutos_por_dia=minutos_estudio_por_dia(ctx, desde, ultima),
            horas_extra_dia=horas_extra_dia,
        )
        resultado["bloques"] = guardar_bloques(ctx, plan, por_id)
        resultado["sin_asignar"] = plan.sin_asignar

    # 5. Clasificar cada entrega según su holgura.
    estados = []
    for entrega in ctx.repo.entregas_abiertas():
        sin_asignar = sum(
            minutos for subtarea_id, minutos in resultado["sin_asignar"].items()
            if por_id[subtarea_id]["entrega_id"] == entrega["id"]
        )
        estados.append(evaluar_entrega(ctx, entrega, desde, sin_asignar))

    en_riesgo = [e for e in estados if e["riesgo"] == "en_riesgo"]
    resumen = f"Bloques agendados desde el {formato_fecha(desde)}: {len(resultado['bloques'])}."
    if perdidos:
        resumen += f" Bloques sin hacer que se reubicaron: {len(perdidos)}."
    if en_riesgo:
        nombres = ", ".join(f"{e['entrega']} ({e['materia']})" for e in en_riesgo)
        faltan = sum(e["horas_sin_agendar"] for e in en_riesgo)
        resumen += f" En riesgo: {nombres}."
        if faltan:
            resumen += f" Faltan {formato_horas(faltan)} por ubicar."
    else:
        resumen += " Ninguna entrega queda en riesgo."

    return {
        "bloques_perdidos": [
            {"subtarea": b["subtarea"], "entrega": b["entrega"]} for b in perdidos
        ],
        "bloques_anteriores": len(anteriores),
        "bloques": resultado["bloques"],
        "entregas": estados,
        "hay_riesgo": bool(en_riesgo),
        "horas_extra_dia": horas_extra_dia,
        "resumen": resumen,
    }
