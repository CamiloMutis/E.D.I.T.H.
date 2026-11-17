"""Skill: consultar los próximos eventos del calendario de Moodle.

La conexión se abre la primera vez que se usa y se reutiliza después, así el
agente arranca aunque Moodle no esté configurado o no responda.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from integraciones.moodle import ClienteMoodle, ErrorMoodle
from skills.agenda import ErrorSkill
from skills.contexto import Contexto, formato_fecha, formato_hora

DIAS_MAX = 90


def _cliente(ctx: Contexto) -> ClienteMoodle:
    if ctx.moodle is None:
        try:
            ctx.moodle = ClienteMoodle.desde_entorno(ctx.ajustes.zona)
        except ErrorMoodle as error:
            raise ErrorSkill(f"No pude entrar a Moodle: {error}") from error
    return ctx.moodle


def consultar_moodle(ctx: Contexto, dias: int = 14, curso: str | None = None) -> dict:
    """Lee las próximas entregas, quices y eventos del calendario de Moodle, la plataforma de la universidad.
    Úsala cuando el estudiante pregunte qué tiene en Moodle, qué le vence pronto o qué publicaron los profesores.

    Args:
        dias: cuántos días hacia adelante consultar, de 1 a 90 (14 por defecto). El último día se
            incluye completo: para ver lo del día 29 cuando hoy es 17, basta con 12.
        curso: parte del nombre del curso para filtrar, p. ej. 'proyectos'; omitir para ver todos.
    """
    dias = int(dias)
    if not 1 <= dias <= DIAS_MAX:
        raise ErrorSkill(f"El rango debe estar entre 1 y {DIAS_MAX} días.")
    ahora = ctx.ahora()
    # El rango cubre el último día completo: las entregas suelen vencer a las 23:59 y el LLM
    # calcula "dias" como diferencia de fechas, no de horas.
    hasta = datetime.combine((ahora + timedelta(days=dias)).date(), time(23, 59, 59), ahora.tzinfo)
    try:
        eventos = _cliente(ctx).proximos_eventos(desde=ahora, hasta=hasta)
    except ErrorMoodle as error:
        ctx.moodle = None  # la próxima vez se vuelve a iniciar sesión
        raise ErrorSkill(f"Moodle no respondió: {error}") from error

    if curso:
        buscado = curso.strip().lower()
        eventos = [e for e in eventos if buscado in e.curso.lower()]

    por_dia: dict[str, int] = {}
    for e in eventos:
        dia = formato_fecha(e.inicio)
        por_dia[dia] = por_dia.get(dia, 0) + 1

    return {
        "dias_consultados": dias,
        "total": len(eventos),
        "cantidad_por_dia": por_dia,
        "eventos": [
            {
                "nombre": e.nombre,
                "curso": e.curso,
                "cuando": f"{formato_fecha(e.inicio)}, {formato_hora(e.inicio)}",
                "fecha": e.inicio.isoformat(),
                "tipo": e.modulo or e.tipo,
                "enlace": e.url,
                "descripcion": e.descripcion[:300],
            }
            for e in eventos
        ],
    }
