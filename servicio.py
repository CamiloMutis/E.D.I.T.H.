"""Deja a E.D.I.T.H. encendido siempre en este computador, o lo apaga.

Uso:
    python servicio.py activar      # arranca ya y cada vez que inicias sesión
    python servicio.py desactivar   # lo detiene y quita el arranque automático
    python servicio.py estado       # ¿está corriendo?
    python servicio.py registro     # lo último que hizo el bot

Funciona sin permisos de administrador:

* Linux: servicio de usuario de systemd; si el bot falla (sin internet, Moodle
  caído), se vuelve a arrancar a los 30 segundos.
* Windows: tarea programada al iniciar sesión, sin ventana.
* macOS: agente de launchd, que lo mantiene vivo.

Mientras el servicio está activo no ejecutes también ``python main.py``:
Telegram solo deja conectada una copia del bot por token.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Callable

RAIZ = Path(__file__).resolve().parent
NOMBRE = "edith"
TAREA_WINDOWS = "E.D.I.T.H."
ETIQUETA_MAC = "co.edith.bot"
REGISTRO = RAIZ / "data" / "edith.log"

Ejecutor = Callable[[list[str]], subprocess.CompletedProcess]


def _ejecutar(comando: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(comando, capture_output=True, text=True)


def _python() -> Path:
    """El Python del entorno virtual del proyecto (o el actual si no hay .venv)."""
    for candidato in (RAIZ / ".venv" / "bin" / "python", RAIZ / ".venv" / "Scripts" / "python.exe"):
        if candidato.exists():
            return candidato
    return Path(sys.executable)


class ErrorServicio(Exception):
    pass


# ---------------------------------------------------------------- Linux (systemd)


def unidad_systemd(python: Path, raiz: Path = RAIZ) -> str:
    return f"""[Unit]
Description=E.D.I.T.H. - agente académico en Telegram
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={raiz}
ExecStart={python} {raiz / "main.py"} --servicio
Restart=on-failure
RestartSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""


class Linux:
    def __init__(self, ejecutar: Ejecutor = _ejecutar, inicio: Path | None = None):
        self.ejecutar = ejecutar
        self.unidad = (inicio or Path.home()) / ".config" / "systemd" / "user" / f"{NOMBRE}.service"

    def _ctl(self, *argumentos: str) -> subprocess.CompletedProcess:
        return self.ejecutar(["systemctl", "--user", *argumentos])

    def activar(self, siempre: bool = False) -> str:
        self.unidad.parent.mkdir(parents=True, exist_ok=True)
        self.unidad.write_text(unidad_systemd(_python()), encoding="utf-8")
        self._ctl("daemon-reload")
        resultado = self._ctl("enable", "--now", f"{NOMBRE}.service")
        if resultado.returncode != 0:
            raise ErrorServicio(resultado.stderr.strip() or "systemctl no pudo activar el servicio.")
        texto = "E.D.I.T.H. quedó encendido y arrancará solo cada vez que inicies sesión."
        if siempre:
            self.ejecutar(["loginctl", "enable-linger", os.getenv("USER", "")])
            texto += " También corre aunque no hayas iniciado sesión."
        return texto

    def desactivar(self) -> str:
        self._ctl("disable", "--now", f"{NOMBRE}.service")
        if self.unidad.exists():
            self.unidad.unlink()
        self._ctl("daemon-reload")
        return "E.D.I.T.H. se apagó y ya no arrancará solo."

    def activo(self) -> bool:
        return self._ctl("is-active", f"{NOMBRE}.service").stdout.strip() == "active"

    def registro(self) -> str:
        return self.ejecutar(["journalctl", "--user", "-u", f"{NOMBRE}.service", "-n", "40", "--no-pager"]).stdout


# ---------------------------------------------------------------- Windows (tarea programada)


