"""Pruebas del acceso al bot: privado por defecto, permitidos, invitación y público (sin red)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from integraciones.telegram_bot import AVISO_PRIVADO, BotEdith, acceso_desde_entorno
from tests.conftest import momento, registrar

DUENO, AMIGO, EXTRANO = 1, 77, 999


class ChatFalso:
    type = "private"

    def __init__(self):
        self.enviados: list[str] = []

    async def send_message(self, texto):
        self.enviados.append(texto)


def actualizacion(telegram_id: int, chat: ChatFalso | None = None):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=telegram_id, full_name="Alguien"),
        effective_chat=chat or ChatFalso(),
        callback_query=None,
    )


def bot(gestor, **opciones) -> BotEdith:
    nuevo = BotEdith(gestor, "123456:token-de-prueba", admin_id=DUENO, **opciones)
    nuevo.enviados = []

    async def enviar(telegram_id, texto, botones=None):
        nuevo.enviados.append((telegram_id, texto))

    nuevo.enviar = enviar
    return nuevo


def test_por_defecto_es_privado(gestor):
    privado = bot(gestor)
    assert privado.acceso == "privado"
    assert privado.puede_usar(DUENO)
    assert not privado.puede_usar(EXTRANO)
    # Con código de invitación y sin indicar el modo, se pide el código.
    assert bot(gestor, codigo_invitacion="abc").acceso == "invitacion"


def test_un_extrano_recibe_el_aviso_una_vez_y_no_se_guarda_nada(gestor):
    privado = bot(gestor)
    chat = ChatFalso()
    for _ in range(3):
        with pytest.raises(ApplicationHandlerStop):
            asyncio.run(privado.portero(actualizacion(EXTRANO, chat), None))
    assert chat.enviados == [AVISO_PRIVADO]       # una sola respuesta, aunque insista
    assert gestor.cuentas.obtener(EXTRANO) is None  # no se creó cuenta


def test_el_dueno_y_los_permitidos_pasan(gestor):
    privado = bot(gestor, permitidos={AMIGO})
    for telegram_id in (DUENO, AMIGO):
        assert asyncio.run(privado.portero(actualizacion(telegram_id), None)) is None
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(privado.portero(actualizacion(EXTRANO), None))


def test_publico_e_invitacion_dejan_pasar_a_todos(gestor):
    for opciones in ({"acceso": "publico"}, {"acceso": "invitacion", "codigo_invitacion": "abc"}):
        assert asyncio.run(bot(gestor, **opciones).portero(actualizacion(EXTRANO), None)) is None
    # En modo público el código no se pide aunque exista.
    assert bot(gestor, acceso="publico", codigo_invitacion="abc").codigo_invitacion is None


def test_modo_desconocido_es_un_error(gestor):
    with pytest.raises(ValueError, match="EDITH_ACCESO"):
        bot(gestor, acceso="secreto")


def test_las_rutinas_ignoran_cuentas_no_autorizadas(gestor):
    # Una cuenta creada cuando el bot era público no recibe nada al volverlo privado.
    registrar(gestor, EXTRANO)
    gestor.reloj.actual = momento(16, 7, 1)
    privado = bot(gestor)
    asyncio.run(privado.tick())
    assert [t for t in privado.enviados if t[0] == EXTRANO] == []

    con_permiso = bot(gestor, permitidos={EXTRANO})
    asyncio.run(con_permiso.tick())
    assert any(t[0] == EXTRANO for t in con_permiso.enviados)


def test_acceso_desde_el_entorno(monkeypatch):
    monkeypatch.setenv("EDITH_ACCESO", " Privado ")
    monkeypatch.setenv("EDITH_USUARIOS_PERMITIDOS", "77, 88 x 99")
    assert acceso_desde_entorno() == ("privado", {77, 88, 99})
    monkeypatch.delenv("EDITH_ACCESO")
    monkeypatch.delenv("EDITH_USUARIOS_PERMITIDOS")
    assert acceso_desde_entorno() == (None, set())


def test_el_dueno_recibe_los_cambios_del_banco_de_llm(gestor):
    from agente.llm import ClienteConRespaldo, RespuestaLLM

    class Falso:
        nombre, modelo = "groq", "m"

        def completar(self, *_):
            return RespuestaLLM(texto="ok")

    gestor.llm = ClienteConRespaldo([Falso(), Falso()])
    privado = bot(gestor)

    async def probar():
        privado.conectar_avisos_del_llm(asyncio.get_running_loop())
        # El LLM corre en otro hilo: el aviso llega desde ahí.
        await asyncio.to_thread(gestor.llm.al_cambiar, "🔁 cambio de proveedor")
        await asyncio.sleep(0.05)

    asyncio.run(probar())
    assert (DUENO, "🔁 cambio de proveedor") in privado.enviados
