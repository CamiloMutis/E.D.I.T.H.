"""Cursos de Moodle que el estudiante no quiere ver, para siempre.

Hay cursos en los que uno está matriculado sin que cuenten para su semestre
(un idioma que ya aprobó, un curso de prueba…). Ocultar un curso hace que sus
tareas no se registren, no se planeen y no avisen nunca más, y cierra lo que
ya se había planeado de él. La lista se guarda en la base del estudiante, así
que sobrevive a reinicios; ``mostrar_curso`` lo deshace.
"""

from __future__ import annotations

import json

from db.repositorio import normalizar
from skills.agenda import ErrorSkill, eliminar_bloque
from skills.contexto import Contexto

PREFERENCIA = "moodle_cursos_ocultos"


def curso_corto(curso: str) -> str:
    """'2026B/INGLES III-VIR-PRE/3PRE06' -> 'INGLES III-VIR-PRE'."""
    partes = (curso or "").split("/")
    return partes[1] if len(partes) >= 3 else (curso or "")


def ocultos(ctx: Contexto) -> list[str]:
    """Los cursos ocultos, tal como se ocultaron (nombre corto)."""
    try:
        valor = json.loads(ctx.repo.preferencia(PREFERENCIA) or "[]")
    except ValueError:
        return []
    return [c for c in valor if isinstance(c, str)]


def esta_oculto(ctx: Contexto, curso: str, lista: list[str] | None = None) -> bool:
    clave = normalizar(curso_corto(curso))
    return bool(clave) and clave in {normalizar(c) for c in (lista if lista is not None else ocultos(ctx))}


def filtrar(ctx: Contexto, elementos: list) -> list:
    """Quita los eventos o avisos (cualquier cosa con ``.curso``) de cursos ocultos."""
    lista = ocultos(ctx)
    if not lista:
        return elementos
    return [e for e in elementos if not esta_oculto(ctx, e.curso, lista)]


def cursos_conocidos(ctx: Contexto) -> list[str]:
    """Los cursos de Moodle que E.D.I.T.H. ha visto, más los ocultos, sin repetir."""
    vistos = {curso_corto(e["curso"]) for e in ctx.repo.moodle_eventos().values() if e.get("curso")}
    return sorted(vistos | set(ocultos(ctx)), key=normalizar)


def _buscar(ctx: Contexto, texto: str, entre: list[str]) -> str:
    """El curso de ``entre`` que corresponde a ``texto`` (un número de la lista o parte del nombre)."""
    texto = (texto or "").strip()
    if texto.isdigit() and 1 <= int(texto) <= len(entre):
        return entre[int(texto) - 1]
    buscado = normalizar(texto)
    coinciden = [c for c in entre if buscado and buscado in normalizar(c)]
    if len(coinciden) == 1:
        return coinciden[0]
    if not coinciden:
        raise ErrorSkill(f"No encontré un curso que coincida con «{texto}».")
    raise ErrorSkill("Hay varios cursos con ese nombre: " + ", ".join(coinciden) + ". Sé más específico.")


def _cerrar_lo_planeado(ctx: Contexto, curso: str) -> int:
    """Cierra las entregas abiertas que Moodle trajo de ese curso y libera sus bloques futuros.

    Se usa la misma búsqueda de materia que usó el planificador al crearlas
    (``planear_moodle._materia``), así se cierran justo esas y no otras.
    """
    materia = ctx.repo.buscar_materia(curso)
    if not materia:
        return 0
    cerradas = 0
    for entrega in ctx.repo.entregas_abiertas(materia["id"]):
        if not entrega.get("moodle_id"):
            continue  # lo que el estudiante registró a mano no se toca
        subtareas = {s["id"] for s in ctx.repo.subtareas_de_entrega(entrega["id"])}
        for bloque in ctx.repo.bloques_pendientes_desde(ctx.ahora()):
            if bloque["subtarea_id"] in subtareas:
                eliminar_bloque(ctx, bloque)
        ctx.repo.cerrar_entrega(entrega["id"], "oculta")
        cerradas += 1
    if not ctx.repo.entregas_abiertas(materia["id"]):
        ctx.repo.activar_materia(materia["id"], False)
    return cerradas


def ocultar_curso(ctx: Contexto, texto: str) -> dict:
    curso = _buscar(ctx, texto, cursos_conocidos(ctx))
    lista = ocultos(ctx)
    if not esta_oculto(ctx, curso, lista):
        lista.append(curso)
        ctx.repo.fijar_preferencia(PREFERENCIA, json.dumps(lista, ensure_ascii=False))
    return {"curso": curso, "entregas_cerradas": _cerrar_lo_planeado(ctx, curso)}


def mostrar_curso(ctx: Contexto, texto: str) -> dict:
    lista = ocultos(ctx)
    if not lista:
        raise ErrorSkill("No tienes cursos ocultos.")
    curso = _buscar(ctx, texto, lista)
    lista = [c for c in lista if normalizar(c) != normalizar(curso)]
    ctx.repo.fijar_preferencia(PREFERENCIA, json.dumps(lista, ensure_ascii=False))
    return {"curso": curso}
