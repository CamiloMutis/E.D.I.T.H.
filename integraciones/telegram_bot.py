"""Bot de Telegram: la interfaz de E.D.I.T.H.

Atiende la conversación con el estudiante, los botones para marcar bloques o
confirmar archivos, y las rutinas a hora fija (resumen de la mañana, revisión
nocturna y reentreno semanal). Los skills y el LLM son síncronos, así que
cada llamada se ejecuta en un hilo aparte para no bloquear al bot.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agente.llm import ErrorLLM
from agente.orquestador import Agente
from ajustes import DIAS_SEMANA
from ml.extraccion_texto import EXTENSIONES
from skills import soporte
from skills.agenda import ErrorSkill, fecha
from skills.contexto import Contexto, formato_hora, formato_horas
from skills.estimar_tiempo import reentrenar_predictor
from skills.organizar_archivos import clasificar_archivo, confirmar_archivo, reentrenar_clasificador
from skills.replanificar import replanificar
from skills.vigilar_moodle import dentro_del_horario, redactar_aviso_moodle, revisar_moodle

registro = logging.getLogger(__name__)

LIMITE_TELEGRAM = 4000
AYUDA = """Escríbeme como a una persona. Por ejemplo:
• "El viernes 28 entrego el taller 3 de Sistemas Operativos, vale 15 %, es sobre semáforos"
• "¿Qué hago hoy?"
• "No alcancé a hacer lo de ayer, reorganiza"
• "¿Qué archivos tengo de Redes Inalámbricas?"

También puedes enviarme un archivo (PDF, Word o PowerPoint) y lo guardo en su materia.

