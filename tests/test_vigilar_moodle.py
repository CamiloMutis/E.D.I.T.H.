"""Pruebas de la vigilancia de Moodle: tareas nuevas, aperturas, entregas, recordatorios, contenido y horario."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import time

import httpx
import pytest

from ajustes import cargar_ajustes
from db.repositorio import Repositorio
from integraciones.moodle import ClienteMoodle, ErrorMoodle
from skills.agenda import ErrorSkill
from skills.vigilar_moodle import dentro_del_horario, redactar_aviso_moodle, revisar_moodle
from tests.conftest import momento, registrar
from tests.test_integraciones import bot_de_prueba
from tests.test_moodle import EVENTOS, URL, MoodleFalso, _evento, cliente, notificacion

# AHORA (conftest) es lunes 16 nov, 8:00. Eventos de EVENTOS (instancia = id + 1000):
#   id 1: dom 22 nov 23:59 · id 3: mié 25 nov 10:00 (quiz) · id 2: dom 29 nov 23:59


@pytest.fixture
def moodle(ctx):
    """Servidor falso con una copia de los eventos que cada prueba puede modificar."""
    servidor = MoodleFalso(eventos=list(EVENTOS))
    ctx.moodle = cliente(servidor)
    return servidor


def test_primera_revision_registra_sin_anunciar_como_nuevas(ctx, moodle):
    resultado = revisar_moodle(ctx)
    assert resultado["primera_vez"] and resultado["conocidas"] == 3
    assert resultado["nuevas"] == [] and resultado["recordatorios"] == []

    texto = redactar_aviso_moodle(ctx, resultado)
    assert "Empecé a vigilar Moodle de 6 a. m. a 10 p. m." in texto
    assert "3 entregas" in texto


def test_sin_cambios_no_hay_nada_que_avisar(ctx, moodle):
    revisar_moodle(ctx)
    resultado = revisar_moodle(ctx)
    assert not resultado["primera_vez"]
    assert redactar_aviso_moodle(ctx, resultado) is None


def test_avisa_una_tarea_nueva(ctx, moodle):
    revisar_moodle(ctx)
    moodle.eventos.append(_evento(4, "Vencimiento de Manual de Usuario", 10, momento(27, 23, 59)))

    resultado = revisar_moodle(ctx)
    assert [e.nombre for e in resultado["nuevas"]] == ["Vencimiento de Manual de Usuario"]
    texto = redactar_aviso_moodle(ctx, resultado)
    assert "📌 Nuevas tareas en Moodle" in texto
    assert "GESTION DE PROYECTOS TI" in texto and "vie 27 nov, 11:59 p. m." in texto
    assert "https://moodle.prueba/mod/assign/view.php?id=" in texto

    # La siguiente revisión ya no la anuncia otra vez.
    assert revisar_moodle(ctx)["nuevas"] == []


def test_avisa_un_cambio_de_fecha(ctx, moodle):
    revisar_moodle(ctx)
    moodle.eventos[1] = _evento(1, "Vencimiento de Matriz de Pruebas", 10, momento(24, 23, 59))

    resultado = revisar_moodle(ctx)
    (evento, antes), = resultado["cambiadas"]
    assert antes == momento(22, 23, 59) and evento.inicio == momento(24, 23, 59)
    assert "antes dom 22 nov, 11:59 p. m., ahora mar 24 nov, 11:59 p. m." in redactar_aviso_moodle(ctx, resultado)


def test_recordatorios_a_72_24_y_3_horas_sin_repetir(ctx, moodle):
    revisar_moodle(ctx)

    def recordados():
        return [e.id for e in revisar_moodle(ctx)["recordatorios"]]

    ctx.reloj.actual = momento(19, 8)          # faltan ~88 h para el día 22
    assert recordados() == []
    ctx.reloj.actual = momento(20, 9)          # faltan ~63 h
    assert recordados() == [1]
    assert recordados() == []                  # no se repite
    ctx.reloj.actual = momento(22, 0, 30)      # faltan ~23 h
    assert recordados() == [1]
    ctx.reloj.actual = momento(22, 21, 30)     # faltan ~2,5 h; el quiz del 25 entra en sus 72 h
    resultado = revisar_moodle(ctx)
    assert [e.id for e in resultado["recordatorios"]] == [1, 3]
    assert "vence en 2 h (dom 22 nov, 11:59 p. m.)" in redactar_aviso_moodle(ctx, resultado)
    assert recordados() == []


def test_si_se_salta_umbrales_avisa_una_sola_vez(ctx, moodle):
    revisar_moodle(ctx)
    ctx.reloj.actual = momento(22, 21)         # ya pasó el de 72 h y el de 24 h sin revisar
    assert [e.id for e in revisar_moodle(ctx)["recordatorios"]] == [1, 3]   # el 3 por sus 72 h
    assert revisar_moodle(ctx)["recordatorios"] == []


def test_la_primera_revision_recuerda_lo_que_ya_esta_cerca(ctx, moodle):
    ctx.reloj.actual = momento(22, 10)
    resultado = revisar_moodle(ctx)
    # El día 22 (a ~14 h) y el quiz del 25 (a 72 h justas).
    assert resultado["primera_vez"] and [e.id for e in resultado["recordatorios"]] == [1, 3]


def test_una_tarea_nueva_cercana_no_se_avisa_dos_veces(ctx, moodle):
    revisar_moodle(ctx)
    moodle.eventos.append(_evento(5, "Quiz sorpresa", 20, momento(17, 10), modulo="quiz"))
    resultado = revisar_moodle(ctx)
    assert [e.id for e in resultado["nuevas"]] == [5]
    assert resultado["recordatorios"] == []


def test_una_tarea_eliminada_deja_de_recordarse(ctx, moodle):
    revisar_moodle(ctx)
    moodle.eventos[:] = [e for e in moodle.eventos if e["id"] != 1]
    revisar_moodle(ctx)
    assert 1 not in ctx.repo.moodle_eventos()
    ctx.reloj.actual = momento(22, 21)
    assert [e.id for e in revisar_moodle(ctx)["recordatorios"]] == [3]   # solo el quiz que sigue vigente


def test_ignora_eventos_que_no_son_vencimientos(ctx, moodle):
    apertura = _evento(6, "Se abre el quiz", 20, momento(18, 8), modulo="quiz")
    apertura["eventtype"] = "open"
    moodle.eventos.append(apertura)
    assert revisar_moodle(ctx)["conocidas"] == 3


def test_error_de_moodle_reinicia_la_sesion(ctx):
    class ClienteCaido:
        def proximos_eventos(self, **_):
            raise ErrorMoodle("sin conexión")

    ctx.moodle = ClienteCaido()
    with pytest.raises(ErrorSkill, match="sin conexión"):
        revisar_moodle(ctx)
    assert ctx.moodle is None


@pytest.mark.parametrize(
    ("hora", "activo"),
    [(time(5, 59), False), (time(6, 0), True), (time(13, 0), True), (time(21, 59), True), (time(22, 0), False)],
)
def test_horario_de_vigilancia(ctx, hora, activo):
    assert dentro_del_horario(ctx, momento(20, hora.hour, hora.minute)) is activo


# --- Configuración -----------------------------------------------------------


def test_configuracion_de_la_vigilancia(tmp_path):
    ruta = tmp_path / "config.yaml"
    ruta.write_text(
        "moodle:\n  intervalo_minutos: 10\n  horario_inicio: '07:30'\n  horario_fin: '21:00'\n"
        "  avisos_horas: [3, 48]\n",
        encoding="utf-8",
    )
    ajustes = cargar_ajustes(ruta)
    assert ajustes.moodle_intervalo_minutos == 10
    assert (ajustes.moodle_horario_inicio, ajustes.moodle_horario_fin) == (time(7, 30), time(21, 0))
    assert ajustes.moodle_avisos_horas == (48, 3)


@pytest.mark.parametrize(
    "contenido",
    [
        "moodle:\n  horario_inicio: '22:00'\n  horario_fin: '06:00'\n",
        "moodle:\n  intervalo_minutos: 1\n",
        "moodle:\n  avisos_horas: [0]\n",
    ],
)
def test_configuracion_de_vigilancia_invalida(tmp_path, contenido):
    ruta = tmp_path / "config.yaml"
    ruta.write_text(contenido, encoding="utf-8")
    with pytest.raises(ValueError):
        cargar_ajustes(ruta)


# --- Bot -----------------------------------------------------------------------


@pytest.fixture
def sesion_moodle(gestor):
    """Estudiante 42 con Moodle conectado contra un servidor falso."""
    sesion = registrar(gestor, 42)
    gestor.cuentas.guardar_token_moodle(42, "usuario", "tok123")
    servidor = MoodleFalso(eventos=list(EVENTOS))
    sesion.ctx.moodle = cliente(servidor)
    sesion.servidor = servidor
    return sesion


@pytest.fixture
def bot(gestor, sesion_moodle):
    return bot_de_prueba(gestor)


def _textos(bot, telegram_id=42):
    return [texto for destino, texto, _ in bot.enviados if destino == telegram_id]


def test_el_bot_programa_la_vigilancia_si_moodle_esta_configurado(bot):
    trabajos = {t.name for t in bot.construir().job_queue.jobs()}
    assert "moodle" in trabajos


def test_sin_moodle_url_no_se_programa_la_vigilancia(bot, gestor):
    gestor.moodle_url = ""
    assert "moodle" not in {t.name for t in bot.construir().job_queue.jobs()}


def test_el_bot_no_revisa_fuera_del_horario(bot, sesion_moodle):
    sesion_moodle.ctx.reloj.actual = momento(20, 23)
    asyncio.run(bot.vigilar_moodle_todos())
    assert [n for n, _ in sesion_moodle.servidor.llamadas] == ["login"]   # solo el inicio de sesión del fixture


def test_el_bot_avisa_dentro_del_horario(bot, sesion_moodle):
    asyncio.run(bot.vigilar_moodle_todos())
    assert "Empecé a vigilar Moodle" in _textos(bot)[-1]
    sesion_moodle.servidor.eventos.append(_evento(4, "Vencimiento de Manual de Usuario", 10, momento(27, 23, 59)))
    asyncio.run(bot.vigilar_moodle_todos())
    assert "Manual de Usuario" in _textos(bot)[-1]


def test_el_comando_manual_ignora_el_horario(bot, sesion_moodle):
    sesion_moodle.ctx.reloj.actual = momento(20, 23)
    assert asyncio.run(bot.vigilar_moodle(42, forzar=True)) is True
    assert "Empecé a vigilar Moodle" in _textos(bot)[-1]


def test_en_pausa_no_se_vigila_moodle(bot, gestor, sesion_moodle):
    gestor.cuentas.actualizar(42, pausado_hasta="indefinido")
    asyncio.run(bot.vigilar_moodle_todos())
    assert bot.enviados == []


def test_token_vencido_pide_reconectar_una_sola_vez(bot, gestor, sesion_moodle):
    sesion_moodle.ctx.moodle = ClienteMoodle.con_token(
        URL, "vencido", sesion_moodle.ctx.ajustes.zona, httpx.MockTransport(sesion_moodle.servidor)
    )
    asyncio.run(bot.vigilar_moodle_todos())
    assert "/conectar_moodle" in _textos(bot)[-1]
    assert gestor.cuentas.obtener(42)["moodle_estado"] == "invalido"
    assert gestor.cuentas.token_moodle(42) is None

    bot.enviados.clear()
    asyncio.run(bot.vigilar_moodle_todos())   # ya no se intenta hasta que reconecte
    assert bot.enviados == []


def test_cada_estudiante_ve_solo_su_moodle(bot, gestor, sesion_moodle):
    otra = registrar(gestor, 77)
    gestor.cuentas.guardar_token_moodle(77, "otro", "tok123")
    servidor_otra = MoodleFalso(eventos=[_evento(9, "Vencimiento de Ensayo", 10, momento(27, 23, 59))])
    otra.ctx.moodle = cliente(servidor_otra)
    asyncio.run(bot.vigilar_moodle_todos())
    assert "3 entregas" in _textos(bot, 42)[-1]
    assert "1 entregas" in _textos(bot, 77)[-1]
    assert set(sesion_moodle.ctx.repo.moodle_eventos()) == {1, 2, 3}
    assert set(otra.ctx.repo.moodle_eventos()) == {9}

def test_lo_entregado_no_se_recuerda(ctx, moodle):
    moodle.estados_tarea[1001] = "submitted"
    revisar_moodle(ctx)
    ctx.reloj.actual = momento(22, 21)
    assert [e.id for e in revisar_moodle(ctx)["recordatorios"]] == [3]   # solo el quiz


def test_confirma_una_vez_cuando_detecta_la_entrega(ctx, moodle):
    revisar_moodle(ctx)
    moodle.estados_tarea[1001] = "submitted"
    ctx.reloj.actual = momento(21, 9)

    resultado = revisar_moodle(ctx)
    assert [e.id for e in resultado["entregadas"]] == [1]
    assert [e.id for e in resultado["recordatorios"]] == []
    assert "✅ Registré tu entrega:\n• Matriz de Pruebas — GESTION DE PROYECTOS TI" in redactar_aviso_moodle(ctx, resultado)

    ctx.reloj.actual = momento(22, 21)
    siguiente = revisar_moodle(ctx)
    assert siguiente["entregadas"] == [] and 1 not in [e.id for e in siguiente["recordatorios"]]


def test_quiz_presentado_no_se_recuerda(ctx, moodle):
    moodle.quices_terminados.add(1003)
    revisar_moodle(ctx)
    ctx.reloj.actual = momento(24, 12)
    assert 3 not in [e.id for e in revisar_moodle(ctx)["recordatorios"]]


def test_avisa_borrador_sin_enviar(ctx, moodle):
    moodle.estados_tarea[1001] = "draft"
    revisar_moodle(ctx)
    ctx.reloj.actual = momento(22, 21)
    resultado = revisar_moodle(ctx)
    assert 1 in [e.id for e in resultado["recordatorios"]]
    assert "tienes un borrador sin enviar" in redactar_aviso_moodle(ctx, resultado)


def test_si_moodle_no_da_el_estado_se_conserva_el_ultimo_conocido(ctx, moodle):
    moodle.estados_tarea[1001] = "submitted"
    revisar_moodle(ctx)

    class SinEstados(MoodleFalso):
        def __call__(self, request):
            if b"mod_assign_get_submission_status" in request.content:
                return __import__("httpx").Response(200, json={"exception": "x", "message": "caído"})
            return super().__call__(request)

    otro = SinEstados(eventos=list(EVENTOS))
    ctx.moodle = cliente(otro)                 # sesión nueva: sin la caché de entregados
    ctx.reloj.actual = momento(22, 21)
    assert 1 not in [e.id for e in revisar_moodle(ctx)["recordatorios"]]


# --- Apertura ----------------------------------------------------------------------


def test_avisa_cuando_abre_una_entrega(ctx, moodle):
    moodle.aperturas[1002] = int(momento(27, 8).timestamp())
    revisar_moodle(ctx)                        # lunes 16: aún cerrada

    ctx.reloj.actual = momento(27, 7, 50)
    assert revisar_moodle(ctx)["aperturas"] == []
    ctx.reloj.actual = momento(27, 8, 5)
    resultado = revisar_moodle(ctx)
    assert [e.id for e in resultado["aperturas"]] == [2]
    texto = redactar_aviso_moodle(ctx, resultado)
    assert "🔓 Ya se puede entregar:" in texto
    assert "abrió vie 27 nov, 8 a. m., vence dom 29 nov, 11:59 p. m." in texto

    ctx.reloj.actual = momento(27, 9)
    assert revisar_moodle(ctx)["aperturas"] == []


def test_avisa_la_apertura_aunque_el_bot_haya_estado_apagado(ctx, moodle):
    moodle.aperturas[1002] = int(momento(27, 8).timestamp())
    revisar_moodle(ctx)
    ctx.reloj.actual = momento(28, 10)         # no revisó en todo el día 27
    assert [e.id for e in revisar_moodle(ctx)["aperturas"]] == [2]


def test_lo_que_ya_estaba_abierto_no_se_anuncia_como_apertura(ctx, moodle):
    moodle.aperturas[1002] = int(momento(10, 8).timestamp())   # abrió antes de empezar a vigilar
    revisar_moodle(ctx)
    ctx.reloj.actual = momento(17, 8)
    assert revisar_moodle(ctx)["aperturas"] == []


def test_tarea_nueva_que_aun_no_abre(ctx, moodle):
    revisar_moodle(ctx)
    moodle.eventos.append(_evento(4, "Vencimiento de Manual de Usuario", 10, momento(30, 23, 59)))
    moodle.aperturas[1004] = int(momento(27, 8).timestamp())

    resultado = revisar_moodle(ctx)
    assert "aún no abre: abre vie 27 nov, 8 a. m." in redactar_aviso_moodle(ctx, resultado)
    ctx.reloj.actual = momento(27, 9)
    assert [e.id for e in revisar_moodle(ctx)["aperturas"]] == [4]


# --- Contenido nuevo -----------------------------------------------------------------


def test_la_primera_vez_no_anuncia_contenido_viejo(ctx, moodle):
    moodle.notificaciones = [notificacion(10), notificacion(11)]
    assert revisar_moodle(ctx)["contenido"] == []
    assert ctx.repo.preferencia("moodle_ultima_notificacion") == "11"


def test_avisa_contenido_nuevo(ctx, moodle):
    moodle.notificaciones = [notificacion(10)]
    revisar_moodle(ctx)
    moodle.notificaciones.append(notificacion(11, cmid=501))
    moodle.notificaciones.append(notificacion(12, curso="2026B/SISTEMAS OPERATIVOS/SO01", tipo="cambio de contenido", cmid=502))

    resultado = revisar_moodle(ctx)
    assert [n.id for n in resultado["contenido"]] == [11, 12]
    texto = redactar_aviso_moodle(ctx, resultado)
    assert "🆕 Contenido en Moodle:" in texto
    assert "• LENGUAJES DE PROGRAMACION: Actividad 501" in texto
    assert "• SISTEMAS OPERATIVOS: Actividad 502 (actualizado)" in texto
    assert revisar_moodle(ctx)["contenido"] == []


def test_no_repite_como_contenido_una_tarea_ya_anunciada(ctx, moodle):
    moodle.notificaciones = [notificacion(10)]
    revisar_moodle(ctx)
    moodle.eventos.append(_evento(4, "Vencimiento de Manual de Usuario", 10, momento(30, 23, 59)))
    # La URL de la tarea 4 usa cmid = instancia (1004) * 10.
    moodle.notificaciones.append({**notificacion(11, cmid=10040), "contexturl": f"{URL}/mod/assign/view.php?id=10040"})

    resultado = revisar_moodle(ctx)
    assert [e.id for e in resultado["nuevas"]] == [4] and resultado["contenido"] == []


def test_si_fallan_las_notificaciones_igual_avisa_las_entregas(ctx, moodle):
    class SinNotificaciones(MoodleFalso):
        def __call__(self, request):
            if b"message_popup_get_popup_notifications" in request.content:
                return __import__("httpx").Response(200, json={"exception": "x", "message": "caído"})
            return super().__call__(request)

    servidor = SinNotificaciones(eventos=list(EVENTOS))
    ctx.moodle = cliente(servidor)
    revisar_moodle(ctx)
    servidor.eventos.append(_evento(4, "Vencimiento de Manual de Usuario", 10, momento(30, 23, 59)))
    assert [e.id for e in revisar_moodle(ctx)["nuevas"]] == [4]


def test_contenido_nuevo_se_puede_desactivar(ctx, moodle):
    ctx.ajustes.moodle_contenido_nuevo = False
    moodle.notificaciones = [notificacion(10)]
    revisar_moodle(ctx)
    assert not any(f == "message_popup_get_popup_notifications" for f, _ in moodle.llamadas)


# --- Base de datos -------------------------------------------------------------------


def test_migra_una_base_creada_con_la_tabla_anterior(tmp_path):
    ruta = tmp_path / "vieja.db"
    with sqlite3.connect(ruta) as conexion:
        conexion.execute(
            "CREATE TABLE moodle_eventos (id INTEGER PRIMARY KEY, nombre TEXT NOT NULL, curso TEXT, "
            "fecha TEXT NOT NULL, url TEXT, avisos TEXT DEFAULT '', vigente INTEGER DEFAULT 1)"
        )
        conexion.execute("INSERT INTO moodle_eventos (id, nombre, fecha) VALUES (1, 'Taller', '2026-11-22T23:59:00-05:00')")

    repo = Repositorio(ruta)
    evento = repo.moodle_eventos()[1]
    assert evento["entregado"] == 0 and evento["apertura"] is None and evento["apertura_avisada"] == 0
    repo.cerrar()


@pytest.mark.parametrize(
    ("original", "limpio"),
    [
        ("Vencimiento de Cargar Manual", "Cargar Manual"),
        ("SUBIR ARCHIVOS FINALES is due", "SUBIR ARCHIVOS FINALES"),
        ("Cierre de Parcial 2", "Parcial 2"),
        ("Taller libre", "Taller libre"),
    ],
)
def test_nombre_sin_lo_que_agrega_moodle(original, limpio):
    from integraciones.moodle import EventoMoodle
    from skills.vigilar_moodle import _nombre

    evento = EventoMoodle(1, original, "", momento(20, 8), 0, "due", "assign", "")
    assert _nombre(evento) == limpio
