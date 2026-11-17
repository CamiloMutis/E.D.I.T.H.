"""Pruebas de los skills sobre una base de datos temporal y el calendario local."""

from datetime import timedelta

import pytest

from skills.agenda import ErrorSkill, fecha
from skills.estimar_tiempo import estimar_tiempo, reentrenar_predictor
from skills.planificar_entrega import planificar_entrega
from skills.replanificar import replanificar
from skills import soporte
from tests.conftest import AHORA, momento

SUBTAREAS = [
    {"nombre": "Repasar semáforos", "tipo_tarea": "lectura", "dificultad": 3, "horas_usuario": 1},
    {"nombre": "Programar la solución", "tipo_tarea": "codigo", "dificultad": 4, "horas_usuario": 2},
    {"nombre": "Escribir el informe", "tipo_tarea": "informe", "dificultad": 3, "horas_usuario": 1},
]


def _entrega(ctx, nombre="Taller 3", dias=6, materia="Sistemas Operativos"):
    limite = (AHORA + timedelta(days=dias)).date().isoformat()
    return soporte.registrar_entrega(ctx, materia, nombre, limite, 15, "Sincronización con semáforos")


def _planificada(ctx, **kwargs):
    entrega = _entrega(ctx, **kwargs)
    return entrega, planificar_entrega(ctx, entrega["entrega_id"], SUBTAREAS)


# ------------------------------------------------------------- estimar_tiempo

def test_estimar_corrige_la_subestimacion(ctx):
    resultado = estimar_tiempo(ctx, "Sistemas Operativos", "codigo", 4, 2)
    assert resultado["fuente"] == "modelo"
    assert resultado["horas"] > 2           # el código siempre toma más de lo estimado
    assert "subestimar" in resultado["mensaje"]


def test_estimar_sin_estimacion_del_usuario(ctx):
    resultado = estimar_tiempo(ctx, "Inteligencia Artificial", "lectura")
    assert resultado["horas_usuario"] is None
    assert 0.25 <= resultado["horas"] <= 40


def test_estimar_acepta_tipo_desconocido(ctx):
    assert estimar_tiempo(ctx, "Sistemas Operativos", "maqueta", 3, 2)["tipo_tarea"] == "taller"


# ---------------------------------------------------------- registrar_entrega

def test_registrar_entrega_reconoce_la_materia_sin_tildes(ctx):
    entrega = soporte.registrar_entrega(ctx, "gestion de proyectos", "Acta", "2026-11-27", 10)
    assert entrega["materia"] == "Gestión de Proyectos de TI"
    assert entrega["peso_nota"] == 0.10
    assert fecha(entrega["fecha_limite"]).hour == 23     # sin hora: final del día


def test_registrar_entrega_rechaza_fecha_pasada_y_materia_inexistente(ctx):
    with pytest.raises(ErrorSkill):
        soporte.registrar_entrega(ctx, "Sistemas Operativos", "Taller", "2026-11-01")
    with pytest.raises(ErrorSkill):
        soporte.registrar_entrega(ctx, "Astrofísica", "Taller", "2026-11-27")


# --------------------------------------------------------- planificar_entrega

def test_planificar_crea_subtareas_bloques_y_eventos(ctx):
    entrega, plan = _planificada(ctx)
    limite = fecha(entrega["fecha_limite"])

    assert len(plan["subtareas"]) == 3
    assert plan["horas_sin_agendar"] == 0
    assert plan["riesgo"] in ("tranquila", "ajustada")
    # Cada bloque quedó también como evento del calendario.
    eventos = ctx.calendario.listar_eventos(AHORA, limite)
    assert len(eventos) == len(plan["bloques"]) > 0
    # Las horas agendadas cubren lo que predijo el modelo, no lo que dijo el usuario.
    agendadas = sum((fecha(b["fin"]) - fecha(b["inicio"])).total_seconds() / 3600 for b in plan["bloques"])
    predichas = sum(s["horas_estimadas"] for s in plan["subtareas"])
    assert agendadas == pytest.approx(predichas, abs=0.5)
    assert predichas > 4
    # Todo termina antes del margen de seguridad.
    assert all(fecha(b["fin"]) <= limite - timedelta(days=1) for b in plan["bloques"])