Comandos:
/hoy — plan del día
/pendientes — entregas abiertas
/revisar_dia — revisar lo de hoy y reorganizar
/semestre N — cambiar el semestre que cursas
/materias — materias activas
/moodle — revisar Moodle ahora (tareas nuevas y por vencer)
/reentrenar — reentrenar los modelos con tus datos"""


def _trozos(texto: str) -> list[str]:
    """Parte un mensaje largo respetando el límite de Telegram."""
    return [texto[i : i + LIMITE_TELEGRAM] for i in range(0, len(texto), LIMITE_TELEGRAM)] or [""]


class BotEdith:
    def __init__(self, ctx: Contexto, agente: Agente, token: str):
        self.ctx = ctx
        self.agente = agente
        self.token = token
        self.app: Application | None = None
        self.bucle: asyncio.AbstractEventLoop | None = None
        # Un solo turno a la vez: el agente guarda memoria de la conversación
        # y dos mensajes simultáneos la mezclarían.
        self._turno = asyncio.Lock()
        self._archivos_en_duda: dict[str, dict] = {}

    # ------------------------------------------------------------ utilidades

    def chat_dueno(self) -> int | None:
        valor = os.getenv("TELEGRAM_CHAT_ID") or self.ctx.repo.preferencia("chat_id")
        return int(valor) if valor else None

    async def _autorizado(self, update: Update) -> bool:
        """El bot es personal: solo responde al chat de su dueño."""
        chat_id = update.effective_chat.id
        dueno = self.chat_dueno()
        if dueno is None:
            self.ctx.repo.fijar_preferencia("chat_id", str(chat_id))
            return True
        if chat_id == dueno:
            return True
        await update.effective_chat.send_message("Este bot es personal y ya tiene dueño.")
        return False

    async def enviar(self, texto: str, botones: list[list[InlineKeyboardButton]] | None = None) -> None:
        """Envía un mensaje al dueño; es lo que usan las rutinas y el vigilante."""
        chat_id = self.chat_dueno()
        if chat_id is None or self.app is None:
            registro.info("Aún no hay chat registrado; mensaje omitido: %s", texto[:60])
            return
        partes = _trozos(texto)
        for i, parte in enumerate(partes):
            marcado = InlineKeyboardMarkup(botones) if botones and i == len(partes) - 1 else None
            await self.app.bot.send_message(chat_id, parte, reply_markup=marcado)

    async def _en_hilo(self, funcion, *argumentos):
        async with self._turno:
            return await asyncio.to_thread(funcion, *argumentos)

    # --------------------------------------------------------------- comandos

    async def cmd_start(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._autorizado(update):
            return
        materias = ", ".join(m["nombre"] for m in self.ctx.repo.materias_activas())
        semestre = self.ctx.repo.preferencia("semestre_actual", "?")
        await update.message.reply_text(
            "Hola, soy E.D.I.T.H. Voy a dirigir tu semestre: planeo tus entregas, estimo cuánto "
            "te demoras de verdad, ordeno tus archivos y corrijo el plan cuando te atrasas.\n\n"
            f"Tengo registrado que cursas el semestre {semestre}: {materias}.\n"
            "Si no es así, cámbialo con /semestre N.\n\n" + AYUDA
        )

    async def cmd_ayuda(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._autorizado(update):
            await update.message.reply_text(AYUDA)

    async def cmd_semestre(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._autorizado(update):
            return
        try:
            semestre = int(context.args[0])
            if not 1 <= semestre <= 10:
                raise ValueError
        except (IndexError, ValueError):
            await update.message.reply_text("Dime el semestre con un número del 1 al 10. Ejemplo: /semestre 7")
            return
        activas = self.ctx.repo.activar_semestre(semestre)
        await update.message.reply_text(
            f"Listo, semestre {semestre}. Materias activas:\n" + "\n".join(f"• {m['nombre']}" for m in activas)
        )

    async def cmd_materias(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._autorizado(update):
            activas = self.ctx.repo.materias_activas()
            await update.message.reply_text(
                "Materias activas:\n" + "\n".join(f"• {m['nombre']}" for m in activas)
            )

    async def cmd_hoy(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._autorizado(update):
            await update.message.reply_text(soporte.resumen_del_dia(self.ctx))

    async def cmd_pendientes(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._autorizado(update):
            return
        entregas = soporte.listar_pendientes(self.ctx)["entregas"]
        if not entregas:
            await update.message.reply_text("No tienes entregas abiertas.")
            return
        lineas = []
        for e in entregas:
            riesgo = soporte.ETIQUETAS_RIESGO.get(e["riesgo"], e["riesgo"])
            lineas.append(f"• {e['nombre']} de {e['materia']} — {e['fecha_limite']} ({riesgo})")
            for s in e["subtareas"]:
                if s["estado"] != "hecha":
                    lineas.append(f"    – {s['nombre']}: faltan {formato_horas(s['horas_restantes'] or 0)}")
        await update.message.reply_text("\n".join(lineas))

    async def cmd_revisar_dia(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._autorizado(update):
            await self.revision_del_dia()

    async def cmd_moodle(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """Revisión manual: no respeta el horario porque el estudiante la pidió."""
        if not await self._autorizado(update):
            return
        if not await self.vigilar_moodle(forzar=True):
            await update.message.reply_text("Revisé Moodle: sin novedades.")

    async def cmd_reentrenar(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._autorizado(update):
            await update.message.reply_text("Reentrenando con tus datos...")
            await self.reentreno()

    # ----------------------------------------------------------- conversación

    async def al_recibir_texto(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._autorizado(update):
            return
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        try:
            respuesta = await self._en_hilo(self.agente.responder, update.message.text)
        except ErrorLLM as error:
            registro.error("LLM no disponible: %s", error)
            respuesta = (
                "Ahora mismo no puedo pensar: el modelo de lenguaje no respondió. "
                "Intenta de nuevo en un minuto. Mientras tanto funcionan /hoy, /pendientes y /revisar_dia."
            )
        for parte in _trozos(respuesta):
            await update.message.reply_text(parte)

    async def al_recibir_documento(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._autorizado(update):
            return
        documento = update.message.document
        nombre = Path(documento.file_name or "archivo").name
        if Path(nombre).suffix.lower() not in EXTENSIONES:
            await update.message.reply_text("Solo sé organizar PDF, Word, PowerPoint y texto.")
            return
        entrada = self.ctx.ajustes.carpeta_destino / "_Entrada"
        entrada.mkdir(parents=True, exist_ok=True)
        destino = entrada / nombre
        archivo = await documento.get_file()
        await archivo.download_to_drive(destino)
        await self.procesar_archivo(destino)

    # --------------------------------------------------------------- archivos

    async def procesar_archivo(self, ruta: Path) -> None:
        """Clasifica un archivo nuevo y avisa dónde quedó, o pregunta si hay duda."""
        try:
            resultado = await self._en_hilo(clasificar_archivo, self.ctx, ruta, self.agente.desempatar_archivo)
        except ErrorSkill as error:
            registro.info("Archivo omitido: %s", error)
            return
        if resultado["estado"] == "movido":
            await self.enviar(self._texto_movido(resultado))
            return
        clave = uuid.uuid4().hex[:8]
        self._archivos_en_duda[clave] = resultado
        botones = [
            [InlineKeyboardButton(materia, callback_data=f"a:{clave}:{i}")]
            for i, materia in enumerate(resultado["candidatas"])
        ]
        botones.append([InlineKeyboardButton("No es de la universidad", callback_data=f"a:{clave}:x")])
        await self.enviar(f"Llegó {resultado['archivo']} y no estoy segura de la materia. ¿De cuál es?", botones)

    @staticmethod
    def _texto_movido(resultado: dict) -> str:
        lugar = resultado["materia"] + (f" / {resultado['entrega']}" if resultado["entrega"] else "")
        return f"Moví {resultado['archivo']} a {lugar}."

    # ---------------------------------------------------------------- rutinas

    async def resumen_matutino(self, _: ContextTypes.DEFAULT_TYPE | None = None) -> None:
        await self.enviar(soporte.resumen_del_dia(self.ctx))

    async def revision_del_dia(self, _: ContextTypes.DEFAULT_TYPE | None = None) -> None:
        """Pregunta por los bloques de hoy sin marcar y luego reorganiza."""
        ahora = self.ctx.ahora()
        sin_marcar = [
            b for b in soporte.bloques_del_dia(self.ctx)
            if b["estado"] == "pendiente" and fecha(b["fin"]) <= ahora
        ]
        if not sin_marcar:
            await self.reorganizar()
            return
        await self.enviar("Revisión del día. ¿Cómo te fue con estos bloques?")
        for b in sin_marcar:
            inicio, fin = fecha(b["inicio"]), fecha(b["fin"])
            await self.enviar(
                f"{formato_hora(inicio)} a {formato_hora(fin)}: {b['subtarea']} ({b['materia']})",
                [self._botones_bloque(b["id"])],
            )
        await self.enviar(
            "Cuando termines de marcar, reorganizo la semana. Lo que quede sin marcar cuenta como no hecho.",
            [[InlineKeyboardButton("Reorganizar ahora", callback_data="rev")]],
        )

    @staticmethod
    def _botones_bloque(bloque_id: int) -> list[InlineKeyboardButton]:
        return [
            InlineKeyboardButton("Hecho", callback_data=f"b:{bloque_id}:hecho"),
            InlineKeyboardButton("A medias", callback_data=f"b:{bloque_id}:a_medias"),
            InlineKeyboardButton("No hecho", callback_data=f"b:{bloque_id}:perdido"),
        ]

    async def reorganizar(self, horas_extra_dia: float = 0.0) -> None:
        """Ejecuta ``replanificar`` y envía el aviso, con opciones si hay riesgo."""
        resultado = await self._en_hilo(replanificar, self.ctx, horas_extra_dia)
        aviso = await self._en_hilo(self.agente.redactar_aviso, resultado)
        botones = None
        if resultado["hay_riesgo"] and horas_extra_dia < 2:
            botones = [
                [
                    InlineKeyboardButton("Estudiar 1 h más al día", callback_data="x:1"),
                    InlineKeyboardButton("2 h más al día", callback_data="x:2"),
                ],
                [InlineKeyboardButton("Dejarlo así", callback_data="x:0")],
            ]
        await self.enviar(aviso, botones)

    async def reentreno(self, _: ContextTypes.DEFAULT_TYPE | None = None) -> None:
        """Reentrena los modelos con el historial real y cuenta qué aprendió."""
        metricas = await self._en_hilo(reentrenar_predictor, self.ctx)
        lineas = [
            "Lo que aprendí de ti esta semana:",
            f"• Predictor de tiempo: {metricas['filas_de_uso']} registros tuyos, "
            f"error medio de {formato_horas(round(metricas['MAE'], 2))} "
            f"(sin modelo sería {formato_horas(round(metricas['MAE_linea_base'], 2))}).",
        ]
        clasificador = await self._en_hilo(reentrenar_clasificador, self.ctx)
        if clasificador:
            lineas.append(
                f"• Clasificador de archivos: sumé {clasificador['ejemplos_confirmados']} archivos que me confirmaste."
            )
        await self.enviar("\n".join(lineas))

    async def vigilar_moodle(self, _: ContextTypes.DEFAULT_TYPE | None = None, forzar: bool = False) -> bool:
        """Revisa Moodle y avisa lo nuevo o lo que está por vencer. Devuelve si envió algo.

        Fuera del horario configurado no consulta nada. Si aún no hay dueño del
        chat tampoco: los avisos se marcarían como enviados sin llegar a nadie.
        """
        if self.chat_dueno() is None or not (forzar or dentro_del_horario(self.ctx)):
            return False
        try:
            resultado = await self._en_hilo(revisar_moodle, self.ctx)
        except ErrorSkill as error:
            registro.warning("Vigilancia de Moodle: %s", error)
            if forzar:
                await self.enviar(str(error))
                return True
            return False
        texto = redactar_aviso_moodle(self.ctx, resultado)
        if texto:
            await self.enviar(texto)
        return texto is not None

    # ---------------------------------------------------------------- botones

    async def al_pulsar_boton(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        consulta = update.callback_query
        await consulta.answer()
        if consulta.message.chat.id != self.chat_dueno():
            return
        accion, _, resto = consulta.data.partition(":")
        try:
            if accion == "b":
                await self._boton_bloque(consulta, resto)
            elif accion == "t":
                await self._boton_subtarea(consulta, resto)
            elif accion == "a":
                await self._boton_archivo(consulta, resto)
            elif accion == "rev":
                await consulta.edit_message_text("Reorganizando...")
                await self.reorganizar()
            elif accion == "x":
                await self._boton_riesgo(consulta, resto)
        except ErrorSkill as error:
            await consulta.edit_message_text(str(error))

    async def _boton_bloque(self, consulta, resto: str) -> None:
        bloque_id, _, estado = resto.partition(":")
        resultado = soporte.marcar_bloque(self.ctx, int(bloque_id), estado)
        etiqueta = {"hecho": "hecho", "a_medias": "a medias", "perdido": "no hecho"}[estado]
        await consulta.edit_message_text(f"{resultado['subtarea']}: {etiqueta}.")
        if resultado["posiblemente_terminada"]:
            sid = resultado["subtarea_id"]
            await self.enviar(
                f"Ya cumpliste las horas previstas de «{resultado['subtarea']}». ¿La terminaste?",
                [
                    [InlineKeyboardButton("Sí, terminada", callback_data=f"t:{sid}:0")],
                    [
                        InlineKeyboardButton("Me falta 1 h", callback_data=f"t:{sid}:1"),
                        InlineKeyboardButton("Me faltan 2 h", callback_data=f"t:{sid}:2"),
                    ],
                ],
            )

    async def _boton_subtarea(self, consulta, resto: str) -> None:
        subtarea_id, _, faltan = resto.partition(":")
        if faltan == "0":
            resultado = await self._en_hilo(soporte.registrar_avance, self.ctx, int(subtarea_id), True)
            await consulta.edit_message_text(f"Anotado: «{resultado['subtarea']}» terminada. {resultado['nota']}")
            return
        await self._en_hilo(soporte.registrar_avance, self.ctx, int(subtarea_id), False, None, float(faltan))
        await consulta.edit_message_text(f"Entendido, le reservo {faltan} h más.")
        await self.reorganizar()

    async def _boton_archivo(self, consulta, resto: str) -> None:
        clave, _, opcion = resto.partition(":")
        pendiente = self._archivos_en_duda.pop(clave, None)
        if not pendiente:
            await consulta.edit_message_text("Ese archivo ya fue atendido.")
            return
        if opcion == "x":
            await consulta.edit_message_text(f"De acuerdo, dejo {pendiente['archivo']} donde está.")
            return
        materia = pendiente["candidatas"][int(opcion)]
        resultado = await self._en_hilo(confirmar_archivo, self.ctx, pendiente["ruta"], materia)
        await consulta.edit_message_text(self._texto_movido(resultado) + " Lo tendré en cuenta la próxima vez.")

    async def _boton_riesgo(self, consulta, resto: str) -> None:
        horas = float(resto)
        if horas == 0:
            await consulta.edit_message_text("De acuerdo, el plan queda como está.")
            return
        await consulta.edit_message_text(f"Subo el tope a {formato_horas(horas)} más por día y reorganizo...")
        await self.reorganizar(horas)

    # ---------------------------------------------------------------- arranque

    async def _al_iniciar(self, app: Application) -> None:
        self.bucle = asyncio.get_running_loop()

    def programar_archivo(self, ruta: Path) -> None:
        """Punto de entrada del vigilante, que corre en otro hilo."""
        if self.bucle:
            asyncio.run_coroutine_threadsafe(self.procesar_archivo(ruta), self.bucle)

    def construir(self) -> Application:
        """Crea la aplicación de Telegram con sus manejadores y rutinas programadas."""
        app = Application.builder().token(self.token).post_init(self._al_iniciar).build()
        self.app = app
        for nombre, manejador in [
            ("start", self.cmd_start), ("ayuda", self.cmd_ayuda), ("help", self.cmd_ayuda),
            ("semestre", self.cmd_semestre), ("materias", self.cmd_materias), ("hoy", self.cmd_hoy),
            ("pendientes", self.cmd_pendientes), ("revisar_dia", self.cmd_revisar_dia),
            ("reentrenar", self.cmd_reentrenar), ("moodle", self.cmd_moodle),
        ]:
            app.add_handler(CommandHandler(nombre, manejador))
        app.add_handler(CallbackQueryHandler(self.al_pulsar_boton))
        app.add_handler(MessageHandler(filters.Document.ALL, self.al_recibir_documento))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.al_recibir_texto))

        ajustes = self.ctx.ajustes

        def _hora(valor: time) -> time:
            return valor.replace(tzinfo=ajustes.zona)

        app.job_queue.run_daily(self.resumen_matutino, _hora(ajustes.hora_resumen), name="resumen")
        app.job_queue.run_daily(self.revision_del_dia, _hora(ajustes.hora_revision), name="revision")
        # python-telegram-bot numera los días de domingo (0) a sábado (6).
        dia = (DIAS_SEMANA.index(ajustes.dia_reentreno) + 1) % 7
        app.job_queue.run_daily(self.reentreno, _hora(ajustes.hora_reentreno), days=(dia,), name="reentreno")
        if ajustes.moodle_vigilancia and os.getenv("MOODLE_URL"):
            app.job_queue.run_repeating(
                self.vigilar_moodle, interval=ajustes.moodle_intervalo_minutos * 60, first=30, name="moodle"
            )
            registro.info(
                "Vigilancia de Moodle cada %s min, de %s a %s",
                ajustes.moodle_intervalo_minutos,
                ajustes.moodle_horario_inicio.strftime("%H:%M"),
                ajustes.moodle_horario_fin.strftime("%H:%M"),
            )
        return app
