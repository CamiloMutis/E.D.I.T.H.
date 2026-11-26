"""Adaptador común para los proveedores de LLM.

El agente trabaja con un formato de mensajes propio y neutral. Cada cliente lo
traduce a la API de su proveedor, de modo que cambiar de Groq a Claude, GPT o
Gemini es cambiar una variable en ``.env`` y no una línea del agente.

Formato de los mensajes:
    {"rol": "usuario", "texto": "..."}
    {"rol": "asistente", "texto": "...", "llamadas": [LlamadaHerramienta, ...]}
    {"rol": "herramienta", "llamada_id": "...", "nombre": "...", "texto": "<json>"}

Respaldo automático: si hay ``GEMINI_API_KEY`` y el proveedor principal es
otro, ``crear_cliente()`` devuelve un ``ClienteConRespaldo``. Cuando el
principal falla por límite de uso (429), error del servidor, tiempo de espera
o conexión, la misma solicitud se repite en Gemini y la conversación sigue.
Tras un 429 el principal queda en pausa unos minutos para no esperar por él en
cada mensaje.

Gemini se usa por su API compatible con OpenAI. Sus modelos recientes devuelven
una «firma de pensamiento» en cada llamada a herramienta y exigen recibirla de
vuelta en los turnos siguientes; por eso ``LlamadaHerramienta`` guarda los
datos propios del proveedor en ``extras``. Si el historial trae llamadas hechas
por otro proveedor (sin firma), se envía el valor comodín que Google documenta.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

registro = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
MODELO_GROQ = "openai/gpt-oss-120b"
MODELO_ANTHROPIC = "claude-haiku-5-5"
MODELO_GEMINI = "gemini-flash-lite-latest"
MAX_TOKENS = 2048
# Segundos que un proveedor queda en pausa después de un 429 (5 minutos).
PAUSA_TRAS_LIMITE = 5 * 60
# Firma comodín que Google acepta para llamadas que Gemini no generó.
FIRMA_COMODIN = "skip_thought_signature_validator"

# Motivos de fallo de un proveedor.
LIMITE = "limite"          # 429: límite de uso o cuota agotada
SERVIDOR = "servidor"      # 5xx del proveedor
CONEXION = "conexion"      # tiempo de espera o red
RECHAZO = "rechazo"        # 400: solicitud rechazada
OTRO = "otro"              # llave inválida, modelo inexistente, etc.


class ErrorLLM(Exception):
    """El proveedor no respondió o rechazó la solicitud."""

    def __init__(self, mensaje: str = "", motivo: str = OTRO):
        super().__init__(mensaje)
        self.motivo = motivo


@dataclass
class LlamadaHerramienta:
    id: str
    nombre: str
    argumentos: dict
    # Datos propios del proveedor que hay que devolverle tal cual (por
    # ejemplo, la firma de pensamiento de Gemini en ``extra_content``).
    extras: dict = field(default_factory=dict)


@dataclass
class RespuestaLLM:
    texto: str = ""
    llamadas: list[LlamadaHerramienta] = field(default_factory=list)


class ClienteLLM(Protocol):
    nombre: str
    modelo: str

    def completar(self, sistema: str, mensajes: list[dict], herramientas: list[dict]) -> RespuestaLLM: ...


def _argumentos(crudo) -> dict:
    """Los argumentos llegan como texto JSON; un modelo puede devolverlos mal formados."""
    if isinstance(crudo, dict):
        return crudo
    try:
        valor = json.loads(crudo or "{}")
        return valor if isinstance(valor, dict) else {}
    except json.JSONDecodeError:
        return {}


def _motivo_openai(error: Exception) -> str:
    import openai

    if isinstance(error, openai.RateLimitError):
        return LIMITE
    if isinstance(error, (openai.APITimeoutError, openai.APIConnectionError)):
        return CONEXION
    if isinstance(error, openai.BadRequestError):
        return RECHAZO
    if isinstance(error, openai.APIStatusError) and error.status_code >= 500:
        return SERVIDOR
    return OTRO


def _motivo_anthropic(error: Exception) -> str:
    import anthropic

    if isinstance(error, anthropic.RateLimitError):
        return LIMITE
    if isinstance(error, (anthropic.APITimeoutError, anthropic.APIConnectionError)):
        return CONEXION
    if isinstance(error, anthropic.BadRequestError):
        return RECHAZO
    if isinstance(error, anthropic.APIStatusError) and error.status_code >= 500:
        return SERVIDOR
    return OTRO


class ClienteOpenAICompatible:
    """Cliente para las API con formato de OpenAI: Groq, Gemini y el propio OpenAI."""

    def __init__(
        self,
        nombre: str,
        api_key: str,
        modelo: str,
        base_url: str | None = None,
        max_retries: int = 4,
    ):
        from openai import OpenAI

        self.nombre = nombre
        self.modelo = modelo
        # Gemini exige recibir de vuelta las firmas de pensamiento; a los demás
        # no se les envían campos que no conocen.
        self.firmas_gemini = nombre == "gemini"
        # max_retries hace que el SDK espere y reintente ante un 429 del plan
        # gratuito. Con un respaldo configurado conviene fallar rápido.
        self._cliente = OpenAI(api_key=api_key, base_url=base_url, max_retries=max_retries, timeout=60)

    @staticmethod
    def _traducir(sistema: str, mensajes: list[dict], firmas_gemini: bool = False) -> list[dict]:
        salida = [{"role": "system", "content": sistema}]
        for m in mensajes:
            if m["rol"] == "usuario":
                salida.append({"role": "user", "content": m["texto"]})
            elif m["rol"] == "asistente":
                mensaje = {"role": "assistant", "content": m.get("texto") or None}
                if m.get("llamadas"):
                    mensaje["tool_calls"] = []
                    for posicion, ll in enumerate(m["llamadas"]):
                        llamada = {
                            "id": ll.id,
                            "type": "function",
                            "function": {
                                "name": ll.nombre,
                                "arguments": json.dumps(ll.argumentos, ensure_ascii=False),
                            },
                        }
                        if firmas_gemini:
                            extra = _extra_gemini(ll, primera=posicion == 0)
                            if extra:
                                llamada["extra_content"] = extra
                        mensaje["tool_calls"].append(llamada)
                salida.append(mensaje)
            else:
                salida.append({"role": "tool", "tool_call_id": m["llamada_id"], "content": m["texto"]})
        return salida

    def completar(self, sistema: str, mensajes: list[dict], herramientas: list[dict]) -> RespuestaLLM:
        import openai

        parametros = {
            "model": self.modelo,
            "messages": self._traducir(sistema, mensajes, self.firmas_gemini),
        }
        if herramientas:
            parametros["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": h["nombre"],
                        "description": h["descripcion"],
                        "parameters": h["parametros"],
                    },
                }
                for h in herramientas
            ]
        ultimo_error: Exception | None = None
        # Algunos modelos abiertos generan de vez en cuando una llamada a
        # herramienta inválida y la API responde 400; un segundo intento suele bastar.
        for _ in range(2):
            try:
                respuesta = self._cliente.chat.completions.create(**parametros)
                break
            except openai.BadRequestError as error:
                ultimo_error = error
                registro.warning("Solicitud rechazada por %s, reintentando: %s", self.nombre, error)
            except openai.OpenAIError as error:
                raise ErrorLLM(f"{self.nombre} no respondió: {error}", _motivo_openai(error)) from error
        else:
            raise ErrorLLM(f"{self.nombre} rechazó la solicitud: {ultimo_error}", RECHAZO) from ultimo_error

        mensaje = respuesta.choices[0].message
        llamadas = []
        for ll in mensaje.tool_calls or []:
            extras = {}
            extra_content = (ll.model_extra or {}).get("extra_content")
            if extra_content:
                extras["extra_content"] = extra_content
            llamadas.append(
                LlamadaHerramienta(ll.id, ll.function.name, _argumentos(ll.function.arguments), extras)
            )
        return RespuestaLLM(texto=mensaje.content or "", llamadas=llamadas)


def _extra_gemini(llamada: LlamadaHerramienta, primera: bool) -> dict | None:
    """``extra_content`` que Gemini espera de vuelta para una llamada del historial.

    Si la llamada viene de Gemini se devuelve su firma tal cual. Si la generó
    otro proveedor (por ejemplo, Groq antes de pasar al respaldo), la primera
    llamada de cada turno lleva la firma comodín que Google documenta; sin ella
    Gemini responde 400.
    """
    extra = llamada.extras.get("extra_content") if llamada.extras else None
    if extra and (extra.get("google") or {}).get("thought_signature"):
        return extra
    if primera:
        return {"google": {"thought_signature": FIRMA_COMODIN}}
    return extra or None


class ClienteAnthropic:
    """Cliente para la API de Claude."""

    nombre = "anthropic"

    def __init__(self, api_key: str, modelo: str, max_retries: int = 4):
        from anthropic import Anthropic

        self.modelo = modelo
        self._cliente = Anthropic(api_key=api_key, max_retries=max_retries, timeout=60)

    @staticmethod
    def _traducir(mensajes: list[dict]) -> list[dict]:
        salida: list[dict] = []
        for m in mensajes:
            if m["rol"] == "usuario":
                salida.append({"role": "user", "content": m["texto"]})
            elif m["rol"] == "asistente":
                contenido = []
                if m.get("texto"):
                    contenido.append({"type": "text", "text": m["texto"]})
                for ll in m.get("llamadas") or []:
                    contenido.append(
                        {"type": "tool_use", "id": ll.id, "name": ll.nombre, "input": ll.argumentos}
                    )
                salida.append({"role": "assistant", "content": contenido})
            else:
                bloque = {"type": "tool_result", "tool_use_id": m["llamada_id"], "content": m["texto"]}
                # Claude espera todos los resultados de un mismo turno en un solo mensaje.
                if salida and salida[-1]["role"] == "user" and isinstance(salida[-1]["content"], list):
                    salida[-1]["content"].append(bloque)
                else:
                    salida.append({"role": "user", "content": [bloque]})
        return salida

    def completar(self, sistema: str, mensajes: list[dict], herramientas: list[dict]) -> RespuestaLLM:
        import anthropic

        parametros = {
            "model": self.modelo,
            "max_tokens": MAX_TOKENS,
            "system": sistema,
            "messages": self._traducir(mensajes),
        }
        if herramientas:
            parametros["tools"] = [
                {"name": h["nombre"], "description": h["descripcion"], "input_schema": h["parametros"]}
                for h in herramientas
            ]
        try:
            respuesta = self._cliente.messages.create(**parametros)
        except anthropic.AnthropicError as error:
            raise ErrorLLM(f"Claude no respondió: {error}", _motivo_anthropic(error)) from error

        texto, llamadas = [], []
        for bloque in respuesta.content:
            if bloque.type == "text":
                texto.append(bloque.text)
            elif bloque.type == "tool_use":
                llamadas.append(LlamadaHerramienta(bloque.id, bloque.name, dict(bloque.input)))
        return RespuestaLLM(texto="".join(texto), llamadas=llamadas)


class ClienteConRespaldo:
    """Prueba el proveedor principal y, si falla, los de respaldo en orden.

    Tras un 429 el proveedor queda en pausa ``pausa`` segundos: mientras dure,
    las solicitudes van directo al siguiente para no esperar por él en cada
    mensaje. Si todos fallan se lanza ``ErrorLLM``.
    """

    def __init__(
        self,
        clientes: list[ClienteLLM],
        pausa: float = PAUSA_TRAS_LIMITE,
        reloj: Callable[[], float] = time.monotonic,
    ):
        if not clientes:
            raise ValueError("Se necesita al menos un cliente.")
        self.clientes = list(clientes)
        self.nombre = "→".join(c.nombre for c in self.clientes)
        self.modelo = "→".join(c.modelo for c in self.clientes)
        self._pausa = pausa
        self._reloj = reloj
        self._pausado_hasta: dict[int, float] = {}
        self.ultimo: ClienteLLM | None = None

    def _en_pausa(self, indice: int) -> bool:
        return self._reloj() < self._pausado_hasta.get(indice, float("-inf"))

    def completar(self, sistema: str, mensajes: list[dict], herramientas: list[dict]) -> RespuestaLLM:
        activos = [i for i in range(len(self.clientes)) if not self._en_pausa(i)]
        # Los proveedores en pausa quedan de último recurso.
        orden = activos + [i for i in range(len(self.clientes)) if i not in activos]
        errores: list[str] = []
        for posicion, indice in enumerate(orden):
            cliente = self.clientes[indice]
            try:
                respuesta = cliente.completar(sistema, mensajes, herramientas)
            except ErrorLLM as error:
                motivo = getattr(error, "motivo", OTRO)
                errores.append(f"{cliente.nombre}: {error}")
                if motivo == LIMITE:
                    self._pausado_hasta[indice] = self._reloj() + self._pausa
                if posicion + 1 < len(orden):
                    siguiente = self.clientes[orden[posicion + 1]]
                    registro.warning(
                        "%s falló (%s); se usa %s (%s) como respaldo.",
                        cliente.nombre, motivo, siguiente.nombre, siguiente.modelo,
                    )
                continue
            if indice != 0 and self.ultimo is not cliente:
                registro.info("Respondiendo con el respaldo %s (%s).", cliente.nombre, cliente.modelo)
            elif indice == 0 and self.ultimo is not None and self.ultimo is not cliente:
                registro.info("%s vuelve a responder como proveedor principal.", cliente.nombre)
            self.ultimo = cliente
            return respuesta
        raise ErrorLLM("Ningún proveedor de LLM respondió. " + " | ".join(errores), OTRO)


def _cliente_gemini(llave: str, max_retries: int = 4) -> ClienteOpenAICompatible:
    modelo = os.getenv("GEMINI_MODEL") or MODELO_GEMINI
    return ClienteOpenAICompatible("gemini", llave, modelo, GEMINI_URL, max_retries=max_retries)


def crear_cliente() -> ClienteLLM:
    """Crea el cliente según ``LLM_PROVIDER`` o, si no se indica, según las llaves disponibles.

    Groq va primero porque su plan gratuito permite usar el proyecto sin costo.
    Si además hay ``GEMINI_API_KEY`` y el principal es otro proveedor, se
    devuelve un ``ClienteConRespaldo`` que pasa a Gemini cuando el principal falla.
    """
    llaves = {
        "groq": os.getenv("GROQ_API_KEY"),
        "anthropic": os.getenv("ANTHROPIC_API_KEY"),
        "openai": os.getenv("OPENAI_API_KEY"),
        "gemini": os.getenv("GEMINI_API_KEY"),
    }
    proveedor = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    if not proveedor:
        proveedor = next((nombre for nombre, llave in llaves.items() if llave), "")
    if proveedor not in llaves:
        raise ErrorLLM(
            "No hay un LLM configurado. Crea una llave gratuita en https://console.groq.com "
            "y ponla en GROQ_API_KEY dentro del archivo .env."
        )
    llave = llaves[proveedor]
    if not llave:
        raise ErrorLLM(f"LLM_PROVIDER es '{proveedor}' pero falta su llave de API en .env.")
    if proveedor == "gemini":
        return _cliente_gemini(llave)

    respaldo = llaves["gemini"]
    # Con respaldo, el principal falla rápido en lugar de esperar reintentos.
    reintentos = 1 if respaldo else 4
    if proveedor == "groq":
        principal = ClienteOpenAICompatible(
            "groq", llave, os.getenv("GROQ_MODEL") or MODELO_GROQ, GROQ_URL, max_retries=reintentos
        )
    elif proveedor == "anthropic":
        principal = ClienteAnthropic(
            llave, os.getenv("ANTHROPIC_MODEL") or MODELO_ANTHROPIC, max_retries=reintentos
        )
    else:
        modelo = os.getenv("OPENAI_MODEL")
        if not modelo:
            raise ErrorLLM("Para usar OpenAI indica el modelo en OPENAI_MODEL dentro de .env.")
        principal = ClienteOpenAICompatible("openai", llave, modelo, max_retries=reintentos)

    if not respaldo:
        return principal
    return ClienteConRespaldo([principal, _cliente_gemini(respaldo)])
