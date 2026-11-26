"""Pruebas del bucle del agente con un LLM simulado y de los adaptadores de proveedor."""

from datetime import timedelta

import pytest

from agente.herramientas import HERRAMIENTAS
from agente.llm import (
    ClienteAnthropic,
    ClienteOpenAICompatible,
    ErrorLLM,
    LlamadaHerramienta,
    RespuestaLLM,
    crear_cliente,
)
from agente.orquestador import Agente
from agente.prompts import aviso_de_respaldo, prompt_de_sistema
from skills.replanificar import replanificar
from tests.conftest import AHORA


class LLMSimulado:
    """Devuelve respuestas preparadas y guarda lo que el agente le envía."""

    nombre, modelo = "simulado", "guion"

    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.recibidos = []

    def completar(self, sistema, mensajes, herramientas):
        self.recibidos.append({"sistema": sistema, "mensajes": list(mensajes), "herramientas": herramientas})
        siguiente = self.respuestas.pop(0)
        return siguiente(mensajes) if callable(siguiente) else siguiente


def test_los_esquemas_de_las_herramientas_estan_completos():
    nombres = {h.nombre for h in HERRAMIENTAS}
    assert {"planificar_entrega", "estimar_tiempo", "organizar_archivos", "replanificar"} <= nombres
    for herramienta in HERRAMIENTAS:
        esquema = herramienta.esquema()
        assert esquema["descripcion"], f"{herramienta.nombre} no tiene docstring"
        assert esquema["parametros"]["type"] == "object"
        assert set(esquema["parametros"]["required"]) <= set(esquema["parametros"]["properties"])


def test_el_agente_encadena_registrar_y_planificar(ctx):
    limite = (AHORA + timedelta(days=6)).date().isoformat()

    def planificar(mensajes):
        # El LLM toma el identificador del resultado de la primera herramienta.
        import json

        entrega_id = json.loads(mensajes[-1]["texto"])["entrega_id"]
        return RespuestaLLM(
            llamadas=[
                LlamadaHerramienta(
                    "c2", "planificar_entrega",
                    {
                        "entrega_id": entrega_id,
                        "subtareas": [
                            {"nombre": "Leer el tema", "tipo_tarea": "lectura", "dificultad": 2, "horas_usuario": 1},
                            {"nombre": "Resolver los puntos", "tipo_tarea": "taller", "dificultad": 3, "horas_usuario": 2},
                        ],
                    },
                )
            ]
        )

    llm = LLMSimulado(
        [
            RespuestaLLM(
                llamadas=[
                    LlamadaHerramienta(
                        "c1", "registrar_entrega",
                        {"materia": "Sistemas Operativos", "nombre": "Taller 3", "fecha_limite": limite, "peso_nota": 15},
                    )
                ]
            ),
            planificar,
            RespuestaLLM(texto="Te reservé los bloques para el Taller 3."),
        ]
    )
    agente = Agente(ctx, llm)

    respuesta = agente.responder("El domingo entrego el taller 3 de operativos, vale 15 %")

    assert respuesta == "Te reservé los bloques para el Taller 3."
    assert len(ctx.repo.entregas_abiertas()) == 1
    assert len(ctx.repo.bloques_pendientes_desde(AHORA)) >= 2
    # El prompt de sistema lleva la fecha y las materias activas.
    assert "2026-11-16" in llm.recibidos[0]["sistema"]
    assert "Sistemas Operativos" in llm.recibidos[0]["sistema"]
    # La memoria guarda la conversación, no los resultados de las herramientas.
    assert [m["rol"] for m in agente.historial] == ["usuario", "asistente"]


def test_los_errores_de_una_herramienta_vuelven_al_llm(ctx):
    llm = LLMSimulado(
        [
            RespuestaLLM(llamadas=[LlamadaHerramienta("c1", "planificar_entrega", {"entrega_id": 999, "subtareas": []})]),
            RespuestaLLM(llamadas=[LlamadaHerramienta("c2", "herramienta_inventada", {})]),
            RespuestaLLM(llamadas=[LlamadaHerramienta("c3", "registrar_entrega", {"materia": "X"})]),
            RespuestaLLM(texto="Necesito que me confirmes la entrega."),
        ]
    )
    agente = Agente(ctx, llm)
    assert agente.responder("planifica") == "Necesito que me confirmes la entrega."
    resultados = [m["texto"] for m in llm.recibidos[-1]["mensajes"] if m["rol"] == "herramienta"]
    assert len(resultados) == 3 and all('"error"' in r for r in resultados)


