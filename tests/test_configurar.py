"""Pruebas del asistente de instalación (sin red: Telegram y los proveedores son simulados)."""

from __future__ import annotations

import json
import os
import stat
import sys

import httpx
import pytest
from cryptography.fernet import Fernet
from dotenv import dotenv_values

import configurar
from agente import llm


def _servidor_falso(llamadas: list):
    def responder(solicitud: httpx.Request) -> httpx.Response:
        llamadas.append(solicitud)
        if solicitud.url.host == "api.telegram.org":
            metodo = solicitud.url.path.rsplit("/", 1)[-1]
            cuerpo = json.loads(solicitud.content or b"{}")
            if "/bot123:MALO/" in str(solicitud.url):
                return httpx.Response(200, json={"ok": False, "description": "Unauthorized"})
            if metodo == "getMe":
                return httpx.Response(200, json={"ok": True, "result": {"username": "mi_bot", "first_name": "E.D.I.T.H."}})
            if metodo == "getUpdates":
                if cuerpo.get("timeout") == 15:
                    mensaje = {"chat": {"type": "private"}, "from": {"id": 42, "first_name": "Camilo"}}
                    return httpx.Response(200, json={"ok": True, "result": [{"update_id": 5, "message": mensaje}]})
                return httpx.Response(200, json={"ok": True, "result": []})
            return httpx.Response(200, json={"ok": True, "result": True})
        if "malo" in solicitud.headers.get("authorization", ""):
            return httpx.Response(401, json={"error": "invalid key"})
        if solicitud.url.host == "api.groq.com":
            return httpx.Response(200, json={"data": [{"id": llm.MODELO_GROQ}]})
        if solicitud.url.host == "api.openai.com":
            return httpx.Response(200, json={"data": [{"id": "whisper-1"}, {"id": "gpt-a"}, {"id": "gpt-b"}]})
        return httpx.Response(404)

    return responder


def _consola(respuestas: list[str], secretos: list[str], dichos: list[str]) -> configurar.Consola:
    def entrada(texto):
        assert respuestas, f"Pregunta inesperada: {texto}"
        return respuestas.pop(0)

    def oculta(texto):
        assert secretos, f"Secreto inesperado: {texto}"
        return secretos.pop(0)

    return configurar.Consola(entrada, oculta, dichos.append)


@pytest.fixture
def http():
    llamadas: list = []
    cliente = httpx.Client(transport=httpx.MockTransport(_servidor_falso(llamadas)))
    cliente.llamadas = llamadas
    yield cliente
    cliente.close()