def test_planificar_no_pisa_las_clases(ctx):
    for dia in range(17, 23):
        ctx.calendario.crear_evento("Clase", momento(dia, 7), momento(dia, 12))
    _, plan = _planificada(ctx)
    for bloque in plan["bloques"]:
        inicio, fin = fecha(bloque["inicio"]), fecha(bloque["fin"])
        assert not (inicio < momento(inicio.day, 12, 15) and fin > momento(inicio.day, 6, 45))


def test_planificar_dos_veces_la_misma_entrega_falla(ctx):
    entrega, _ = _planificada(ctx)
    with pytest.raises(ErrorSkill):
        planificar_entrega(ctx, entrega["entrega_id"], SUBTAREAS)


def test_planificar_entrega_imposible_queda_en_riesgo(ctx):
    limite = (AHORA + timedelta(days=1)).date().isoformat()
    entrega = soporte.registrar_entrega(ctx, "Inteligencia Artificial", "Proyecto final", limite, 30)
    enorme = [{"nombre": f"Parte {i}", "tipo_tarea": "codigo", "dificultad": 5, "horas_usuario": 8} for i in range(4)]
    plan = planificar_entrega(ctx, entrega["entrega_id"], enorme)
    assert plan["riesgo"] == "en_riesgo"
    assert plan["horas_sin_agendar"] > 0


# ------------------------------------------------------- avance y replanificar

def test_marcar_bloque_descuenta_horas_y_se_puede_corregir(ctx):
    _, plan = _planificada(ctx)
    bloque = plan["bloques"][0]
    duracion = (fecha(bloque["fin"]) - fecha(bloque["inicio"])).total_seconds() / 3600

    hecho = soporte.marcar_bloque(ctx, bloque["bloque_id"], "hecho")
    assert hecho["horas_hechas"] == pytest.approx(duracion)
    # El usuario se arrepiente: en realidad no lo hizo.
    corregido = soporte.marcar_bloque(ctx, bloque["bloque_id"], "perdido")
    assert corregido["horas_hechas"] == 0
    assert corregido["horas_restantes"] == pytest.approx(hecho["horas_restantes"] + duracion)


def test_replanificar_reubica_los_bloques_no_hechos(ctx):
    entrega, plan = _planificada(ctx)
    primero = fecha(plan["bloques"][0]["fin"])
    ctx.reloj.actual = primero + timedelta(hours=1)        # pasó el primer bloque sin marcarse

    resultado = replanificar(ctx)

    assert len(resultado["bloques_perdidos"]) == 1
    assert not resultado["hay_riesgo"]
    # Las horas pendientes siguen completas: lo que no está en el bloque que
    # el estudiante hace en este momento quedó agendado en el futuro.
    def horas(bloques):
        return sum((fecha(b["fin"]) - fecha(b["inicio"])).total_seconds() / 3600 for b in bloques)

    restantes = sum(s["horas_restantes"] for s in ctx.repo.subtareas_de_entrega(entrega["entrega_id"]))
    en_curso = ctx.repo.bloques_en_curso(ctx.ahora())
    assert len(en_curso) == 1
    assert horas(resultado["bloques"]) + horas(en_curso) == pytest.approx(restantes, abs=0.5)
    assert all(fecha(b["inicio"]) >= ctx.ahora() for b in resultado["bloques"])
    # El calendario no conserva eventos huérfanos: perdidos + nuevos.
    eventos = ctx.calendario.listar_eventos(AHORA, AHORA + timedelta(days=10))
    assert len(eventos) == len(resultado["bloques"]) + len(plan["bloques"]) - resultado["bloques_anteriores"]


