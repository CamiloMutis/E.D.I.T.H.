"""Pruebas del bot de Telegram (sin red), del vigilante de archivos y de la configuración."""

import asyncio
import time
from datetime import timedelta

import pytest

from agente.llm import ErrorLLM
from ajustes import RAIZ, cargar_ajustes
from integraciones.telegram_bot import BotEdith, _trozos
from integraciones.vigilante_archivos import VigilanteArchivos
from skills import soporte
from skills.agenda import fecha
from skills.planificar_entrega import planificar_entrega
from tests.conftest import AHORA, registrar
from tests.test_archivos import TEXTO_AJENO, TEXTO_SO
from tests.test_skills import SUBTAREAS


class SinLLM:
    nombre, modelo = "ninguno", "-"

    def completar(self, *_):
        raise ErrorLLM("sin llave")


class ConsultaFalsa:
    """Sustituye al botón pulsado en Telegram."""

    def __init__(self):
        self.textos = []

    async def edit_message_text(self, texto, reply_markup=None):
        self.textos.append(texto)


def bot_de_prueba(gestor) -> BotEdith:
    """Bot que, en lugar de enviar a Telegram, guarda (destinatario, texto, botones)."""
    bot = BotEdith(gestor, "123456:token-de-prueba", admin_id=1)
    bot.enviados = []

    async def enviar(telegram_id, texto, botones=None):
        bot.enviados.append((telegram_id, texto, botones))

    bot.enviar = enviar
    return bot


@pytest.fixture
def sesion(gestor):
    return registrar(gestor, 42)


@pytest.fixture
def bot(gestor, sesion):
    return bot_de_prueba(gestor)


def _con_entrega(ctx):
    limite = (AHORA + timedelta(days=6)).date().isoformat()
    entrega = soporte.registrar_entrega(ctx, "Sistemas Operativos", "Taller 3", limite, 15)
    return entrega, planificar_entrega(ctx, entrega["entrega_id"], SUBTAREAS)


def test_el_bot_registra_comandos_y_rutinas(bot):
    app = bot.construir()
    comandos = {c for grupo in app.handlers.values() for m in grupo for c in getattr(m, "commands", [])}
    assert {"start", "hoy", "pendientes", "revisar_dia", "semestre", "pausar", "reanudar", "estado",
            "ajustes", "calendario", "mis_datos", "olvidarme", "conectar_moodle", "admin"} <= comandos
    assert {t.name for t in app.job_queue.jobs()} == {"rutinas", "reentreno", "moodle"}


def test_trozos_respeta_el_limite_de_telegram():
    assert [len(t) for t in _trozos("x" * 9000)] == [4000, 4000, 1000]


def test_revision_del_dia_pregunta_marca_y_reorganiza(bot, sesion):
    ctx = sesion.ctx
    _, plan = _con_entrega(ctx)
    primero = plan["bloques"][0]
    ctx.reloj.actual = fecha(primero["fin"]).replace(hour=22, minute=0)

    asyncio.run(bot.revision_del_dia(42))
    con_botones = [texto for _, texto, botones in bot.enviados if botones]
    assert len(con_botones) >= 2                      # un mensaje por bloque y el de reorganizar
    assert bot.enviados[-1][2][0][0].callback_data == "rev"
    assert {destino for destino, _, _ in bot.enviados} == {42}

    # El estudiante marca el primer bloque como no hecho y pide reorganizar.
    consulta = ConsultaFalsa()
    asyncio.run(bot._boton_bloque(42, consulta, f"{primero['bloque_id']}:perdido"))
    assert "no hecho" in consulta.textos[0]
    bot.enviados.clear()
    asyncio.run(bot.reorganizar(42))
    assert bot.enviados and "Bloques agendados" in bot.enviados[-1][1]   # aviso de respaldo, sin LLM


