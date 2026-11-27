"""Pruebas de los cursos de Moodle ocultos: no se registran, no se planean, no avisan, y se puede deshacer."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from skills import cursos_ocultos
from skills.agenda import ErrorSkill
from skills.consultar_moodle import consultar_moodle
from skills.vigilar_moodle import revisar_moodle
from tests.test_integraciones import bot_de_prueba
from tests.test_moodle import EVENTOS, MoodleFalso, _evento, cliente
from tests.conftest import momento, registrar

GESTION = "GESTION DE PROYECTOS TI"     # curso 10 del Moodle falso
SISTEMAS = "SISTEMAS OPERATIVOS"        # curso 20


@pytest.fixture
def moodle(ctx):
    servidor = MoodleFalso(eventos=list(EVENTOS))
    ctx.moodle = cliente(servidor)
    revisar_moodle(ctx)  # primera revisión: registra y planea todo
    return servidor


def _entregas(ctx, estado="abierta"):
    filas = ctx.repo._filas("SELECT e.nombre, e.estado FROM entregas e WHERE e.moodle_id IS NOT NULL", ())
    return [f["nombre"] for f in filas if f["estado"] == estado]


def test_curso_corto():
    assert cursos_ocultos.curso_corto("2026B/INGLES III-VIR-PRE/3PRE06") == "INGLES III-VIR-PRE"
    assert cursos_ocultos.curso_corto("Sin barras") == "Sin barras"


def test_ocultar_cierra_lo_planeado_y_ya_no_vuelve(ctx, moodle):
    assert len(_entregas(ctx)) == 3
    assert cursos_ocultos.cursos_conocidos(ctx) == [GESTION, SISTEMAS]

    resultado = cursos_ocultos.ocultar_curso(ctx, "gestion")
    assert resultado == {"curso": GESTION, "entregas_cerradas": 2}
    abiertas = _entregas(ctx)
    assert len(abiertas) == 1 and "Quiz" in abiertas[0]       # solo queda la de Sistemas Operativos
    assert len(_entregas(ctx, "oculta")) == 2
    # Sin bloques futuros de las entregas ocultas.
    assert all(b["entrega_id"] in {e["id"] for e in ctx.repo.entregas_abiertas()}
               for b in ctx.repo.bloques_pendientes_desde(ctx.ahora()))

    # Una tarea nueva de ese curso no se registra ni se anuncia, ni en la vigilancia ni al consultar.
    moodle.eventos.append(_evento(9, "Vencimiento de Acta de cierre", 10, momento(27, 23, 59)))
    resultado = revisar_moodle(ctx)
    assert resultado["nuevas"] == []
    assert len(_entregas(ctx)) == 1
    assert all(GESTION not in e["curso"] for e in consultar_moodle(ctx)["eventos"])

    # Sigue en la lista (marcado como oculto) para poder deshacerlo.
    assert GESTION in cursos_ocultos.cursos_conocidos(ctx)
    assert cursos_ocultos.ocultos(ctx) == [GESTION]


def test_mostrar_curso_lo_vuelve_a_vigilar(ctx, moodle):
    cursos_ocultos.ocultar_curso(ctx, "1")                  # por número de la lista
    assert cursos_ocultos.mostrar_curso(ctx, "gestion") == {"curso": GESTION}
    assert cursos_ocultos.ocultos(ctx) == []
    revisar_moodle(ctx)
    assert len(_entregas(ctx)) == 3                         # vuelven a planearse


def test_errores_claros(ctx, moodle):
    with pytest.raises(ErrorSkill, match="No encontré"):
        cursos_ocultos.ocultar_curso(ctx, "astronomia")
    with pytest.raises(ErrorSkill, match="No tienes cursos ocultos"):
        cursos_ocultos.mostrar_curso(ctx, "1")


def test_los_comandos_de_telegram(gestor):
    sesion = registrar(gestor, 42)
    servidor = MoodleFalso(eventos=list(EVENTOS))
    sesion.ctx.moodle = cliente(servidor)
    revisar_moodle(sesion.ctx)
    bot = bot_de_prueba(gestor)
    respuestas: list[str] = []

    def mensaje(*argumentos):
        async def responder(texto, **_):
            respuestas.append(texto)

        return (
            SimpleNamespace(effective_user=SimpleNamespace(id=42), message=SimpleNamespace(reply_text=responder)),
            SimpleNamespace(args=list(argumentos)),
        )

    asyncio.run(bot.cmd_ocultar_curso(*mensaje()))
    assert "1. GESTION DE PROYECTOS TI" in respuestas[-1]
    asyncio.run(bot.cmd_ocultar_curso(*mensaje("1")))
    assert "para siempre" in respuestas[-1] and "Cerré 2" in respuestas[-1]
    asyncio.run(bot.cmd_ocultar_curso(*mensaje()))
    assert "GESTION DE PROYECTOS TI (oculto)" in respuestas[-1]
    asyncio.run(bot.cmd_mostrar_curso(*mensaje("1")))
    assert "Vuelvo a vigilar" in respuestas[-1]
