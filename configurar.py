"""Asistente de instalación: arma el archivo .env haciendo preguntas.

Uso:
    python configurar.py

Cada persona monta su propio E.D.I.T.H. con su propio bot de Telegram y sus
propias llaves, sin editar el .env a mano. El asistente:

1. Pide el token del bot (se crea con @BotFather), lo comprueba con Telegram y,
   si quieres, le cambia el nombre visible y la descripción.
2. Detecta tu id de Telegram cuando le escribes al bot (serás el administrador).
3. Arma el banco de LLM: eliges los proveedores en orden, pegas cada llave y se
   comprueba en el momento.
4. Pregunta quién puede usar el bot. Por defecto es privado: solo tú; a los
   demás les responde que es privado sin gastar tus llaves.
5. Confirma la dirección de Moodle y genera la clave de cifrado.

Si ya existe un .env, sus valores se ofrecen como respuesta por defecto, la
clave de cifrado nunca se cambia y se guarda una copia en .env.bak.
"""

from __future__ import annotations

import getpass
import os
import secrets
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx
from dotenv import dotenv_values

RAIZ = Path(__file__).resolve().parent
RUTA_ENV = RAIZ / ".env"
MOODLE_POR_DEFECTO = "https://pregradov.usc.edu.co"
TELEGRAM_API = "https://api.telegram.org"
ANTHROPIC_API = "https://api.anthropic.com/v1"
OPENAI_API = "https://api.openai.com/v1"


@dataclass(frozen=True)
class Proveedor:
    clave: str            # nombre en LLM_PROVIDERS
    titulo: str
    costo: str
    url_llave: str
    base_url: str | None  # None = API de OpenAI; "anthropic" usa su propia API
    modelo: str | None    # None = se elige de la lista que devuelve el proveedor


def _catalogo() -> list[Proveedor]:
    from agente import llm

    return [
        Proveedor("groq", "Groq", "gratis", "https://console.groq.com/keys", llm.GROQ_URL, llm.MODELO_GROQ),
        Proveedor("gemini", "Gemini (Google)", "gratis", "https://aistudio.google.com/apikey",
                  llm.GEMINI_URL, llm.MODELO_GEMINI),
        Proveedor("openai", "GPT (OpenAI)", "de pago", "https://platform.openai.com/api-keys", None, None),
        Proveedor("anthropic", "Claude (Anthropic)", "de pago", "https://platform.claude.com/settings/keys",
                  "anthropic", llm.MODELO_ANTHROPIC),
    ]


# ---------------------------------------------------------------- entrada y salida


class Consola:
    """Preguntas en la terminal. Las pruebas la reemplazan por respuestas fijas."""

    def __init__(
        self,
        entrada: Callable[[str], str] = input,
        oculta: Callable[[str], str] = getpass.getpass,
        salida: Callable[[str], None] = print,
    ):
        self._entrada, self._oculta, self.decir = entrada, oculta, salida

    def preguntar(self, texto: str, defecto: str | None = None) -> str:
        pista = f" [{defecto}]" if defecto else ""
        respuesta = self._entrada(f"{texto}{pista}: ").strip()
        return respuesta or (defecto or "")

    def secreto(self, texto: str, hay_actual: bool = False) -> str:
        pista = " (Enter para dejar la actual)" if hay_actual else ""
        return self._oculta(f"{texto}{pista}: ").strip()

    def si_no(self, texto: str, defecto: bool = True) -> bool:
        respuesta = self._entrada(f"{texto} [{'S/n' if defecto else 's/N'}]: ").strip().lower()
        if not respuesta:
            return defecto
        return respuesta[0] in ("s", "y")

    def elegir(self, texto: str, opciones: list[str], defecto: int = 1) -> int:
        """Devuelve el índice (desde 0) de la opción elegida."""
        self.decir(texto)
        for numero, opcion in enumerate(opciones, start=1):
            self.decir(f"  {numero}. {opcion}")
        while True:
            respuesta = self.preguntar("Elige un número", str(defecto))
            if respuesta.isdigit() and 1 <= int(respuesta) <= len(opciones):
                return int(respuesta) - 1
            self.decir("Ese número no está en la lista.")

    def titulo(self, texto: str) -> None:
        self.decir("")
        self.decir(f"── {texto} " + "─" * max(4, 60 - len(texto)))


