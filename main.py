"""Punto de entrada de E.D.I.T.H.

Uso:
    python main.py              # arranca el bot de Telegram y las rutinas programadas
    python main.py --consola    # conversa con el agente en la terminal, sin Telegram
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from ajustes import cargar_ajustes
from arranque import construir_contexto


def configurar_registro() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Las librerías de red escriben una línea por solicitud; no aportan en uso normal.
    for ruidoso in ("httpx", "httpcore", "apscheduler", "googleapiclient"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)


def modo_consola(ctx, agente) -> None:
    """Conversación con el agente en la terminal. Sirve para probar sin Telegram."""
    from agente.llm import ErrorLLM
    from agente.prompts import aviso_de_respaldo
    from skills import soporte
    from skills.agenda import ErrorSkill
    from skills.organizar_archivos import clasificar_archivo
    from skills.replanificar import replanificar

    print("E.D.I.T.H. en modo consola. Comandos: /hoy, /pendientes, /revisar_dia, /archivo RUTA, /salir")
    while True:
        try:
            texto = input("\ntú> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not texto:
            continue
        try:
            if texto in ("/salir", "/exit"):
                return
            if texto == "/hoy":
                print(soporte.resumen_del_dia(ctx))
            elif texto == "/pendientes":
                for e in soporte.listar_pendientes(ctx)["entregas"]:
                    print(f"• {e['nombre']} de {e['materia']} — {e['fecha_limite']} ({e['riesgo']})")
            elif texto == "/revisar_dia":
                resultado = replanificar(ctx)
                try:
                    print(agente.redactar_aviso(resultado) if agente else aviso_de_respaldo(resultado))
                except ErrorLLM:
                    print(aviso_de_respaldo(resultado))
            elif texto.startswith("/archivo "):
                print(clasificar_archivo(ctx, texto.split(" ", 1)[1].strip().strip('"')))
            elif agente is None:
                print("Sin llave de LLM solo funcionan los comandos. Configura GROQ_API_KEY en .env.")
            else:
                print(f"\nedith> {agente.responder(texto)}")
        except (ErrorSkill, ErrorLLM) as error:
            print(f"[error] {error}")


def main() -> int:
    analizador = argparse.ArgumentParser(description="E.D.I.T.H., agente que dirige tu semestre")
    analizador.add_argument("--consola", action="store_true", help="conversar en la terminal, sin Telegram")
    argumentos = analizador.parse_args()

    configurar_registro()
    registro = logging.getLogger("edith")
    ajustes = cargar_ajustes()
    ctx = construir_contexto(ajustes)

    from agente.llm import ErrorLLM, crear_cliente
    from agente.orquestador import Agente

    try:
        llm = crear_cliente()
        agente = Agente(ctx, llm)
        registro.info("LLM en uso: %s (%s)", llm.nombre, llm.modelo)
    except ErrorLLM as error:
        if not argumentos.consola:
            print(f"Error: {error}", file=sys.stderr)
            return 1
        registro.warning("%s", error)
        agente = None

    if argumentos.consola:
        modo_consola(ctx, agente)
        return 0

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print(
            "Error: falta TELEGRAM_BOT_TOKEN en el archivo .env. Créalo con @BotFather "
            "o usa 'python main.py --consola' para probar sin Telegram.",
            file=sys.stderr,
        )
        return 1

    from integraciones.telegram_bot import BotEdith
    from integraciones.vigilante_archivos import VigilanteArchivos

    bot = BotEdith(ctx, agente, token)
    app = bot.construir()
    vigilante = VigilanteArchivos(ajustes.carpeta_vigilada, bot.programar_archivo)
    vigilante.iniciar()
    registro.info("E.D.I.T.H. en línea. Escríbele /start a tu bot en Telegram. Ctrl+C para detener.")
    try:
        app.run_polling()
    finally:
        vigilante.detener()
        ctx.repo.cerrar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
