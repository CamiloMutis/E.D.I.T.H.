"""Adaptador común para los proveedores de LLM.

El agente trabaja con un formato de mensajes propio y neutral. Cada cliente lo
traduce a la API de su proveedor, de modo que cambiar de Groq a Claude o a GPT
es cambiar una variable en ``.env`` y no una línea del agente.

Formato de los mensajes:
    {"rol": "usuario", "texto": "..."}
    {"rol": "asistente", "texto": "...", "llamadas": [LlamadaHerramienta, ...]}
    {"rol": "herramienta", "llamada_id": "...", "nombre": "...", "texto": "<json>"}
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Protocol

registro = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1"
MODELO_GROQ = "openai/gpt-oss-120b"
MODELO_ANTHROPIC = "claude-haiku-5-5"
MAX_TOKENS = 2048


class ErrorLLM(Exception):
    """El proveedor no respondió o rechazó la solicitud."""


@dataclass
class LlamadaHerramienta:
    id: str
    nombre: str
    argumentos: dict


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


class ClienteOpenAICompatible:
    """Cliente para las API con formato de OpenAI: Groq y el propio OpenAI."""

    def __init__(self, nombre: str, api_key: str, modelo: str, base_url: str | None = None):
        from openai import OpenAI

        self.nombre = nombre
        self.modelo = modelo
        # max_retries hace que el SDK espere y reintente ante un 429 del plan gratuito.
        self._cliente = OpenAI(api_key=api_key, base_url=base_url, max_retries=4, timeout=60)

    @staticmethod
    def _traducir(sistema: str, mensajes: list[dict]) -> list[dict]:
        salida = [{"role": "system", "content": sistema}]
        for m in mensajes:
            if m["rol"] == "usuario":
                salida.append({"role": "user", "content": m["texto"]})
            elif m["rol"] == "asistente":
                mensaje = {"role": "assistant", "content": m.get("texto") or None}
                if m.get("llamadas"):
                    mensaje["tool_calls"] = [
                        {
                            "id": ll.id,
                            "type": "function",
                            "function": {
                                "name": ll.nombre,
                                "arguments": json.dumps(ll.argumentos, ensure_ascii=False),
                            },
                        }
                        for ll in m["llamadas"]
                    ]
                salida.append(mensaje)
            else:
                salida.append({"role": "tool", "tool_call_id": m["llamada_id"], "content": m["texto"]})
        return salida

    def completar(self, sistema: str, mensajes: list[dict], herramientas: list[dict]) -> RespuestaLLM:
        import openai

        parametros = {"model": self.modelo, "messages": self._traducir(sistema, mensajes)}
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
                raise ErrorLLM(f"{self.nombre} no respondió: {error}") from error
        else:
            raise ErrorLLM(f"{self.nombre} rechazó la solicitud: {ultimo_error}")

        mensaje = respuesta.choices[0].message
        llamadas = [
            LlamadaHerramienta(ll.id, ll.function.name, _argumentos(ll.function.arguments))
            for ll in (mensaje.tool_calls or [])
        ]
        return RespuestaLLM(texto=mensaje.content or "", llamadas=llamadas)


class ClienteAnthropic:
    """Cliente para la API de Claude."""

    nombre = "anthropic"

    def __init__(self, api_key: str, modelo: str):
        from anthropic import Anthropic

        self.modelo = modelo
        self._cliente = Anthropic(api_key=api_key, max_retries=4, timeout=60)

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
            raise ErrorLLM(f"Claude no respondió: {error}") from error

        texto, llamadas = [], []
        for bloque in respuesta.content:
            if bloque.type == "text":
                texto.append(bloque.text)
            elif bloque.type == "tool_use":
                llamadas.append(LlamadaHerramienta(bloque.id, bloque.name, dict(bloque.input)))
        return RespuestaLLM(texto="".join(texto), llamadas=llamadas)


def crear_cliente() -> ClienteLLM:
    """Crea el cliente según ``LLM_PROVIDER`` o, si no se indica, según las llaves disponibles.

    Groq va primero porque su plan gratuito permite usar el proyecto sin costo.
    """
    llaves = {
        "groq": os.getenv("GROQ_API_KEY"),
        "anthropic": os.getenv("ANTHROPIC_API_KEY"),
        "openai": os.getenv("OPENAI_API_KEY"),
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

    if proveedor == "groq":
        return ClienteOpenAICompatible("groq", llave, os.getenv("GROQ_MODEL") or MODELO_GROQ, GROQ_URL)
    if proveedor == "anthropic":
        return ClienteAnthropic(llave, os.getenv("ANTHROPIC_MODEL") or MODELO_ANTHROPIC)
    modelo = os.getenv("OPENAI_MODEL")
    if not modelo:
        raise ErrorLLM("Para usar OpenAI indica el modelo en OPENAI_MODEL dentro de .env.")
    return ClienteOpenAICompatible("openai", llave, modelo)
