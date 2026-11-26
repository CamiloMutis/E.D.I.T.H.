"""Pruebas del respaldo entre proveedores de LLM y de las firmas de Gemini (sin red)."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import openai
import pytest

from agente import llm
from agente.llm import (
    FIRMA_COMODIN,
    ClienteConRespaldo,
    ClienteOpenAICompatible,
    ErrorLLM,
    LlamadaHerramienta,
    RespuestaLLM,
    crear_cliente,
)

MENSAJES = [{"rol": "usuario", "texto": "hola"}]


class RelojFalso:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class ClienteFalso:
    """Cliente que responde o falla según una lista de resultados."""

    def __init__(self, nombre: str, resultados: list):
        self.nombre = nombre
        self.modelo = f"modelo-{nombre}"
        self.resultados = list(resultados)
        self.llamadas = 0

    def completar(self, sistema, mensajes, herramientas):
        self.llamadas += 1
        resultado = self.resultados.pop(0) if self.resultados else RespuestaLLM(texto=self.nombre)
        if isinstance(resultado, Exception):
            raise resultado
        return resultado


def _limite():
    return ErrorLLM("429", llm.LIMITE)


# ------------------------------------------------------------------ respaldo

def test_pasa_al_respaldo_ante_limite_de_uso():
    principal = ClienteFalso("groq", [_limite()])
    respaldo = ClienteFalso("gemini", [])
    cliente = ClienteConRespaldo([principal, respaldo], reloj=RelojFalso())
    assert cliente.completar("s", MENSAJES, []).texto == "gemini"
    assert cliente.nombre == "groq→gemini"
    assert cliente.modelo == "modelo-groq→modelo-gemini"


@pytest.mark.parametrize("motivo", [llm.SERVIDOR, llm.CONEXION, llm.RECHAZO])
def test_pasa_al_respaldo_ante_otros_fallos_sin_pausa(motivo):
    principal = ClienteFalso("groq", [ErrorLLM("x", motivo)])
    respaldo = ClienteFalso("gemini", [])
    cliente = ClienteConRespaldo([principal, respaldo], reloj=RelojFalso())
    assert cliente.completar("s", MENSAJES, []).texto == "gemini"
    # Sin 429 no hay pausa: el siguiente mensaje vuelve a probar el principal.
    assert cliente.completar("s", MENSAJES, []).texto == "groq"


def test_pausa_tras_limite_y_regreso_al_principal():
    reloj = RelojFalso()
    principal = ClienteFalso("groq", [_limite()])
    respaldo = ClienteFalso("gemini", [])
    cliente = ClienteConRespaldo([principal, respaldo], pausa=300, reloj=reloj)

    cliente.completar("s", MENSAJES, [])
    assert principal.llamadas == 1
    # Durante la pausa ni siquiera se intenta el principal.
    reloj.t += 299
    assert cliente.completar("s", MENSAJES, []).texto == "gemini"
    assert principal.llamadas == 1
    # Pasada la pausa, el principal vuelve a responder.
    reloj.t += 2
    assert cliente.completar("s", MENSAJES, []).texto == "groq"
    assert principal.llamadas == 2


def test_principal_en_pausa_es_ultimo_recurso():
    reloj = RelojFalso()
    principal = ClienteFalso("groq", [_limite(), RespuestaLLM(texto="groq de nuevo")])
    respaldo = ClienteFalso("gemini", [RespuestaLLM(texto="ok"), ErrorLLM("caído", llm.SERVIDOR)])
    cliente = ClienteConRespaldo([principal, respaldo], reloj=reloj)
    cliente.completar("s", MENSAJES, [])
    assert cliente.completar("s", MENSAJES, []).texto == "groq de nuevo"


def test_si_todos_fallan_lanza_error_llm():
    principal = ClienteFalso("groq", [_limite()])
    respaldo = ClienteFalso("gemini", [ErrorLLM("sin cuota", llm.LIMITE)])
    cliente = ClienteConRespaldo([principal, respaldo], reloj=RelojFalso())
    with pytest.raises(ErrorLLM) as error:
        cliente.completar("s", MENSAJES, [])
    assert "groq" in str(error.value) and "gemini" in str(error.value)


# ------------------------------------------------------------------ motivos de error

def _respuesta(codigo: int) -> httpx.Response:
    return httpx.Response(codigo, request=httpx.Request("POST", "https://api.prueba"))


def test_clasificacion_de_errores_de_openai():
    peticion = httpx.Request("POST", "https://api.prueba")
    assert llm._motivo_openai(openai.RateLimitError("x", response=_respuesta(429), body=None)) == llm.LIMITE
    assert llm._motivo_openai(openai.InternalServerError("x", response=_respuesta(503), body=None)) == llm.SERVIDOR
    assert llm._motivo_openai(openai.APITimeoutError(request=peticion)) == llm.CONEXION
    assert llm._motivo_openai(openai.APIConnectionError(request=peticion)) == llm.CONEXION
    assert llm._motivo_openai(openai.BadRequestError("x", response=_respuesta(400), body=None)) == llm.RECHAZO
    assert llm._motivo_openai(openai.AuthenticationError("x", response=_respuesta(401), body=None)) == llm.OTRO


def _cliente_con_sdk_falso(nombre: str, errores: list[Exception], respuesta=None) -> ClienteOpenAICompatible:
    cliente = ClienteOpenAICompatible(nombre, "clave-de-prueba", "modelo", max_retries=0)

    def crear(**parametros):
        cliente.enviado = parametros
        if errores:
            raise errores.pop(0)
        return respuesta

    cliente._cliente = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=crear)))
    return cliente


def test_el_cliente_marca_el_motivo_del_error():
    cliente = _cliente_con_sdk_falso("groq", [openai.RateLimitError("x", response=_respuesta(429), body=None)])
    with pytest.raises(ErrorLLM) as error:
        cliente.completar("s", MENSAJES, [])
    assert error.value.motivo == llm.LIMITE

    # Un 400 se reintenta una vez y luego se marca como rechazo.
    rechazo = openai.BadRequestError("x", response=_respuesta(400), body=None)
    cliente = _cliente_con_sdk_falso("groq", [rechazo, rechazo])
    with pytest.raises(ErrorLLM) as error:
        cliente.completar("s", MENSAJES, [])
    assert error.value.motivo == llm.RECHAZO


# ------------------------------------------------------------------ firmas de Gemini

FIRMA = {"google": {"thought_signature": "firma-real"}}


def _historial(llamadas):
    return [
        {"rol": "usuario", "texto": "hola"},
        {"rol": "asistente", "texto": "", "llamadas": llamadas},
        *[{"rol": "herramienta", "llamada_id": ll.id, "nombre": ll.nombre, "texto": "{}"} for ll in llamadas],
    ]


def test_la_construccion_posicional_sigue_funcionando():
    llamada = LlamadaHerramienta("a", "listar_pendientes", {})
    assert llamada.extras == {}


def test_la_firma_de_gemini_se_lee_y_se_devuelve():
    llamada_sdk = openai.types.chat.ChatCompletionMessageToolCall.model_validate(
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "listar_pendientes", "arguments": "{}"},
            "extra_content": FIRMA,
        }
    )
    respuesta = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[llamada_sdk]))]
    )
    gemini = _cliente_con_sdk_falso("gemini", [], respuesta)
    resultado = gemini.completar("s", MENSAJES, [])
    assert resultado.llamadas[0].extras == {"extra_content": FIRMA}

    # En el turno siguiente la firma vuelve tal cual.
    gemini.completar("s", _historial(resultado.llamadas), [])
    enviada = gemini.enviado["messages"][2]["tool_calls"][0]
    assert enviada["extra_content"] == FIRMA


def test_llamadas_de_otro_proveedor_llevan_la_firma_comodin_en_gemini():
    llamadas = [LlamadaHerramienta("a", "listar_pendientes", {}), LlamadaHerramienta("b", "replanificar", {})]
    traducidos = ClienteOpenAICompatible._traducir("s", _historial(llamadas), firmas_gemini=True)
    primera, segunda = traducidos[2]["tool_calls"]
    # Como en las llamadas paralelas de Gemini, solo la primera lleva firma.
    assert primera["extra_content"] == {"google": {"thought_signature": FIRMA_COMODIN}}
    assert "extra_content" not in segunda


def test_groq_y_openai_no_reciben_campos_de_gemini():
    llamadas = [LlamadaHerramienta("a", "listar_pendientes", {}, {"extra_content": FIRMA})]
    traducidos = ClienteOpenAICompatible._traducir("s", _historial(llamadas))
    assert "extra_content" not in traducidos[2]["tool_calls"][0]
    assert not ClienteOpenAICompatible("groq", "clave", "m").firmas_gemini
    assert ClienteOpenAICompatible("gemini", "clave", "m").firmas_gemini


# ------------------------------------------------------------------ crear_cliente

VARIABLES = (
    "LLM_PROVIDER", "GROQ_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "OPENAI_MODEL", "GROQ_MODEL", "GEMINI_API_KEY", "GEMINI_MODEL",
)


@pytest.fixture
def entorno_limpio(monkeypatch):
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    return monkeypatch


def test_crear_cliente_agrega_gemini_como_respaldo(entorno_limpio):
    entorno_limpio.setenv("GROQ_API_KEY", "clave-de-prueba")
    entorno_limpio.setenv("GEMINI_API_KEY", "clave-de-prueba")
    cliente = crear_cliente()
    assert isinstance(cliente, ClienteConRespaldo)
    assert cliente.nombre == "groq→gemini"
    principal, respaldo = cliente.clientes
    assert respaldo.modelo == llm.MODELO_GEMINI
    # Con respaldo, el principal falla rápido en lugar de reintentar varias veces.
    assert principal._cliente.max_retries <= 1
    assert str(respaldo._cliente.base_url).startswith(llm.GEMINI_URL.rstrip("/"))

    entorno_limpio.setenv("LLM_PROVIDER", "anthropic")
    entorno_limpio.setenv("ANTHROPIC_API_KEY", "clave-de-prueba")
    assert crear_cliente().nombre == "anthropic→gemini"


def test_crear_cliente_sin_llave_de_gemini_no_cambia(entorno_limpio):
    entorno_limpio.setenv("GROQ_API_KEY", "clave-de-prueba")
    cliente = crear_cliente()
    assert isinstance(cliente, ClienteOpenAICompatible)
    assert cliente.nombre == "groq"
    assert cliente._cliente.max_retries == 4


def test_gemini_como_proveedor_principal(entorno_limpio):
    entorno_limpio.setenv("GEMINI_API_KEY", "clave-de-prueba")
    entorno_limpio.setenv("GEMINI_MODEL", "gemini-otro")
    # Sin otras llaves se elige Gemini automáticamente.
    cliente = crear_cliente()
    assert (cliente.nombre, cliente.modelo) == ("gemini", "gemini-otro")

    entorno_limpio.setenv("GROQ_API_KEY", "clave-de-prueba")
    entorno_limpio.setenv("LLM_PROVIDER", "gemini")
    assert crear_cliente().nombre == "gemini"

    entorno_limpio.delenv("GEMINI_API_KEY")
    with pytest.raises(ErrorLLM):
        crear_cliente()