def test_replanificar_detecta_riesgo_y_las_horas_extra_lo_alivian(ctx):
    entrega, _ = _planificada(ctx, dias=6)
    # No se hizo nada y la entrega es mañana a mediodía... casi.
    ctx.reloj.actual = fecha(entrega["fecha_limite"]) - timedelta(hours=20)

    sin_extra = replanificar(ctx)
    assert sin_extra["hay_riesgo"]
    estado = next(e for e in sin_extra["entregas"] if e["entrega_id"] == entrega["entrega_id"])
    assert estado["riesgo"] == "en_riesgo" and estado["horas_sin_agendar"] > 0

    con_extra = replanificar(ctx, horas_extra_dia=4)
    estado_extra = next(e for e in con_extra["entregas"] if e["entrega_id"] == entrega["entrega_id"])
    assert estado_extra["horas_sin_agendar"] < estado["horas_sin_agendar"]


def test_replanificar_prioriza_la_entrega_mas_cercana(ctx):
    cercana, _ = _planificada(ctx, nombre="Quiz", dias=3)
    _planificada(ctx, nombre="Proyecto", dias=9, materia="Arquitectura de Software")
    resultado = replanificar(ctx)
    assert resultado["bloques"][0]["entrega"] == "Quiz"


def test_registrar_avance_alimenta_el_historial(ctx):
    entrega, _ = _planificada(ctx)
    subtarea = ctx.repo.subtareas_de_entrega(entrega["entrega_id"])[0]
    antes = len(ctx.repo.historial(fuente="uso"))

    resultado = soporte.registrar_avance(ctx, subtarea["id"], terminada=True, horas_reales=2.5)

    assert resultado["estado"] == "hecha"
    uso = ctx.repo.historial(fuente="uso")
    assert len(uso) == antes + 1
    assert uso[-1]["real_h"] == 2.5 and uso[-1]["estimado_h"] == 1
    # Sus bloques futuros se liberan del calendario.
    assert all(b["subtarea_id"] != subtarea["id"] for b in ctx.repo.bloques_pendientes_desde(ctx.ahora()))
    # Registrar dos veces no duplica la fila del historial.
    soporte.registrar_avance(ctx, subtarea["id"], terminada=True, horas_reales=2.5)
    assert len(ctx.repo.historial(fuente="uso")) == antes + 1


def test_marcar_entregada_libera_el_calendario(ctx):
    entrega, plan = _planificada(ctx)
    resultado = soporte.marcar_entregada(ctx, entrega["entrega_id"])
    assert resultado["bloques_liberados"] == len(plan["bloques"])
    assert ctx.calendario.listar_eventos(AHORA, AHORA + timedelta(days=10)) == []
    assert soporte.listar_pendientes(ctx)["total"] == 0


def test_resumen_y_consulta_de_calendario(ctx):
    _planificada(ctx)
    ctx.calendario.crear_evento("Clase: Redes Inalámbricas", momento(17, 14), momento(17, 16))
    assert "Taller 3" in soporte.resumen_del_dia(ctx)
    consulta = soporte.consultar_calendario(ctx, "2026-11-17", "2026-11-17")
    assert any("Redes" in e["titulo"] for e in consulta["eventos"])
    assert consulta["huecos_libres"]


def test_reentrenar_incluye_el_historial_de_uso(ctx, tmp_path):
    ctx.ajustes.carpeta_modelos = tmp_path / "modelos"     # no pisa los modelos del repositorio
    ctx.repo.agregar_historial("Sistemas Operativos", "codigo", 4, 2.0, 5.0, "lunes", "noche", "uso")
    metricas = reentrenar_predictor(ctx)
    assert metricas["filas_de_uso"] == 1
    assert metricas["MAE"] < metricas["MAE_linea_base"]
    assert (ctx.ajustes.carpeta_modelos / "predictor_tiempo.joblib").exists()