def test_instalacion_nueva_arma_el_env(tmp_path, http, monkeypatch):
    ruta = tmp_path / ".env"
    respuestas = [
        "Moodle Bot",   # nombre visible
        "1,3",          # banco: Groq y luego GPT
        "n",            # ¿otra llave de Groq?
        "",             # modelo de Groq (por defecto)
        "n",            # ¿otra llave de GPT?
        "",             # modelo de GPT (el primero de la lista)
        "",             # acceso: por defecto, solo yo
        "",             # Moodle por defecto
    ]
    secretos = ["123:ABC", "gsk_1", "sk-1"]
    configurar.asistente(_consola(respuestas, secretos, []), http, ruta, espera_id=5)

    assert not respuestas and not secretos
    env = dotenv_values(ruta)
    assert env["TELEGRAM_BOT_TOKEN"] == "123:ABC"
    assert env["TELEGRAM_ADMIN_ID"] == "42"          # detectado al escribirle al bot
    assert env["EDITH_ACCESO"] == "privado"          # privado por defecto
    assert env["EDITH_CODIGO_INVITACION"] == ""
    assert env["LLM_PROVIDERS"] == "groq,openai"
    assert env["GROQ_MODEL"] == llm.MODELO_GROQ
    assert env["OPENAI_MODEL"] == "gpt-a"            # sugerido de la lista del proveedor
    assert env["MOODLE_URL"] == configurar.MOODLE_POR_DEFECTO
    Fernet(env["EDITH_CLAVE_MAESTRA"].encode())      # clave de cifrado válida
    assert any(str(s.url).endswith("/setMyName") for s in http.llamadas)
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(ruta).st_mode) == 0o600

    # Con ese .env el banco de LLM arranca tal cual.
    for clave in ("LLM_PROVIDER", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(clave, raising=False)
    for clave, valor in env.items():
        monkeypatch.setenv(clave, valor)
    assert llm.crear_cliente().nombre == "groq→openai"


def test_datos_invalidos_se_vuelven_a_pedir(tmp_path, http):
    ruta = tmp_path / ".env"
    dichos: list[str] = []
    respuestas = [
        "",      # nombre visible igual
        "1",     # solo Groq
        "n",     # no guardar la llave mala
        "n",     # ¿otra llave?
        "",      # modelo
        "2",     # yo y algunas personas
        "77,88", # sus ids
        "",      # Moodle
    ]
    secretos = ["sin-dos-puntos", "123:MALO", "123:ABC", "gsk_malo", "gsk_bueno"]
    configurar.asistente(_consola(respuestas, secretos, dichos), http, ruta, espera_id=5)
    env = dotenv_values(ruta)
    assert env["TELEGRAM_BOT_TOKEN"] == "123:ABC"
    assert env["GROQ_API_KEY"] == "gsk_bueno"
    assert env["EDITH_ACCESO"] == "privado"
    assert env["EDITH_USUARIOS_PERMITIDOS"] == "77,88"
    assert any("no es válida" in d for d in dichos)


def test_volver_a_configurar_conserva_la_clave_y_las_otras_variables(tmp_path, http):
    ruta = tmp_path / ".env"
    clave = Fernet.generate_key().decode()
    ruta.write_text(
        "TELEGRAM_BOT_TOKEN=123:ABC\nTELEGRAM_ADMIN_ID=7\nGROQ_API_KEY=gsk_viejo\nEDITH_ACCESO=publico\n"
        f"EDITH_CLAVE_MAESTRA={clave}\nGOOGLE_CALENDAR_ID=primary\nMOODLE_URL=https://moodle.ejemplo.edu\n",
        encoding="utf-8",
    )
    respuestas = [
        "",     # nombre visible
        "s",    # mantener id de administrador
        "",     # banco por defecto (Groq)
        "",     # modelo
        "",     # acceso: el actual (público)
        "",     # Moodle actual
    ]
    secretos = ["", ""]   # Enter: mantener token y llave
    configurar.asistente(_consola(respuestas, secretos, []), http, ruta, espera_id=5)
    env = dotenv_values(ruta)
    assert env["EDITH_CLAVE_MAESTRA"] == clave       # nunca se cambia
    assert env["TELEGRAM_ADMIN_ID"] == "7"
    assert env["GROQ_API_KEY"] == "gsk_viejo"
    assert env["EDITH_ACCESO"] == "publico"
    assert env["GOOGLE_CALENDAR_ID"] == "primary"    # variable ajena conservada
    assert env["MOODLE_URL"] == "https://moodle.ejemplo.edu"
    assert (tmp_path / ".env.bak").exists()


def test_detectar_id_no_espera_para_siempre(http):
    reloj = iter([0, 999]).__next__
    assert configurar.detectar_id(http, "123:ABC", espera=1, reloj=reloj) is None


def test_reutiliza_la_clave_que_el_bot_ya_creo(tmp_path):
    # El bot, sin EDITH_CLAVE_MAESTRA en el .env, crea data/clave_maestra; cambiarla
    # dejaría ilegibles los accesos a Moodle ya guardados.
    (tmp_path / "data").mkdir()
    existente = Fernet.generate_key().decode()
    (tmp_path / "data" / "clave_maestra").write_text(existente + "\n", encoding="utf-8")
    assert configurar.clave_de_cifrado({}, tmp_path) == existente
    assert configurar.clave_de_cifrado({"EDITH_CLAVE_MAESTRA": "la-del-env"}, tmp_path) == "la-del-env"
    nueva = configurar.clave_de_cifrado({}, tmp_path / "vacio")
    Fernet(nueva.encode())