# ---------------------------------------------------------------- Telegram


class ErrorConfiguracion(Exception):
    pass


def _telegram(http: httpx.Client, token: str, metodo: str, **parametros) -> dict:
    try:
        respuesta = http.post(f"{TELEGRAM_API}/bot{token}/{metodo}", json=parametros, timeout=40)
        datos = respuesta.json()
    except (httpx.HTTPError, ValueError) as error:
        raise ErrorConfiguracion(f"No pude hablar con Telegram: {error}") from error
    if not datos.get("ok"):
        raise ErrorConfiguracion(datos.get("description") or "Telegram rechazó la solicitud.")
    return datos.get("result")


def validar_token(http: httpx.Client, token: str) -> dict:
    """Devuelve los datos del bot (``username``, ``first_name``) o lanza ``ErrorConfiguracion``."""
    if ":" not in token:
        raise ErrorConfiguracion("Eso no parece un token: se ve como 123456789:AAH...")
    return _telegram(http, token, "getMe")


def detectar_id(http: httpx.Client, token: str, espera: float = 120, reloj=time.monotonic) -> dict | None:
    """Espera el primer mensaje privado al bot y devuelve ``{"id", "nombre"}``.

    Devuelve None si nadie escribió a tiempo o si el bot ya está corriendo en
    otro lado (Telegram solo deja leer los mensajes a una copia a la vez).
    """
    try:
        pendientes = _telegram(http, token, "getUpdates", offset=-1, timeout=0)
    except ErrorConfiguracion:
        return None
    desde = (pendientes[-1]["update_id"] + 1) if pendientes else None
    limite = reloj() + espera
    while reloj() < limite:
        try:
            parametros = {"timeout": 15, "allowed_updates": ["message"]}
            if desde is not None:
                parametros["offset"] = desde
            novedades = _telegram(http, token, "getUpdates", **parametros)
        except ErrorConfiguracion:
            return None
        for novedad in novedades:
            desde = novedad["update_id"] + 1
            mensaje = novedad.get("message") or {}
            if (mensaje.get("chat") or {}).get("type") == "private" and mensaje.get("from"):
                quien = mensaje["from"]
                # Se marca como leído para que el bot no lo procese después.
                _telegram(http, token, "getUpdates", offset=desde, timeout=0)
                return {"id": quien["id"], "nombre": quien.get("first_name") or ""}
    return None


# ---------------------------------------------------------------- LLM


def validar_llave(http: httpx.Client, proveedor: Proveedor | None, llave: str, base_url: str | None = None) -> list[str]:
    """Comprueba la llave pidiendo la lista de modelos y la devuelve.

    Lanza ``ErrorConfiguracion`` con un mensaje claro si la llave no sirve.
    """
    if proveedor is not None and proveedor.base_url == "anthropic":
        url = f"{ANTHROPIC_API}/models"
        cabeceras = {"x-api-key": llave, "anthropic-version": "2023-06-01"}
    else:
        base = base_url or (proveedor.base_url if proveedor else None) or OPENAI_API
        url = f"{base.rstrip('/')}/models"
        cabeceras = {"Authorization": f"Bearer {llave}"} if llave else {}
    try:
        respuesta = http.get(url, headers=cabeceras, timeout=20)
    except httpx.HTTPError as error:
        raise ErrorConfiguracion(f"No hubo conexión con {url}: {error}") from error
    if respuesta.status_code in (401, 403):
        raise ErrorConfiguracion("La llave no es válida o no tiene permisos.")
    if respuesta.status_code >= 400:
        raise ErrorConfiguracion(f"El proveedor respondió {respuesta.status_code}: {respuesta.text[:200]}")
    try:
        return [m.get("id") for m in respuesta.json().get("data", []) if m.get("id")]
    except ValueError:
        return []


