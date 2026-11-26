"""Carga de la configuración del proyecto.

Las reglas viven en ``config.yaml`` y los secretos en ``.env``. Este módulo
los reúne en un solo objeto ``Ajustes`` que el resto del código recibe ya
validado, de modo que ningún otro módulo lee archivos de configuración.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent

DIAS_SEMANA = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]


@dataclass(frozen=True)
class Reglas:
    """Restricciones que el planificador respeta siempre."""

    horas_max_dia: float = 4.0
    bloque_min_minutos: int = 45
    bloque_max_minutos: int = 120
    descanso_minutos: int = 15
    franja_inicio: time = time(7, 0)
    franja_fin: time = time(23, 0)
    margen_dias: float = 1.0


@dataclass
class Ajustes:
    zona: ZoneInfo = field(default_factory=lambda: ZoneInfo("America/Bogota"))
    semestre_actual: int = 7
    reglas: Reglas = field(default_factory=Reglas)
    holgura_tranquila: float = 1.5
    holgura_ajustada: float = 1.0
    umbral_confianza: float = 0.7
    hora_resumen: time = time(7, 0)
    hora_revision: time = time(22, 0)
    dia_reentreno: str = "domingo"
    hora_reentreno: time = time(20, 0)
    carpeta_vigilada: Path = field(default_factory=lambda: Path.home() / "Downloads")
    carpeta_destino: Path = field(default_factory=lambda: Path.home() / "Universidad")
    calendario_backend: str = "auto"
    # Vigilancia de Moodle: tareas nuevas y recordatorios de vencimiento.
    moodle_vigilancia: bool = True
    moodle_intervalo_minutos: int = 15
    moodle_horario_inicio: time = time(6, 0)
    moodle_horario_fin: time = time(22, 0)
    moodle_avisos_horas: tuple[int, ...] = (72, 24, 3)
    moodle_dias_adelante: int = 60
    moodle_contenido_nuevo: bool = True
    ruta_db: Path = field(default_factory=lambda: RAIZ / "data" / "edith.db")
    carpeta_modelos: Path = field(default_factory=lambda: RAIZ / "ml" / "modelos")
    carpeta_datos: Path = field(default_factory=lambda: RAIZ / "data")


def _hora(texto: str) -> time:
    horas, minutos = str(texto).split(":")
    return time(int(horas), int(minutos))


def _ruta(texto: str) -> Path:
    ruta = Path(os.path.expandvars(str(texto))).expanduser()
    return ruta if ruta.is_absolute() else RAIZ / ruta


def cargar_ajustes(ruta_config: Path | None = None) -> Ajustes:
    """Lee ``config.yaml`` (y ``.env``) y devuelve los ajustes del proyecto.

    Las claves que falten en el archivo conservan su valor por defecto, así
    que un ``config.yaml`` parcial sigue siendo válido.
    """
    load_dotenv(RAIZ / ".env")
    ruta_config = ruta_config or RAIZ / "config.yaml"
    datos: dict = {}
    if ruta_config.exists():
        datos = yaml.safe_load(ruta_config.read_text(encoding="utf-8")) or {}

    base = Ajustes()
    r = datos.get("reglas", {})
    reglas = Reglas(
        horas_max_dia=float(r.get("horas_max_dia", base.reglas.horas_max_dia)),
        bloque_min_minutos=int(r.get("bloque_min_minutos", base.reglas.bloque_min_minutos)),
        bloque_max_minutos=int(r.get("bloque_max_minutos", base.reglas.bloque_max_minutos)),
        descanso_minutos=int(r.get("descanso_minutos", base.reglas.descanso_minutos)),
        franja_inicio=_hora(r.get("franja_inicio", "07:00")),
        franja_fin=_hora(r.get("franja_fin", "23:00")),
        margen_dias=float(r.get("margen_dias", base.reglas.margen_dias)),
    )
    if reglas.bloque_min_minutos > reglas.bloque_max_minutos:
        raise ValueError("config.yaml: bloque_min_minutos no puede superar a bloque_max_minutos")
    if reglas.franja_inicio >= reglas.franja_fin:
        raise ValueError("config.yaml: franja_inicio debe ser anterior a franja_fin")

    holgura = datos.get("holgura", {})
    rutinas = datos.get("rutinas", {})
    carpetas = datos.get("carpetas", {})
    dia_reentreno = str(rutinas.get("reentreno_dia", base.dia_reentreno)).lower()
    if dia_reentreno not in DIAS_SEMANA:
        raise ValueError(f"config.yaml: reentreno_dia desconocido: {dia_reentreno}")

    moodle = datos.get("moodle", {}) or {}
    horario_inicio = _hora(moodle.get("horario_inicio", "06:00"))
    horario_fin = _hora(moodle.get("horario_fin", "22:00"))
    if horario_inicio >= horario_fin:
        raise ValueError("config.yaml: moodle.horario_inicio debe ser anterior a moodle.horario_fin")
    avisos_horas = tuple(sorted({int(h) for h in moodle.get("avisos_horas", base.moodle_avisos_horas)}, reverse=True))
    if not avisos_horas or min(avisos_horas) <= 0:
        raise ValueError("config.yaml: moodle.avisos_horas debe tener horas positivas")
    intervalo = int(moodle.get("intervalo_minutos", base.moodle_intervalo_minutos))
    if intervalo < 5:
        raise ValueError("config.yaml: moodle.intervalo_minutos debe ser de al menos 5")

    return Ajustes(
        zona=ZoneInfo(datos.get("zona_horaria", "America/Bogota")),
        semestre_actual=int(datos.get("semestre_actual", base.semestre_actual)),
        reglas=reglas,
        holgura_tranquila=float(holgura.get("tranquila", base.holgura_tranquila)),
        holgura_ajustada=float(holgura.get("ajustada", base.holgura_ajustada)),
        umbral_confianza=float(
            datos.get("clasificador", {}).get("umbral_confianza", base.umbral_confianza)
        ),
        hora_resumen=_hora(rutinas.get("resumen", "07:00")),
        hora_revision=_hora(rutinas.get("revision", "22:00")),
        dia_reentreno=dia_reentreno,
        hora_reentreno=_hora(rutinas.get("reentreno_hora", "20:00")),
        carpeta_vigilada=_ruta(carpetas.get("vigilada", "~/Downloads")),
        carpeta_destino=_ruta(carpetas.get("destino", "~/Universidad")),
        calendario_backend=str(datos.get("calendario", {}).get("backend", "auto")).lower(),
        ruta_db=_ruta(datos.get("base_de_datos", "data/edith.db")),
        moodle_vigilancia=bool(moodle.get("vigilancia", base.moodle_vigilancia)),
        moodle_intervalo_minutos=intervalo,
        moodle_horario_inicio=horario_inicio,
        moodle_horario_fin=horario_fin,
        moodle_avisos_horas=avisos_horas,
        moodle_dias_adelante=int(moodle.get("dias_adelante", base.moodle_dias_adelante)),
        moodle_contenido_nuevo=bool(moodle.get("avisar_contenido_nuevo", base.moodle_contenido_nuevo)),
    )
