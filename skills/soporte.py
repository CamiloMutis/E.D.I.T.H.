"""Herramientas de soporte del agente.

No son skills principales, pero el agente las necesita para registrar
entregas, consultar el calendario y llevar el avance real, que es lo que
alimenta al modelo de tiempos.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from planificador.scheduler import huecos_libres
from skills.agenda import ErrorSkill, eliminar_bloque, fecha, ocupados
from skills.contexto import (
    Contexto,
    dia_semana,
    formato_bloque,
    formato_fecha,
    formato_hora,
    formato_horas,
    franja,
)

ESTADOS_BLOQUE = {"hecho": 1.0, "a_medias": 0.5, "perdido": 0.0}
ETIQUETAS_RIESGO = {"tranquila": "tranquila", "ajustada": "ajustada", "en_riesgo": "EN RIESGO"}


def interpretar_fecha(ctx: Contexto, texto: str, fin_del_dia: bool = True) -> datetime:
    """Convierte ``2026-11-28`` o ``2026-11-28T14:00`` en una fecha con zona horaria.

    Una fecha sin hora se entiende como el final de ese día si es una fecha
    límite, o como su comienzo si es el inicio de un rango de consulta.
    """
    try:
        momento = datetime.fromisoformat(str(texto).strip())
    except ValueError as error:
        raise ErrorSkill(
            f"No entiendo la fecha '{texto}'. Usa el formato AAAA-MM-DD o AAAA-MM-DDTHH:MM."
        ) from error
    if len(str(texto).strip()) <= 10:
        momento = datetime.combine(momento.date(), time(23, 59) if fin_del_dia else time(0, 0))
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=ctx.ajustes.zona)
    return momento.astimezone(ctx.ajustes.zona)


def registrar_entrega(
    ctx: Contexto,
    materia: str,
    nombre: str,
    fecha_limite: str,
    peso_nota: float | None = None,
    descripcion: str = "",
) -> dict:
    """Crea una entrega nueva (taller, parcial, proyecto, exposición).

    Args:
        materia: nombre de la materia, como lo diga el estudiante.
        nombre: nombre corto de la entrega, por ejemplo "Taller 3".
        fecha_limite: fecha en formato AAAA-MM-DD o AAAA-MM-DDTHH:MM.
        peso_nota: porcentaje de la nota (15 o 0.15 significan 15 %).
        descripcion: tema o enunciado, si se conoce.

    Returns:
        El ``entrega_id`` que necesita ``planificar_entrega``.
    """
    encontrada = ctx.repo.buscar_materia(materia)
    if not encontrada:
        activas = ", ".join(m["nombre"] for m in ctx.repo.materias_activas())
        raise ErrorSkill(f"No encuentro la materia '{materia}'. Materias activas: {activas}.")
    if not encontrada["activa"]:
        ctx.repo.activar_materia(encontrada["id"])
    limite = interpretar_fecha(ctx, fecha_limite)
    if limite <= ctx.ahora():
        raise ErrorSkill(f"La fecha {formato_fecha(limite)} ya pasó. Confirma la fecha con el estudiante.")
    peso = None
    if peso_nota is not None:
        peso = float(peso_nota)
        peso = peso / 100 if peso > 1 else peso
    entrega_id = ctx.repo.crear_entrega(
        encontrada["id"], str(nombre).strip(), limite, peso, str(descripcion or "").strip()
    )
    return {
        "entrega_id": entrega_id,
        "materia": encontrada["nombre"],
        "nombre": str(nombre).strip(),
        "fecha_limite": limite.isoformat(),
        "fecha_legible": f"{formato_fecha(limite)}, {formato_hora(limite)}",
        "peso_nota": peso,
    }


def listar_pendientes(ctx: Contexto) -> dict:
    """Devuelve las entregas abiertas con sus subtareas, ordenadas por fecha y peso."""
    ahora = ctx.ahora()
    entregas = []
    for entrega in ctx.repo.entregas_abiertas():
        limite = fecha(entrega["fecha_limite"])
        subtareas = ctx.repo.subtareas_de_entrega(entrega["id"])
        entregas.append(
            {
                "entrega_id": entrega["id"],
                "materia": entrega["materia"],
                "nombre": entrega["nombre"],
                "fecha_limite": f"{formato_fecha(limite)}, {formato_hora(limite)}",
                "dias_restantes": (limite.date() - ahora.date()).days,
                "peso_nota": entrega["peso_nota"],
                "riesgo": entrega["riesgo"] or "sin planificar",
                "subtareas": [
                    {
                        "subtarea_id": s["id"],
                        "nombre": s["nombre"],
                        "estado": s["estado"],
                        "horas_restantes": s["horas_restantes"],
                    }
                    for s in subtareas
                ],
            }
        )
    return {"entregas": entregas, "total": len(entregas)}


def consultar_calendario(ctx: Contexto, desde: str, hasta: str) -> dict:
    """Lee los eventos y los huecos libres del calendario en un rango de fechas.

    Args:
        desde: fecha inicial, AAAA-MM-DD.
        hasta: fecha final incluida, AAAA-MM-DD.
    """
    inicio = interpretar_fecha(ctx, desde, fin_del_dia=False)
    fin = interpretar_fecha(ctx, hasta, fin_del_dia=True)
    if fin <= inicio:
        raise ErrorSkill("El rango de fechas está invertido.")
    if fin - inicio > timedelta(days=31):
        raise ErrorSkill("El rango máximo de consulta es de un mes.")
    eventos = ctx.calendario.listar_eventos(inicio, fin)
    libres = huecos_libres(ocupados(ctx, inicio, fin), max(inicio, ctx.ahora()), fin, ctx.ajustes.reglas)
    return {
        "eventos": [{"titulo": e.titulo, "cuando": formato_bloque(e.inicio, e.fin)} for e in eventos],
        "huecos_libres": [
            {"cuando": formato_bloque(h.inicio, h.fin), "horas": round(h.minutos / 60, 2)} for h in libres
        ],
    }


def marcar_bloque(ctx: Contexto, bloque_id: int, estado: str) -> dict:
    """Registra cómo le fue al estudiante en un bloque: hecho, a_medias o perdido.

    Un bloque hecho descuenta su duración de las horas restantes de la
    subtarea; uno a medias descuenta la mitad.
    """
    if estado not in ESTADOS_BLOQUE:
        raise ErrorSkill("El estado debe ser hecho, a_medias o perdido.")
    bloque = ctx.repo.obtener_bloque(int(bloque_id))
    if not bloque:
        raise ErrorSkill(f"No existe el bloque {bloque_id}.")
    subtarea = ctx.repo.obtener_subtarea(bloque["subtarea_id"])
    duracion = (fecha(bloque["fin"]) - fecha(bloque["inicio"])).total_seconds() / 3600

    # Si el bloque ya estaba marcado, primero se deshace su efecto anterior.
    previo = ESTADOS_BLOQUE.get(bloque["estado"], 0.0) * duracion
    nuevo = ESTADOS_BLOQUE[estado] * duracion
    hechas = max(0.0, (subtarea["horas_hechas"] or 0) - previo + nuevo)
    restantes = max(0.0, (subtarea["horas_restantes"] or 0) + previo - nuevo)

    ctx.repo.actualizar_estado_bloque(bloque["id"], estado)
    ctx.repo.actualizar_subtarea(
        subtarea["id"], horas_hechas=round(hechas, 2), horas_restantes=round(restantes, 2),
        estado="en_curso" if hechas > 0 else subtarea["estado"],
    )
    return {
        "bloque_id": bloque["id"],
        "subtarea_id": subtarea["id"],
        "subtarea": subtarea["nombre"],
        "entrega": subtarea["entrega"],
        "estado": estado,
        "horas_hechas": round(hechas, 2),
        "horas_restantes": round(restantes, 2),
        "posiblemente_terminada": restantes == 0 and hechas > 0,
    }


def registrar_avance(
    ctx: Contexto,
    subtarea_id: int,
    terminada: bool = True,
    horas_reales: float | None = None,
    horas_restantes: float | None = None,
) -> dict:
    """Guarda el avance real de una subtarea.

    Args:
        subtarea_id: subtarea sobre la que informa el estudiante.
        terminada: verdadero si ya la acabó.
        horas_reales: horas que le tomó en total; si no se indican, se usan
            las de los bloques que marcó como hechos.
        horas_restantes: si no la terminó, cuántas horas cree que le faltan.

    Al terminar una subtarea se agrega una fila al historial de tiempos, que
    es el dato con el que el modelo aprende cómo trabaja este estudiante.
    """
    subtarea = ctx.repo.obtener_subtarea(int(subtarea_id))
    if not subtarea:
        raise ErrorSkill(f"No existe la subtarea {subtarea_id}.")
    ahora = ctx.ahora()

    if not terminada:
        if horas_restantes is None:
            raise ErrorSkill("Indica cuántas horas faltan para terminar la subtarea.")
        campos = {"horas_restantes": max(0.25, float(horas_restantes)), "estado": "en_curso"}
        if horas_reales is not None:
            campos["horas_hechas"] = float(horas_reales)
        ctx.repo.actualizar_subtarea(subtarea["id"], **campos)
        return {
            "subtarea": subtarea["nombre"],
            "estado": "en_curso",
            "horas_restantes": campos["horas_restantes"],
            "sugerencia": "Ejecuta replanificar para reservar las horas que faltan.",
        }

    reales = float(horas_reales) if horas_reales else float(subtarea["horas_hechas"] or 0)
    if reales <= 0:
        raise ErrorSkill("Indica cuántas horas tomó la subtarea para poder aprender de ella.")
    ya_registrada = subtarea["estado"] == "hecha"
    ctx.repo.actualizar_subtarea(subtarea["id"], estado="hecha", horas_restantes=0, horas_hechas=reales)
    for bloque in ctx.repo.bloques_pendientes_desde(ahora):
        if bloque["subtarea_id"] == subtarea["id"]:
            eliminar_bloque(ctx, bloque)

    estimado = subtarea["horas_usuario"] or subtarea["horas_predichas"] or reales
    if not ya_registrada:
        bloques = ctx.repo.bloques_de_subtarea(subtarea["id"])
        referencia = fecha(bloques[0]["inicio"]) if bloques else ahora
        ctx.repo.agregar_historial(
            subtarea["materia"], subtarea["tipo_tarea"], subtarea["dificultad"] or 3,
            float(estimado), reales, dia_semana(referencia), franja(referencia), "uso",
            subtarea_id=subtarea["id"],
        )
    return {
        "subtarea": subtarea["nombre"],
        "estado": "hecha",
        "horas_reales": reales,
        "horas_estimadas_por_el_estudiante": subtarea["horas_usuario"],
        "horas_predichas": subtarea["horas_predichas"],
        "nota": f"Tomó {formato_horas(reales)}; se había previsto {formato_horas(subtarea['horas_predichas'] or 0)}.",
    }


def marcar_entregada(ctx: Contexto, entrega_id: int) -> dict:
    """Cierra una entrega y libera del calendario los bloques que le quedaban."""
    entrega = ctx.repo.obtener_entrega(int(entrega_id))
    if not entrega:
        raise ErrorSkill(f"No existe la entrega {entrega_id}.")
    ids = {s["id"] for s in ctx.repo.subtareas_de_entrega(entrega["id"])}
    liberados = 0
    for bloque in ctx.repo.bloques_pendientes_desde(ctx.ahora()):
        if bloque["subtarea_id"] in ids:
            eliminar_bloque(ctx, bloque)
            liberados += 1
    for subtarea_id in ids:
        ctx.repo.actualizar_subtarea(subtarea_id, estado="hecha", horas_restantes=0)
    ctx.repo.marcar_entregada(entrega["id"])
    return {"entrega": entrega["nombre"], "materia": entrega["materia"], "bloques_liberados": liberados}


def bloques_del_dia(ctx: Contexto, dia: datetime | None = None) -> list[dict]:
    """Bloques de estudio de un día (por defecto hoy), en orden."""
    dia = dia or ctx.ahora()
    inicio = dia.replace(hour=0, minute=0, second=0, microsecond=0)
    return ctx.repo.bloques_entre(inicio, inicio + timedelta(days=1))


def resumen_del_dia(ctx: Contexto) -> str:
    """Texto del resumen de la mañana: bloques de hoy, entregas de la semana y archivos."""
    ahora = ctx.ahora()
    lineas = [f"Buenos días. Hoy es {formato_fecha(ahora)}."]

    bloques = [b for b in bloques_del_dia(ctx) if b["estado"] != "perdido"]
    if bloques:
        lineas.append("\nBloques de hoy:")
        for b in bloques:
            inicio, fin = fecha(b["inicio"]), fecha(b["fin"])
            lineas.append(
                f"• {formato_hora(inicio)} a {formato_hora(fin)}: {b['subtarea']} ({b['materia']})"
            )
    else:
        lineas.append("\nHoy no tienes bloques de estudio agendados.")

    semana = [
        e for e in ctx.repo.entregas_abiertas()
        if fecha(e["fecha_limite"]) <= ahora + timedelta(days=7)
    ]
    if semana:
        lineas.append("\nEntregas de los próximos 7 días:")
        for e in semana:
            limite = fecha(e["fecha_limite"])
            riesgo = ETIQUETAS_RIESGO.get(e["riesgo"] or "", "sin planificar")
            lineas.append(f"• {formato_fecha(limite)}: {e['nombre']} de {e['materia']} ({riesgo})")

    materias_de_hoy = {b["materia"] for b in bloques}
    archivos = []
    for materia in ctx.repo.materias_activas():
        if materia["nombre"] in materias_de_hoy:
            archivos += [f"• {a['nombre']} ({materia['nombre']})" for a in ctx.repo.archivos_de(materia["id"])[:3]]
    if archivos:
        lineas.append("\nArchivos para lo de hoy:")
        lineas.extend(archivos)
    return "\n".join(lineas)
