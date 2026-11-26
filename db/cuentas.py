"""Cuentas de los estudiantes que usan E.D.I.T.H. desde Telegram.

Cada estudiante tiene su propia base de datos (``usuarios/<telegram_id>/edith.db``)
con sus materias, entregas, bloques y archivos; así un error no puede mezclar los
datos de dos personas. Esta base central solo guarda quién es cada uno, en qué
paso del registro va, si está en pausa, sus ajustes y su acceso a Moodle.

De Moodle se guarda únicamente el token que entrega la plataforma, cifrado. La
contraseña se usa una sola vez para pedir ese token y no se almacena.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

registro = logging.getLogger(__name__)

ESQUEMA = """
CREATE TABLE IF NOT EXISTS usuarios (
  telegram_id     INTEGER PRIMARY KEY,
  nombre          TEXT,
  estado          TEXT DEFAULT 'registro',  -- registro | activo
  paso            TEXT,                     -- paso pendiente de una conversación guiada
  dato_temporal   TEXT,                     -- p. ej. el usuario de Moodle mientras se pide la clave
  semestre        INTEGER,
  pausado_hasta   TEXT,                     -- ISO 8601, 'indefinido' o NULL si no está en pausa
  moodle_usuario  TEXT,
  moodle_token    TEXT,                     -- cifrado con la clave maestra
  moodle_estado   TEXT DEFAULT 'sin_conectar',  -- sin_conectar | conectado | invalido
  ajustes         TEXT DEFAULT '{}',        -- JSON con lo que el estudiante cambió de config.yaml
  acepto_datos    TEXT,                     -- cuándo aceptó el tratamiento de datos
  creado          TEXT,
  ultimo_mensaje  TEXT
);

