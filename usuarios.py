"""Sesiones de los estudiantes: el contexto de cada uno, armado a demanda.

Cada estudiante tiene su carpeta ``<datos>/usuarios/<telegram_id>/`` con su base de
datos SQLite y sus archivos organizados. El gestor crea su ``Contexto`` (con sus
ajustes, su calendario y su acceso a Moodle) y su ``Agente`` (con su propia
memoria de conversación) la primera vez que se necesitan y los reutiliza después.
Los modelos de ML y el cliente del LLM son compartidos.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from ajustes import RAIZ, Ajustes, personalizar
from db.cuentas import Cifrador, Cuentas
from db.repositorio import Repositorio
from integraciones.calendario import CalendarioLocal
from integraciones.moodle import ClienteMoodle, ErrorMoodle
from ml.clasificador import ClasificadorMaterias
from ml.predictor_tiempo import PredictorTiempo
from skills.contexto import Contexto

registro = logging.getLogger(__name__)

RUTA_PENSUM = RAIZ / "data" / "pensum.json"


@dataclass
class Sesion:
    telegram_id: int
    ctx: Contexto
    agente: object | None  # agente.orquestador.Agente, o None sin LLM
    # Un turno a la vez por estudiante: su agente guarda memoria de la conversación.
    candado: asyncio.Lock = field(default_factory=asyncio.Lock)


class GestorUsuarios:
    def __init__(
        self,
        ajustes: Ajustes,
        cuentas: Cuentas,
        predictor: PredictorTiempo,
        clasificador: ClasificadorMaterias,
        llm=None,
        moodle_url: str | None = None,
        reloj: Callable[[], datetime] | None = None,
    ):
        self.ajustes = ajustes
        self.cuentas = cuentas
        self.predictor = predictor
        self.clasificador = clasificador
        self.llm = llm
        self.moodle_url = moodle_url if moodle_url is not None else os.getenv("MOODLE_URL", "")
        self.reloj = reloj
        self.carpeta_usuarios = ajustes.carpeta_estado / "usuarios"
        self._sesiones: dict[int, Sesion] = {}

    def ahora(self) -> datetime:
        return self.reloj() if self.reloj else datetime.now(self.ajustes.zona)

    def carpeta(self, telegram_id: int) -> Path:
        return self.carpeta_usuarios / str(int(telegram_id))

    # --------------------------------------------------------------- sesiones

    def _ajustes_de(self, usuario: dict) -> Ajustes:
        return personalizar(self.ajustes, usuario["ajustes"], self.carpeta(usuario["telegram_id"]), usuario["semestre"])

    def sesion(self, telegram_id: int) -> Sesion:
        """Sesión del estudiante; la crea si es la primera vez que se necesita."""
        if telegram_id in self._sesiones:
            return self._sesiones[telegram_id]
        usuario = self.cuentas.obtener(telegram_id)
        if usuario is None:
            raise KeyError(f"El usuario {telegram_id} no está registrado")
        ajustes = self._ajustes_de(usuario)
        repo = Repositorio(ajustes.ruta_db)
        repo.sembrar_pensum(RUTA_PENSUM, ajustes.semestre_actual)
        from ml.entrenar import RUTA_TIEMPOS

        repo.sembrar_historial(RUTA_TIEMPOS)
        ctx = Contexto(
            repo,
            CalendarioLocal(repo, ajustes.zona),
            ajustes,
            self.predictor,
            self.clasificador,
            reloj=self.reloj,
            fabrica_moodle=lambda: self._cliente_moodle(telegram_id),
        )
        agente = None
        if self.llm is not None:
            from agente.orquestador import Agente

            agente = Agente(ctx, self.llm)
        sesion = Sesion(telegram_id, ctx, agente)
        self._sesiones[telegram_id] = sesion
        return sesion

    def refrescar(self, telegram_id: int) -> None:
        """Aplica ajustes o semestre recién cambiados sin perder la memoria de la conversación."""
        sesion = self._sesiones.get(telegram_id)
        usuario = self.cuentas.obtener(telegram_id)
        if sesion and usuario:
            sesion.ctx.ajustes = self._ajustes_de(usuario)

    def _cliente_moodle(self, telegram_id: int) -> ClienteMoodle:
        if not self.moodle_url:
            raise ErrorMoodle("Moodle no está configurado en este servidor (falta MOODLE_URL).")
        token = self.cuentas.token_moodle(telegram_id)
        if not token:
            raise ErrorMoodle("Aún no has conectado tu Moodle. Hazlo con /conectar_moodle.")
        return ClienteMoodle.con_token(self.moodle_url, token, self.ajustes.zona)

    def olvidar_cliente_moodle(self, telegram_id: int) -> None:
        sesion = self._sesiones.get(telegram_id)
        if sesion and sesion.ctx.moodle is not None:
            sesion.ctx.moodle.cerrar()
            sesion.ctx.moodle = None

    def cerrar(self, telegram_id: int) -> None:
        sesion = self._sesiones.pop(telegram_id, None)
        if sesion:
            if sesion.ctx.moodle is not None:
                sesion.ctx.moodle.cerrar()
            sesion.ctx.repo.cerrar()

    def cerrar_todo(self) -> None:
        for telegram_id in list(self._sesiones):
            self.cerrar(telegram_id)
        self.cuentas.cerrar()

    def borrar(self, telegram_id: int) -> None:
        """Elimina la cuenta y todos los datos del estudiante (/olvidarme)."""
        self.cerrar(telegram_id)
        shutil.rmtree(self.carpeta(telegram_id), ignore_errors=True)
        self.cuentas.eliminar(telegram_id)

    # ------------------------------------------------------------ reentreno

    def reentrenar_todos(self) -> dict:
        """Reentrena los modelos compartidos con lo que aportaron todos los estudiantes.

        El predictor aprende de las horas reales que cada uno registró y el
        clasificador de los archivos cuya materia confirmaron.
        """
        import pandas as pd

        from ml import clasificador as modulo_clasificador
        from ml import predictor_tiempo
        from ml.entrenar import RUTA_CORPUS, asegurar_datos, cargar_tiempos

        columnas = ("materia", "tipo_tarea", "dificultad", "estimado_h", "real_h", "dia_semana", "franja", "fuente")
        uso: list[dict] = []
        ejemplos: list[dict] = []
        for usuario in self.cuentas.activos():
            repo = self.sesion(usuario["telegram_id"]).ctx.repo
            uso += [{k: h[k] for k in columnas} for h in repo.historial(fuente="uso")]
            ejemplos += repo.ejemplos_clasificador()

        asegurar_datos()
        metricas = predictor_tiempo.entrenar_y_guardar(cargar_tiempos(uso), self.ajustes.carpeta_modelos)
        self.predictor.recargar()
        resultado = {"predictor": metricas, "filas_de_uso": len(uso), "ejemplos_confirmados": len(ejemplos)}
        if ejemplos:
            corpus = modulo_clasificador.combinar_corpus(pd.read_csv(RUTA_CORPUS), ejemplos)
            resultado["clasificador"] = modulo_clasificador.entrenar_y_guardar(corpus, self.ajustes.carpeta_modelos)
            self.clasificador.recargar()
        return resultado


def preparar_modelos(ajustes: Ajustes) -> tuple[PredictorTiempo, ClasificadorMaterias]:
    """Carga los modelos y los entrena si no están en disco o son de otra versión de scikit-learn."""
    from ml import entrenar

    predictor = PredictorTiempo(ajustes.carpeta_modelos)
    clasificador = ClasificadorMaterias(ajustes.carpeta_modelos)
    if not (predictor.disponible and clasificador.disponible):
        registro.info("Entrenando los modelos por primera vez; tarda menos de un minuto")
        entrenar.entrenar_todo(carpeta=ajustes.carpeta_modelos)
        predictor.recargar()
        clasificador.recargar()
    entrenar.asegurar_datos()
    return predictor, clasificador


def abrir_cuentas(ajustes: Ajustes) -> Cuentas:
    carpeta = ajustes.carpeta_estado
    return Cuentas(carpeta / "cuentas.db", Cifrador(ruta_clave=carpeta / "clave_maestra"))


def migrar_instalacion_anterior(gestor: GestorUsuarios, telegram_id: int | None) -> bool:
    """Convierte la instalación de un solo usuario en la cuenta de su dueño.

    Antes todo vivía en ``config.yaml: base_de_datos`` y el dueño era el chat
    guardado en la preferencia ``chat_id`` (o ``TELEGRAM_CHAT_ID``). Si ese
    estudiante aún no tiene cuenta, se le crea con una copia de esa base, así no
    pierde sus entregas, bloques ni avisos de Moodle ya enviados. Si en ``.env``
    están sus credenciales de Moodle, se cambian por un token cifrado.
    """
    anterior = gestor.ajustes.ruta_db
    if not anterior.exists():
        return False
    try:
        with closing(sqlite3.connect(anterior)) as conexion:
            prefs = dict(conexion.execute("SELECT clave, valor FROM preferencias").fetchall())
    except sqlite3.Error:
        return False
    dueno = telegram_id or (int(prefs["chat_id"]) if prefs.get("chat_id") else None)
    if not dueno or gestor.cuentas.obtener(dueno):
        return False

    destino = gestor.carpeta(dueno) / "edith.db"
    destino.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(anterior)) as origen, closing(sqlite3.connect(destino)) as copia:
        origen.backup(copia)
    ahora = gestor.ahora()
    gestor.cuentas.crear(dueno, "", ahora)
    gestor.cuentas.actualizar(
        dueno,
        estado="activo",
        semestre=int(prefs.get("semestre_actual") or gestor.ajustes.semestre_actual),
        acepto_datos=ahora.isoformat(),
    )
    usuario_moodle, clave = os.getenv("MOODLE_USUARIO"), os.getenv("MOODLE_CLAVE")
    if gestor.moodle_url and usuario_moodle and clave:
        try:
            cliente = ClienteMoodle(gestor.moodle_url, usuario_moodle, clave, gestor.ajustes.zona)
            gestor.cuentas.guardar_token_moodle(dueno, usuario_moodle, cliente.token)
            cliente.cerrar()
        except ErrorMoodle as error:
            registro.warning("No se pudo conectar el Moodle del dueño al migrar: %s", error)
    registro.info("Instalación anterior migrada a la cuenta %s", dueno)
    return True