def _pedir_llaves(consola: Consola, http: httpx.Client, nombre: str, proveedor: Proveedor | None,
                  actuales: list[str], base_url: str | None = None) -> tuple[list[str], list[str]]:
    """Pide una o varias llaves, las valida y devuelve (llaves, modelos disponibles)."""
    llaves: list[str] = []
    modelos: list[str] = []
    while True:
        hay_actual = bool(actuales) and not llaves
        llave = consola.secreto(f"Llave de {nombre}" + (" (otra más)" if llaves else ""), hay_actual)
        if not llave and hay_actual:
            llaves = list(actuales)
            consola.decir(f"  Se mantienen {len(llaves)} llave(s) actual(es). Las compruebo…")
            for posicion, actual in enumerate(llaves, start=1):
                try:
                    modelos = validar_llave(http, proveedor, actual, base_url) or modelos
                    consola.decir(f"  ✓ La llave {posicion} funciona.")
                except ErrorConfiguracion as error:
                    consola.decir(f"  ✗ La llave {posicion}: {error} Puedes cambiarla volviendo a ejecutar el asistente.")
            break
        if not llave:
            if llaves:
                break
            consola.decir("  Necesito al menos una llave.")
            continue
        try:
            modelos = validar_llave(http, proveedor, llave, base_url) or modelos
            consola.decir("  ✓ La llave funciona.")
            llaves.append(llave)
        except ErrorConfiguracion as error:
            consola.decir(f"  ✗ {error}")
            if consola.si_no("  ¿La guardo de todas formas?", defecto=False):
                llaves.append(llave)
            else:
                continue
        if not consola.si_no(f"¿Agregar otra llave de {nombre}? (por ejemplo, de otra cuenta gratuita)", False):
            break
    return llaves, modelos


def _elegir_modelo(consola: Consola, nombre: str, defecto: str | None, disponibles: list[str], filtro: str = "") -> str:
    sugeridos = [m for m in disponibles if filtro in m.lower()][:8] if disponibles else []
    if not defecto and sugeridos:
        consola.decir(f"  Algunos modelos disponibles: {', '.join(sugeridos)}")
        defecto = sugeridos[0]
    while True:
        modelo = consola.preguntar(f"Modelo de {nombre}", defecto)
        if modelo:
            return modelo
        consola.decir("  Escribe el nombre del modelo.")


def configurar_llm(consola: Consola, http: httpx.Client, actual: dict) -> dict[str, str]:
    catalogo = _catalogo()
    consola.titulo("3/5 · Cerebro: banco de modelos de lenguaje")
    consola.decir(
        "Elige uno o varios proveedores EN ORDEN. Se usa el primero y, si se queda sin cuota\n"
        "o falla, la conversación sigue con el siguiente. Con Groq (gratis) basta para empezar."
    )
    for numero, p in enumerate(catalogo, start=1):
        consola.decir(f"  {numero}. {p.titulo} ({p.costo}) — llave en {p.url_llave}")
    consola.decir(f"  {len(catalogo) + 1}. Otra API compatible con OpenAI (OpenRouter, DeepSeek, Ollama…)")

    previo = [n.strip() for n in (actual.get("LLM_PROVIDERS") or "").split(",") if n.strip()]
    claves = [p.clave for p in catalogo]
    defecto = ",".join(str(claves.index(n) + 1) for n in previo if n in claves) or "1"
    while True:
        respuesta = consola.preguntar("Números en orden, separados por comas (ej. 1,3,4)", defecto)
        numeros = [n.strip() for n in respuesta.split(",") if n.strip()]
        if numeros and all(n.isdigit() and 1 <= int(n) <= len(catalogo) + 1 for n in numeros):
            break
        consola.decir("Usa los números de la lista, por ejemplo 1 o 1,3.")

    valores: dict[str, str] = {}
    orden: list[str] = []
    for numero in dict.fromkeys(int(n) for n in numeros):
        if numero <= len(catalogo):
            p = catalogo[numero - 1]
            consola.decir(f"\n{p.titulo}: crea tu llave en {p.url_llave}")
            actuales = [k.strip() for k in (actual.get(f"{p.clave.upper()}_API_KEY") or "").split(",") if k.strip()]
            llaves, modelos = _pedir_llaves(consola, http, p.titulo, p, actuales)
            defecto_modelo = actual.get(f"{p.clave.upper()}_MODEL") or p.modelo
            modelo = _elegir_modelo(consola, p.titulo, defecto_modelo, modelos, "gpt" if p.clave == "openai" else "")
            valores[f"{p.clave.upper()}_API_KEY"] = ",".join(llaves)
            valores[f"{p.clave.upper()}_MODEL"] = modelo
            orden.append(p.clave)
        else:
            consola.decir("\nOtra API compatible con OpenAI.")
            nombre = ""
            while not nombre or nombre in claves or nombre in orden:
                nombre = consola.preguntar("Ponle un nombre corto (letras, ej. openrouter)").lower()
                nombre = "".join(c for c in nombre if c.isalnum() or c == "_")
            prefijo = nombre.upper()
            base = consola.preguntar("Dirección de la API (ej. https://openrouter.ai/api/v1)",
                                     actual.get(f"{prefijo}_BASE_URL"))
            sin_llave = consola.si_no("¿Es un servidor propio que no pide llave (como Ollama)?", False)
            llaves, modelos = ([], [])
            if sin_llave:
                try:
                    modelos = validar_llave(http, None, "", base)
                    consola.decir("  ✓ El servidor responde.")
                except ErrorConfiguracion as error:
                    consola.decir(f"  ✗ {error} (lo dejo configurado igual)")
            else:
                actuales = [k for k in (actual.get(f"{prefijo}_API_KEY") or "").split(",") if k]
                llaves, modelos = _pedir_llaves(consola, http, nombre, None, actuales, base)
            modelo = _elegir_modelo(consola, nombre, actual.get(f"{prefijo}_MODEL"), modelos)
            valores.update({f"{prefijo}_BASE_URL": base, f"{prefijo}_MODEL": modelo,
                            f"{prefijo}_API_KEY": ",".join(llaves)})
            orden.append(nombre)
    valores["LLM_PROVIDERS"] = ",".join(orden)
    return valores


