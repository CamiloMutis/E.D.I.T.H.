"""Adaptador común para los proveedores de LLM.

El agente trabaja con un formato de mensajes propio y neutral. Cada cliente lo
traduce a la API de su proveedor, de modo que cambiar de Groq a Claude, GPT o
Gemini es cambiar una variable en ``.env`` y no una línea del agente.

Formato de los mensajes:
    {"rol": "usuario", "texto": "..."}
    {"rol": "asistente", "texto": "...", "llamadas": [LlamadaHerramienta, ...]}
    {"rol": "herramienta", "llamada_id": "...", "nombre": "...", "texto": "<json>"}

Banco de proveedores: quien instala E.D.I.T.H. decide el orden en ``.env``
(``LLM_PROVIDERS=groq,openai,anthropic``) y puede poner varias llaves de un
mismo proveedor separadas por comas. ``crear_cliente()`` devuelve entonces un
``ClienteConRespaldo``: cuando un proveedor falla por límite de uso (429),
error del servidor, tiempo de espera o conexión, la misma solicitud pasa al
siguiente y la conversación sigue. Tras un 429 ese proveedor queda en pausa
unos minutos para no esperar por él en cada mensaje. Cualquier API compatible
con OpenAI (OpenRouter, DeepSeek, Ollama…) entra al banco con
``<NOMBRE>_BASE_URL``, ``<NOMBRE>_MODEL`` y ``<NOMBRE>_API_KEY``.

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
        # Se llama con un texto cuando el banco pasa a un respaldo o vuelve al
        # principal (por ejemplo, para avisarle al administrador por Telegram).
        self.al_cambiar: Callable[[str], None] | None = None

    def _avisar(self, texto: str) -> None:
        if self.al_cambiar is None:
            return
        try:
            self.al_cambiar(texto)
        except Exception:  # un aviso fallido nunca debe tumbar la respuesta
            registro.exception("No se pudo avisar el cambio de proveedor")

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
                anterior = self.ultimo or self.clientes[0]
                self._avisar(
                    f"🔁 {anterior.nombre} no está respondiendo ({'; '.join(errores)[:200]}). "
                    f"Sigo con el respaldo {cliente.nombre} ({cliente.modelo})."
                )
            elif indice == 0 and self.ultimo is not None and self.ultimo is not cliente:
                registro.info("%s vuelve a responder como proveedor principal.", cliente.nombre)
                self._avisar(f"✅ {cliente.nombre} volvió a responder; es otra vez el proveedor principal.")
            self.ultimo = cliente
            return respuesta
        raise ErrorLLM("Ningún proveedor de LLM respondió. " + " | ".join(errores), OTRO)


# Proveedores conocidos: URL de su API (None = la de OpenAI) y modelo por defecto
# (None = hay que indicarlo en <NOMBRE>_MODEL). El orden es el del banco cuando
# no se define LLM_PROVIDERS: Groq primero porque su plan gratuito basta.
PROVEEDORES = {
    "groq": (GROQ_URL, MODELO_GROQ),
    "openai": (None, None),
    "anthropic": (None, MODELO_ANTHROPIC),
    "gemini": (GEMINI_URL, MODELO_GEMINI),
}


def _lista(valor: str | None) -> list[str]:
    """``"a, b ,,c"`` → ``["a", "b", "c"]``."""
    return [parte.strip() for parte in (valor or "").split(",") if parte.strip()]


def _variable(nombre: str, sufijo: str) -> str:
    """Nombre de la variable de entorno de un proveedor: ``open-router`` → ``OPEN_ROUTER_API_KEY``."""
    return f"{nombre.upper().replace('-', '_')}_{sufijo}"


def _llaves(nombre: str) -> list[str]:
    """Llaves de un proveedor; ``GROQ_API_KEY=llave1,llave2`` da dos llaves."""
    return _lista(os.getenv(_variable(nombre, "API_KEY")))


def _clientes_de(nombre: str, reintentos: int) -> list[ClienteLLM]:
    """Un cliente por cada llave del proveedor. Lanza ``ErrorLLM`` si falta algo."""
    conocido = PROVEEDORES.get(nombre)
    base_url = os.getenv(_variable(nombre, "BASE_URL")) or (conocido[0] if conocido else None)
    modelo = os.getenv(_variable(nombre, "MODEL")) or (conocido[1] if conocido else None)
    llaves = _llaves(nombre)
    if not conocido and not base_url:
        raise ErrorLLM(
            f"'{nombre}' no es un proveedor conocido: para usarlo como API compatible con OpenAI "
            f"define {_variable(nombre, 'BASE_URL')}, {_variable(nombre, 'MODEL')} y {_variable(nombre, 'API_KEY')}."
        )
    if not llaves:
        if conocido:
            raise ErrorLLM(f"Falta la llave de '{nombre}' en {_variable(nombre, 'API_KEY')} dentro de .env.")
        # Un servidor propio (por ejemplo Ollama) puede no pedir llave.
        llaves = ["sin-llave"]
    if not modelo:
        raise ErrorLLM(f"Para usar '{nombre}' indica el modelo en {_variable(nombre, 'MODEL')} dentro de .env.")

    clientes: list[ClienteLLM] = []
    for posicion, llave in enumerate(llaves, start=1):
        if nombre == "anthropic":
            cliente = ClienteAnthropic(llave, modelo, max_retries=reintentos)
        else:
            cliente = ClienteOpenAICompatible(nombre, llave, modelo, base_url, max_retries=reintentos)
        if len(llaves) > 1:
            # Se distinguen en el registro sin mostrar la llave.
            cliente.nombre = f"{nombre}#{posicion}"
        clientes.append(cliente)
    return clientes


def orden_de_proveedores() -> list[str]:
    """Orden del banco de LLM según ``.env``.

    1. ``LLM_PROVIDERS=groq,openai,anthropic``: exactamente ese orden.
    2. Si no, ``LLM_PROVIDER`` (o el primero con llave) va primero y detrás, como
       respaldo, los demás proveedores conocidos que tengan llave.
    """
    explicito = [nombre.lower() for nombre in _lista(os.getenv("LLM_PROVIDERS"))]
    if explicito:
        return list(dict.fromkeys(explicito))
    con_llave = [nombre for nombre in PROVEEDORES if _llaves(nombre)]
    principal = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    if not principal:
        return con_llave
    return [principal] + [nombre for nombre in con_llave if nombre != principal]


def crear_cliente() -> ClienteLLM:
    """Crea el banco de LLM configurado en ``.env``.

    Con un solo proveedor y una sola llave devuelve ese cliente. Con varios
    devuelve un ``ClienteConRespaldo`` que los recorre en orden: cuando uno se
    queda sin cuota, falla o no responde, la misma solicitud pasa al siguiente.
    """
    orden = orden_de_proveedores()
    if not orden:
        raise ErrorLLM(
            "No hay un LLM configurado. Crea una llave gratuita en https://console.groq.com "
            "y ponla en GROQ_API_KEY dentro del archivo .env."
        )
    explicito = bool(_lista(os.getenv("LLM_PROVIDERS")))
    varios = len(orden) > 1 or len(_llaves(orden[0])) > 1
    # En un banco cada proveedor falla rápido para pasar al siguiente en lugar
    # de quedarse reintentando; solo el último insiste.
    clientes: list[ClienteLLM] = []
    for posicion, nombre in enumerate(orden):
        ultimo = posicion == len(orden) - 1
        try:
            clientes.extend(_clientes_de(nombre, reintentos=4 if (ultimo or not varios) else 1))
        except ErrorLLM as error:
            # El orden escrito a mano y el proveedor principal deben estar
            # completos; un respaldo detectado solo por su llave se omite.
            if explicito or posicion == 0:
                raise
            registro.warning("Se omite %s del banco de LLM: %s", nombre, error)
    if len(clientes) == 1:
        return clientes[0]
    return ClienteConRespaldo(clientes)