CREATE TABLE IF NOT EXISTS uso_llm (
  telegram_id     INTEGER NOT NULL,
  dia             TEXT NOT NULL,
  mensajes        INTEGER DEFAULT 0,
  PRIMARY KEY (telegram_id, dia)
);
"""

CAMPOS_EDITABLES = {
    "nombre", "estado", "paso", "dato_temporal", "semestre", "pausado_hasta",
    "moodle_usuario", "moodle_token", "moodle_estado", "acepto_datos", "ultimo_mensaje",
}
PAUSA_INDEFINIDA = "indefinido"


class Cifrador:
    """Cifra los tokens con Fernet (AES con autenticación).

    La clave sale de ``EDITH_CLAVE_MAESTRA``; si no está, se crea una en
    ``<datos>/clave_maestra`` la primera vez. Si esa clave se pierde, los tokens
    guardados no se pueden leer y cada estudiante debe volver a conectar Moodle.
    """

    def __init__(self, clave: str | bytes | None = None, ruta_clave: Path | None = None):
        clave = clave or os.getenv("EDITH_CLAVE_MAESTRA")
        if not clave:
            if ruta_clave is None:
                raise ValueError("Falta EDITH_CLAVE_MAESTRA y no hay dónde guardar una clave nueva.")
            if ruta_clave.exists():
                clave = ruta_clave.read_text(encoding="utf-8").strip()
            else:
                ruta_clave.parent.mkdir(parents=True, exist_ok=True)
                clave = Fernet.generate_key().decode()
                ruta_clave.write_text(clave, encoding="utf-8")
                ruta_clave.chmod(0o600)
                registro.warning(
                    "Se creó una clave de cifrado en %s. Guárdala: sin ella no se pueden leer "
                    "los accesos a Moodle guardados.", ruta_clave,
                )
        self._fernet = Fernet(clave.encode() if isinstance(clave, str) else clave)

    def cifrar(self, texto: str) -> str:
        return self._fernet.encrypt(texto.encode()).decode()

    def descifrar(self, texto: str) -> str | None:
        try:
            return self._fernet.decrypt(texto.encode()).decode()
        except (InvalidToken, ValueError):
            return None


class Cuentas:
    """Lectura y escritura de la base central de cuentas."""

    def __init__(self, ruta: Path | str, cifrador: Cifrador):
        self.ruta = str(ruta)
        if self.ruta != ":memory:":
            Path(self.ruta).parent.mkdir(parents=True, exist_ok=True)
        self.cifrador = cifrador
        self._conexion = sqlite3.connect(self.ruta, check_same_thread=False)
        self._conexion.row_factory = sqlite3.Row
        self._candado = threading.RLock()
        with self._candado:
            if self.ruta != ":memory:":
                self._conexion.execute("PRAGMA journal_mode = WAL")
            self._conexion.executescript(ESQUEMA)
            self._conexion.commit()

    def _ejecutar(self, sql: str, parametros: tuple = ()) -> sqlite3.Cursor:
        with self._candado:
            cursor = self._conexion.execute(sql, parametros)
            self._conexion.commit()
            return cursor

    def _filas(self, sql: str, parametros: tuple = ()) -> list[dict]:
        with self._candado:
            return [self._decodificar(dict(f)) for f in self._conexion.execute(sql, parametros).fetchall()]

    @staticmethod
    def _decodificar(fila: dict) -> dict:
        if "ajustes" in fila:
            fila["ajustes"] = json.loads(fila["ajustes"] or "{}")
        return fila

    def cerrar(self) -> None:
        with self._candado:
            self._conexion.close()

    # -------------------------------------------------------------- usuarios

    def obtener(self, telegram_id: int) -> dict | None:
        filas = self._filas("SELECT * FROM usuarios WHERE telegram_id = ?", (telegram_id,))
        return filas[0] if filas else None

    def crear(self, telegram_id: int, nombre: str, ahora: datetime) -> dict:
        self._ejecutar(
            "INSERT OR IGNORE INTO usuarios (telegram_id, nombre, creado) VALUES (?, ?, ?)",
            (telegram_id, nombre, ahora.isoformat()),
        )
        return self.obtener(telegram_id)

    def actualizar(self, telegram_id: int, **campos) -> None:
        desconocidos = set(campos) - CAMPOS_EDITABLES
        if desconocidos:
            raise ValueError(f"Campos no editables: {', '.join(sorted(desconocidos))}")
        if not campos:
            return
        asignaciones = ", ".join(f"{c} = ?" for c in campos)
        self._ejecutar(
            f"UPDATE usuarios SET {asignaciones} WHERE telegram_id = ?", (*campos.values(), telegram_id)
        )

    def fijar_ajuste(self, telegram_id: int, clave: str, valor) -> dict:
        usuario = self.obtener(telegram_id)
        ajustes = usuario["ajustes"] if usuario else {}
        ajustes[clave] = valor
        self._ejecutar(
            "UPDATE usuarios SET ajustes = ? WHERE telegram_id = ?",
            (json.dumps(ajustes, ensure_ascii=False), telegram_id),
        )
        return ajustes

    def activos(self) -> list[dict]:
        """Usuarios que terminaron el registro."""
        return self._filas("SELECT * FROM usuarios WHERE estado = 'activo' ORDER BY telegram_id")

    def todos(self) -> list[dict]:
        return self._filas("SELECT * FROM usuarios ORDER BY telegram_id")

    def eliminar(self, telegram_id: int) -> None:
        self._ejecutar("DELETE FROM uso_llm WHERE telegram_id = ?", (telegram_id,))
        self._ejecutar("DELETE FROM usuarios WHERE telegram_id = ?", (telegram_id,))

    # ----------------------------------------------------------------- moodle

    def guardar_token_moodle(self, telegram_id: int, usuario_moodle: str, token: str) -> None:
        self.actualizar(
            telegram_id,
            moodle_usuario=usuario_moodle,
            moodle_token=self.cifrador.cifrar(token),
            moodle_estado="conectado",
        )

    def token_moodle(self, telegram_id: int) -> str | None:
        usuario = self.obtener(telegram_id)
        if not usuario or not usuario["moodle_token"]:
            return None
        return self.cifrador.descifrar(usuario["moodle_token"])

    def olvidar_moodle(self, telegram_id: int, estado: str = "sin_conectar") -> None:
        self.actualizar(telegram_id, moodle_token=None, moodle_estado=estado)

    # ------------------------------------------------------------------ pausa

    @staticmethod
    def en_pausa(usuario: dict, ahora: datetime) -> bool:
        hasta = usuario.get("pausado_hasta")
        if not hasta:
            return False
        if hasta == PAUSA_INDEFINIDA:
            return True
        return datetime.fromisoformat(hasta) > ahora

    # ------------------------------------------------------------ uso del LLM

    def sumar_mensaje(self, telegram_id: int, dia: str) -> int:
        """Cuenta un mensaje atendido por el LLM y devuelve cuántos lleva ese día."""
        self._ejecutar(
            "INSERT INTO uso_llm (telegram_id, dia, mensajes) VALUES (?, ?, 1) "
            "ON CONFLICT(telegram_id, dia) DO UPDATE SET mensajes = mensajes + 1",
            (telegram_id, dia),
        )
        return self.mensajes_del_dia(telegram_id, dia)

    def mensajes_del_dia(self, telegram_id: int, dia: str) -> int:
        filas = self._filas(
            "SELECT mensajes FROM uso_llm WHERE telegram_id = ? AND dia = ?", (telegram_id, dia)
        )
        return filas[0]["mensajes"] if filas else 0
