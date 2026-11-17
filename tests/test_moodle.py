"""Pruebas de la integración con Moodle y del skill consultar_moodle, con un servidor simulado (sin red)."""

from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest

from agente.herramientas import POR_NOMBRE
from agente.llm import LlamadaHerramienta, RespuestaLLM
from agente.orquestador import Agente
from integraciones.moodle import ClienteMoodle, ErrorMoodle
from skills.agenda import ErrorSkill
from skills.consultar_moodle import consultar_moodle
from tests.conftest import AHORA, ZONA, momento
from tests.test_agente import LLMSimulado

URL = "https://moodle.prueba"

CURSOS = [
    {"id": 10, "fullname": "2026B/GESTION DE PROYECTOS TI/GPT01"},
    {"id": 20, "fullname": "2026B/SISTEMAS OPERATIVOS/SO01"},
]


def _evento(id_, nombre, curso_id, cuando, modulo="assign", instancia=None, descripcion=""):
    return {
        "id": id_,
        "name": nombre,
        "courseid": curso_id,
        "timestart": int(cuando.timestamp()),
        "timeduration": 0,
        "eventtype": "due",
        "modulename": modulo,
        "instance": instancia or id_ + 1000,
        "description": descripcion,
    }


# Desordenados a propósito: el cliente debe devolverlos por fecha.
EVENTOS = [
    _evento(2, "Vencimiento de Plan de Pruebas", 10, momento(29, 23, 59)),
    _evento(1, "Vencimiento de Matriz de Pruebas", 10, momento(22, 23, 59), descripcion="<p>Usar el&nbsp;formato</p>"),
    _evento(3, "Quiz semáforos", 20, momento(25, 10), modulo="quiz"),
]


class MoodleFalso:
    """Responde como Moodle y guarda lo que recibe."""

    def __init__(self, token_valido=True, eventos=None):
        self.token_valido = token_valido
        self.eventos = EVENTOS if eventos is None else eventos
        self.llamadas: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        datos = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if request.url.path == "/login/token.php":
            self.llamadas.append(("login", datos))
            if not self.token_valido:
                return httpx.Response(200, json={"error": "Datos de acceso inválidos", "errorcode": "invalidlogin"})
            return httpx.Response(200, json={"token": "tok123"})

        funcion = datos["wsfunction"]
        self.llamadas.append((funcion, datos))
        assert datos["wstoken"] == "tok123"
        if funcion == "core_course_get_enrolled_courses_by_timeline_classification":
            return httpx.Response(200, json={"courses": CURSOS})
        if funcion == "core_calendar_get_calendar_events":
            desde, hasta = int(datos["options[timestart]"]), int(datos["options[timeend]"])
            eventos = [e for e in self.eventos if desde <= e["timestart"] <= hasta]
            return httpx.Response(200, json={"events": eventos, "warnings": []})
        if funcion == "core_course_get_course_module_by_instance":
            return httpx.Response(200, json={"cm": {"id": int(datos["instance"]) * 10}})
        if funcion == "core_webservice_get_site_info":
            return httpx.Response(200, json={"fullname": "ESTUDIANTE DE PRUEBA"})
        return httpx.Response(200, json={"exception": "x", "message": f"Función no permitida: {funcion}"})


def cliente(servidor: MoodleFalso | None = None) -> ClienteMoodle:
    return ClienteMoodle(URL, "usuario", "clave", ZONA, transporte=httpx.MockTransport(servidor or MoodleFalso()))


# --- Cliente ---------------------------------------------------------------


def test_inicia_sesion_con_el_servicio_de_la_app_movil():
    servidor = MoodleFalso()
    cliente(servidor)
    nombre, datos = servidor.llamadas[0]
    assert nombre == "login"
    assert datos == {"username": "usuario", "password": "clave", "service": "moodle_mobile_app"}


def test_credenciales_invalidas():
    with pytest.raises(ErrorMoodle, match="rechazó el inicio de sesión"):
        cliente(MoodleFalso(token_valido=False))


def test_faltan_variables_de_entorno(monkeypatch):
    for variable in ("MOODLE_URL", "MOODLE_USUARIO", "MOODLE_CLAVE"):
        monkeypatch.delenv(variable, raising=False)
    with pytest.raises(ErrorMoodle, match="MOODLE_URL, MOODLE_USUARIO, MOODLE_CLAVE"):
        ClienteMoodle.desde_entorno()


def test_proximos_eventos_ordenados_y_completos():
    servidor = MoodleFalso()
    eventos = cliente(servidor).proximos_eventos(dias=30, desde=AHORA)

    assert [e.nombre for e in eventos] == [
        "Vencimiento de Matriz de Pruebas",
        "Quiz semáforos",
        "Vencimiento de Plan de Pruebas",
    ]
    primero = eventos[0]
    assert primero.curso == "2026B/GESTION DE PROYECTOS TI/GPT01"
    assert primero.inicio == momento(22, 23, 59)
    assert primero.inicio.tzinfo == ZONA
    assert primero.modulo == "assign"
    assert primero.descripcion == "Usar el formato"
    # La URL usa el id del módulo del curso, no el de la instancia.
    assert primero.url == f"{URL}/mod/assign/view.php?id=10010"
    assert eventos[1].url == f"{URL}/mod/quiz/view.php?id=10030"


