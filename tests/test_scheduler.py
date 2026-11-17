"""Pruebas del algoritmo de asignación de bloques."""

from datetime import time, timedelta

from ajustes import Reglas
from planificador.scheduler import (
    Intervalo,
    Tarea,
    asignar,
    asignar_hacia_atras,
    capacidad_minutos,
    clasificar_riesgo,
    horas_a_minutos,
    huecos_libres,
    redondear_arriba,
)
from tests.conftest import momento

REGLAS = Reglas()


def _sin_solapes(bloques, ocupados):
    intervalos = [(b.inicio, b.fin) for b in bloques] + [(o.inicio, o.fin) for o in ocupados]
    intervalos.sort()
    return all(anterior[1] <= siguiente[0] for anterior, siguiente in zip(intervalos, intervalos[1:]))


def test_horas_a_minutos_redondea_al_cuarto_de_hora():
    assert horas_a_minutos(1.0) == 60
    assert horas_a_minutos(1.1) == 75
    assert horas_a_minutos(0) == 0


def test_redondear_arriba():
    assert redondear_arriba(momento(16, 8, 7)) == momento(16, 8, 15)
    assert redondear_arriba(momento(16, 8, 0)) == momento(16, 8, 0)


def test_huecos_respetan_franja_y_eventos():
    clase = Intervalo(momento(16, 9), momento(16, 11))
    huecos = huecos_libres([clase], momento(16, 6), momento(16, 23, 59), REGLAS)
    assert huecos[0].inicio == momento(16, 7)          # nada antes de la franja
    assert huecos[0].fin == momento(16, 8, 45)          # descanso antes de la clase
    assert huecos[1].inicio == momento(16, 11, 15)      # descanso después de la clase
    assert huecos[-1].fin == momento(16, 23)            # nada después de la franja


def test_huecos_descartan_tramos_menores_al_bloque_minimo():
    ocupados = [Intervalo(momento(16, 7), momento(16, 12)), Intervalo(momento(16, 12, 50), momento(16, 23))]
    assert huecos_libres(ocupados, momento(16, 7), momento(16, 23), REGLAS) == []


def test_asignar_no_pisa_eventos_y_respeta_tope_diario():
    clases = [Intervalo(momento(17, 7), momento(17, 9)), Intervalo(momento(17, 14), momento(17, 16))]
    tarea = Tarea(1, horas_a_minutos(6), momento(20, 23, 59))
    plan = asignar([tarea], clases, momento(17, 7), REGLAS)

    assert plan.sin_asignar == {}
    assert plan.minutos_asignados(1) == 360
    assert _sin_solapes(plan.bloques, clases)
    por_dia = {}
    for bloque in plan.bloques:
        por_dia[bloque.inicio.date()] = por_dia.get(bloque.inicio.date(), 0) + bloque.minutos
    assert max(por_dia.values()) <= REGLAS.horas_max_dia * 60


def test_bloques_cumplen_duracion_minima_y_maxima():
    plan = asignar([Tarea(1, horas_a_minutos(5), momento(20, 23, 59))], [], momento(17, 7), REGLAS)
    for bloque in plan.bloques:
        assert REGLAS.bloque_min_minutos <= bloque.minutos <= REGLAS.bloque_max_minutos
        assert time(7, 0) <= bloque.inicio.time() and bloque.fin.time() <= time(23, 0)


def test_dia_lleno_pasa_al_dia_siguiente():
    dia_lleno = [Intervalo(momento(17, 7), momento(17, 23))]
    plan = asignar([Tarea(1, 120, momento(20, 23, 59))], dia_lleno, momento(17, 7), REGLAS)
    assert plan.sin_asignar == {}
    assert all(bloque.inicio.date() == momento(18, 0).date() for bloque in plan.bloques)


def test_entrega_imposible_queda_sin_asignar():
    # 20 horas de trabajo y la entrega es mañana: no cabe con el tope de 4 horas diarias.
    plan = asignar([Tarea(1, horas_a_minutos(20), momento(18, 12))], [], momento(17, 7), REGLAS)
    assert plan.sin_asignar[1] > 0
    assert plan.minutos_asignados(1) + plan.sin_asignar[1] == 20 * 60