def test_bloque_hecho_pregunta_si_la_subtarea_termino_y_aprende(bot, sesion):
    ctx = sesion.ctx
    entrega, plan = _con_entrega(ctx)
    subtarea = ctx.repo.subtareas_de_entrega(entrega["entrega_id"])[0]
    for bloque in ctx.repo.bloques_de_subtarea(subtarea["id"]):
        asyncio.run(bot._boton_bloque(42, ConsultaFalsa(), f"{bloque['id']}:hecho"))
    _, texto, botones = bot.enviados[-1]
    assert "¿La terminaste?" in texto and botones[0][0].callback_data == f"t:{subtarea['id']}:0"

    consulta = ConsultaFalsa()
    asyncio.run(bot._boton_subtarea(42, consulta, f"{subtarea['id']}:0"))
    assert "terminada" in consulta.textos[0]
    assert len(ctx.repo.historial(fuente="uso")) == 1


def test_archivo_dudoso_pregunta_y_el_usuario_confirma(bot, sesion, tmp_path):
    ruta = tmp_path / "recibo.txt"
    ruta.write_text(TEXTO_AJENO, encoding="utf-8")
    asyncio.run(bot.procesar_archivo(42, ruta))
    _, texto, botones = bot.enviados[-1]
    assert "recibo.txt" in texto and botones[-1][0].callback_data.endswith(":x")

    # Otro estudiante no puede responder por él.
    ajena = ConsultaFalsa()
    asyncio.run(bot._boton_archivo(7, ajena, botones[0][0].callback_data.split(":", 1)[1]))
    assert ajena.textos == ["Ese archivo ya fue atendido."] and ruta.exists()

    consulta = ConsultaFalsa()
    asyncio.run(bot._boton_archivo(42, consulta, botones[0][0].callback_data.split(":", 1)[1]))
    assert consulta.textos[0].startswith("Moví recibo.txt")
    assert not ruta.exists()
    movidos = list(sesion.ctx.ajustes.carpeta_destino.rglob("recibo.txt"))
    assert len(movidos) == 1


def test_archivo_claro_se_mueve_a_la_carpeta_del_estudiante(bot, sesion, tmp_path):
    ruta = tmp_path / "guia.txt"
    ruta.write_text(TEXTO_SO, encoding="utf-8")
    asyncio.run(bot.procesar_archivo(42, ruta))
    assert bot.enviados[-1] == (42, "Moví guia.txt a Sistemas Operativos.", None)
    assert (sesion.ctx.ajustes.carpeta_destino / "Sistemas Operativos" / "guia.txt").exists()


def test_el_vigilante_detecta_un_archivo_nuevo(tmp_path):
    vistos = []
    vigilante = VigilanteArchivos(tmp_path, vistos.append)
    assert vigilante.iniciar()
    try:
        (tmp_path / "descarga.crdownload").write_text("temporal", encoding="utf-8")   # se ignora
        (tmp_path / "taller.pdf").write_bytes(b"%PDF-1.4 contenido")
        limite = time.monotonic() + 15
        while not vistos and time.monotonic() < limite:
            time.sleep(0.2)
    finally:
        vigilante.detener()
    assert [ruta.name for ruta in vistos] == ["taller.pdf"]


def test_vigilante_sin_carpeta_no_falla(tmp_path):
    assert VigilanteArchivos(tmp_path / "no_existe", print).iniciar() is False


def test_configuracion_del_repositorio_es_valida():
    ajustes = cargar_ajustes(RAIZ / "config.yaml")
    assert ajustes.reglas.bloque_min_minutos <= ajustes.reglas.bloque_max_minutos
    assert ajustes.carpeta_vigilada.is_absolute() and ajustes.ruta_db.is_absolute()
    assert ajustes.dia_reentreno == "domingo"


def test_configuracion_invalida_se_rechaza(tmp_path):
    ruta = tmp_path / "config.yaml"
    ruta.write_text("reglas:\n  franja_inicio: '23:00'\n  franja_fin: '07:00'\n", encoding="utf-8")
    with pytest.raises(ValueError):
        cargar_ajustes(ruta)
