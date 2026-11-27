"""Pruebas de servicio.py: activar y desactivar en Linux, Windows y macOS sin tocar el sistema."""

from __future__ import annotations

import subprocess

import pytest

import servicio


class Ejecutor:
    """Anota los comandos en lugar de ejecutarlos."""

    def __init__(self, salidas: dict[str, str] | None = None, falla: str | None = None):
        self.comandos: list[list[str]] = []
        self.salidas = salidas or {}
        self.falla = falla

    def __call__(self, comando):
        self.comandos.append(comando)
        texto = " ".join(comando)
        codigo = 1 if self.falla and self.falla in texto else 0
        salida = next((v for k, v in self.salidas.items() if k in texto), "")
        return subprocess.CompletedProcess(comando, codigo, salida, "falló" if codigo else "")

    def hubo(self, *partes: str) -> bool:
        return any(all(p in " ".join(c) for p in partes) for c in self.comandos)


def test_linux_activar_y_desactivar(tmp_path):
    ejecutor = Ejecutor()
    linux = servicio.Linux(ejecutor, inicio=tmp_path)
    assert "encendido" in linux.activar()
    unidad = linux.unidad.read_text(encoding="utf-8")
    assert "Restart=on-failure" in unidad
    assert "main.py --servicio" in unidad          # la copia del servicio se identifica
    assert ejecutor.hubo("systemctl", "--user", "enable", "--now", "edith.service")

    assert "apagó" in linux.desactivar()
    assert not linux.unidad.exists()
    assert ejecutor.hubo("systemctl", "--user", "disable", "--now")


def test_linux_estado(tmp_path):
    assert servicio.Linux(Ejecutor({"is-active": "active\n"}), inicio=tmp_path).activo()
    assert not servicio.Linux(Ejecutor({"is-active": "inactive\n"}), inicio=tmp_path).activo()


def test_linux_error_claro_si_systemd_falla(tmp_path):
    with pytest.raises(servicio.ErrorServicio):
        servicio.Linux(Ejecutor(falla="enable"), inicio=tmp_path).activar()


def test_windows_tarea_programada():
    ejecutor = Ejecutor({"/Query": "Estado: En ejecución"})
    windows = servicio.Windows(ejecutor)
    windows.activar()
    assert ejecutor.hubo("schtasks", "/Create", "ONLOGON", "--servicio", "--registro")
    assert ejecutor.hubo("schtasks", "/Run")
    assert windows.activo()                         # Windows en español
    assert servicio.Windows(Ejecutor({"/Query": "Status: Running"})).activo()
    windows.desactivar()
    assert ejecutor.hubo("schtasks", "/End") and ejecutor.hubo("schtasks", "/Delete", "/F")


def test_mac_launchd(tmp_path):
    ejecutor = Ejecutor()
    mac = servicio.Mac(ejecutor, inicio=tmp_path)
    mac.activar()
    plist = mac.plist.read_text(encoding="utf-8")
    assert "<key>KeepAlive</key><true/>" in plist and "--servicio" in plist
    mac.desactivar()
    assert not mac.plist.exists()
    assert ejecutor.hubo("launchctl", "unload", "-w")


def test_orden_desconocida_muestra_la_ayuda(capsys):
    assert servicio.main(["volar"]) == 1
    assert "python servicio.py activar" in capsys.readouterr().out
