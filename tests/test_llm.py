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
    "LLM_PROVIDER", "LLM_PROVIDERS", "GROQ_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "OPENAI_MODEL", "GROQ_MODEL", "ANTHROPIC_MODEL", "GEMINI_API_KEY", "GEMINI_MODEL",
    "GROQ_BASE_URL", "OPENAI_BASE_URL", "OPENROUTER_API_KEY", "OPENROUTER_MODEL",
    "OPENROUTER_BASE_URL", "OLLAMA_BASE_URL", "OLLAMA_MODEL", "OLLAMA_API_KEY",
)


@pytest.fixture
def entorno_limpio(monkeypatch):
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    return monkeypatch


def test_las_llaves_presentes_forman_el_banco(entorno_limpio):
    entorno_limpio.setenv("GROQ_API_KEY", "clave-de-prueba")
    entorno_limpio.setenv("GEMINI_API_KEY", "clave-de-prueba")
    cliente = crear_cliente()
    assert isinstance(cliente, ClienteConRespaldo)
    assert cliente.nombre == "groq→gemini"
    principal, respaldo = cliente.clientes
    assert respaldo.modelo == llm.MODELO_GEMINI
    # En un banco, cada proveedor falla rápido para pasar al siguiente; el último insiste.
    assert principal._cliente.max_retries <= 1
    assert respaldo._cliente.max_retries == 4
    assert str(respaldo._cliente.base_url).startswith(llm.GEMINI_URL.rstrip("/"))

    entorno_limpio.setenv("LLM_PROVIDER", "anthropic")
    entorno_limpio.setenv("ANTHROPIC_API_KEY", "clave-de-prueba")
    assert crear_cliente().nombre == "anthropic→groq→gemini"


def test_gemini_ya_no_es_obligatorio(entorno_limpio):
    entorno_limpio.setenv("GROQ_API_KEY", "clave-de-prueba")
    entorno_limpio.setenv("OPENAI_API_KEY", "clave-de-prueba")
    entorno_limpio.setenv("OPENAI_MODEL", "gpt-x")
    entorno_limpio.setenv("ANTHROPIC_API_KEY", "clave-de-prueba")
    cliente = crear_cliente()
    assert cliente.nombre == "groq→openai→anthropic"
    assert "gemini" not in cliente.nombre


def test_el_usuario_elige_el_orden_del_banco(entorno_limpio):
    entorno_limpio.setenv("LLM_PROVIDERS", "groq, openai , anthropic")
    entorno_limpio.setenv("GROQ_API_KEY", "clave-de-prueba")
    entorno_limpio.setenv("OPENAI_API_KEY", "clave-de-prueba")
    entorno_limpio.setenv("OPENAI_MODEL", "gpt-x")
    entorno_limpio.setenv("ANTHROPIC_API_KEY", "clave-de-prueba")
    entorno_limpio.setenv("GEMINI_API_KEY", "clave-de-prueba")
    cliente = crear_cliente()
    # Gemini tiene llave pero no está en la lista: no se usa.
    assert cliente.nombre == "groq→openai→anthropic"
    assert [c.modelo for c in cliente.clientes] == [llm.MODELO_GROQ, "gpt-x", llm.MODELO_ANTHROPIC]

    entorno_limpio.setenv("LLM_PROVIDERS", "anthropic,groq")
    assert crear_cliente().nombre == "anthropic→groq"


def test_varias_llaves_de_un_mismo_proveedor(entorno_limpio):
    entorno_limpio.setenv("GROQ_API_KEY", "llave-1, llave-2")
    cliente = crear_cliente()
    assert cliente.nombre == "groq#1→groq#2"
    assert [c._cliente.api_key for c in cliente.clientes] == ["llave-1", "llave-2"]


def test_api_compatible_con_openai_entra_al_banco(entorno_limpio):
    entorno_limpio.setenv("LLM_PROVIDERS", "groq,openrouter,ollama")
    entorno_limpio.setenv("GROQ_API_KEY", "clave-de-prueba")
    entorno_limpio.setenv("OPENROUTER_API_KEY", "clave-de-prueba")
    entorno_limpio.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    entorno_limpio.setenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct")
    # Un servidor local puede no pedir llave.
    entorno_limpio.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    entorno_limpio.setenv("OLLAMA_MODEL", "llama3.2")
    cliente = crear_cliente()
    assert cliente.nombre == "groq→openrouter→ollama"
    _, router, local = cliente.clientes
    assert str(router._cliente.base_url).startswith("https://openrouter.ai/api/v1")
    assert router.modelo == "meta-llama/llama-3.3-70b-instruct"
    assert local._cliente.api_key == "sin-llave"


def test_errores_de_configuracion_del_banco(entorno_limpio):
    entorno_limpio.setenv("GROQ_API_KEY", "clave-de-prueba")
    # Proveedor desconocido sin URL.
    entorno_limpio.setenv("LLM_PROVIDERS", "groq,inventado")
    with pytest.raises(ErrorLLM, match="INVENTADO_BASE_URL"):
        crear_cliente()
    # En la lista escrita a mano, un proveedor sin llave es un error.
    entorno_limpio.setenv("LLM_PROVIDERS", "groq,anthropic")
    with pytest.raises(ErrorLLM, match="ANTHROPIC_API_KEY"):
        crear_cliente()

    # Detectado solo por su llave, un respaldo incompleto se omite y el bot arranca.
    entorno_limpio.delenv("LLM_PROVIDERS")
    entorno_limpio.setenv("OPENAI_API_KEY", "clave-de-prueba")   # sin OPENAI_MODEL
    cliente = crear_cliente()
    assert cliente.nombre == "groq"


def test_crear_cliente_con_un_solo_proveedor(entorno_limpio):
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
    assert crear_cliente().nombre == "gemini→groq"

    entorno_limpio.delenv("GEMINI_API_KEY")
    with pytest.raises(ErrorLLM):
        crear_cliente()


def test_avisa_cuando_cambia_de_proveedor_y_cuando_vuelve():
    reloj = RelojFalso()
    principal = ClienteFalso("groq", [_limite()])
    respaldo = ClienteFalso("openai", [])
    cliente = ClienteConRespaldo([principal, respaldo], pausa=300, reloj=reloj)
    avisos: list[str] = []
    cliente.al_cambiar = avisos.append

    cliente.completar("s", MENSAJES, [])
    cliente.completar("s", MENSAJES, [])          # sigue en el respaldo: no repite el aviso
    assert len(avisos) == 1 and "groq" in avisos[0] and "openai" in avisos[0]

    reloj.t += 301                                # termina la pausa y Groq responde
    cliente.completar("s", MENSAJES, [])
    assert len(avisos) == 2 and "volvió" in avisos[1]


def test_un_aviso_que_falla_no_tumba_la_respuesta():
    cliente = ClienteConRespaldo([ClienteFalso("groq", [_limite()]), ClienteFalso("openai", [])],
                                 reloj=RelojFalso())

    def roto(_texto):
        raise RuntimeError("Telegram caído")

    cliente.al_cambiar = roto
    assert cliente.completar("s", MENSAJES, []).texto == "openai"