def test_el_agente_se_detiene_si_el_llm_no_deja_de_llamar_herramientas(ctx):
    bucle = [RespuestaLLM(llamadas=[LlamadaHerramienta(f"c{i}", "listar_pendientes", {})]) for i in range(20)]
    respuesta = Agente(ctx, LLMSimulado(bucle)).responder("hola")
    assert "otra forma" in respuesta


def test_aviso_de_respaldo_cuando_el_llm_falla(ctx):
    class LLMCaido:
        nombre, modelo = "caido", "-"

        def completar(self, *_):
            raise ErrorLLM("sin conexión")

    resultado = replanificar(ctx)
    agente = Agente(ctx, LLMCaido())
    assert agente.redactar_aviso(resultado) == aviso_de_respaldo(resultado)
    assert agente.desempatar_archivo("texto", ["Sistemas Operativos"]) is None


def test_desempate_solo_acepta_materias_de_la_lista(ctx):
    materias = ["Sistemas Operativos", "Inteligencia Artificial"]
    assert Agente(ctx, LLMSimulado([RespuestaLLM(texto="Sistemas Operativos")])).desempatar_archivo("t", materias) == "Sistemas Operativos"
    assert Agente(ctx, LLMSimulado([RespuestaLLM(texto="NINGUNA")])).desempatar_archivo("t", materias) is None


def test_prompt_de_sistema(ctx):
    texto = prompt_de_sistema(ctx)
    assert "E.D.I.T.H." in texto and "lunes 2026-11-16 08:00" in texto


# ------------------------------------------------------------------ proveedores

MENSAJES = [
    {"rol": "usuario", "texto": "hola"},
    {"rol": "asistente", "texto": "", "llamadas": [LlamadaHerramienta("a", "listar_pendientes", {}), LlamadaHerramienta("b", "replanificar", {"horas_extra_dia": 1})]},
    {"rol": "herramienta", "llamada_id": "a", "nombre": "listar_pendientes", "texto": "{}"},
    {"rol": "herramienta", "llamada_id": "b", "nombre": "replanificar", "texto": "{}"},
]


def test_traduccion_al_formato_de_openai_y_groq():
    traducidos = ClienteOpenAICompatible._traducir("sistema", MENSAJES)
    assert [m["role"] for m in traducidos] == ["system", "user", "assistant", "tool", "tool"]
    assert traducidos[2]["tool_calls"][1]["function"]["arguments"] == '{"horas_extra_dia": 1}'
    assert traducidos[3]["tool_call_id"] == "a"


def test_traduccion_al_formato_de_claude():
    traducidos = ClienteAnthropic._traducir(MENSAJES)
    assert [m["role"] for m in traducidos] == ["user", "assistant", "user"]
    assert [b["type"] for b in traducidos[1]["content"]] == ["tool_use", "tool_use"]
    # Los dos resultados van juntos en un solo mensaje del usuario.
    assert [b["tool_use_id"] for b in traducidos[2]["content"]] == ["a", "b"]


def test_seleccion_de_proveedor(monkeypatch):
    for variable in ("LLM_PROVIDER", "GROQ_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_MODEL", "GROQ_MODEL",
                     "GEMINI_API_KEY", "GEMINI_MODEL"):
        monkeypatch.delenv(variable, raising=False)
    with pytest.raises(ErrorLLM):
        crear_cliente()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "clave-de-prueba")
    assert crear_cliente().nombre == "anthropic"

    # Con varias llaves y sin LLM_PROVIDER, Groq va primero porque es gratuito.
    monkeypatch.setenv("GROQ_API_KEY", "clave-de-prueba")
    cliente = crear_cliente()
    assert (cliente.nombre, cliente.modelo) == ("groq", "openai/gpt-oss-120b")

    monkeypatch.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(ErrorLLM):          # falta la llave de OpenAI
        crear_cliente()
    monkeypatch.setenv("OPENAI_API_KEY", "clave-de-prueba")
    with pytest.raises(ErrorLLM):          # falta indicar el modelo
        crear_cliente()
    monkeypatch.setenv("OPENAI_MODEL", "modelo-x")
    assert crear_cliente().modelo == "modelo-x"
