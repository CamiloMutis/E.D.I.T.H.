"""Prepara un escenario de demostración con datos ficticios.

Crea un horario de clases para las próximas dos semanas, dos entregas ya
planificadas y un bloque de ayer sin hacer, de modo que haya algo que mostrar
y algo que replanificar. Usa una base de datos aparte (``data/demo.db``) y el
calendario local, así que no toca el calendario real ni los datos del usuario.

Uso:
    python scripts/preparar_demo.py
    Luego, en config.yaml: base_de_datos: data/demo.db y calendario.backend: local
"""

from __future__ import annotations

import sys
from datetime import datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ajustes import RAIZ, cargar_ajustes  # noqa: E402
from arranque import construir_contexto  # noqa: E402
from skills.planificar_entrega import planificar_entrega  # noqa: E402
from skills.soporte import registrar_entrega  # noqa: E402

# Horario ficticio del semestre 7: (día de la semana, inicio, fin, materia)
HORARIO = [
    (0, time(7, 0), time(9, 0), "Sistemas Operativos"),
    (0, time(18, 0), time(20, 0), "Inteligencia Artificial"),
    (1, time(9, 0), time(11, 0), "Arquitectura de Software"),
    (2, time(7, 0), time(9, 0), "Sistemas Operativos"),
    (2, time(14, 0), time(16, 0), "Redes Inalámbricas"),
    (3, time(9, 0), time(11, 0), "Lenguajes de Programación"),
    (3, time(18, 0), time(20, 0), "Gestión de Proyectos de TI"),
    (4, time(14, 0), time(16, 0), "Inteligencia Artificial"),
]


def main() -> None:
    ajustes = cargar_ajustes()
    ajustes.ruta_db = RAIZ / "data" / "demo.db"
    if ajustes.ruta_db.exists():
        ajustes.ruta_db.unlink()
    ajustes.semestre_actual = 7
    ajustes.calendario_backend = "local"
    ctx = construir_contexto(ajustes)

    hoy = ctx.ahora().replace(hour=0, minute=0, second=0, microsecond=0)
    for desplazamiento in range(-1, 15):
        dia = hoy + timedelta(days=desplazamiento)
        for dia_semana, inicio, fin, materia in HORARIO:
            if dia.weekday() == dia_semana:
                ctx.calendario.crear_evento(
                    f"Clase: {materia}",
                    datetime.combine(dia.date(), inicio, tzinfo=ajustes.zona),
                    datetime.combine(dia.date(), fin, tzinfo=ajustes.zona),
                )

    taller = registrar_entrega(
        ctx, "Sistemas Operativos", "Taller 3", (hoy + timedelta(days=6)).date().isoformat(), 15,
        "Sincronización con semáforos y memoria compartida",
    )
    planificar_entrega(
        ctx, taller["entrega_id"],
        [
            {"nombre": "Repasar semáforos y exclusión mutua", "tipo_tarea": "lectura", "dificultad": 3, "horas_usuario": 1},
            {"nombre": "Programar la solución en C", "tipo_tarea": "codigo", "dificultad": 4, "horas_usuario": 2},
            {"nombre": "Probar y escribir el informe", "tipo_tarea": "informe", "dificultad": 3, "horas_usuario": 1},
        ],
    )
    exposicion = registrar_entrega(
        ctx, "Arquitectura de Software", "Exposición arquitectura hexagonal",
        (hoy + timedelta(days=9)).date().isoformat(), 20, "Puertos y adaptadores con un ejemplo",
    )
    planificar_entrega(
        ctx, exposicion["entrega_id"],
        [
            {"nombre": "Investigar el patrón y un caso real", "tipo_tarea": "lectura", "dificultad": 3, "horas_usuario": 2},
            {"nombre": "Armar las diapositivas", "tipo_tarea": "presentacion", "dificultad": 3, "horas_usuario": 2},
            {"nombre": "Ensayar la exposición", "tipo_tarea": "estudio", "dificultad": 2, "horas_usuario": 1},
        ],
    )

    # Un bloque de ayer que quedó sin hacer, para que la revisión tenga qué reubicar.
    primera = ctx.repo.subtareas_de_entrega(taller["entrega_id"])[0]
    ayer = hoy - timedelta(days=1)
    inicio = datetime.combine(ayer.date(), time(20, 15), tzinfo=ajustes.zona)
    fin = inicio + timedelta(hours=1)
    evento = ctx.calendario.crear_evento(f"Estudio: {primera['nombre']}", inicio, fin)
    ctx.repo.crear_bloque(primera["id"], inicio, fin, evento)

    print(f"Escenario de demostración creado en {ajustes.ruta_db}")
    print("Entregas: Taller 3 (Sistemas Operativos) y Exposición (Arquitectura de Software)")
    print("Para usarlo: en config.yaml pon base_de_datos: data/demo.db y calendario.backend: local")
    ctx.repo.cerrar()


if __name__ == "__main__":
    main()