def test_pide_los_eventos_de_todos_los_cursos_matriculados():
    servidor = MoodleFalso()
    cliente(servidor).proximos_eventos(dias=30, desde=AHORA)
    datos = dict(servidor.llamadas)["core_calendar_get_calendar_events"]
    assert {datos["events[courseids][0]"], datos["events[courseids][1]"]} == {"10", "20"}
    assert int(datos["options[timestart]"]) == int(AHORA.timestamp())
    assert int(datos["options[timeend]"]) == int((AHORA + timedelta(days=30)).timestamp())


def test_respeta_el_rango_de_dias():
    eventos = cliente().proximos_eventos(dias=7, desde=AHORA)
    assert [e.nombre for e in eventos] == ["Vencimiento de Matriz de Pruebas"]


def test_error_de_la_api_se_convierte_en_error_moodle():
    with pytest.raises(ErrorMoodle, match="Función no permitida"):
        cliente().llamar("core_funcion_inexistente")


def test_sin_red():
    def caido(_request):
        raise httpx.ConnectError("sin conexión")

    with pytest.raises(ErrorMoodle, match="No se pudo conectar"):
        ClienteMoodle(URL, "u", "c", ZONA, transporte=httpx.MockTransport(caido))


# --- Skill -----------------------------------------------------------------


def test_skill_devuelve_eventos_para_el_agente(ctx):
    ctx.moodle = cliente()
    resultado = consultar_moodle(ctx, dias=10)

    assert resultado["total"] == 2
    primero = resultado["eventos"][0]
    assert primero["nombre"] == "Vencimiento de Matriz de Pruebas"
    assert primero["cuando"] == "dom 22 nov, 11:59 p. m."
    assert primero["tipo"] == "assign"
    assert primero["enlace"].startswith(URL)
    json.dumps(resultado)  # el resultado debe poder enviarse al LLM


def test_skill_filtra_por_curso(ctx):
    ctx.moodle = cliente()
    resultado = consultar_moodle(ctx, dias=30, curso="sistemas")
    assert [e["nombre"] for e in resultado["eventos"]] == ["Quiz semáforos"]


@pytest.mark.parametrize("dias", [0, 91])
def test_skill_rechaza_rangos_invalidos(ctx, dias):
    ctx.moodle = cliente()
    with pytest.raises(ErrorSkill, match="entre 1 y 90"):
        consultar_moodle(ctx, dias=dias)


def test_skill_sin_configuracion(ctx, monkeypatch):
    monkeypatch.delenv("MOODLE_URL", raising=False)
    with pytest.raises(ErrorSkill, match="No pude entrar a Moodle"):
        consultar_moodle(ctx)


def test_skill_reinicia_la_sesion_si_moodle_falla(ctx):
    class ClienteCaido:
        def proximos_eventos(self, *_args, **_kwargs):
            raise ErrorMoodle("token vencido")

    ctx.moodle = ClienteCaido()
    with pytest.raises(ErrorSkill, match="token vencido"):
        consultar_moodle(ctx)
    assert ctx.moodle is None


# --- Agente ----------------------------------------------------------------


def test_la_herramienta_esta_registrada():
    esquema = POR_NOMBRE["consultar_moodle"].esquema()
    assert "Moodle" in esquema["descripcion"]
    assert set(esquema["parametros"]["properties"]) == {"dias", "curso"}


def test_el_agente_responde_con_los_eventos_de_moodle(ctx):
    ctx.moodle = cliente()

    def responder(mensajes):
        resultado = json.loads(mensajes[-1]["texto"])
        nombres = ", ".join(e["nombre"] for e in resultado["eventos"])
        return RespuestaLLM(texto=f"En Moodle tienes: {nombres}.")

    llm = LLMSimulado(
        [
            RespuestaLLM(llamadas=[LlamadaHerramienta("m1", "consultar_moodle", {"dias": 10})]),
            responder,
        ]
    )
    respuesta = Agente(ctx, llm).responder("¿Qué tengo pendiente en Moodle?")

    assert respuesta == "En Moodle tienes: Vencimiento de Matriz de Pruebas, Quiz semáforos."
    assert any(h["nombre"] == "consultar_moodle" for h in llm.recibidos[0]["herramientas"])
    assert "consultar_moodle" in llm.recibidos[0]["sistema"]


def test_el_agente_recibe_el_error_si_moodle_no_esta_configurado(ctx, monkeypatch):
    monkeypatch.delenv("MOODLE_URL", raising=False)
    llm = LLMSimulado(
        [
            RespuestaLLM(llamadas=[LlamadaHerramienta("m1", "consultar_moodle", {})]),
            lambda mensajes: RespuestaLLM(texto=json.loads(mensajes[-1]["texto"])["error"]),
        ]
    )
    respuesta = Agente(ctx, llm).responder("¿Qué hay en Moodle?")
    assert "No pude entrar a Moodle" in respuesta


def test_skill_incluye_el_ultimo_dia_completo(ctx):
    # Hoy es 16 nov a las 8:00; "dias=13" llega al 29 nov, y la entrega de ese día vence a las 23:59.
    ctx.moodle = cliente()
    resultado = consultar_moodle(ctx, dias=13)
    assert "Vencimiento de Plan de Pruebas" in [e["nombre"] for e in resultado["eventos"]]


def test_skill_cuenta_los_eventos_por_dia(ctx):
    ctx.moodle = cliente()
    resultado = consultar_moodle(ctx, dias=30)
    assert resultado["cantidad_por_dia"] == {"dom 22 nov": 1, "mié 25 nov": 1, "dom 29 nov": 1}
