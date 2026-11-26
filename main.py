"""Punto de entrada de E.D.I.T.H.

Uso:
    python main.py              # bot de Telegram para varios estudiantes, con sus rutinas
    python main.py --consola    # conversa con el agente en la terminal, sin Telegram (un usuario)

El bot consulta Telegram por su cuenta (polling). Si se define EDITH_WEBHOOK_URL
(la dirección pública https del servidor), en cambio escucha en el puerto PORT
(8080 por defecto): Telegram envía los mensajes a /telegram y /salud responde
"ok" para los chequeos de salud y los servicios que lo mantienen despierto.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
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


async def correr_webhook(app, url_publica: str, puerto: int) -> None:
    """Sirve el webhook de Telegram y /salud con tornado (viene con python-telegram-bot[webhooks])."""
    import signal

    import tornado.web
    from telegram import Update

    secreto = os.getenv("EDITH_WEBHOOK_SECRETO") or hashlib.sha256(app.bot.token.encode()).hexdigest()[:32]

    class Webhook(tornado.web.RequestHandler):
        async def post(self):
            if self.request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secreto:
                self.set_status(403)
                return
            await app.update_queue.put(Update.de_json(json.loads(self.request.body), app.bot))

    class Salud(tornado.web.RequestHandler):
        def get(self):
            self.write("ok")

        def head(self):
            self.set_status(200)

    parar = asyncio.Event()
    bucle = asyncio.get_running_loop()
    for senal in (signal.SIGINT, signal.SIGTERM):
        try:
            bucle.add_signal_handler(senal, parar.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows

    async with app:
        await app.bot.set_webhook(
            f"{url_publica.rstrip('/')}/telegram", secret_token=secreto, allowed_updates=Update.ALL_TYPES
        )
        if app.post_init:
            await app.post_init(app)
        await app.start()
        servidor = tornado.web.Application(
            [(r"/telegram", Webhook), (r"/salud", Salud), (r"/", Salud)]
        ).listen(puerto, "0.0.0.0")
        logging.getLogger("edith").info("Webhook en %s/telegram, escuchando en el puerto %s", url_publica, puerto)
        try:
            await parar.wait()
        finally:
            servidor.stop()
            await app.stop()


def modo_bot(ajustes, llm) -> int:
    from integraciones.telegram_bot import BotEdith, admin_desde_entorno
    from usuarios import GestorUsuarios, abrir_cuentas, migrar_instalacion_anterior, preparar_modelos

    registro = logging.getLogger("edith")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print(
            "Error: falta TELEGRAM_BOT_TOKEN en el archivo .env. Créalo con @BotFather "
            "o usa 'python main.py --consola' para probar sin Telegram.",
            file=sys.stderr,
        )
        return 1

    predictor, clasificador = preparar_modelos(ajustes)
    gestor = GestorUsuarios(ajustes, abrir_cuentas(ajustes), predictor, clasificador, llm)
    admin = admin_desde_entorno()
    if migrar_instalacion_anterior(gestor, admin):
        registro.info("Tus datos de la versión de un solo usuario pasaron a tu cuenta.")
    registro.info("Datos en %s · %s usuarios activos", ajustes.carpeta_estado, len(gestor.cuentas.activos()))
    if not gestor.moodle_url:
        registro.warning("Sin MOODLE_URL: los estudiantes no podrán conectar Moodle")

    bot = BotEdith(gestor, token, admin, os.getenv("EDITH_CODIGO_INVITACION"))
    url_webhook = os.getenv("EDITH_WEBHOOK_URL")
    try:
        if url_webhook:
            app = bot.construir(con_updater=False)
            asyncio.run(correr_webhook(app, url_webhook, int(os.getenv("PORT") or 8080)))
        else:
            app = bot.construir()
            registro.info("E.D.I.T.H. en línea. Escríbele /start a tu bot en Telegram. Ctrl+C para detener.")
            app.run_polling(drop_pending_updates=False)
    finally:
        gestor.cerrar_todo()
    return 0


def main() -> int:
    analizador = argparse.ArgumentParser(description="E.D.I.T.H., agente que dirige tu semestre")
    analizador.add_argument("--consola", action="store_true", help="conversar en la terminal, sin Telegram")
    argumentos = analizador.parse_args()

    configurar_registro()
    registro = logging.getLogger("edith")
    ajustes = cargar_ajustes()

    from agente.llm import ErrorLLM, crear_cliente

    try:
        llm = crear_cliente()
        registro.info("LLM en uso: %s (%s)", llm.nombre, llm.modelo)
    except ErrorLLM as error:
        if not argumentos.consola:
            print(f"Error: {error}", file=sys.stderr)
            return 1
        registro.warning("%s", error)
        llm = None

    if not argumentos.consola:
        return modo_bot(ajustes, llm)

    from agente.orquestador import Agente

    ctx = construir_contexto(ajustes)
    modo_consola(ctx, Agente(ctx, llm) if llm else None)
    ctx.repo.cerrar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
