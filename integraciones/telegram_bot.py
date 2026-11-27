"""Bot de Telegram: la interfaz de E.D.I.T.H. para varios estudiantes.

Todo se maneja desde el chat:

* **Registro guiado** (``/start``): aceptación del tratamiento de datos, semestre y
  conexión con Moodle. La contraseña de Moodle se usa una vez para pedir un token,
  el mensaje que la contiene se borra del chat y solo se guarda el token, cifrado.
* **Conversación** con el agente, archivos, botones para marcar bloques.
* **Rutinas** por estudiante, cada una a su hora: resumen de la mañana, revisión
  de la noche y vigilancia de Moodle. Se detienen con ``/pausar``.
* **Cuenta**: ``/estado``, ``/ajustes``, ``/calendario``, ``/mis_datos`` y ``/olvidarme``.

Cada estudiante tiene su propia sesión (base de datos, agente y candado); los
skills y el LLM son síncronos y se ejecutan en un hilo aparte para no bloquear al bot.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, time, timedelta
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agente.llm import ErrorLLM
from agente.prompts import aviso_de_respaldo
from ajustes import DIAS_SEMANA
from db.cuentas import PAUSA_INDEFINIDA
from integraciones.moodle import ClienteMoodle, ErrorMoodle, ErrorSesionMoodle
from ml.extraccion_texto import EXTENSIONES
from skills import soporte
from skills.agenda import ErrorSkill, fecha
from skills.contexto import formato_fecha, formato_hora, formato_horas
from skills.exportar_calendario import calendario_ics
from skills.organizar_archivos import clasificar_archivo, confirmar_archivo
from skills.replanificar import replanificar
from skills.vigilar_moodle import (
    PREFERENCIA_ULTIMA_REVISION,
    dentro_del_horario,
    redactar_aviso_moodle,
    revisar_moodle,
)
from usuarios import GestorUsuarios, Sesion

registro = logging.getLogger(__name__)

LIMITE_TELEGRAM = 4000
LIMITE_ARCHIVO = 45 * 1024 * 1024  # Telegram acepta hasta 50 MB por documento
VENTANA_RUTINA = timedelta(hours=2)  # si el bot estuvo apagado, la rutina se envía hasta 2 h tarde
PREF_ULTIMO_RESUMEN = "ultimo_resumen"
PREF_ULTIMA_REVISION_DIA = "ultima_revision_dia"

AYUDA = """Escríbeme como a una persona. Por ejemplo:
• "El viernes 28 entrego el taller 3 de Sistemas Operativos, vale 15 %, es sobre semáforos"
• "¿Qué hago hoy?"
• "¿Qué tengo en Moodle esta semana?"
• "No alcancé a hacer lo de ayer, reorganiza"

También puedes enviarme un archivo (PDF, Word o PowerPoint) y lo guardo en su materia.

Tu semestre:
/hoy — plan del día
/pendientes — entregas abiertas
/revisar_dia — revisar lo de hoy y reorganizar
/moodle — revisar Moodle ahora
/calendario — tu plan en un archivo para Google Calendar u Outlook
/semestre N — cambiar el semestre · /materias — materias activas

Tu cuenta:
/pausar — pausar avisos (ej.: /pausar 3d, /pausar hasta 30/11)
/reanudar — volver a recibir avisos
/ajustes — horas de estudio, horarios de avisos
/estado — qué tengo conectado
/conectar_moodle · /desconectar_moodle
/mis_datos — descargar todo lo que guardo de ti
/olvidarme — borrar tu cuenta y tus datos"""

COMANDOS_MENU = [
    ("hoy", "Plan del día"),
    ("pendientes", "Entregas abiertas y su riesgo"),
    ("moodle", "Revisar Moodle ahora"),
    ("revisar_dia", "Revisar lo de hoy y reorganizar"),
    ("calendario", "Exportar tu plan a tu calendario"),
    ("pausar", "Pausar los avisos"),
    ("reanudar", "Volver a recibir avisos"),
    ("ajustes", "Horas de estudio y horarios"),
    ("estado", "Qué tengo conectado"),
    ("ayuda", "Qué puedo hacer"),
]

POLITICA = """Antes de empezar, lo importante sobre tus datos:

• Guardo tus materias, entregas, bloques de estudio y los archivos que me envíes, solo para ayudarte con tu semestre.
• Si conectas Moodle, uso tu contraseña una sola vez para pedirle a Moodle un permiso de acceso (token). La contraseña no se guarda y borro el mensaje donde la escribas. El token queda cifrado.
• Lo que me escribes se procesa con un modelo de lenguaje externo (Groq o Gemini) para entenderte.
• Puedes pausarme con /pausar, descargar todo con /mis_datos y borrar todo con /olvidarme.