class Windows:
    def __init__(self, ejecutar: Ejecutor = _ejecutar):
        self.ejecutar = ejecutar

    def activar(self, siempre: bool = False) -> str:
        python = _python()
        # pythonw.exe corre sin abrir una ventana de consola.
        sin_ventana = python.with_name("pythonw.exe")
        python = sin_ventana if sin_ventana.exists() else python
        accion = f'"{python}" "{RAIZ / "main.py"}" --servicio --registro "{REGISTRO}"'
        creada = self.ejecutar(["schtasks", "/Create", "/TN", TAREA_WINDOWS, "/TR", accion,
                                "/SC", "ONLOGON", "/RL", "LIMITED", "/F"])
        if creada.returncode != 0:
            raise ErrorServicio(creada.stderr.strip() or creada.stdout.strip() or "No se pudo crear la tarea.")
        self.ejecutar(["schtasks", "/Run", "/TN", TAREA_WINDOWS])
        return "E.D.I.T.H. quedó encendido y arrancará solo cada vez que inicies sesión en Windows."

    def desactivar(self) -> str:
        self.ejecutar(["schtasks", "/End", "/TN", TAREA_WINDOWS])
        self.ejecutar(["schtasks", "/Delete", "/TN", TAREA_WINDOWS, "/F"])
        return "E.D.I.T.H. se apagó y ya no arrancará solo."

    def activo(self) -> bool:
        consulta = self.ejecutar(["schtasks", "/Query", "/TN", TAREA_WINDOWS, "/FO", "LIST"])
        # schtasks responde en el idioma de Windows: "Running" o "En ejecución".
        return consulta.returncode == 0 and any(
            estado in consulta.stdout for estado in ("Running", "En ejecución")
        )

    def registro(self) -> str:
        return _final_del_archivo(REGISTRO)


# ---------------------------------------------------------------- macOS (launchd)


def plist_launchd(python: Path, raiz: Path = RAIZ) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{ETIQUETA_MAC}</string>
  <key>ProgramArguments</key>
  <array><string>{python}</string><string>{raiz / "main.py"}</string><string>--servicio</string></array>
  <key>WorkingDirectory</key><string>{raiz}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{REGISTRO}</string>
  <key>StandardErrorPath</key><string>{REGISTRO}</string>
</dict>
</plist>
"""


class Mac:
    def __init__(self, ejecutar: Ejecutor = _ejecutar, inicio: Path | None = None):
        self.ejecutar = ejecutar
        self.plist = (inicio or Path.home()) / "Library" / "LaunchAgents" / f"{ETIQUETA_MAC}.plist"

    def activar(self, siempre: bool = False) -> str:
        REGISTRO.parent.mkdir(parents=True, exist_ok=True)
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        self.plist.write_text(plist_launchd(_python()), encoding="utf-8")
        self.ejecutar(["launchctl", "unload", str(self.plist)])
        cargado = self.ejecutar(["launchctl", "load", "-w", str(self.plist)])
        if cargado.returncode != 0:
            raise ErrorServicio(cargado.stderr.strip() or "launchctl no pudo cargar el servicio.")
        return "E.D.I.T.H. quedó encendido y arrancará solo cada vez que inicies sesión."

    def desactivar(self) -> str:
        if self.plist.exists():
            self.ejecutar(["launchctl", "unload", "-w", str(self.plist)])
            self.plist.unlink()
        return "E.D.I.T.H. se apagó y ya no arrancará solo."

    def activo(self) -> bool:
        return self.ejecutar(["launchctl", "list", ETIQUETA_MAC]).returncode == 0

    def registro(self) -> str:
        return _final_del_archivo(REGISTRO)


def _final_del_archivo(ruta: Path, lineas: int = 40) -> str:
    if not ruta.exists():
        return "Aún no hay registro."
    return "\n".join(ruta.read_text(encoding="utf-8", errors="replace").splitlines()[-lineas:])


def sistema_actual(ejecutar: Ejecutor = _ejecutar):
    nombre = platform.system()
    if nombre == "Linux":
        return Linux(ejecutar)
    if nombre == "Windows":
        return Windows(ejecutar)
    if nombre == "Darwin":
        return Mac(ejecutar)
    raise ErrorServicio(f"No sé dejar a E.D.I.T.H. encendido en {nombre}; ejecútalo con python main.py.")


def main(argumentos: list[str] | None = None) -> int:
    argumentos = sys.argv[1:] if argumentos is None else argumentos
    orden = argumentos[0] if argumentos else ""
    try:
        sistema = sistema_actual()
        if orden == "activar":
            if not (RAIZ / ".env").exists():
                print("Primero configura tu bot: python configurar.py")
                return 1
            print(sistema.activar(siempre="--siempre" in argumentos))
            print("Para apagarlo: python servicio.py desactivar")
        elif orden == "desactivar":
            print(sistema.desactivar())
        elif orden == "estado":
            print("E.D.I.T.H. está encendido." if sistema.activo() else "E.D.I.T.H. está apagado.")
        elif orden == "registro":
            print(sistema.registro())
        else:
            print(__doc__.split("Funciona")[0].strip())
            return 1
    except (ErrorServicio, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
