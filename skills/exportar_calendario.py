"""Exporta el plan del estudiante como archivo de calendario (.ics).

En el servidor cada estudiante usa el calendario interno de E.D.I.T.H.; con este
archivo lleva sus bloques de estudio, sus entregas y los vencimientos de Moodle a
Google Calendar, Outlook o el calendario del celular, sin dar permisos a nadie.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from skills.agenda import fecha
from skills.contexto import Contexto

DOMINIO = "edith.local"


def _utc(momento: datetime) -> str:
    return momento.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _escapar(texto: str) -> str:
    return (
        (texto or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    )


def _plegar(linea: str) -> list[str]:
    """RFC 5545: líneas de máximo 75 octetos; las siguientes empiezan con un espacio."""
    partes, actual = [], ""
    for caracter in linea:
        if len((actual + caracter).encode()) > 74:
            partes.append(actual)
            actual = " "
        actual += caracter
    return partes + [actual]


def calendario_ics(ctx: Contexto, dias: int = 60) -> str:
    """Bloques pendientes, entregas abiertas y vencimientos de Moodle de los próximos ``dias``."""
    ahora = ctx.ahora()
    hasta = ahora + timedelta(days=dias)
    sello = _utc(ahora)
    eventos: list[tuple[str, datetime, datetime, str, str]] = []

    for b in ctx.repo.bloques_pendientes_desde(ahora - timedelta(days=1)):
        inicio = fecha(b["inicio"])
        if inicio <= hasta:
            eventos.append((
                f"bloque-{b['id']}", inicio, fecha(b["fin"]),
                f"📚 {b['subtarea']} ({b['materia']})", f"Bloque de estudio para {b['entrega']}.",
            ))
    for e in ctx.repo.entregas_abiertas():
        limite = fecha(e["fecha_limite"])
        if ahora <= limite <= hasta:
            eventos.append((
                f"entrega-{e['id']}", limite - timedelta(minutes=30), limite,
                f"📌 Entrega: {e['nombre']} ({e['materia']})", e.get("descripcion") or "",
            ))
    for m in ctx.repo.moodle_eventos().values():
        vence = fecha(m["fecha"])
        if m["entregado"] or not ahora <= vence <= hasta:
            continue
        eventos.append((
            f"moodle-{m['id']}", vence - timedelta(minutes=30), vence,
            f"⏰ Moodle: {m['nombre']}", f"{m['curso'] or ''}\n{m['url'] or ''}".strip(),
        ))

    lineas = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//E.D.I.T.H.//Plan de estudio//ES",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:E.D.I.T.H.",
    ]
    for uid, inicio, fin, titulo, descripcion in sorted(eventos, key=lambda e: e[1]):
        lineas += [
            "BEGIN:VEVENT",
            f"UID:{uid}@{DOMINIO}",
            f"DTSTAMP:{sello}",
            f"DTSTART:{_utc(inicio)}",
            f"DTEND:{_utc(fin)}",
            f"SUMMARY:{_escapar(titulo)}",
        ]
        if descripcion:
            lineas.append(f"DESCRIPTION:{_escapar(descripcion)}")
        lineas.append("END:VEVENT")
    lineas.append("END:VCALENDAR")
    return "\r\n".join(parte for linea in lineas for parte in _plegar(linea)) + "\r\n"
