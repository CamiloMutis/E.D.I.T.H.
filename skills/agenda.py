"""Operaciones de agenda que comparten ``planificar_entrega`` y ``replanificar``."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from planificador.scheduler import (
    Intervalo,
    ResultadoPlan,
    capacidad_minutos,
    clasificar_riesgo,
    horas_a_minutos,
    limite_de_trabajo,
)
from skills.contexto import Contexto, formato_bloque


class ErrorSkill(Exception):
    """Problema que se le debe contar al usuario (dato faltante, entrega inexistente)."""


def fecha(texto: str) -> datetime:
    return datetime.fromisoformat(texto)


def ocupados(ctx: Contexto, desde: datetime, hasta: datetime) -> list[Intervalo]:
    """Eventos del calendario que no se pueden pisar, incluidos los bloques ya agendados."""
    if hasta <= desde:
        return []
    return [Intervalo(e.inicio, e.fin) for e in ctx.calendario.listar_eventos(desde, hasta)]


def minutos_estudio_por_dia(ctx: Contexto, desde: datetime, hasta: datetime) -> dict[date, int]:
    """Minutos de estudio ya agendados por día, para aplicar el tope diario."""
    inicio_dia = desde.replace(hour=0, minute=0, second=0, microsecond=0)
    minutos: dict[date, int] = defaultdict(int)
    for bloque in ctx.repo.bloques_entre(inicio_dia, hasta + timedelta(days=1)):
        if bloque["estado"] == "perdido":
            continue
        inicio, fin = fecha(bloque["inicio"]), fecha(bloque["fin"])
        minutos[inicio.date()] += int((fin - inicio).total_seconds() // 60)
    return dict(minutos)


def minutos_en_curso(ctx: Contexto, ahora: datetime) -> dict[int, int]:
    """Minutos por subtarea del bloque que el estudiante está haciendo en este momento.

    Ese bloque se conserva al replanificar, así que su tiempo no debe
    agendarse otra vez.
    """
    minutos: dict[int, int] = defaultdict(int)
    for bloque in ctx.repo.bloques_en_curso(ahora):
        duracion = (fecha(bloque["fin"]) - fecha(bloque["inicio"])).total_seconds() // 60
        minutos[bloque["subtarea_id"]] += int(duracion)
    return dict(minutos)


def guardar_bloques(ctx: Contexto, plan: ResultadoPlan, subtareas: dict[int, dict]) -> list[dict]:
    """Crea en el calendario y en la base de datos los bloques que asignó el planificador."""
    creados = []
    for bloque in plan.bloques:
        subtarea = subtareas[bloque.tarea_id]
        titulo = f"Estudio: {subtarea['nombre']} · {subtarea['entrega']} ({subtarea['materia']})"
        evento_id = ctx.calendario.crear_evento(
            titulo, bloque.inicio, bloque.fin, "Bloque agendado por E.D.I.T.H."
        )
        bloque_id = ctx.repo.crear_bloque(subtarea["id"], bloque.inicio, bloque.fin, evento_id)
        creados.append(
            {
                "bloque_id": bloque_id,
                "subtarea": subtarea["nombre"],
                "entrega": subtarea["entrega"],
                "cuando": formato_bloque(bloque.inicio, bloque.fin),
                "inicio": bloque.inicio.isoformat(),
                "fin": bloque.fin.isoformat(),
            }
        )
    return creados


def eliminar_bloque(ctx: Contexto, bloque: dict) -> None:
    """Borra un bloque del calendario y de la base de datos."""
    if bloque.get("calendar_event"):
        try:
            ctx.calendario.eliminar_evento(bloque["calendar_event"])
        except Exception:
            # El evento pudo borrarse a mano; el bloque se elimina igual.
            pass
    ctx.repo.eliminar_bloque(bloque["id"])


def evaluar_entrega(
    ctx: Contexto, entrega: dict, desde: datetime, minutos_sin_asignar: int = 0
) -> dict:
    """Calcula la holgura de una entrega y actualiza su nivel de riesgo.

    Holgura = horas disponibles antes de la entrega / horas pendientes. Las
    horas disponibles son las que siguen libres más las que la propia entrega
    ya tiene reservadas.
    """
    pendientes = [s for s in ctx.repo.subtareas_de_entrega(entrega["id"]) if s["estado"] != "hecha"]
    en_curso = minutos_en_curso(ctx, ctx.ahora())
    minutos_pendientes = sum(
        max(0, horas_a_minutos(s["horas_restantes"] or 0) - en_curso.get(s["id"], 0)) for s in pendientes
    )
    limite = fecha(entrega["fecha_limite"])

    if minutos_pendientes == 0:
        riesgo, holgura = "tranquila", None
    elif limite <= desde:
        riesgo, holgura = "en_riesgo", 0.0
    else:
        hasta = limite_de_trabajo(limite, desde, ctx.ajustes.reglas)
        libres = capacidad_minutos(
            ocupados(ctx, desde, hasta), desde, hasta, ctx.ajustes.reglas,
            minutos_estudio_por_dia(ctx, desde, hasta),
        )
        ids = {s["id"] for s in pendientes}
        propios = sum(
            int((fecha(b["fin"]) - fecha(b["inicio"])).total_seconds() // 60)
            for b in ctx.repo.bloques_pendientes_desde(desde)
            if b["subtarea_id"] in ids
        )
        holgura = round((libres + propios) / minutos_pendientes, 2)
        riesgo = clasificar_riesgo(holgura, ctx.ajustes.holgura_tranquila, ctx.ajustes.holgura_ajustada)
        if minutos_sin_asignar > 0:
            riesgo = "en_riesgo"

    ctx.repo.actualizar_riesgo(entrega["id"], riesgo, holgura)
    return {
        "entrega_id": entrega["id"],
        "entrega": entrega["nombre"],
        "materia": entrega["materia"],
        "riesgo": riesgo,
        "holgura": holgura,
        "horas_pendientes": round(minutos_pendientes / 60, 2),
        "horas_sin_agendar": round(minutos_sin_asignar / 60, 2),
    }