# ---------------------------------------------------------------- .env


SECCIONES = [
    ("Telegram", ["TELEGRAM_BOT_TOKEN", "TELEGRAM_ADMIN_ID"]),
    ("Quién puede usar el bot: privado (solo tú y los permitidos), invitacion o publico",
     ["EDITH_ACCESO", "EDITH_USUARIOS_PERMITIDOS", "EDITH_CODIGO_INVITACION"]),
    ("Cifrado de los accesos a Moodle: no la cambies ni la pierdas", ["EDITH_CLAVE_MAESTRA"]),
    ("Moodle", ["MOODLE_URL"]),
]


def escribir_env(ruta: Path, valores: dict[str, str], previo: dict[str, str]) -> None:
    """Escribe el .env por secciones; conserva las variables previas que el asistente no tocó."""
    usadas: set[str] = set()
    lineas = ["# Generado por `python configurar.py`. Puedes volver a ejecutarlo cuando quieras.", ""]
    for titulo, claves in SECCIONES:
        lineas.append(f"# --- {titulo} ---")
        for clave in claves:
            lineas.append(f"{clave}={valores.get(clave, '')}")
            usadas.add(clave)
        lineas.append("")
    lineas.append("# --- Banco de LLM (en orden; ver .env.example para más opciones) ---")
    claves_llm = ["LLM_PROVIDERS"] + sorted(
        k for k in valores if k not in usadas and k != "LLM_PROVIDERS"
    )
    for clave in claves_llm:
        lineas.append(f"{clave}={valores.get(clave, '')}")
        usadas.add(clave)
    otras = {k: v for k, v in previo.items() if k not in usadas and v not in (None, "")}
    if otras:
        lineas += ["", "# --- Otras variables que ya tenías ---"]
        lineas += [f"{k}={v}" for k, v in otras.items()]
    if ruta.exists():
        shutil.copy2(ruta, ruta.with_name(ruta.name + ".bak"))
    ruta.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    try:
        os.chmod(ruta, 0o600)  # solo tu usuario puede leerlo
    except OSError:
        pass


# ---------------------------------------------------------------- asistente


