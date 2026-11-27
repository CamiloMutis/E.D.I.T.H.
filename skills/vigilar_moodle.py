"""Vigilancia de Moodle: tareas nuevas, aperturas, fechas cambiadas, vencimientos y contenido nuevo.

Moodle no avisa a programas externos cuando un profesor publica algo, así que
el bot llama a ``revisar_moodle`` cada pocos minutos dentro del horario
configurado. Cada revisión compara Moodle con lo guardado en la tabla
``moodle_eventos`` y devuelve solo lo que hay que contarle al estudiante; los
avisos enviados quedan anotados para no repetirlos.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from integraciones.moodle import ErrorMoodle, EventoMoodle, NotificacionMoodle
from skills.agenda import ErrorSkill, fecha
from skills.consultar_moodle import obtener_cliente
from skills.contexto import Contexto, formato_fecha, formato_hora, formato_vence, lista_por_materia
from skills.planear_moodle import sincronizar_entregas

# Solo estos eventos son vencimientos. La apertura de un quiz ("open") se toma
# del propio quiz, y las sesiones de clase no ameritan recordatorio.
TIPOS_VENCIMIENTO = {"due", "close"}
PREFERENCIA_SEMBRADO = "moodle_sembrado"
PREFERENCIA_ULTIMA_REVISION = "moodle_ultima_revision"
PREFERENCIA_ULTIMA_NOTIFICACION = "moodle_ultima_notificacion"
MAX_CONTENIDOS = 10


def dentro_del_horario(ctx: Contexto, momento: datetime | None = None) -> bool:
    hora = (momento or ctx.ahora()).astimezone(ctx.ajustes.zona).time()
    return ctx.ajustes.moodle_horario_inicio <= hora < ctx.ajustes.moodle_horario_fin


def _umbrales_alcanzados(ctx: Contexto, vence: datetime, ahora: datetime) -> set[int]:
    horas_faltan = (vence - ahora).total_seconds() / 3600
    return {h for h in ctx.ajustes.moodle_avisos_horas if horas_faltan <= h}


def revisar_moodle(ctx: Contexto) -> dict:
    """Compara Moodle con lo conocido y devuelve lo que hay que avisar.

    La primera revisión solo registra lo existente (no lo anuncia como nuevo),
    pero sí recuerda lo que ya está cerca de vencer. Lo entregado no se recuerda.
    """
    ahora = ctx.ahora()
    hasta = ahora + timedelta(days=ctx.ajustes.moodle_dias_adelante)
    cliente = obtener_cliente(ctx)
    try:
        eventos = cliente.proximos_eventos(desde=ahora, hasta=hasta)
    except ErrorMoodle as error:
        ctx.moodle = None  # la próxima revisión vuelve a iniciar sesión
        raise ErrorSkill(f"Moodle no respondió: {error}") from error

    repo = ctx.repo
    primera_vez = repo.preferencia(PREFERENCIA_SEMBRADO) is None
    ultima = repo.preferencia(PREFERENCIA_ULTIMA_REVISION)
    ultima_revision = fecha(ultima) if ultima else None
    conocidos = repo.moodle_eventos()
    vistos: set[int] = set()
    actividades: list[tuple[EventoMoodle, bool]] = []
    resultado: dict = {
        "primera_vez": primera_vez,
        "nuevas": [],
        "aperturas": [],
        "cambiadas": [],
        "recordatorios": [],
        "entregadas": [],
        "contenido": [],
    }

    for e in eventos:
        if e.tipo not in TIPOS_VENCIMIENTO:
            continue
        vistos.add(e.id)
        previo = conocidos.get(e.id)
        alcanzados = _umbrales_alcanzados(ctx, e.inicio, ahora)
        # Si Moodle no pudo dar el estado (vacío), se conserva el último conocido.
        entregado = e.entregado if e.estado_entrega else bool(previo and previo["entregado"])
        abierta = e.apertura is None or e.apertura <= ahora

        if previo is None:
            apertura_avisada = abierta  # si ya estaba abierta, no hay apertura que anunciar
            if primera_vez:
                avisos: set[int] = set()  # los recordatorios cercanos se envían abajo
            else:
                if not entregado:
                    resultado["nuevas"].append(e)  # el anuncio dice "aún no abre" si es el caso
                avisos = alcanzados
        else:
            apertura_avisada = bool(previo["apertura_avisada"])
            avisos = previo["avisos"]
            if fecha(previo["fecha"]) != e.inicio:
                if not entregado:
                    resultado["cambiadas"].append((e, fecha(previo["fecha"])))
                avisos = alcanzados
            if entregado and not previo["entregado"]:
                resultado["entregadas"].append(e)

        # Abrió entre la revisión anterior y esta (o mientras el bot estuvo apagado).
        if not apertura_avisada and abierta:
            if not entregado and e.apertura and ultima_revision and e.apertura > ultima_revision:
                resultado["aperturas"].append(e)
            apertura_avisada = True

        if not entregado and alcanzados - avisos:
            resultado["recordatorios"].append(e)
        if alcanzados - avisos:
            avisos = avisos | alcanzados  # entregado o no, ese umbral ya pasó

        actividades.append((e, entregado))
        repo.guardar_evento_moodle(
            e.id, e.nombre, e.curso, e.inicio, e.url, avisos,
            apertura=e.apertura, apertura_avisada=apertura_avisada, entregado=entregado,
        )

    # Lo que ya venció o desapareció de Moodle deja de vigilarse, sin avisar.
    for id_ in set(conocidos) - vistos:
        repo.retirar_evento_moodle(id_)

    if ctx.ajustes.moodle_contenido_nuevo:
        urls_anunciadas = {e.url for e in resultado["nuevas"]}
        resultado["contenido"] = [
            n for n in _contenido_nuevo(ctx, cliente) if n.url not in urls_anunciadas
        ]

    # Todo lo de Moodle es una entrega ya definida: queda registrada y planeada.
    if ctx.ajustes.moodle_planear_entregas:
        resultado.update(sincronizar_entregas(ctx, actividades, _nombre, _curso_corto))
    else:
        resultado.update(planeadas=[], reagendadas=[], cerradas=[])

    resultado["conocidas"] = len(vistos)
    resultado["recordatorios"].sort(key=lambda e: e.inicio)
    if primera_vez:
        repo.fijar_preferencia(PREFERENCIA_SEMBRADO, ahora.isoformat())
    repo.fijar_preferencia(PREFERENCIA_ULTIMA_REVISION, ahora.isoformat())
    return resultado


def _contenido_nuevo(ctx: Contexto, cliente) -> list[NotificacionMoodle]:
    """Notificaciones de contenido posteriores a la última leída. La primera vez solo marca hasta dónde se leyó."""
    ultima = int(ctx.repo.preferencia(PREFERENCIA_ULTIMA_NOTIFICACION) or 0)
    try:
        notificaciones = cliente.notificaciones_contenido(despues_de=ultima)
    except ErrorMoodle:
        return []  # no vale la pena perder los avisos de entregas por esto
    if notificaciones:
        ctx.repo.fijar_preferencia(PREFERENCIA_ULTIMA_NOTIFICACION, str(notificaciones[-1].id))
    return notificaciones if ultima else []


# ------------------------------------------------------------------ redacción


def _curso_corto(curso: str) -> str:
    """'2026B/GESTION DE PROYECTOS TI/GPT01' -> 'GESTION DE PROYECTOS TI'."""
    partes = curso.split("/")
    return partes[1] if len(partes) >= 3 else curso


def _nombre(evento: EventoMoodle) -> str:
    """Quita lo que Moodle agrega al nombre ('Vencimiento de X', 'Cierre de X', 'X is due'); en el aviso sobra."""
    nombre = evento.nombre.strip()
    for prefijo in ("Vencimiento de ", "Cierre de "):
        nombre = nombre.removeprefix(prefijo)
    for sufijo in (" is due", " closes"):
        nombre = nombre.removesuffix(sufijo)
    return nombre.strip()


def _cuando(momento: datetime) -> str:
    return f"{formato_fecha(momento)}, {formato_hora(momento)}"


def _faltan(vence: datetime, ahora: datetime) -> str:
    horas = (vence - ahora).total_seconds() / 3600
    if horas < 1:
        return "en menos de 1 h"
    if horas < 48:
        return f"en {int(horas)} h"
    return f"en {int(horas // 24)} días"


def _notas(e: EventoMoodle, ahora: datetime) -> str:
    """Líneas extra: si aún no abre o si hay un borrador sin enviar."""
    notas = []
    if e.apertura and e.apertura > ahora:
        notas.append(f"  aún no abre: abre {_cuando(e.apertura)}")
    if e.estado_entrega == "draft":
        notas.append("  tienes un borrador sin enviar")
    return "".join(f"\n{n}" for n in notas)


def redactar_aviso_moodle(ctx: Contexto, resultado: dict) -> str | None:
    """Mensaje de Telegram con lo que encontró la revisión, o ``None`` si no hay nada que contar."""
    ahora = ctx.ahora()
    ajustes = ctx.ajustes
    secciones: list[str] = []

    def seccion(titulo: str, lineas: list[str]) -> None:
        if lineas:
            secciones.append("\n".join([titulo, *lineas]))

    if resultado["primera_vez"]:
        secciones.append(
            f"👀 Empecé a vigilar Moodle de {formato_hora(ajustes.moodle_horario_inicio)} a "
            f"{formato_hora(ajustes.moodle_horario_fin)}: ya conozco {resultado['conocidas']} "
            "entregas próximas; te aviso cuando publiquen nuevas, abran, cambien de fecha o se acerque "
            "un vencimiento. Lo que ya entregaste no te lo recuerdo."
        )
    seccion("📌 Nuevas tareas en Moodle:", [
        f"• {_nombre(e)} — {_curso_corto(e.curso)}\n  vence {formato_vence(e.inicio)}{_notas(e, ahora)}\n  {e.url}"
        for e in resultado["nuevas"]
    ])
    seccion("🔓 Ya se puede entregar:", [
        f"• {_nombre(e)} — {_curso_corto(e.curso)}\n  abrió {_cuando(e.apertura)}, vence {formato_vence(e.inicio)}\n  {e.url}"
        for e in resultado["aperturas"]
    ])
    seccion("🔁 Cambió la fecha:", [
        f"• {_nombre(e)} — {_curso_corto(e.curso)}\n  antes {formato_vence(antes)}, ahora {formato_vence(e.inicio)}"
        for e, antes in resultado["cambiadas"]
    ])
    seccion("⏰ Por vencer:", [
        f"• {_nombre(e)} — {_curso_corto(e.curso)}\n  vence {_faltan(e.inicio, ahora)} ({formato_vence(e.inicio)})"
        f"{_notas(e, ahora)}\n  {e.url}"
        for e in resultado["recordatorios"]
    ])
    planeadas = resultado.get("planeadas", [])
    if planeadas:
        secciones.append("🗓️ Agregué a tu plan:\n\n" + lista_por_materia(planeadas, ahora)
                         + "\n\nTus bloques de estudio están en /hoy.")
    reagendadas = resultado.get("reagendadas", [])
    if reagendadas:
        secciones.append("📆 Reacomodé tu plan por el cambio de fecha:\n\n" + lista_por_materia(reagendadas, ahora))
    seccion("✅ Registré tu entrega:", [
        f"• {_nombre(e)} — {_curso_corto(e.curso)}" for e in resultado["entregadas"]
    ])
    contenido = resultado["contenido"]
    seccion("🆕 Contenido en Moodle:", [
        f"• {_curso_corto(n.curso)}: {n.titulo or 'actividad'}"
        f"{' (actualizado)' if n.tipo == 'cambio' else ''}\n  {n.url}"
        for n in contenido[-MAX_CONTENIDOS:]
    ] + ([f"…y {len(contenido) - MAX_CONTENIDOS} más."] if len(contenido) > MAX_CONTENIDOS else []))

    return "\n\n".join(secciones) or None
