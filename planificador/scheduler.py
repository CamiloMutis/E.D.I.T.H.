"""Asignación de bloques de estudio en el calendario.

Es un algoritmo voraz y determinista: recorre las tareas en orden de urgencia
y llena los huecos libres respetando las reglas del usuario. No usa el LLM,
porque las restricciones (no pisar clases, tope diario, franja horaria) deben
cumplirse siempre y el resultado tiene que poder probarse.

Todas las duraciones se manejan en minutos enteros para evitar errores de
redondeo con horas decimales.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ajustes import Reglas

PASO_MINUTOS = 15  # los bloques empiezan y terminan en cuartos de hora


@dataclass(frozen=True)
class Intervalo:
    inicio: datetime
    fin: datetime

    @property
    def minutos(self) -> int:
        return int((self.fin - self.inicio).total_seconds() // 60)


@dataclass(frozen=True)
class Tarea:
    """Trabajo pendiente que hay que ubicar antes de su fecha límite."""

    id: int
    minutos: int
    fecha_limite: datetime
    grupo: int = 0  # entrega a la que pertenece; se usa para calcular la holgura


@dataclass(frozen=True)
class BloqueAsignado:
    tarea_id: int
    inicio: datetime
    fin: datetime

    @property
    def minutos(self) -> int:
        return int((self.fin - self.inicio).total_seconds() // 60)


@dataclass
class ResultadoPlan:
    bloques: list[BloqueAsignado] = field(default_factory=list)
    sin_asignar: dict[int, int] = field(default_factory=dict)  # tarea_id -> minutos que no cupieron

    def minutos_asignados(self, tarea_id: int) -> int:
        return sum(b.minutos for b in self.bloques if b.tarea_id == tarea_id)


def horas_a_minutos(horas: float) -> int:
    """Convierte horas a minutos redondeando hacia arriba al cuarto de hora."""
    return max(0, math.ceil(horas * 60 / PASO_MINUTOS - 1e-9) * PASO_MINUTOS)


def redondear_arriba(momento: datetime) -> datetime:
    """Siguiente cuarto de hora exacto (o el mismo instante si ya lo es)."""
    base = momento.replace(second=0, microsecond=0)
    sobrante = base.minute % PASO_MINUTOS
    if sobrante or momento != base:
        base += timedelta(minutes=PASO_MINUTOS - sobrante if sobrante else PASO_MINUTOS)
    return base


def limite_de_trabajo(fecha_limite: datetime, desde: datetime, reglas: Reglas) -> datetime:
    """Momento en que el trabajo debe estar terminado.

    Normalmente es la fecha límite menos el margen de seguridad. Si la entrega
    está tan cerca que el margen no deja espacio, se usa la fecha límite real:
    es preferible trabajar sin margen a no agendar nada.
    """
    con_margen = fecha_limite - timedelta(days=reglas.margen_dias)
    return con_margen if con_margen > desde else fecha_limite


def _restar(libre: Intervalo, ocupados: list[Intervalo], descanso: timedelta) -> list[Intervalo]:
    """Quita de un intervalo libre los tramos ocupados, dejando el descanso a cada lado."""
    tramos = [libre]
    for ocupado in ocupados:
        inicio_o, fin_o = ocupado.inicio - descanso, ocupado.fin + descanso
        siguientes = []
        for tramo in tramos:
            if fin_o <= tramo.inicio or inicio_o >= tramo.fin:
                siguientes.append(tramo)
                continue
            if inicio_o > tramo.inicio:
                siguientes.append(Intervalo(tramo.inicio, inicio_o))
            if fin_o < tramo.fin:
                siguientes.append(Intervalo(fin_o, tramo.fin))
        tramos = siguientes
    return tramos


def huecos_libres(
    ocupados: list[Intervalo], desde: datetime, hasta: datetime, reglas: Reglas
) -> list[Intervalo]:
    """Huecos disponibles entre ``desde`` y ``hasta``, en orden cronológico.

    Cada hueco cae dentro de la franja permitida de un solo día, no pisa
    ningún evento existente y mide al menos un bloque mínimo.
    """
    if hasta <= desde:
        return []
    descanso = timedelta(minutes=reglas.descanso_minutos)
    huecos: list[Intervalo] = []
    dia = desde.date()
    while dia <= hasta.date():
        inicio_franja = datetime.combine(dia, reglas.franja_inicio, tzinfo=desde.tzinfo)
        fin_franja = datetime.combine(dia, reglas.franja_fin, tzinfo=desde.tzinfo)
        inicio = redondear_arriba(max(inicio_franja, desde))
        fin = min(fin_franja, hasta)
        if fin > inicio:
            del_dia = [o for o in ocupados if o.fin > inicio - descanso and o.inicio < fin + descanso]
            for tramo in _restar(Intervalo(inicio, fin), del_dia, descanso):
                tramo = Intervalo(redondear_arriba(tramo.inicio), tramo.fin)
                if tramo.minutos >= reglas.bloque_min_minutos:
                    huecos.append(tramo)
        dia += timedelta(days=1)
    return sorted(huecos, key=lambda h: h.inicio)


def _tope_diario(reglas: Reglas, horas_extra_dia: float) -> int:
    return int(round((reglas.horas_max_dia + horas_extra_dia) * 60))


def capacidad_minutos(
    ocupados: list[Intervalo],
    desde: datetime,
    hasta: datetime,
    reglas: Reglas,
    minutos_por_dia: dict[date, int] | None = None,
    horas_extra_dia: float = 0.0,
) -> int:
    """Minutos de estudio que todavía caben en el rango, contando el tope diario."""
    usados = minutos_por_dia or {}
    tope = _tope_diario(reglas, horas_extra_dia)
    libres_por_dia: dict[date, int] = defaultdict(int)
    for hueco in huecos_libres(ocupados, desde, hasta, reglas):
        libres_por_dia[hueco.inicio.date()] += hueco.minutos
    return sum(
        max(0, min(libres, tope - usados.get(dia, 0))) for dia, libres in libres_por_dia.items()
    )


def asignar(
    tareas: list[Tarea],
    ocupados: list[Intervalo],
    desde: datetime,
    reglas: Reglas,
    direccion: str = "adelante",
    minutos_por_dia: dict[date, int] | None = None,
    horas_extra_dia: float = 0.0,
) -> ResultadoPlan:
    """Ubica las tareas en los huecos libres, en el orden en que llegan.

    Args:
        tareas: trabajo pendiente, ya ordenado por prioridad.
        ocupados: eventos existentes que no se pueden pisar.
        desde: primer instante en que se puede agendar.
        reglas: restricciones del usuario.
        direccion: ``"adelante"`` llena desde ``desde`` hacia la fecha límite
            (replanificación); ``"atras"`` llena desde la fecha límite hacia
            el presente (plan inicial de una entrega).
        minutos_por_dia: minutos de estudio ya agendados por día, para que el
            tope diario cuente los bloques de otras entregas.
        horas_extra_dia: horas adicionales al tope diario, cuando el usuario
            acepta esforzarse más para salvar una entrega.

    Returns:
        Los bloques asignados y, por tarea, los minutos que no cupieron.
    """
    if direccion not in ("adelante", "atras"):
        raise ValueError(f"direccion desconocida: {direccion}")

    resultado = ResultadoPlan()
    ocupados = list(ocupados)
    usados: dict[date, int] = defaultdict(int, minutos_por_dia or {})
    tope = _tope_diario(reglas, horas_extra_dia)
    minimo, maximo = reglas.bloque_min_minutos, reglas.bloque_max_minutos

    for tarea in tareas:
        restante = tarea.minutos
        if restante <= 0:
            continue
        hasta = limite_de_trabajo(tarea.fecha_limite, desde, reglas)

        # Tras cada bloque se recalculan los huecos: así el descanso queda
        # reservado y una misma tarea puede ocupar dos tramos del mismo día.
        while restante > 0:
            huecos = huecos_libres(ocupados, desde, hasta, reglas)
            if direccion == "atras":
                huecos.reverse()
            colocado = False
            for hueco in huecos:
                dia = hueco.inicio.date()
                disponible_dia = tope - usados[dia]
                duracion = min(restante, maximo, hueco.minutos, disponible_dia)
                duracion = duracion // PASO_MINUTOS * PASO_MINUTOS
                if duracion < minimo:
                    # Un remanente corto se agenda como bloque mínimo; un hueco o
                    # un tope diario que no alcanza para el mínimo se descarta.
                    if restante < minimo and hueco.minutos >= minimo and disponible_dia >= minimo:
                        duracion = minimo
                    else:
                        continue
                # Evita dejar un remanente más corto que el bloque mínimo: se
                # acorta este bloque para que el siguiente alcance el mínimo.
                sobra = restante - duracion
                if 0 < sobra < minimo and duracion - (minimo - sobra) >= minimo:
                    duracion -= minimo - sobra
                if direccion == "atras":
                    inicio = hueco.fin - timedelta(minutes=duracion)
                    inicio -= timedelta(minutes=inicio.minute % PASO_MINUTOS)
                else:
                    inicio = hueco.inicio
                fin = inicio + timedelta(minutes=duracion)
                resultado.bloques.append(BloqueAsignado(tarea.id, inicio, fin))
                ocupados.append(Intervalo(inicio, fin))
                usados[dia] += duracion
                restante -= min(duracion, restante)
                colocado = True
                break
            if not colocado:
                break

        if restante > 0:
            resultado.sin_asignar[tarea.id] = restante

    resultado.bloques.sort(key=lambda b: b.inicio)
    return resultado


def asignar_hacia_atras(
    tareas: list[Tarea],
    ocupados: list[Intervalo],
    desde: datetime,
    reglas: Reglas,
    minutos_por_dia: dict[date, int] | None = None,
) -> ResultadoPlan:
    """Plan inicial de una entrega: se agenda de la fecha límite hacia atrás.

    Trabaja en dos pasadas. La primera recorre los días desde la fecha límite
    hacia el presente y decide qué días hace falta estudiar. La
    segunda ubica las tareas dentro de esos días, en su orden natural y lo más
    temprano posible. Así el trabajo queda pegado a la entrega solo lo
    necesario, se conserva el orden de las subtareas y no se empuja todo a la
    última hora de la noche.

    Args:
        tareas: subtareas de una misma entrega, en el orden en que se deben hacer.
    """
    usados = dict(minutos_por_dia or {})
    primera = asignar(list(reversed(tareas)), ocupados, desde, reglas, "atras", usados)
    if not primera.bloques:
        return primera

    dias_elegidos = {bloque.inicio.date() for bloque in primera.bloques}

    # Los días que la primera pasada no usó se cierran; los elegidos conservan
    # su cupo real, para que un bloque pueda correrse dentro del mismo día.
    tope = _tope_diario(reglas, 0.0)
    ultimo = max(t.fecha_limite for t in tareas).date()
    cupos: dict[date, int] = {}
    dia = desde.date()
    while dia <= ultimo:
        cupos[dia] = usados.get(dia, 0) if dia in dias_elegidos else tope
        dia += timedelta(days=1)
    segunda = asignar(tareas, ocupados, desde, reglas, "adelante", cupos)

    # Si al reordenar algo dejó de caber (huecos muy fragmentados), vale la primera pasada.
    if sum(segunda.sin_asignar.values()) > sum(primera.sin_asignar.values()):
        return primera
    return segunda


def clasificar_riesgo(holgura: float | None, tranquila: float = 1.5, ajustada: float = 1.0) -> str:
    """Traduce la holgura (horas disponibles / horas pendientes) a un nivel de riesgo."""
    if holgura is None:
        return "tranquila"
    if holgura >= tranquila:
        return "tranquila"
    if holgura >= ajustada:
        return "ajustada"
    return "en_riesgo"
