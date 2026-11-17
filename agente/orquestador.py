"""Bucle del agente: pensar, llamar una herramienta, observar y responder.

El LLM decide qué herramienta usar y con qué argumentos; este módulo la
ejecuta, le devuelve el resultado y repite hasta que el LLM responde con texto.
"""

from __future__ import annotations

import json
import logging

from agente import prompts
from agente.herramientas import HERRAMIENTAS, POR_NOMBRE
from agente.llm import ClienteLLM, ErrorLLM, LlamadaHerramienta
from skills.agenda import ErrorSkill
from skills.contexto import Contexto

registro = logging.getLogger(__name__)

MAX_PASOS = 8          # llamadas al LLM por mensaje del usuario
MAX_HISTORIAL = 12     # mensajes de conversación que se conservan como memoria


class Agente:
    """Agente orquestador de E.D.I.T.H."""

    def __init__(self, ctx: Contexto, llm: ClienteLLM):
        self.ctx = ctx
        self.llm = llm
        self.historial: list[dict] = []
        self._esquemas = [h.esquema() for h in HERRAMIENTAS]

    def ejecutar_herramienta(self, llamada: LlamadaHerramienta) -> dict:
        """Ejecuta una herramienta y devuelve siempre un diccionario, incluso si falla.

        Los errores vuelven al LLM como dato para que pueda corregir la
        llamada o explicarle el problema al estudiante.
        """
        herramienta = POR_NOMBRE.get(llamada.nombre)
        if not herramienta:
            return {"error": f"No existe la herramienta '{llamada.nombre}'."}
        try:
            return herramienta.funcion(self.ctx, **llamada.argumentos)
        except ErrorSkill as error:
            return {"error": str(error)}
        except TypeError as error:
            return {"error": f"Argumentos inválidos para {llamada.nombre}: {error}"}
        except Exception as error:
            registro.exception("Falló la herramienta %s", llamada.nombre)
            return {"error": f"Fallo interno en {llamada.nombre}: {error}"}

    def responder(self, texto_usuario: str) -> str:
        """Atiende un mensaje del estudiante y devuelve la respuesta del agente."""
        mensajes = self.historial + [{"rol": "usuario", "texto": texto_usuario}]
        sistema = prompts.prompt_de_sistema(self.ctx)
        respuesta_final = ""

        for _ in range(MAX_PASOS):
            respuesta = self.llm.completar(sistema, mensajes, self._esquemas)
            if not respuesta.llamadas:
                respuesta_final = respuesta.texto.strip()
                break
            mensajes.append({"rol": "asistente", "texto": respuesta.texto, "llamadas": respuesta.llamadas})
            for llamada in respuesta.llamadas:
                resultado = self.ejecutar_herramienta(llamada)
                registro.info("Herramienta %s(%s)", llamada.nombre, llamada.argumentos)
                mensajes.append(
                    {
                        "rol": "herramienta",
                        "llamada_id": llamada.id,
                        "nombre": llamada.nombre,
                        "texto": json.dumps(resultado, ensure_ascii=False, default=str),
                    }
                )
        else:
            respuesta_final = "Me enredé con esa solicitud. ¿Puedes decírmela de otra forma?"

        respuesta_final = respuesta_final or "Listo."
        # Como memoria se guarda solo la conversación, no los resultados de las
        # herramientas: el estado real siempre se vuelve a leer de la base de datos.
        self.historial += [
            {"rol": "usuario", "texto": texto_usuario},
            {"rol": "asistente", "texto": respuesta_final},
        ]
        self.historial = self.historial[-MAX_HISTORIAL:]
        return respuesta_final

    def redactar_aviso(self, resultado_replan: dict) -> str:
        """Redacta el aviso de la revisión nocturna a partir del resultado de replanificar."""
        resumen = {
            clave: resultado_replan[clave]
            for clave in ("resumen", "bloques_perdidos", "bloques", "entregas", "hay_riesgo")
        }
        peticion = prompts.AVISO_RIESGO.format(resultado=json.dumps(resumen, ensure_ascii=False, default=str))
        try:
            texto = self.llm.completar(
                prompts.prompt_de_sistema(self.ctx), [{"rol": "usuario", "texto": peticion}], []
            ).texto.strip()
        except ErrorLLM as error:
            registro.warning("Sin LLM para el aviso: %s", error)
            texto = ""
        return texto or prompts.aviso_de_respaldo(resultado_replan)

    def desempatar_archivo(self, texto: str, materias: list[str]) -> str | None:
        """Segunda opinión del LLM cuando el clasificador no está seguro de un archivo."""
        peticion = prompts.DESEMPATE.format(materias="; ".join(materias), texto=texto)
        try:
            respuesta = self.llm.completar(
                "Clasificas documentos académicos por materia.", [{"rol": "usuario", "texto": peticion}], []
            ).texto.strip()
        except ErrorLLM as error:
            registro.warning("Sin LLM para desempatar: %s", error)
            return None
        return respuesta if respuesta in materias else None