def clave_de_cifrado(previo: dict[str, str], raiz: Path) -> str:
    """La clave que ya cifra los accesos a Moodle; solo si no hay ninguna se crea una.

    Orden: la del .env; la que el bot creó en ``<datos>/clave_maestra`` cuando
    el .env no tenía (cambiarla dejaría ilegibles los tokens guardados); una nueva.
    """
    if previo.get("EDITH_CLAVE_MAESTRA"):
        return previo["EDITH_CLAVE_MAESTRA"]
    datos = Path(previo.get("EDITH_DATOS") or os.getenv("EDITH_DATOS") or raiz / "data")
    if not datos.is_absolute():
        datos = raiz / datos
    archivo = datos / "clave_maestra"
    if archivo.exists():
        existente = archivo.read_text(encoding="utf-8").strip()
        if existente:
            return existente
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()



def asistente(consola: Consola, http: httpx.Client, ruta: Path = RUTA_ENV, espera_id: float = 120) -> dict[str, str]:
    previo = {k: (v or "") for k, v in dotenv_values(ruta).items()} if ruta.exists() else {}
    valores: dict[str, str] = {}
    consola.decir("Hola, vamos a configurar tu propio E.D.I.T.H. Toma unos 5 minutos.")
    if previo:
        consola.decir("Ya tienes un .env: lo que está entre [corchetes] es tu valor actual; Enter lo mantiene.")

    # 1. Bot de Telegram
    consola.titulo("1/5 · Tu bot de Telegram")
    consola.decir(
        "Si aún no tienes bot: abre Telegram, busca @BotFather, envía /newbot y elige\n"
        "un nombre (ej. «Moodle Bot») y un usuario terminado en «bot» (ej. mi_moodle_bot).\n"
        "BotFather te da un token como 123456789:AAH..."
    )
    while True:
        token = consola.secreto("Pega el token", bool(previo.get("TELEGRAM_BOT_TOKEN"))) or previo.get(
            "TELEGRAM_BOT_TOKEN", "")
        try:
            bot = validar_token(http, token)
            break
        except ErrorConfiguracion as error:
            consola.decir(f"  ✗ {error}")
    usuario_bot = bot.get("username", "")
    consola.decir(f"  ✓ Bot encontrado: {bot.get('first_name')} (@{usuario_bot}) — t.me/{usuario_bot}")
    valores["TELEGRAM_BOT_TOKEN"] = token
    nombre = consola.preguntar("¿Cómo quieres que se llame en los chats? (nombre visible)", bot.get("first_name"))
    if nombre and nombre != bot.get("first_name"):
        try:
            _telegram(http, token, "setMyName", name=nombre[:64])
            consola.decir(f"  ✓ Ahora se llama «{nombre}». (El @{usuario_bot} solo se cambia en BotFather.)")
        except ErrorConfiguracion as error:
            consola.decir(f"  ✗ No pude cambiar el nombre: {error}")
    try:
        _telegram(
            http, token, "setMyShortDescription",
            short_description="Tu asistente de semestre: vigila Moodle, planea tus entregas y te avisa a tiempo.",
        )
    except ErrorConfiguracion:
        pass

    # 2. Administrador
    consola.titulo("2/5 · Tú eres el administrador")
    admin = previo.get("TELEGRAM_ADMIN_ID", "")
    if admin and consola.si_no(f"Ya tienes el id de administrador {admin}. ¿Lo mantengo?"):
        valores["TELEGRAM_ADMIN_ID"] = admin
    else:
        consola.decir(f"Abre t.me/{usuario_bot} y envíale cualquier mensaje (por ejemplo «hola»). Te espero…")
        encontrado = detectar_id(http, token, espera_id)
        if encontrado:
            consola.decir(f"  ✓ Hola, {encontrado['nombre']}. Tu id es {encontrado['id']}.")
            valores["TELEGRAM_ADMIN_ID"] = str(encontrado["id"])
        else:
            consola.decir("  No te detecté (¿el bot está encendido en otro lado?). Pídele tu id a @userinfobot.")
            valores["TELEGRAM_ADMIN_ID"] = consola.preguntar("Tu id de Telegram (número, o Enter para omitir)", admin)

    # 3. LLM
    valores.update(configurar_llm(consola, http, previo))

    # 4. Quién lo usa
    consola.titulo("4/5 · ¿Quién puede usar tu bot?")
    consola.decir("Cualquiera puede encontrar un bot buscando su @ en Telegram, y cada mensaje gasta TUS llaves.")
    modo_previo = (previo.get("EDITH_ACCESO") or "privado").lower()
    opciones = [
        ("privado", "Solo yo (recomendado): a los demás les responde que es privado"),
        ("privado+", "Yo y algunas personas que yo autorice (por su id de Telegram)"),
        ("invitacion", "Quien tenga un código de invitación que yo reparta"),
        ("publico", "Cualquier estudiante"),
    ]
    por_defecto = 2 if previo.get("EDITH_USUARIOS_PERMITIDOS") else (
        {"privado": 1, "invitacion": 3, "publico": 4}.get(modo_previo, 1))
    uso = opciones[consola.elegir("Elige:", [texto for _, texto in opciones], defecto=por_defecto)][0]
    valores["EDITH_ACCESO"] = "privado" if uso.startswith("privado") else uso
    valores["EDITH_USUARIOS_PERMITIDOS"] = ""
    valores["EDITH_CODIGO_INVITACION"] = ""
    if uso == "privado+":
        consola.decir("  Cada persona puede ver su id escribiéndole a @userinfobot.")
        valores["EDITH_USUARIOS_PERMITIDOS"] = consola.preguntar(
            "Ids permitidos, separados por comas", previo.get("EDITH_USUARIOS_PERMITIDOS"))
    elif uso == "invitacion":
        valores["EDITH_CODIGO_INVITACION"] = consola.preguntar(
            "Código de invitación", previo.get("EDITH_CODIGO_INVITACION") or secrets.token_urlsafe(6))
    if valores["EDITH_ACCESO"] == "privado" and not valores.get("TELEGRAM_ADMIN_ID"):
        consola.decir("  ⚠ Sin tu id de administrador nadie podrá usar el bot. Vuelve a correr el asistente.")

    # 5. Moodle y cifrado
    consola.titulo("5/5 · Moodle")
    valores["MOODLE_URL"] = consola.preguntar(
        "Dirección de tu Moodle", previo.get("MOODLE_URL") or MOODLE_POR_DEFECTO
    ).rstrip("/")
    consola.decir("  Tu usuario y contraseña de Moodle NO van aquí: los das en el chat con /start\n"
                  "  y solo se guarda un permiso de acceso cifrado.")
    valores["EDITH_CLAVE_MAESTRA"] = clave_de_cifrado(previo, ruta.parent)
    consola.decir("  Guarda una copia de EDITH_CLAVE_MAESTRA (en tu gestor de contraseñas, por ejemplo):\n"
                  "  si se pierde, habrá que volver a conectar Moodle.")

    escribir_env(ruta, valores, previo)
    consola.titulo("Listo")
    consola.decir(f"Guardé tu configuración en {ruta.name}" + (" (la anterior quedó en .env.bak)." if previo else "."))
    consola.decir(f"Tu bot: t.me/{usuario_bot}")
    return valores


def main() -> int:
    consola = Consola()
    try:
        with httpx.Client() as http:
            asistente(consola, http)
    except (KeyboardInterrupt, EOFError):
        consola.decir("\nCancelado; no cambié nada.")
        return 1
    consola.titulo("¿Cómo quieres usarlo?")
    modo = consola.elegir(
        "E.D.I.T.H. solo responde mientras el programa está encendido en este computador.",
        [
            "Dejarlo encendido siempre: arranca solo con el computador (se apaga con: python servicio.py desactivar)",
            "Encenderlo ahora, solo mientras tenga esta terminal abierta",
            "No encenderlo todavía",
        ],
    )
    if modo == 0:
        import servicio

        try:
            consola.decir(servicio.sistema_actual().activar())
            consola.decir("Para apagarlo: python servicio.py desactivar  ·  ¿Está corriendo?: python servicio.py estado")
        except (servicio.ErrorServicio, OSError) as error:
            consola.decir(f"No pude dejarlo como servicio ({error}). Enciéndelo con: python main.py")
    elif modo == 1:
        os.execv(sys.executable, [sys.executable, str(RAIZ / "main.py")])
    else:
        consola.decir("Cuando quieras: python main.py (o python servicio.py activar para dejarlo siempre).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
