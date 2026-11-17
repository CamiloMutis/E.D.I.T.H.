"""Vigilante de la carpeta de descargas.

Avisa cada vez que aparece un archivo académico nuevo para que el agente lo
clasifique. Funciona igual en Linux y en Windows gracias a ``watchdog``.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from ml.extraccion_texto import EXTENSIONES

registro = logging.getLogger(__name__)

ESPERA_ESTABLE = 1.0   # segundos sin cambios de tamaño para dar la descarga por terminada
ESPERA_MAXIMA = 120.0


def esperar_archivo_completo(ruta: Path, espera: float = ESPERA_ESTABLE, maximo: float = ESPERA_MAXIMA) -> bool:
    """Espera a que el archivo deje de crecer. Devuelve ``False`` si desaparece o no termina."""
    limite = time.monotonic() + maximo
    anterior = -1
    while time.monotonic() < limite:
        try:
            actual = ruta.stat().st_size
        except OSError:
            return False
        if actual == anterior and actual > 0:
            return True
        anterior = actual
        time.sleep(espera)
    return False


class _Manejador(FileSystemEventHandler):
    def __init__(self, al_llegar: Callable[[Path], None]):
        self._al_llegar = al_llegar
        self._vistos: set[str] = set()
        self._candado = threading.Lock()

    def _atender(self, ruta_texto: str) -> None:
        ruta = Path(ruta_texto)
        # Los navegadores descargan con un nombre temporal (.crdownload, .part)
        # y renombran al terminar; solo interesan las extensiones finales.
        if ruta.suffix.lower() not in EXTENSIONES or ruta.name.startswith(("~$", ".")):
            return
        with self._candado:
            if ruta_texto in self._vistos:
                return
            self._vistos.add(ruta_texto)
        threading.Thread(target=self._procesar, args=(ruta,), daemon=True).start()

    def _procesar(self, ruta: Path) -> None:
        try:
            if esperar_archivo_completo(ruta):
                self._al_llegar(ruta)
        except Exception:
            registro.exception("No se pudo procesar %s", ruta.name)
        finally:
            with self._candado:
                self._vistos.discard(str(ruta))

    def on_created(self, event) -> None:
        if not event.is_directory:
            self._atender(event.src_path)

    def on_moved(self, event) -> None:
        if not event.is_directory:
            self._atender(event.dest_path)


class VigilanteArchivos:
    """Observa una carpeta y llama a ``al_llegar(ruta)`` por cada archivo nuevo."""

    def __init__(self, carpeta: Path, al_llegar: Callable[[Path], None]):
        self.carpeta = Path(carpeta)
        self._observador = Observer()
        self._observador.schedule(_Manejador(al_llegar), str(self.carpeta), recursive=False)

    def iniciar(self) -> bool:
        if not self.carpeta.is_dir():
            registro.warning("La carpeta vigilada %s no existe; el vigilante queda apagado", self.carpeta)
            return False
        self._observador.start()
        registro.info("Vigilando %s", self.carpeta)
        return True

    def detener(self) -> None:
        if self._observador.is_alive():
            self._observador.stop()
            self._observador.join(timeout=5)
