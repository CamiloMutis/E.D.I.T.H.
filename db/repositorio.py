"""Acceso a la base de datos SQLite.

Todo el SQL del proyecto está en esta clase. Los skills trabajan con
diccionarios y fechas de Python y no conocen la estructura de las tablas.
"""

from __future__ import annotations

import csv
import difflib
import json
import sqlite3
import threading
import unicodedata
from datetime import datetime
from pathlib import Path

RUTA_ESQUEMA = Path(__file__).resolve().parent / "esquema.sql"


def normalizar(texto: str) -> str:
    """Minúsculas y sin tildes, para comparar nombres escritos de formas distintas."""
    sin_tildes = unicodedata.normalize("NFKD", texto or "")
    sin_tildes = "".join(c for c in sin_tildes if not unicodedata.combining(c))
    return " ".join(sin_tildes.lower().split())


def _iso(valor: datetime | str) -> str:
    return valor.isoformat() if isinstance(valor, datetime) else valor


class Repositorio:
    """Lectura y escritura de materias, entregas, subtareas, bloques y archivos."""

    def __init__(self, ruta: Path | str):
        self.ruta = str(ruta)
        if self.ruta != ":memory:":
            Path(self.ruta).parent.mkdir(parents=True, exist_ok=True)
        # El bot atiende mensajes, tareas programadas y el vigilante de archivos
        # desde hilos distintos; el candado serializa el acceso a la conexión.
        self._conexion = sqlite3.connect(self.ruta, check_same_thread=False)
        self._conexion.row_factory = sqlite3.Row
        self._conexion.execute("PRAGMA foreign_keys = ON")
        self._candado = threading.RLock()
        with self._candado:
            self._conexion.executescript(RUTA_ESQUEMA.read_text(encoding="utf-8"))
            self._conexion.commit()

    # ------------------------------------------------------------------ base

    def _ejecutar(self, sql: str, parametros: tuple = ()) -> sqlite3.Cursor:
        with self._candado:
            cursor = self._conexion.execute(sql, parametros)
            self._conexion.commit()
            return cursor

    def _filas(self, sql: str, parametros: tuple = ()) -> list[dict]:
        with self._candado:
            return [dict(f) for f in self._conexion.execute(sql, parametros).fetchall()]

    def _fila(self, sql: str, parametros: tuple = ()) -> dict | None:
        filas = self._filas(sql, parametros)
        return filas[0] if filas else None

    def cerrar(self) -> None:
        with self._candado:
            self._conexion.close()

    # -------------------------------------------------------------- materias

    def sembrar_pensum(self, ruta_json: Path, semestre_actual: int | None = None) -> int:
        """Carga las materias del pénsum si la tabla está vacía. Devuelve cuántas insertó."""
        if self._fila("SELECT 1 FROM materias LIMIT 1"):
            return 0
        pensum = json.loads(Path(ruta_json).read_text(encoding="utf-8"))
        with self._candado:
            self._conexion.executemany(
                "INSERT INTO materias (nombre, semestre, creditos, area) VALUES (?, ?, ?, ?)",
                [
                    (m["nombre"], m["semestre"], m["creditos"], m.get("area"))
                    for m in pensum["materias"]
                ],
            )
            self._conexion.commit()
        if semestre_actual is not None:
            self.activar_semestre(semestre_actual)
        return len(pensum["materias"])

    def activar_semestre(self, semestre: int) -> list[dict]:
        """Deja activas solo las materias del semestre indicado."""
        self._ejecutar("UPDATE materias SET activa = (semestre = ?)", (semestre,))
        self.fijar_preferencia("semestre_actual", str(semestre))
        return self.materias_activas()

    def activar_materia(self, materia_id: int, activa: bool = True) -> None:
        self._ejecutar("UPDATE materias SET activa = ? WHERE id = ?", (int(activa), materia_id))

    def materias(self) -> list[dict]:
        return self._filas("SELECT * FROM materias ORDER BY semestre, id")

    def materias_activas(self) -> list[dict]:
        return self._filas("SELECT * FROM materias WHERE activa = 1 ORDER BY id")

    def obtener_materia(self, materia_id: int) -> dict | None:
        return self._fila("SELECT * FROM materias WHERE id = ?", (materia_id,))

    def buscar_materia(self, nombre: str) -> dict | None:
        """Encuentra una materia aunque el nombre venga incompleto o sin tildes.

        Se buscan primero las materias activas: si el usuario dice "Cálculo"
        y cursa Cálculo II, esa es la que quiere decir.
        """
        objetivo = normalizar(nombre)
        if not objetivo:
            return None
        todas = self.materias()
        for grupo in ([m for m in todas if m["activa"]], todas):
            nombres = {normalizar(m["nombre"]): m for m in grupo}
            if objetivo in nombres:
                return nombres[objetivo]
            contenidas = [m for clave, m in nombres.items() if objetivo in clave or clave in objetivo]
            if len(contenidas) == 1:
                return contenidas[0]
            cercanas = difflib.get_close_matches(objetivo, list(nombres), n=1, cutoff=0.72)
            if cercanas:
                return nombres[cercanas[0]]
            if contenidas:
                return min(contenidas, key=lambda m: len(m["nombre"]))
        return None

    def fijar_carpeta_materia(self, materia_id: int, carpeta: str) -> None:
        self._ejecutar("UPDATE materias SET carpeta = ? WHERE id = ?", (carpeta, materia_id))

    # -------------------------------------------------------------- entregas

    def crear_entrega(
        self,
        materia_id: int,
        nombre: str,
        fecha_limite: datetime,
        peso_nota: float | None = None,
        descripcion: str = "",
    ) -> int:
        cursor = self._ejecutar(
            "INSERT INTO entregas (materia_id, nombre, descripcion, fecha_limite, peso_nota) "
            "VALUES (?, ?, ?, ?, ?)",
            (materia_id, nombre, descripcion, _iso(fecha_limite), peso_nota),
        )
        return cursor.lastrowid

    def obtener_entrega(self, entrega_id: int) -> dict | None:
        return self._fila(
            "SELECT e.*, m.nombre AS materia FROM entregas e "
            "JOIN materias m ON m.id = e.materia_id WHERE e.id = ?",
            (entrega_id,),
        )

    def entregas_abiertas(self, materia_id: int | None = None) -> list[dict]:
        sql = (
            "SELECT e.*, m.nombre AS materia FROM entregas e "
            "JOIN materias m ON m.id = e.materia_id WHERE e.estado = 'abierta'"
        )
        parametros: tuple = ()
        if materia_id is not None:
            sql += " AND e.materia_id = ?"
            parametros = (materia_id,)
        sql += " ORDER BY e.fecha_limite, e.peso_nota DESC"
        return self._filas(sql, parametros)

    def actualizar_riesgo(self, entrega_id: int, riesgo: str, holgura: float | None) -> None:
        self._ejecutar(
            "UPDATE entregas SET riesgo = ?, holgura = ? WHERE id = ?",
            (riesgo, holgura, entrega_id),
        )

    def marcar_entregada(self, entrega_id: int) -> None:
        self._ejecutar("UPDATE entregas SET estado = 'entregada' WHERE id = ?", (entrega_id,))

    # ------------------------------------------------------------- subtareas

    def crear_subtarea(
        self,
        entrega_id: int,
        nombre: str,
        tipo_tarea: str,
        dificultad: int,
        horas_usuario: float | None,
        horas_predichas: float,
    ) -> int:
        cursor = self._ejecutar(
            "INSERT INTO subtareas (entrega_id, nombre, tipo_tarea, dificultad, horas_usuario, "
            "horas_predichas, horas_restantes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entrega_id, nombre, tipo_tarea, dificultad, horas_usuario, horas_predichas, horas_predichas),
        )
        return cursor.lastrowid

    _SQL_SUBTAREA = (
        "SELECT s.*, e.nombre AS entrega, e.fecha_limite, e.peso_nota, e.materia_id, "
        "m.nombre AS materia FROM subtareas s "
        "JOIN entregas e ON e.id = s.entrega_id JOIN materias m ON m.id = e.materia_id "
    )

    def obtener_subtarea(self, subtarea_id: int) -> dict | None:
        return self._fila(self._SQL_SUBTAREA + "WHERE s.id = ?", (subtarea_id,))

    def subtareas_de_entrega(self, entrega_id: int) -> list[dict]:
        return self._filas(self._SQL_SUBTAREA + "WHERE s.entrega_id = ? ORDER BY s.id", (entrega_id,))

    def subtareas_abiertas(self) -> list[dict]:
        """Subtareas sin terminar de entregas abiertas, en orden de urgencia."""
        return self._filas(
            self._SQL_SUBTAREA
            + "WHERE s.estado != 'hecha' AND e.estado = 'abierta' "
            "ORDER BY e.fecha_limite, e.peso_nota DESC, s.id"
        )

    def actualizar_subtarea(self, subtarea_id: int, **campos) -> None:
        permitidos = {"horas_restantes", "horas_hechas", "estado", "horas_predichas"}
        campos = {k: v for k, v in campos.items() if k in permitidos}
        if not campos:
            return
        asignaciones = ", ".join(f"{campo} = ?" for campo in campos)
        self._ejecutar(
            f"UPDATE subtareas SET {asignaciones} WHERE id = ?", (*campos.values(), subtarea_id)
        )

    # --------------------------------------------------------------- bloques

    _SQL_BLOQUE = (
        "SELECT b.*, s.nombre AS subtarea, s.entrega_id, e.nombre AS entrega, "
        "m.nombre AS materia FROM bloques b "
        "JOIN subtareas s ON s.id = b.subtarea_id JOIN entregas e ON e.id = s.entrega_id "
        "JOIN materias m ON m.id = e.materia_id "
    )

    def crear_bloque(
        self, subtarea_id: int, inicio: datetime, fin: datetime, calendar_event: str | None
    ) -> int:
        cursor = self._ejecutar(
            "INSERT INTO bloques (subtarea_id, inicio, fin, calendar_event) VALUES (?, ?, ?, ?)",
            (subtarea_id, _iso(inicio), _iso(fin), calendar_event),
        )
        return cursor.lastrowid

    def obtener_bloque(self, bloque_id: int) -> dict | None:
        return self._fila(self._SQL_BLOQUE + "WHERE b.id = ?", (bloque_id,))

    def bloques_entre(self, desde: datetime, hasta: datetime) -> list[dict]:
        """Bloques que empiezan dentro del rango. Las fechas ISO de una misma zona ordenan bien como texto."""
        return self._filas(
            self._SQL_BLOQUE + "WHERE b.inicio >= ? AND b.inicio < ? ORDER BY b.inicio",
            (_iso(desde), _iso(hasta)),
        )

    def bloques_pendientes_desde(self, desde: datetime) -> list[dict]:
        return self._filas(
            self._SQL_BLOQUE + "WHERE b.estado = 'pendiente' AND b.inicio >= ? ORDER BY b.inicio",
            (_iso(desde),),
        )

    def bloques_vencidos_sin_marcar(self, ahora: datetime) -> list[dict]:
        return self._filas(
            self._SQL_BLOQUE + "WHERE b.estado = 'pendiente' AND b.fin <= ? ORDER BY b.inicio",
            (_iso(ahora),),
        )

    def bloques_en_curso(self, ahora: datetime) -> list[dict]:
        """Bloques pendientes que ya empezaron y todavía no terminan."""
        return self._filas(
            self._SQL_BLOQUE + "WHERE b.estado = 'pendiente' AND b.inicio < ? AND b.fin > ?",
            (_iso(ahora), _iso(ahora)),
        )

    def bloques_de_subtarea(self, subtarea_id: int) -> list[dict]:
        return self._filas(
            self._SQL_BLOQUE + "WHERE b.subtarea_id = ? ORDER BY b.inicio", (subtarea_id,)
        )

    def actualizar_estado_bloque(self, bloque_id: int, estado: str) -> None:
        self._ejecutar("UPDATE bloques SET estado = ? WHERE id = ?", (estado, bloque_id))

    def eliminar_bloque(self, bloque_id: int) -> None:
        self._ejecutar("DELETE FROM bloques WHERE id = ?", (bloque_id,))

    # ------------------------------------------------------------- historial

    def agregar_historial(
        self,
        materia: str,
        tipo_tarea: str,
        dificultad: int,
        estimado_h: float,
        real_h: float,
        dia_semana: str,
        franja: str,
        fuente: str,
        subtarea_id: int | None = None,
    ) -> int:
        cursor = self._ejecutar(
            "INSERT INTO historial_tiempos (subtarea_id, materia, tipo_tarea, dificultad, "
            "estimado_h, real_h, dia_semana, franja, fuente) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (subtarea_id, materia, tipo_tarea, dificultad, estimado_h, real_h, dia_semana, franja, fuente),
        )
        return cursor.lastrowid

    def historial(self, fuente: str | None = None) -> list[dict]:
        if fuente:
            return self._filas("SELECT * FROM historial_tiempos WHERE fuente = ?", (fuente,))
        return self._filas("SELECT * FROM historial_tiempos")

    def sembrar_historial(self, ruta_csv: Path) -> int:
        """Carga el dataset inicial de tiempos si el historial está vacío."""
        if self._fila("SELECT 1 FROM historial_tiempos LIMIT 1") or not Path(ruta_csv).exists():
            return 0
        with open(ruta_csv, newline="", encoding="utf-8") as archivo:
            filas = [
                (
                    f["materia"], f["tipo_tarea"], int(f["dificultad"]), float(f["estimado_h"]),
                    float(f["real_h"]), f["dia_semana"], f["franja"], f.get("fuente", "sintetico"),
                )
                for f in csv.DictReader(archivo)
            ]
        with self._candado:
            self._conexion.executemany(
                "INSERT INTO historial_tiempos (materia, tipo_tarea, dificultad, estimado_h, "
                "real_h, dia_semana, franja, fuente) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                filas,
            )
            self._conexion.commit()
        return len(filas)

    # -------------------------------------------------------------- archivos

    def registrar_archivo(
        self,
        ruta: str,
        nombre: str,
        materia_id: int,
        entrega_id: int | None,
        confianza: float,
        confirmado: bool,
        registrado_en: datetime,
    ) -> int:
        cursor = self._ejecutar(
            "INSERT INTO archivos (ruta, nombre, materia_id, entrega_id, confianza, confirmado, "
            "registrado_en) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ruta, nombre, materia_id, entrega_id, confianza, int(confirmado), _iso(registrado_en)),
        )
        return cursor.lastrowid

    def archivos_de(self, materia_id: int, entrega_id: int | None = None) -> list[dict]:
        sql = (
            "SELECT a.*, e.nombre AS entrega FROM archivos a "
            "LEFT JOIN entregas e ON e.id = a.entrega_id WHERE a.materia_id = ?"
        )
        parametros: tuple = (materia_id,)
        if entrega_id is not None:
            sql += " AND a.entrega_id = ?"
            parametros += (entrega_id,)
        return self._filas(sql + " ORDER BY a.registrado_en DESC", parametros)

    def agregar_ejemplo_clasificador(self, texto: str, materia: str) -> None:
        self._ejecutar(
            "INSERT INTO ejemplos_clasificador (texto, materia) VALUES (?, ?)", (texto, materia)
        )

    def ejemplos_clasificador(self) -> list[dict]:
        return self._filas("SELECT texto, materia FROM ejemplos_clasificador")

    # ------------------------------------------------------ calendario local

    def crear_evento_local(self, titulo: str, inicio: datetime, fin: datetime, descripcion: str) -> int:
        cursor = self._ejecutar(
            "INSERT INTO eventos_locales (titulo, inicio, fin, descripcion) VALUES (?, ?, ?, ?)",
            (titulo, _iso(inicio), _iso(fin), descripcion),
        )
        return cursor.lastrowid

    def eventos_locales_entre(self, desde: datetime, hasta: datetime) -> list[dict]:
        return self._filas(
            "SELECT * FROM eventos_locales WHERE fin > ? AND inicio < ? ORDER BY inicio",
            (_iso(desde), _iso(hasta)),
        )

    def mover_evento_local(self, evento_id: int, inicio: datetime, fin: datetime) -> None:
        self._ejecutar(
            "UPDATE eventos_locales SET inicio = ?, fin = ? WHERE id = ?",
            (_iso(inicio), _iso(fin), evento_id),
        )

    def eliminar_evento_local(self, evento_id: int) -> None:
        self._ejecutar("DELETE FROM eventos_locales WHERE id = ?", (evento_id,))

    # ---------------------------------------------------------- preferencias

    def preferencia(self, clave: str, defecto: str | None = None) -> str | None:
        fila = self._fila("SELECT valor FROM preferencias WHERE clave = ?", (clave,))
        return fila["valor"] if fila else defecto

    def fijar_preferencia(self, clave: str, valor: str) -> None:
        self._ejecutar(
            "INSERT INTO preferencias (clave, valor) VALUES (?, ?) "
            "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
            (clave, valor),
        )