def test_bloque_que_no_cabe_en_huecos_pequenos():
    # Solo hay huecos de 30 minutos, menores que el bloque mínimo.
    ocupados = [Intervalo(momento(17, 7, 30) + timedelta(hours=h), momento(17, 8) + timedelta(hours=h)) for h in range(16)]
    assert huecos_libres(ocupados, momento(17, 7), momento(17, 23), REGLAS) == []
    plan = asignar([Tarea(1, 60, momento(17, 23))], ocupados, momento(17, 7), REGLAS)
    assert plan.sin_asignar == {1: 60}


def test_el_tope_diario_cuenta_bloques_previos_y_horas_extra():
    tarea = Tarea(1, 120, momento(17, 23))
    previos = {momento(17, 0).date(): 240}
    reglas = Reglas(margen_dias=0)
    assert asignar([tarea], [], momento(17, 7), reglas, minutos_por_dia=previos).sin_asignar == {1: 120}
    con_extra = asignar([tarea], [], momento(17, 7), reglas, minutos_por_dia=previos, horas_extra_dia=2)
    assert con_extra.sin_asignar == {}


def test_prioridad_la_primera_tarea_recibe_los_primeros_huecos():
    urgente = Tarea(1, 120, momento(18, 23, 59))
    lejana = Tarea(2, 120, momento(25, 23, 59))
    plan = asignar([urgente, lejana], [], momento(17, 7), REGLAS)
    primero = min(plan.bloques, key=lambda b: b.inicio)
    assert primero.tarea_id == 1


def test_margen_de_seguridad_antes_de_la_entrega():
    plan = asignar([Tarea(1, 120, momento(20, 18))], [], momento(16, 8), REGLAS)
    assert all(bloque.fin <= momento(19, 18) for bloque in plan.bloques)


def test_sin_margen_posible_se_usa_la_fecha_limite():
    # La entrega es en 6 horas: el margen de un día no cabe, pero se agenda igual.
    plan = asignar([Tarea(1, 60, momento(16, 14))], [], momento(16, 8), REGLAS)
    assert plan.sin_asignar == {}
    assert all(bloque.fin <= momento(16, 14) for bloque in plan.bloques)


def test_hacia_atras_queda_cerca_de_la_entrega_y_conserva_el_orden():
    limite = momento(27, 23, 59)
    tareas = [Tarea(1, 120, limite), Tarea(2, 180, limite), Tarea(3, 60, limite)]
    plan = asignar_hacia_atras(tareas, [], momento(16, 8), REGLAS)

    assert plan.sin_asignar == {}
    # Cerca de la fecha límite, no al comienzo del periodo disponible.
    assert min(b.inicio for b in plan.bloques) >= momento(25, 0)
    assert max(b.fin for b in plan.bloques) <= limite - timedelta(days=REGLAS.margen_dias)
    # El orden de las subtareas se mantiene en el calendario.
    orden = [b.tarea_id for b in plan.bloques]
    assert orden == sorted(orden)


def test_sin_remanentes_menores_al_bloque_minimo():
    # 2 h 15 min: no debe quedar un bloque de 15 minutos.
    plan = asignar([Tarea(1, 135, momento(20, 23, 59))], [], momento(17, 7), REGLAS)
    assert plan.minutos_asignados(1) == 135
    assert all(bloque.minutos >= REGLAS.bloque_min_minutos for bloque in plan.bloques)


def test_capacidad_respeta_el_tope_diario():
    # Dos días completamente libres: 4 horas por día.
    assert capacidad_minutos([], momento(17, 7), momento(18, 23), REGLAS) == 480


def test_clasificar_riesgo():
    assert clasificar_riesgo(2.0) == "tranquila"
    assert clasificar_riesgo(1.2) == "ajustada"
    assert clasificar_riesgo(0.8) == "en_riesgo"
    assert clasificar_riesgo(None) == "tranquila"