Este es un proyecto académico de la Universidad Santiago de Cali (Ley 1581 de 2012). ¿Aceptas?"""


def _trozos(texto: str) -> list[str]:
    """Parte un mensaje largo respetando el límite de Telegram."""
    return [texto[i : i + LIMITE_TELEGRAM] for i in range(0, len(texto), LIMITE_TELEGRAM)] or [""]


def interpretar_pausa(argumentos: list[str], ahora: datetime) -> datetime | str | None:
    """'' → indefinida; '3h', '2d', '1s' → duración; 'hasta 30/11' o '30/11' → hasta ese día a las 6 a. m.

    Devuelve ``None`` si no se entiende.
    """
    texto = " ".join(argumentos).strip().lower().removeprefix("hasta").strip()
    if not texto:
        return PAUSA_INDEFINIDA
    duracion = re.fullmatch(r"(\d+)\s*(h|hora|horas|d|dia|dias|día|días|s|semana|semanas)", texto)
    if duracion:
        cantidad, unidad = int(duracion.group(1)), duracion.group(2)[0]
        if cantidad <= 0:
            return None
        dias = {"h": cantidad / 24, "d": cantidad, "s": cantidad * 7}[unidad]
        return ahora + timedelta(days=dias)
    dia = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", texto)
    if dia:
        d, m, a = int(dia.group(1)), int(dia.group(2)), dia.group(3)
        anio = int(a) + (2000 if a and len(a) == 2 else 0) if a else ahora.year
        try:
            objetivo = datetime(anio, m, d, 6, 0, tzinfo=ahora.tzinfo)
        except ValueError:
            return None
        if objetivo <= ahora and not a:
            objetivo = objetivo.replace(year=anio + 1)
        return objetivo if objetivo > ahora else None
    return None


def _describir_pausa(hasta: str | None) -> str:
    if not hasta:
        return "activo"
    if hasta == PAUSA_INDEFINIDA:
        return "en pausa hasta que escribas /reanudar"
    momento = datetime.fromisoformat(hasta)
    return f"en pausa hasta el {formato_fecha(momento)}, {formato_hora(momento)}"


class BotEdith:
    def __init__(
        self,
        gestor: GestorUsuarios,
        token: str,
        admin_id: int | None = None,
        codigo_invitacion: str | None = None,
    ):
        self.gestor = gestor
        self.cuentas = gestor.cuentas
        self.token = token
        self.admin_id = admin_id
        self.codigo_invitacion = (codigo_invitacion or "").strip() or None
        self.app: Application | None = None
        self._archivos_en_duda: dict[str, tuple[int, dict]] = {}
        self._moodle_en_curso = asyncio.Lock()

    # ------------------------------------------------------------ utilidades

    def es_admin(self, telegram_id: int) -> bool:
        return self.admin_id is not None and telegram_id == self.admin_id

    async def enviar(self, telegram_id: int, texto: str, botones: list[list[InlineKeyboardButton]] | None = None) -> None:
        """Envía un mensaje a un estudiante; lo usan las rutinas, los avisos y los botones."""
        if self.app is None:
            registro.info("Bot sin iniciar; mensaje omitido: %s", texto[:60])
            return
        partes = _trozos(texto)
        try:
            for i, parte in enumerate(partes):
                marcado = InlineKeyboardMarkup(botones) if botones and i == len(partes) - 1 else None
                await self.app.bot.send_message(telegram_id, parte, reply_markup=marcado)
        except Forbidden:
            # El estudiante bloqueó el bot: se pausa para no seguir intentándolo.
            registro.info("El usuario %s bloqueó el bot; queda en pausa", telegram_id)
            self.cuentas.actualizar(telegram_id, pausado_hasta=PAUSA_INDEFINIDA)

    async def _en_hilo(self, sesion: Sesion, funcion, *argumentos):
        async with sesion.candado:
            return await asyncio.to_thread(funcion, *argumentos)

    async def _sesion_activa(self, update: Update) -> Sesion | None:
        """Sesión del estudiante si ya terminó el registro; si no, le indica cómo empezar."""
        telegram_id = update.effective_user.id
        usuario = self.cuentas.obtener(telegram_id)
        if usuario and usuario["estado"] == "activo":
            return self.gestor.sesion(telegram_id)
        mensaje = update.effective_message
        if mensaje:
            if usuario and usuario["paso"]:
                await mensaje.reply_text("Terminemos primero tu registro. Responde la pregunta anterior o escribe /start.")
            else:
                await mensaje.reply_text("Hola. Para empezar escribe /start y te configuro en un minuto.")
        return None

    def _en_pausa(self, usuario: dict) -> bool:
        return self.cuentas.en_pausa(usuario, self.gestor.ahora())

    # ---------------------------------------------------------------- registro

    async def cmd_start(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        usuario_tg = update.effective_user
        usuario = self.cuentas.obtener(usuario_tg.id)
        if usuario and usuario["estado"] == "activo":
            sesion = self.gestor.sesion(usuario_tg.id)
            materias = ", ".join(m["nombre"] for m in sesion.ctx.repo.materias_activas())
            await update.message.reply_text(
                f"Hola de nuevo. Sigues en semestre {usuario['semestre']}: {materias}.\n\n{AYUDA}"
            )
            return
        if usuario is None:
            usuario = self.cuentas.crear(usuario_tg.id, usuario_tg.full_name or "", self.gestor.ahora())
        await update.message.reply_text(
            "Hola, soy E.D.I.T.H. Dirijo tu semestre: vigilo Moodle por ti, planeo tus entregas, "
            "estimo cuánto te demoras de verdad, ordeno tus archivos y corrijo el plan cuando te atrasas."
        )
        if self.codigo_invitacion and usuario["paso"] in (None, "codigo"):
            self.cuentas.actualizar(usuario_tg.id, paso="codigo")
            await update.message.reply_text("Este bot está en prueba. Escribe el código de invitación:")
            return
        await self._pedir_consentimiento(usuario_tg.id)

    async def _pedir_consentimiento(self, telegram_id: int) -> None:
        self.cuentas.actualizar(telegram_id, paso="consentimiento")
        await self.enviar(telegram_id, POLITICA, [[
            InlineKeyboardButton("Acepto", callback_data="reg:acepto"),
            InlineKeyboardButton("No acepto", callback_data="reg:no"),
        ]])

    async def _pedir_semestre(self, telegram_id: int) -> None:
        self.cuentas.actualizar(telegram_id, paso="semestre")
        filas = [
            [InlineKeyboardButton(str(n), callback_data=f"reg:sem:{n}") for n in range(inicio, inicio + 5)]
            for inicio in (1, 6)
        ]
        await self.enviar(telegram_id, "¿En qué semestre estás? (Ingeniería de Sistemas)", filas)

    async def _pedir_usuario_moodle(self, telegram_id: int, en_registro: bool) -> None:
        self.cuentas.actualizar(telegram_id, paso="moodle_usuario", dato_temporal=None)
        botones = [[InlineKeyboardButton(
            "Conectar después" if en_registro else "Cancelar", callback_data="reg:sin_moodle"
        )]]
        await self.enviar(
            telegram_id,
            f"Conectemos tu Moodle ({self.gestor.moodle_url}) para avisarte de tareas nuevas, "
            "cambios de fecha y vencimientos.\n\nEscribe tu usuario de Moodle:",
            botones,
        )

    async def _paso_de_registro(self, update: Update, usuario: dict) -> None:
        """Atiende una respuesta escrita durante el registro o la conexión con Moodle."""
        telegram_id = usuario["telegram_id"]
        texto = (update.message.text or "").strip()
        paso = usuario["paso"]

        if paso == "codigo":
            if texto == self.codigo_invitacion:
                await self._pedir_consentimiento(telegram_id)
            else:
                await update.message.reply_text("Ese código no es. Pídeselo a quien te invitó y escríbelo de nuevo.")
        elif paso in ("consentimiento", "semestre"):
            await update.message.reply_text("Usa los botones del mensaje anterior para responder.")
        elif paso == "moodle_usuario":
            self.cuentas.actualizar(telegram_id, paso="moodle_clave", dato_temporal=texto)
            await update.message.reply_text(
                "Ahora tu contraseña de Moodle. La uso una sola vez para pedirle a Moodle un permiso "
                "de acceso, no la guardo y borro tu mensaje apenas la lea."
            )
        elif paso == "moodle_clave":
            await self._conectar_moodle(update, usuario, texto)

    async def _conectar_moodle(self, update: Update, usuario: dict, clave: str) -> None:
        telegram_id = usuario["telegram_id"]
        try:
            await update.message.delete()
        except TelegramError:
            await update.message.reply_text("No pude borrar tu mensaje con la contraseña; bórralo tú, por favor.")
        usuario_moodle = usuario["dato_temporal"] or ""
        self.cuentas.actualizar(telegram_id, paso=None, dato_temporal=None)
        await update.effective_chat.send_message("Verificando con Moodle...")

        def _pedir_token() -> tuple[str, str]:
            cliente = ClienteMoodle(self.gestor.moodle_url, usuario_moodle, clave, self.gestor.ajustes.zona)
            try:
                return cliente.token, cliente.usuario()
            finally:
                cliente.cerrar()

        try:
            token, nombre = await asyncio.to_thread(_pedir_token)
        except ErrorMoodle as error:
            registro.info("Moodle rechazó la conexión de %s: %s", telegram_id, error)
            en_registro = usuario["estado"] != "activo"
            await self.enviar(telegram_id, "Moodle no aceptó ese usuario y contraseña. Intentemos otra vez.")
            await self._pedir_usuario_moodle(telegram_id, en_registro)
            return

        self.cuentas.guardar_token_moodle(telegram_id, usuario_moodle, token)
        self.gestor.olvidar_cliente_moodle(telegram_id)
        await self.enviar(telegram_id, f"Listo, {nombre or 'conectado'}: tu Moodle quedó conectado.")
        if usuario["estado"] != "activo":
            await self._terminar_registro(telegram_id)
        else:
            await self.vigilar_moodle(telegram_id, forzar=True)

    async def _terminar_registro(self, telegram_id: int) -> None:
        self.cuentas.actualizar(telegram_id, estado="activo", paso=None, dato_temporal=None)
        sesion = self.gestor.sesion(telegram_id)
        materias = "\n".join(f"• {m['nombre']}" for m in sesion.ctx.repo.materias_activas())
        ajustes = sesion.ctx.ajustes
        await self.enviar(
            telegram_id,
            f"¡Todo listo! Estas son tus materias:\n{materias}\nSi falta o sobra alguna, dímelo.\n\n"
            f"Cada día te envío el plan a las {formato_hora(ajustes.hora_resumen)} y reviso cómo te fue a las "
            f"{formato_hora(ajustes.hora_revision)}.\n\n{AYUDA}",
        )
        usuario = self.cuentas.obtener(telegram_id)
        if usuario["moodle_estado"] == "conectado":
            await self.vigilar_moodle(telegram_id, forzar=True)

    async def _boton_registro(self, consulta, telegram_id: int, resto: str) -> None:
        usuario = self.cuentas.obtener(telegram_id)
        if usuario is None:
            await consulta.edit_message_text("Escribe /start para empezar.")
            return
        if resto == "acepto" and usuario["paso"] == "consentimiento":
            self.cuentas.actualizar(telegram_id, acepto_datos=self.gestor.ahora().isoformat())
            await consulta.edit_message_text("Gracias. Empecemos.")
            await self._pedir_semestre(telegram_id)
        elif resto == "no":
            self.cuentas.eliminar(telegram_id)
            await consulta.edit_message_text(
                "Entendido, no guardé nada. Si cambias de opinión, escribe /start."
            )
        elif resto.startswith("sem:") and usuario["paso"] == "semestre":
            semestre = int(resto.split(":")[1])
            self.cuentas.actualizar(telegram_id, semestre=semestre)
            await consulta.edit_message_text(f"Semestre {semestre}.")
            if self.gestor.moodle_url:
                await self._pedir_usuario_moodle(telegram_id, en_registro=True)
            else:
                await self._terminar_registro(telegram_id)
        elif resto == "sin_moodle" and usuario["paso"] in ("moodle_usuario", "moodle_clave"):
            self.cuentas.actualizar(telegram_id, paso=None, dato_temporal=None)
            if usuario["estado"] != "activo":
                await consulta.edit_message_text("Sin problema; conéctalo cuando quieras con /conectar_moodle.")
                await self._terminar_registro(telegram_id)
            else:
                await consulta.edit_message_text("Cancelado.")
        else:
            await consulta.edit_message_text("Ese paso ya quedó atrás.")

    async def cmd_cancelar(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        usuario = self.cuentas.obtener(update.effective_user.id)
        if usuario and usuario["paso"] in ("moodle_usuario", "moodle_clave"):
            self.cuentas.actualizar(usuario["telegram_id"], paso=None, dato_temporal=None)
            if usuario["estado"] != "activo":
                await self._terminar_registro(usuario["telegram_id"])
                return
        await update.message.reply_text("Cancelado.")

    # --------------------------------------------------------- comandos de cuenta

    async def cmd_conectar_moodle(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._sesion_activa(update):
            return
        if not self.gestor.moodle_url:
            await update.message.reply_text("Este servidor no tiene Moodle configurado (falta MOODLE_URL).")
            return
        await self._pedir_usuario_moodle(update.effective_user.id, en_registro=False)

    async def cmd_desconectar_moodle(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._sesion_activa(update):
            return
        telegram_id = update.effective_user.id
        self.cuentas.olvidar_moodle(telegram_id)
        self.gestor.olvidar_cliente_moodle(telegram_id)
        await update.message.reply_text("Desconecté tu Moodle y borré el acceso guardado. Vuelve con /conectar_moodle.")

    async def cmd_pausar(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._sesion_activa(update):
            return
        hasta = interpretar_pausa(context.args or [], self.gestor.ahora())
        if hasta is None:
            await update.message.reply_text(
                "No entendí hasta cuándo. Ejemplos: /pausar · /pausar 3h · /pausar 2d · /pausar 1s · /pausar hasta 30/11"
            )
            return
        valor = hasta if isinstance(hasta, str) else hasta.isoformat()
        self.cuentas.actualizar(update.effective_user.id, pausado_hasta=valor)
        await update.message.reply_text(
            f"Listo, quedo {_describir_pausa(valor)}. No te envío resúmenes, revisiones ni avisos de Moodle; "
            "si me escribes, te respondo igual. Al volver te cuento lo que haya pasado en Moodle."
        )

    async def cmd_reanudar(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._sesion_activa(update):
            return
        await self._reanudar(update.effective_user.id, "De vuelta. Reviso Moodle por si pasó algo mientras tanto.")

    async def _reanudar(self, telegram_id: int, mensaje: str) -> None:
        self.cuentas.actualizar(telegram_id, pausado_hasta=None)
        await self.enviar(telegram_id, mensaje)
        usuario = self.cuentas.obtener(telegram_id)
        if usuario["moodle_estado"] == "conectado":
            await self.vigilar_moodle(telegram_id, forzar=True)

    async def cmd_estado(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        sesion = await self._sesion_activa(update)
        if not sesion:
            return
        usuario = self.cuentas.obtener(sesion.telegram_id)
        ajustes = sesion.ctx.ajustes
        moodle = {
            "conectado": f"conectado como {usuario['moodle_usuario']}",
            "invalido": "el acceso dejó de funcionar; reconéctalo con /conectar_moodle",
        }.get(usuario["moodle_estado"], "sin conectar (/conectar_moodle)")
        ultima = sesion.ctx.repo.preferencia(PREFERENCIA_ULTIMA_REVISION)
        if ultima:
            momento = fecha(ultima)
            moodle += f"\n  última revisión: {formato_fecha(momento)}, {formato_hora(momento)}"
        hoy = self.gestor.ahora().date().isoformat()
        llm = getattr(self.gestor.llm, "nombre", None) or "sin LLM"
        await update.message.reply_text(
            f"Estado: {_describir_pausa(usuario['pausado_hasta'])}\n"
            f"Semestre: {usuario['semestre']}\n"
            f"Moodle: {moodle}\n"
            f"Resumen diario: {formato_hora(ajustes.hora_resumen)} · revisión: {formato_hora(ajustes.hora_revision)}\n"
            f"Avisos de Moodle: de {formato_hora(ajustes.moodle_horario_inicio)} a "
            f"{formato_hora(ajustes.moodle_horario_fin)}\n"
            f"Estudio máximo: {formato_horas(ajustes.reglas.horas_max_dia)} al día\n"
            f"Mensajes hoy: {self.cuentas.mensajes_del_dia(sesion.telegram_id, hoy)} de "
            f"{self.gestor.ajustes.limite_mensajes_dia} · modelo: {llm}"
        )

    # ajustes -------------------------------------------------------------------

    OPCIONES_AJUSTES = {
        "horas_max_dia": ("Horas de estudio por día", [("2 h", "2"), ("3 h", "3"), ("4 h", "4"), ("5 h", "5"), ("6 h", "6")]),
        "hora_resumen": ("Hora del resumen", [("6 a. m.", "06:00"), ("7 a. m.", "07:00"), ("8 a. m.", "08:00"), ("9 a. m.", "09:00")]),
        "hora_revision": ("Hora de la revisión", [("8 p. m.", "20:00"), ("9 p. m.", "21:00"), ("10 p. m.", "22:00"), ("11 p. m.", "23:00")]),
        "moodle_horario": ("Horario de avisos de Moodle", [("6 a. m.–10 p. m.", "06:00-22:00"), ("7 a. m.–9 p. m.", "07:00-21:00"), ("8 a. m.–8 p. m.", "08:00-20:00")]),
        "moodle_contenido_nuevo": ("Avisos de contenido nuevo", [("Sí", "1"), ("No", "0")]),
    }

    async def cmd_ajustes(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._sesion_activa(update):
            return
        botones = [
            [InlineKeyboardButton(titulo, callback_data=f"aj:menu:{clave}")]
            for clave, (titulo, _) in self.OPCIONES_AJUSTES.items()
        ]
        await update.message.reply_text("¿Qué quieres cambiar? (/estado muestra cómo está todo ahora)",
                                        reply_markup=InlineKeyboardMarkup(botones))

    async def _boton_ajustes(self, consulta, telegram_id: int, resto: str) -> None:
        accion, _, valor = resto.partition(":")
        if accion == "menu":
            titulo, opciones = self.OPCIONES_AJUSTES[valor]
            botones = [[InlineKeyboardButton(etiqueta, callback_data=f"aj:{valor}:{dato}") for etiqueta, dato in opciones]]
            await consulta.edit_message_text(f"{titulo}:", reply_markup=InlineKeyboardMarkup(botones))
            return
        if accion not in self.OPCIONES_AJUSTES:
            return
        if accion == "moodle_horario":
            inicio, fin = valor.split("-")
            self.cuentas.fijar_ajuste(telegram_id, "moodle_horario_inicio", inicio)
            self.cuentas.fijar_ajuste(telegram_id, "moodle_horario_fin", fin)
        else:
            dato = bool(int(valor)) if accion == "moodle_contenido_nuevo" else valor
            self.cuentas.fijar_ajuste(telegram_id, accion, dato)
        self.gestor.refrescar(telegram_id)
        etiqueta = dict((d, e) for e, d in self.OPCIONES_AJUSTES[accion][1])[valor]
        await consulta.edit_message_text(f"Listo: {self.OPCIONES_AJUSTES[accion][0].lower()} → {etiqueta}.")

    # datos ---------------------------------------------------------------------

    async def cmd_calendario(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        sesion = await self._sesion_activa(update)
        if not sesion:
            return
        contenido = await self._en_hilo(sesion, calendario_ics, sesion.ctx)
        await update.message.reply_document(
            contenido.encode("utf-8"),
            filename="edith.ics",
            caption=(
                "Tu plan de los próximos 60 días: bloques de estudio, entregas y vencimientos de Moodle. "
                "Ábrelo o impórtalo en Google Calendar (Configuración → Importar) u Outlook. "
                "Pídemelo de nuevo cuando cambie el plan."
            ),
        )

    async def cmd_mis_datos(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        sesion = await self._sesion_activa(update)
        if not sesion:
            return
        carpeta = self.gestor.carpeta(sesion.telegram_id)
        await update.message.reply_text("Preparando tus datos...")
        with tempfile.TemporaryDirectory() as temporal:
            async with sesion.candado:
                archivo = await asyncio.to_thread(
                    shutil.make_archive, str(Path(temporal) / "edith_mis_datos"), "zip", carpeta
                )
            if Path(archivo).stat().st_size > LIMITE_ARCHIVO:
                archivo = str(carpeta / "edith.db")
                await update.message.reply_text("Tus archivos superan el límite de Telegram; te envío solo la base de datos.")
            with open(archivo, "rb") as datos:
                await update.message.reply_document(
                    datos, filename=Path(archivo).name,
                    caption="Todo lo que guardo de ti: tu base de datos (SQLite) y tus archivos organizados.",
                )

    async def cmd_olvidarme(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.cuentas.obtener(update.effective_user.id):
            await update.message.reply_text("No tengo datos tuyos.")
            return
        await update.message.reply_text(
            "Voy a borrar tu cuenta, tus entregas, tu plan, tus archivos y el acceso a Moodle. "
            "No se puede deshacer. ¿Seguro?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Sí, borrar todo", callback_data="olv:si"),
                InlineKeyboardButton("No", callback_data="olv:no"),
            ]]),
        )

    async def _boton_olvidar(self, consulta, telegram_id: int, resto: str) -> None:
        if resto != "si":
            await consulta.edit_message_text("No borré nada.")
            return
        sesion = self.gestor._sesiones.get(telegram_id)
        if sesion:
            async with sesion.candado:
                self.gestor.borrar(telegram_id)
        else:
            self.gestor.borrar(telegram_id)
        await consulta.edit_message_text("Listo, borré todo lo tuyo. Si algún día quieres volver, escribe /start.")

    # ------------------------------------------------------- comandos del semestre

    async def cmd_ayuda(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        texto = AYUDA
        if self.es_admin(update.effective_user.id):
            texto += "\n\nAdministración:\n/admin — uso del bot\n/anuncio TEXTO — mensaje a todos\n/reentrenar — reentrenar los modelos"
        await update.message.reply_text(texto)

    async def cmd_semestre(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        sesion = await self._sesion_activa(update)
        if not sesion:
            return
        try:
            semestre = int(context.args[0])
            if not 1 <= semestre <= 10:
                raise ValueError
        except (IndexError, ValueError):
            await update.message.reply_text("Dime el semestre con un número del 1 al 10. Ejemplo: /semestre 7")
            return
        self.cuentas.actualizar(sesion.telegram_id, semestre=semestre)
        self.gestor.refrescar(sesion.telegram_id)
        activas = await self._en_hilo(sesion, sesion.ctx.repo.activar_semestre, semestre)
        await update.message.reply_text(
            f"Listo, semestre {semestre}. Materias activas:\n" + "\n".join(f"• {m['nombre']}" for m in activas)
        )

    async def cmd_materias(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        sesion = await self._sesion_activa(update)
        if sesion:
            activas = sesion.ctx.repo.materias_activas()
            await update.message.reply_text("Materias activas:\n" + "\n".join(f"• {m['nombre']}" for m in activas))

    async def cmd_hoy(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        sesion = await self._sesion_activa(update)
        if sesion:
            await update.message.reply_text(await self._en_hilo(sesion, soporte.resumen_del_dia, sesion.ctx))

    async def cmd_pendientes(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        sesion = await self._sesion_activa(update)
        if not sesion:
            return
        texto = await self._en_hilo(sesion, soporte.texto_pendientes, sesion.ctx)
        for parte in _trozos(texto):
            await update.message.reply_text(parte)

    async def cmd_revisar_dia(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        sesion = await self._sesion_activa(update)
        if sesion:
            await self.revision_del_dia(sesion.telegram_id)

    async def cmd_moodle(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """Revisión manual: no respeta el horario ni la pausa porque el estudiante la pidió."""
        sesion = await self._sesion_activa(update)
        if not sesion:
            return
        usuario = self.cuentas.obtener(sesion.telegram_id)
        if usuario["moodle_estado"] != "conectado":
            await update.message.reply_text("Aún no tengo acceso a tu Moodle. Conéctalo con /conectar_moodle.")
            return
        await context_typing(update)
        if not await self.vigilar_moodle(sesion.telegram_id, forzar=True):
            await update.message.reply_text("Revisé Moodle: sin novedades.")

    # -------------------------------------------------------------- administración

    async def cmd_admin(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.es_admin(update.effective_user.id):
            return
        ahora = self.gestor.ahora()
        todos = self.cuentas.todos()
        activos = [u for u in todos if u["estado"] == "activo"]
        hoy = ahora.date().isoformat()
        mensajes = sum(self.cuentas.mensajes_del_dia(u["telegram_id"], hoy) for u in todos)
        await update.message.reply_text(
            f"Usuarios: {len(activos)} activos, {len(todos) - len(activos)} en registro\n"
            f"En pausa: {sum(1 for u in activos if self._en_pausa(u))}\n"
            f"Moodle conectado: {sum(1 for u in activos if u['moodle_estado'] == 'conectado')}"
            f" · con acceso vencido: {sum(1 for u in activos if u['moodle_estado'] == 'invalido')}\n"
            f"Mensajes al LLM hoy: {mensajes}\n"
            f"LLM: {getattr(self.gestor.llm, 'nombre', 'ninguno')} ({getattr(self.gestor.llm, 'modelo', '-')})"
        )

    async def cmd_anuncio(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.es_admin(update.effective_user.id):
            return
        texto = " ".join(context.args or []).strip()
        if not texto:
            await update.message.reply_text("Uso: /anuncio TEXTO")
            return
        activos = self.cuentas.activos()
        for usuario in activos:
            await self.enviar(usuario["telegram_id"], f"📣 {texto}")
        await update.message.reply_text(f"Enviado a {len(activos)} usuarios.")

    async def cmd_reentrenar(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.es_admin(update.effective_user.id):
            await update.message.reply_text("Los modelos se reentrenan solos cada semana con lo que aprendo de todos.")
            return
        await update.message.reply_text("Reentrenando con los datos de todos...")
        await self.reentreno()

    # ----------------------------------------------------------- conversación

    async def al_recibir_texto(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        telegram_id = update.effective_user.id
        usuario = self.cuentas.obtener(telegram_id)
        if usuario and usuario["paso"]:
            await self._paso_de_registro(update, usuario)
            return
        sesion = await self._sesion_activa(update)
        if not sesion:
            return
        if sesion.agente is None:
            await update.message.reply_text("Ahora mismo no tengo modelo de lenguaje; funcionan los comandos (/ayuda).")
            return
        ahora = self.gestor.ahora()
        hoy = ahora.date().isoformat()
        limite = self.gestor.ajustes.limite_mensajes_dia
        if self.cuentas.mensajes_del_dia(telegram_id, hoy) >= limite and not self.es_admin(telegram_id):
            await update.message.reply_text(
                f"Por hoy llegaste al límite de {limite} mensajes conversados (es una prueba gratuita). "
                "Los comandos siguen funcionando: /hoy, /pendientes, /moodle."
            )
            return
        self.cuentas.sumar_mensaje(telegram_id, hoy)
        self.cuentas.actualizar(telegram_id, ultimo_mensaje=ahora.isoformat())
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        try:
            respuesta = await self._en_hilo(sesion, sesion.agente.responder, update.message.text)
        except ErrorLLM as error:
            registro.error("LLM no disponible: %s", error)
            respuesta = (
                "Ahora mismo no puedo pensar: el modelo de lenguaje no respondió. "
                "Intenta de nuevo en un minuto. Mientras tanto funcionan /hoy, /pendientes y /moodle."
            )
        for parte in _trozos(respuesta):
            await update.message.reply_text(parte)

    async def al_recibir_documento(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        sesion = await self._sesion_activa(update)
        if not sesion:
            return
        documento = update.message.document
        nombre = Path(documento.file_name or "archivo").name
        if Path(nombre).suffix.lower() not in EXTENSIONES:
            await update.message.reply_text("Solo sé organizar PDF, Word, PowerPoint y texto.")
            return
        if (documento.file_size or 0) > 20 * 1024 * 1024:
            await update.message.reply_text("Telegram no me deja descargar archivos de más de 20 MB.")
            return
        entrada = sesion.ctx.ajustes.carpeta_destino / "_Entrada"
        entrada.mkdir(parents=True, exist_ok=True)
        destino = entrada / nombre
        archivo = await documento.get_file()
        await archivo.download_to_drive(destino)
        await self.procesar_archivo(sesion.telegram_id, destino)

    # --------------------------------------------------------------- archivos

    async def procesar_archivo(self, telegram_id: int, ruta: Path) -> None:
        """Clasifica un archivo nuevo y avisa dónde quedó, o pregunta si hay duda."""
        sesion = self.gestor.sesion(telegram_id)
        desempate = sesion.agente.desempatar_archivo if sesion.agente else None
        try:
            resultado = await self._en_hilo(sesion, clasificar_archivo, sesion.ctx, ruta, desempate)
        except ErrorSkill as error:
            await self.enviar(telegram_id, f"No pude organizar {ruta.name}: {error}")
            return
        if resultado["estado"] == "movido":
            await self.enviar(telegram_id, self._texto_movido(resultado))
            return
        clave = uuid.uuid4().hex[:8]
        self._archivos_en_duda[clave] = (telegram_id, resultado)
        botones = [
            [InlineKeyboardButton(materia, callback_data=f"a:{clave}:{i}")]
            for i, materia in enumerate(resultado["candidatas"])
        ]
        botones.append([InlineKeyboardButton("No es de la universidad", callback_data=f"a:{clave}:x")])
        await self.enviar(telegram_id, f"Llegó {resultado['archivo']} y no estoy segura de la materia. ¿De cuál es?", botones)

    @staticmethod
    def _texto_movido(resultado: dict) -> str:
        lugar = resultado["materia"] + (f" / {resultado['entrega']}" if resultado["entrega"] else "")
        return f"Moví {resultado['archivo']} a {lugar}."

    # ---------------------------------------------------------------- rutinas

    async def revision_del_dia(self, telegram_id: int) -> None:
        """Pregunta por los bloques de hoy sin marcar y luego reorganiza."""
        sesion = self.gestor.sesion(telegram_id)
        ctx = sesion.ctx
        ahora = ctx.ahora()
        sin_marcar = [
            b for b in soporte.bloques_del_dia(ctx)
            if b["estado"] == "pendiente" and fecha(b["fin"]) <= ahora
        ]
        if not sin_marcar:
            await self.reorganizar(telegram_id)
            return
        await self.enviar(telegram_id, "Revisión del día. ¿Cómo te fue con estos bloques?")
        for b in sin_marcar:
            inicio, fin = fecha(b["inicio"]), fecha(b["fin"])
            await self.enviar(
                telegram_id,
                f"{formato_hora(inicio)} a {formato_hora(fin)}: {b['subtarea']} ({b['materia']})",
                [self._botones_bloque(b["id"])],
            )
        await self.enviar(
            telegram_id,
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

    async def reorganizar(self, telegram_id: int, horas_extra_dia: float = 0.0) -> None:
        """Ejecuta ``replanificar`` y envía el aviso, con opciones si hay riesgo."""
        sesion = self.gestor.sesion(telegram_id)
        resultado = await self._en_hilo(sesion, replanificar, sesion.ctx, horas_extra_dia)
        if sesion.agente:
            aviso = await self._en_hilo(sesion, sesion.agente.redactar_aviso, resultado)
        else:
            aviso = aviso_de_respaldo(resultado)
        botones = None
        if resultado["hay_riesgo"] and horas_extra_dia < 2:
            botones = [
                [
                    InlineKeyboardButton("Estudiar 1 h más al día", callback_data="x:1"),
                    InlineKeyboardButton("2 h más al día", callback_data="x:2"),
                ],
                [InlineKeyboardButton("Dejarlo así", callback_data="x:0")],
            ]
        await self.enviar(telegram_id, aviso, botones)

    async def reentreno(self, _: ContextTypes.DEFAULT_TYPE | None = None) -> None:
        """Reentrena los modelos compartidos y le cuenta al administrador qué aprendió."""
        try:
            resultado = await asyncio.to_thread(self.gestor.reentrenar_todos)
        except Exception:
            registro.exception("Falló el reentreno")
            if self.admin_id:
                await self.enviar(self.admin_id, "El reentreno semanal falló; revisa los registros.")
            return
        metricas = resultado["predictor"]
        lineas = [
            "Reentreno semanal:",
            f"• Predictor de tiempo: {resultado['filas_de_uso']} registros reales de los estudiantes, "
            f"error medio de {formato_horas(round(metricas['MAE'], 2))} "
            f"(sin modelo sería {formato_horas(round(metricas['MAE_linea_base'], 2))}).",
        ]
        if resultado.get("clasificador"):
            lineas.append(f"• Clasificador de archivos: sumé {resultado['ejemplos_confirmados']} archivos confirmados.")
        registro.info("%s", " ".join(lineas))
        if self.admin_id:
            await self.enviar(self.admin_id, "\n".join(lineas))

    async def vigilar_moodle(self, telegram_id: int, forzar: bool = False) -> bool:
        """Revisa el Moodle de un estudiante y avisa lo nuevo o lo que está por vencer. Devuelve si envió algo.

        Fuera de su horario no consulta nada, salvo que lo pida (``forzar``). Si el
        token dejó de servir, deja de intentarlo y le pide que vuelva a conectar.
        """
        sesion = self.gestor.sesion(telegram_id)
        if not (forzar or dentro_del_horario(sesion.ctx)):
            return False
        try:
            resultado = await self._en_hilo(sesion, revisar_moodle, sesion.ctx)
        except ErrorSkill as error:
            if isinstance(error.__cause__, ErrorSesionMoodle) or "invalidtoken" in str(error).lower():
                self.cuentas.olvidar_moodle(telegram_id, estado="invalido")
                self.gestor.olvidar_cliente_moodle(telegram_id)
                await self.enviar(
                    telegram_id,
                    "Tu acceso a Moodle dejó de funcionar (¿cambiaste la contraseña?). "
                    "Reconéctalo con /conectar_moodle para seguir recibiendo avisos.",
                )
                return True
            registro.warning("Vigilancia de Moodle de %s: %s", telegram_id, error)
            if forzar:
                await self.enviar(telegram_id, str(error))
                return True
            return False
        texto = redactar_aviso_moodle(sesion.ctx, resultado)
        if texto:
            await self.enviar(telegram_id, texto)
        return texto is not None

    # ------------------------------------------------- trabajos programados

    async def tick(self, _: ContextTypes.DEFAULT_TYPE | None = None) -> None:
        """Cada minuto: fin de pausas, resumen de la mañana y revisión de la noche de cada estudiante.

        Lo enviado se anota en la base del estudiante, así un reinicio no lo repite
        y, si el bot estuvo apagado a esa hora, se envía al volver (hasta 2 h tarde).
        """
        ahora = self.gestor.ahora()
        hoy = ahora.date().isoformat()
        for usuario in self.cuentas.activos():
            telegram_id = usuario["telegram_id"]
            try:
                hasta = usuario["pausado_hasta"]
                if hasta and hasta != PAUSA_INDEFINIDA and datetime.fromisoformat(hasta) <= ahora:
                    await self._reanudar(telegram_id, "Terminó tu pausa: vuelvo a enviarte avisos.")
                    continue
                if self._en_pausa(usuario):
                    continue
                sesion = self.gestor.sesion(telegram_id)
                ajustes, repo = sesion.ctx.ajustes, sesion.ctx.repo
                if self._toca(ahora, ajustes.hora_resumen) and repo.preferencia(PREF_ULTIMO_RESUMEN) != hoy:
                    repo.fijar_preferencia(PREF_ULTIMO_RESUMEN, hoy)
                    await self.enviar(telegram_id, await self._en_hilo(sesion, soporte.resumen_del_dia, sesion.ctx))
                if self._toca(ahora, ajustes.hora_revision) and repo.preferencia(PREF_ULTIMA_REVISION_DIA) != hoy:
                    repo.fijar_preferencia(PREF_ULTIMA_REVISION_DIA, hoy)
                    await self.revision_del_dia(telegram_id)
            except Exception:
                registro.exception("Falló la rutina del usuario %s", telegram_id)

    @staticmethod
    def _toca(ahora: datetime, hora: time) -> bool:
        programada = datetime.combine(ahora.date(), hora, ahora.tzinfo)
        return programada <= ahora < programada + VENTANA_RUTINA

    async def vigilar_moodle_todos(self, _: ContextTypes.DEFAULT_TYPE | None = None) -> None:
        """Revisa Moodle de cada estudiante conectado y sin pausa, uno a la vez para no saturar la plataforma."""
        if self._moodle_en_curso.locked():
            return  # la ronda anterior aún no termina
        async with self._moodle_en_curso:
            for usuario in self.cuentas.activos():
                if usuario["moodle_estado"] != "conectado" or self._en_pausa(usuario):
                    continue
                try:
                    await self.vigilar_moodle(usuario["telegram_id"])
                except Exception:
                    registro.exception("Falló la vigilancia de Moodle de %s", usuario["telegram_id"])
                await asyncio.sleep(1)

    # ---------------------------------------------------------------- botones

    async def al_pulsar_boton(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        consulta = update.callback_query
        await consulta.answer()
        telegram_id = consulta.from_user.id
        accion, _, resto = (consulta.data or "").partition(":")
        try:
            if accion == "reg":
                await self._boton_registro(consulta, telegram_id, resto)
                return
            if accion == "olv":
                await self._boton_olvidar(consulta, telegram_id, resto)
                return
            usuario = self.cuentas.obtener(telegram_id)
            if not usuario or usuario["estado"] != "activo":
                await consulta.edit_message_text("Escribe /start para empezar.")
                return
            if accion == "aj":
                await self._boton_ajustes(consulta, telegram_id, resto)
            elif accion == "b":
                await self._boton_bloque(telegram_id, consulta, resto)
            elif accion == "t":
                await self._boton_subtarea(telegram_id, consulta, resto)
            elif accion == "a":
                await self._boton_archivo(telegram_id, consulta, resto)
            elif accion == "rev":
                await consulta.edit_message_text("Reorganizando...")
                await self.reorganizar(telegram_id)
            elif accion == "x":
                await self._boton_riesgo(telegram_id, consulta, resto)
        except ErrorSkill as error:
            await consulta.edit_message_text(str(error))
        except BadRequest as error:
            registro.info("Botón sin efecto: %s", error)  # p. ej. el mensaje ya tenía ese texto

    async def _boton_bloque(self, telegram_id: int, consulta, resto: str) -> None:
        sesion = self.gestor.sesion(telegram_id)
        bloque_id, _, estado = resto.partition(":")
        resultado = await self._en_hilo(sesion, soporte.marcar_bloque, sesion.ctx, int(bloque_id), estado)
        etiqueta = {"hecho": "hecho", "a_medias": "a medias", "perdido": "no hecho"}[estado]
        await consulta.edit_message_text(f"{resultado['subtarea']}: {etiqueta}.")
        if resultado["posiblemente_terminada"]:
            sid = resultado["subtarea_id"]
            await self.enviar(
                telegram_id,
                f"Ya cumpliste las horas previstas de «{resultado['subtarea']}». ¿La terminaste?",
                [
                    [InlineKeyboardButton("Sí, terminada", callback_data=f"t:{sid}:0")],
                    [
                        InlineKeyboardButton("Me falta 1 h", callback_data=f"t:{sid}:1"),
                        InlineKeyboardButton("Me faltan 2 h", callback_data=f"t:{sid}:2"),
                    ],
                ],
            )

    async def _boton_subtarea(self, telegram_id: int, consulta, resto: str) -> None:
        sesion = self.gestor.sesion(telegram_id)
        subtarea_id, _, faltan = resto.partition(":")
        if faltan == "0":
            resultado = await self._en_hilo(sesion, soporte.registrar_avance, sesion.ctx, int(subtarea_id), True)
            await consulta.edit_message_text(f"Anotado: «{resultado['subtarea']}» terminada. {resultado['nota']}")
            return
        await self._en_hilo(sesion, soporte.registrar_avance, sesion.ctx, int(subtarea_id), False, None, float(faltan))
        await consulta.edit_message_text(f"Entendido, le reservo {faltan} h más.")
        await self.reorganizar(telegram_id)

    async def _boton_archivo(self, telegram_id: int, consulta, resto: str) -> None:
        clave, _, opcion = resto.partition(":")
        dueno, pendiente = self._archivos_en_duda.get(clave, (None, None))
        if not pendiente or dueno != telegram_id:
            await consulta.edit_message_text("Ese archivo ya fue atendido.")
            return
        del self._archivos_en_duda[clave]
        if opcion == "x":
            Path(pendiente["ruta"]).unlink(missing_ok=True)
            await consulta.edit_message_text(f"De acuerdo, descarté {pendiente['archivo']}.")
            return
        sesion = self.gestor.sesion(telegram_id)
        materia = pendiente["candidatas"][int(opcion)]
        resultado = await self._en_hilo(sesion, confirmar_archivo, sesion.ctx, pendiente["ruta"], materia)
        await consulta.edit_message_text(self._texto_movido(resultado) + " Lo tendré en cuenta la próxima vez.")

    async def _boton_riesgo(self, telegram_id: int, consulta, resto: str) -> None:
        horas = float(resto)
        if horas == 0:
            await consulta.edit_message_text("De acuerdo, el plan queda como está.")
            return
        await consulta.edit_message_text(f"Subo el tope a {formato_horas(horas)} más por día y reorganizo...")
        await self.reorganizar(telegram_id, horas)

    # ---------------------------------------------------------------- errores

    async def al_fallar(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        registro.error("Error atendiendo una actualización", exc_info=context.error)
        if isinstance(update, Update) and update.effective_chat:
            try:
                await update.effective_chat.send_message("Algo falló de mi lado. Intenta de nuevo en un momento.")
            except TelegramError:
                pass
        if self.admin_id:
            try:
                await self.enviar(self.admin_id, f"⚠️ Error: {type(context.error).__name__}: {str(context.error)[:300]}")
            except TelegramError:
                pass

    # ---------------------------------------------------------------- arranque

    async def al_iniciar(self, app: Application) -> None:
        try:
            await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMANDOS_MENU])
        except TelegramError as error:
            registro.warning("No se pudo publicar el menú de comandos: %s", error)

    def construir(self, con_updater: bool = True) -> Application:
        """Crea la aplicación de Telegram con sus manejadores y trabajos programados.

        Sin ``con_updater`` la aplicación no consulta Telegram por su cuenta: las
        actualizaciones llegan por el webhook (ver ``main.py``).
        """
        constructor = Application.builder().token(self.token).post_init(self.al_iniciar)
        if not con_updater:
            constructor = constructor.updater(None)
        app = constructor.build()
        self.app = app
        privado = filters.ChatType.PRIVATE
        for nombre, manejador in [
            ("start", self.cmd_start), ("ayuda", self.cmd_ayuda), ("help", self.cmd_ayuda),
            ("cancelar", self.cmd_cancelar),
            ("semestre", self.cmd_semestre), ("materias", self.cmd_materias), ("hoy", self.cmd_hoy),
            ("pendientes", self.cmd_pendientes), ("revisar_dia", self.cmd_revisar_dia),
            ("moodle", self.cmd_moodle), ("calendario", self.cmd_calendario),
            ("pausar", self.cmd_pausar), ("reanudar", self.cmd_reanudar), ("estado", self.cmd_estado),
            ("ajustes", self.cmd_ajustes), ("conectar_moodle", self.cmd_conectar_moodle),
            ("desconectar_moodle", self.cmd_desconectar_moodle), ("mis_datos", self.cmd_mis_datos),
            ("olvidarme", self.cmd_olvidarme),
            ("admin", self.cmd_admin), ("anuncio", self.cmd_anuncio), ("reentrenar", self.cmd_reentrenar),
        ]:
            app.add_handler(CommandHandler(nombre, manejador, filters=privado))
        app.add_handler(CallbackQueryHandler(self.al_pulsar_boton))
        app.add_handler(MessageHandler(privado & filters.Document.ALL, self.al_recibir_documento))
        app.add_handler(MessageHandler(privado & filters.TEXT & ~filters.COMMAND, self.al_recibir_texto))
        app.add_error_handler(self.al_fallar)

        ajustes = self.gestor.ajustes
        app.job_queue.run_repeating(self.tick, interval=60, first=15, name="rutinas")
        # python-telegram-bot numera los días de domingo (0) a sábado (6).
        dia = (DIAS_SEMANA.index(ajustes.dia_reentreno) + 1) % 7
        app.job_queue.run_daily(
            self.reentreno, ajustes.hora_reentreno.replace(tzinfo=ajustes.zona), days=(dia,), name="reentreno"
        )
        if ajustes.moodle_vigilancia and self.gestor.moodle_url:
            app.job_queue.run_repeating(
                self.vigilar_moodle_todos, interval=ajustes.moodle_intervalo_minutos * 60, first=45, name="moodle"
            )
            registro.info("Vigilancia de Moodle cada %s min", ajustes.moodle_intervalo_minutos)
        return app


async def context_typing(update: Update) -> None:
    try:
        await update.effective_chat.send_action(ChatAction.TYPING)
    except TelegramError:
        pass


def admin_desde_entorno() -> int | None:
    valor = os.getenv("TELEGRAM_ADMIN_ID") or os.getenv("TELEGRAM_CHAT_ID")
    return int(valor) if valor and valor.strip().lstrip("-").isdigit() else None

