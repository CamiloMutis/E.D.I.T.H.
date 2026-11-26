"""Pruebas del modo de varios estudiantes: cuentas, registro guiado, pausa, rutinas, ajustes y privacidad."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import time, timedelta
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet

import integraciones.telegram_bot as modulo_bot
from agente.llm import RespuestaLLM
from db.cuentas import PAUSA_INDEFINIDA, Cifrador, Cuentas
from db.repositorio import Repositorio
from integraciones.moodle import ClienteMoodle
from integraciones.telegram_bot import interpretar_pausa
from skills import soporte
from skills.exportar_calendario import calendario_ics
from tests.conftest import AHORA, RAIZ, momento, registrar
from tests.test_integraciones import ConsultaFalsa, _con_entrega, bot_de_prueba
from tests.test_moodle import EVENTOS, URL, MoodleFalso
from usuarios import migrar_instalacion_anterior

# --- Telegram falso --------------------------------------------------------------


class MensajeFalso:
    def __init__(self, texto: str = ""):
        self.text = texto
        self.respuestas: list[str] = []
        self.documentos: list[tuple[str, bytes]] = []
        self.borrado = False

    async def reply_text(self, texto, reply_markup=None):
        self.respuestas.append(texto)

    async def reply_document(self, documento, filename=None, caption=None):
        datos = documento if isinstance(documento, bytes) else documento.read()
        self.documentos.append((filename, datos))

    async def delete(self):
        self.borrado = True


class ChatFalso:
    def __init__(self, id_: int):
        self.id = id_
        self.mensajes: list[str] = []

    async def send_message(self, texto, reply_markup=None):
        self.mensajes.append(texto)

    async def send_action(self, _):
        pass


def update(telegram_id: int, texto: str = "") -> SimpleNamespace:
    mensaje = MensajeFalso(texto)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=telegram_id, full_name="Ana Pérez"),
        message=mensaje,
        effective_message=mensaje,
        effective_chat=ChatFalso(telegram_id),
    )


def contexto(*args: str) -> SimpleNamespace:
    async def send_chat_action(*_):
        pass

    return SimpleNamespace(args=list(args), bot=SimpleNamespace(send_chat_action=send_chat_action))


def textos(bot, telegram_id: int) -> list[str]:
    return [texto for destino, texto, _ in bot.enviados if destino == telegram_id]


@pytest.fixture
def bot(gestor):
    return bot_de_prueba(gestor)


@pytest.fixture
def servidor(gestor, monkeypatch):
    """Moodle falso para el registro: la clave correcta es cualquiera salvo 'mala'."""
    servidor = MoodleFalso(eventos=list(EVENTOS))

    def cliente_con_clave(url, usuario, clave, zona):
        servidor.token_valido = clave != "mala"
        return ClienteMoodle(URL, usuario, clave, zona, transporte=httpx.MockTransport(servidor))

    monkeypatch.setattr(modulo_bot, "ClienteMoodle", cliente_con_clave)
    monkeypatch.setattr(
        gestor, "_cliente_moodle",
        lambda tid: ClienteMoodle.con_token(URL, gestor.cuentas.token_moodle(tid), gestor.ajustes.zona,
                                            httpx.MockTransport(servidor)),
    )
    return servidor


# --- Cifrado y cuentas -------------------------------------------------------------


def test_el_token_se_guarda_cifrado(gestor):
    gestor.cuentas.crear(5, "Ana", AHORA)
    gestor.cuentas.guardar_token_moodle(5, "ana", "tok-secreto")
    crudo = sqlite3.connect(gestor.cuentas.ruta).execute("SELECT moodle_token FROM usuarios").fetchone()[0]
    assert "tok-secreto" not in crudo
    assert gestor.cuentas.token_moodle(5) == "tok-secreto"


def test_sin_clave_maestra_se_crea_una_privada_y_se_reutiliza(tmp_path, monkeypatch):
    monkeypatch.delenv("EDITH_CLAVE_MAESTRA", raising=False)
    ruta = tmp_path / "clave_maestra"
    cifrado = Cifrador(ruta_clave=ruta).cifrar("hola")
    if os.name != "nt":   # en Windows los permisos no son de tipo Unix
        assert ruta.stat().st_mode & 0o777 == 0o600
    assert Cifrador(ruta_clave=ruta).descifrar(cifrado) == "hola"
    assert Cifrador(Fernet.generate_key()).descifrar(cifrado) is None   # con otra clave no se lee


def test_campos_no_editables_se_rechazan(gestor):
    gestor.cuentas.crear(5, "Ana", AHORA)
    with pytest.raises(ValueError):
        gestor.cuentas.actualizar(5, ajustes="{}")


@pytest.mark.parametrize("texto, esperado", [
    ("", PAUSA_INDEFINIDA),
    ("3h", AHORA + timedelta(hours=3)),
    ("2d", AHORA + timedelta(days=2)),
    ("1 semana", AHORA + timedelta(days=7)),
    ("hasta 30/11", momento(30, 6)),
    ("20/11/2026", momento(20, 6)),
    ("mañana", None),
    ("0d", None),
    ("31/02", None),
])
def test_interpretar_pausa(texto, esperado):
    assert interpretar_pausa(texto.split(), AHORA) == esperado


# --- Registro guiado ---------------------------------------------------------------


def _registrarse(bot, clave: str = "buena"):
    asyncio.run(bot.cmd_start(update(42), contexto()))
    asyncio.run(bot._boton_registro(ConsultaFalsa(), 42, "acepto"))
    asyncio.run(bot._boton_registro(ConsultaFalsa(), 42, "sem:7"))
    asyncio.run(bot.al_recibir_texto(update(42, "1110123"), contexto()))
    mensaje_clave = update(42, clave)
    asyncio.run(bot.al_recibir_texto(mensaje_clave, contexto()))
    return mensaje_clave


def test_registro_completo_guarda_solo_el_token_y_borra_la_clave(bot, gestor, servidor):
    mensaje_clave = _registrarse(bot)

    usuario = gestor.cuentas.obtener(42)
    assert usuario["estado"] == "activo" and usuario["semestre"] == 7 and usuario["acepto_datos"]
    assert usuario["moodle_estado"] == "conectado" and usuario["moodle_usuario"] == "1110123"
    assert gestor.cuentas.token_moodle(42) == "tok123"
    assert usuario["dato_temporal"] is None and usuario["paso"] is None
    assert mensaje_clave.message.borrado
    assert "buena" not in open(gestor.cuentas.ruta, "rb").read().decode("latin-1")
    enviados = textos(bot, 42)
    assert any("ESTUDIANTE DE PRUEBA: tu Moodle quedó conectado" in t for t in enviados)
    assert any("Todo listo" in t for t in enviados)
    assert "Empecé a vigilar Moodle" in enviados[-1]   # primera revisión, sin anunciar como nuevo


def test_clave_equivocada_vuelve_a_pedir_el_usuario(bot, gestor, servidor):
    _registrarse(bot, clave="mala")
    usuario = gestor.cuentas.obtener(42)
    assert usuario["estado"] == "registro" and usuario["paso"] == "moodle_usuario"
    assert gestor.cuentas.token_moodle(42) is None
    assert any("no aceptó" in t for t in textos(bot, 42))


def test_se_puede_terminar_el_registro_sin_moodle(bot, gestor):
    asyncio.run(bot.cmd_start(update(42), contexto()))
    asyncio.run(bot._boton_registro(ConsultaFalsa(), 42, "acepto"))
    asyncio.run(bot._boton_registro(ConsultaFalsa(), 42, "sem:3"))
    asyncio.run(bot._boton_registro(ConsultaFalsa(), 42, "sin_moodle"))
    usuario = gestor.cuentas.obtener(42)
    assert usuario["estado"] == "activo" and usuario["moodle_estado"] == "sin_conectar"
    assert all(m["semestre"] == 3 for m in gestor.sesion(42).ctx.repo.materias_activas())


def test_no_aceptar_no_guarda_nada(bot, gestor):
    asyncio.run(bot.cmd_start(update(42), contexto()))
    asyncio.run(bot._boton_registro(ConsultaFalsa(), 42, "no"))
    assert gestor.cuentas.obtener(42) is None


def test_codigo_de_invitacion(gestor):
    bot = bot_de_prueba(gestor)
    bot.codigo_invitacion = "USC2026"
    asyncio.run(bot.cmd_start(update(42), contexto()))
    asyncio.run(bot.al_recibir_texto(equivocado := update(42, "otro"), contexto()))
    assert gestor.cuentas.obtener(42)["paso"] == "codigo" and "no es" in equivocado.message.respuestas[0]
    asyncio.run(bot.al_recibir_texto(update(42, "USC2026"), contexto()))
    assert gestor.cuentas.obtener(42)["paso"] == "consentimiento"


def test_sin_registro_no_hay_acceso(bot, gestor):
    pedido = update(99, "¿qué hago hoy?")
    asyncio.run(bot.al_recibir_texto(pedido, contexto()))
    assert "/start" in pedido.message.respuestas[0]
    asyncio.run(bot.cmd_hoy(pedido, contexto()))
    assert gestor.cuentas.obtener(99) is None


# --- Pausa y rutinas ---------------------------------------------------------------


def test_el_resumen_llega_una_vez_a_su_hora(bot, gestor):
    registrar(gestor, 42)
    gestor.reloj.actual = momento(16, 6, 59)
    asyncio.run(bot.tick())
    assert textos(bot, 42) == []
    gestor.reloj.actual = momento(16, 7, 1)
    asyncio.run(bot.tick())
    asyncio.run(bot.tick())
    assert len(textos(bot, 42)) == 1
    gestor.reloj.actual = momento(17, 9, 30)    # el bot estuvo apagado a las 7 y volvió tarde
    asyncio.run(bot.tick())
    assert len(textos(bot, 42)) == 1


def test_cada_estudiante_recibe_el_resumen_a_su_hora(bot, gestor):
    registrar(gestor, 42)
    registrar(gestor, 77)
    gestor.cuentas.fijar_ajuste(77, "hora_resumen", "08:00")
    gestor.refrescar(77)
    gestor.reloj.actual = momento(16, 7, 5)
    asyncio.run(bot.tick())
    assert len(textos(bot, 42)) == 1 and textos(bot, 77) == []
    gestor.reloj.actual = momento(16, 8, 5)
    asyncio.run(bot.tick())
    assert len(textos(bot, 77)) == 1


def test_pausar_detiene_las_rutinas_y_al_vencer_avisa(bot, gestor):
    registrar(gestor, 42)
    pedido = update(42)
    asyncio.run(bot.cmd_pausar(pedido, contexto("2d")))
    assert "en pausa hasta el mié 18 nov" in pedido.message.respuestas[0]

    gestor.reloj.actual = momento(17, 7, 5)
    asyncio.run(bot.tick())
    assert textos(bot, 42) == []

    gestor.reloj.actual = momento(18, 8, 1)
    asyncio.run(bot.tick())
    assert "Terminó tu pausa" in textos(bot, 42)[0]
    assert gestor.cuentas.obtener(42)["pausado_hasta"] is None


def test_reanudar_quita_la_pausa(bot, gestor):
    registrar(gestor, 42)
    asyncio.run(bot.cmd_pausar(update(42), contexto()))
    assert gestor.cuentas.obtener(42)["pausado_hasta"] == PAUSA_INDEFINIDA
    asyncio.run(bot.cmd_reanudar(update(42), contexto()))
    assert gestor.cuentas.obtener(42)["pausado_hasta"] is None
    assert "De vuelta" in textos(bot, 42)[0]


def test_limite_diario_de_mensajes(bot, gestor):
    class LLMFalso:
        nombre, modelo = "falso", "-"

        def completar(self, *_):
            return RespuestaLLM(texto="Hola")

    gestor.llm = LLMFalso()
    gestor.ajustes.limite_mensajes_dia = 2
    registrar(gestor, 42)
    respuestas = []
    for _ in range(3):
        pedido = update(42, "hola")
        asyncio.run(bot.al_recibir_texto(pedido, contexto()))
        respuestas += pedido.message.respuestas
    assert respuestas[:2] == ["Hola", "Hola"] and "límite de 2 mensajes" in respuestas[2]


def test_estado_resume_la_cuenta(bot, gestor):
    registrar(gestor, 42)
    gestor.cuentas.guardar_token_moodle(42, "1110123", "tok")
    pedido = update(42)
    asyncio.run(bot.cmd_estado(pedido, contexto()))
    texto = pedido.message.respuestas[0]
    assert "Estado: activo" in texto and "conectado como 1110123" in texto and "Semestre: 7" in texto


# --- Ajustes, datos y privacidad ---------------------------------------------------


def test_ajustes_cambian_las_reglas_del_estudiante(bot, gestor):
    sesion = registrar(gestor, 42)
    asyncio.run(bot._boton_ajustes(ConsultaFalsa(), 42, "horas_max_dia:6"))
    asyncio.run(bot._boton_ajustes(ConsultaFalsa(), 42, "moodle_horario:08:00-20:00"))
    assert sesion.ctx.ajustes.reglas.horas_max_dia == 6.0
    assert sesion.ctx.ajustes.moodle_horario_inicio == time(8, 0)
    assert gestor.ajustes.reglas.horas_max_dia == 4.0   # los demás no cambian


def test_los_datos_de_un_estudiante_no_se_ven_desde_otro(gestor):
    ana = registrar(gestor, 42)
    luis = registrar(gestor, 77)
    _con_entrega(ana.ctx)
    assert ana.ctx.repo.entregas_abiertas() and luis.ctx.repo.entregas_abiertas() == []
    assert ana.ctx.ajustes.carpeta_destino != luis.ctx.ajustes.carpeta_destino
    assert soporte.listar_pendientes(luis.ctx)["entregas"] == []


def test_olvidarme_borra_la_cuenta_y_la_carpeta(bot, gestor):
    sesion = registrar(gestor, 42)
    _con_entrega(sesion.ctx)
    carpeta = gestor.carpeta(42)
    assert carpeta.exists()
    asyncio.run(bot._boton_olvidar(ConsultaFalsa(), 42, "si"))
    assert not carpeta.exists() and gestor.cuentas.obtener(42) is None


def test_mis_datos_envia_un_zip(bot, gestor):
    registrar(gestor, 42)
    pedido = update(42)
    asyncio.run(bot.cmd_mis_datos(pedido, contexto()))
    nombre, datos = pedido.message.documentos[0]
    assert nombre == "edith_mis_datos.zip" and datos[:2] == b"PK"


def test_calendario_ics_incluye_bloques_y_entregas(gestor):
    sesion = registrar(gestor, 42)
    _con_entrega(sesion.ctx)
    ics = calendario_ics(sesion.ctx)
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert ics.count("BEGIN:VEVENT") >= 2
    assert "Entrega: Taller 3 (Sistemas Operativos)" in ics
    assert all(len(linea.encode()) <= 75 for linea in ics.split("\r\n"))


# --- Migración de la versión de un solo usuario -------------------------------------


def test_la_instalacion_anterior_pasa_a_la_cuenta_del_dueno(gestor, monkeypatch):
    monkeypatch.delenv("MOODLE_USUARIO", raising=False)
    anterior = Repositorio(gestor.ajustes.ruta_db)
    anterior.sembrar_pensum(RAIZ / "data" / "pensum.json", 5)
    anterior.fijar_preferencia("chat_id", "42")
    anterior.cerrar()

    assert migrar_instalacion_anterior(gestor, None)
    usuario = gestor.cuentas.obtener(42)
    assert usuario["estado"] == "activo" and usuario["semestre"] == 5
    assert all(m["semestre"] == 5 for m in gestor.sesion(42).ctx.repo.materias_activas())
    assert not migrar_instalacion_anterior(gestor, None)   # solo una vez


def test_sin_instalacion_anterior_no_se_migra(gestor):
    assert not migrar_instalacion_anterior(gestor, 42)
    assert gestor.cuentas.obtener(42) is None


def test_cuentas_en_memoria(tmp_path):
    cuentas = Cuentas(":memory:", Cifrador(Fernet.generate_key()))
    cuentas.crear(1, "x", AHORA)
    assert cuentas.sumar_mensaje(1, "2026-11-16") == 1
    assert cuentas.sumar_mensaje(1, "2026-11-16") == 2
    assert cuentas.mensajes_del_dia(1, "2026-11-17") == 0
