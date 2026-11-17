"""Skill ``organizar_archivos`` (Organizar).

Clasifica cada archivo nuevo por materia con el modelo entrenado, lo mueve a
``Universidad/<Materia>/<Entrega>/`` y lo vincula a su tarea. También responde
qué archivos hay de una materia o entrega.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Callable

from db.repositorio import normalizar
from ml.extraccion_texto import EXTENSIONES, extraer_texto
from skills.agenda import ErrorSkill
from skills.contexto import Contexto

# Por debajo de esta probabilidad frente a todo el pénsum, el documento
# probablemente no es académico (una factura, un instalador) y se pregunta.
CONFIANZA_ABSOLUTA_MINIMA = 0.20
SIMILITUD_MINIMA_ENTREGA = 0.5
CARACTERES_INVALIDOS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def nombre_de_carpeta(nombre: str) -> str:
    """Nombre válido como carpeta tanto en Linux como en Windows."""
    limpio = CARACTERES_INVALIDOS.sub(" ", nombre)
    limpio = " ".join(limpio.split()).rstrip(". ")
    return limpio or "Sin nombre"


def _palabras(texto: str) -> set[str]:
    return {p for p in re.findall(r"[a-z0-9]+", normalizar(texto)) if len(p) > 2 or p.isdigit()}


def vincular_entrega(ctx: Contexto, materia_id: int, nombre_archivo: str, texto: str) -> dict | None:
    """Busca la entrega abierta de la materia que mejor corresponde al archivo.

    Compara las palabras del nombre de la entrega con el nombre del archivo y
    el comienzo de su texto: "taller3.pdf" corresponde a "Taller 3".
    """
    separado = re.sub(r"(?<=[a-zA-Z])(?=\d)|(?<=\d)(?=[a-zA-Z])", " ", Path(nombre_archivo).stem)
    del_archivo = _palabras(separado) | _palabras(texto[:600])
    mejor, mejor_puntaje = None, 0.0
    for entrega in ctx.repo.entregas_abiertas(materia_id):
        de_entrega = _palabras(entrega["nombre"])
        if not de_entrega:
            continue
        puntaje = len(de_entrega & del_archivo) / len(de_entrega)
        if puntaje > mejor_puntaje:
            mejor, mejor_puntaje = entrega, puntaje
    return mejor if mejor_puntaje >= SIMILITUD_MINIMA_ENTREGA else None


def _destino_libre(carpeta: Path, nombre: str) -> Path:
    """Ruta de destino que no pisa un archivo existente: taller.pdf, taller (2).pdf..."""
    destino = carpeta / nombre
    contador = 2
    while destino.exists():
        destino = carpeta / f"{Path(nombre).stem} ({contador}){Path(nombre).suffix}"
        contador += 1
    return destino


def mover_y_registrar(
    ctx: Contexto, ruta: Path, materia: dict, texto: str, confianza: float, confirmado: bool
) -> dict:
    """Mueve el archivo a la carpeta de su materia (y entrega) y lo guarda en la base de datos."""
    entrega = vincular_entrega(ctx, materia["id"], ruta.name, texto)
    carpeta = ctx.ajustes.carpeta_destino / nombre_de_carpeta(materia["nombre"])
    if not materia.get("carpeta"):
        ctx.repo.fijar_carpeta_materia(materia["id"], str(carpeta))
    if entrega:
        carpeta = carpeta / nombre_de_carpeta(entrega["nombre"])
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = _destino_libre(carpeta, ruta.name)
    shutil.move(str(ruta), str(destino))
    ctx.repo.registrar_archivo(
        str(destino), destino.name, materia["id"], entrega["id"] if entrega else None,
        confianza, confirmado, ctx.ahora(),
    )
    return {
        "estado": "movido",
        "archivo": destino.name,
        "materia": materia["nombre"],
        "entrega": entrega["nombre"] if entrega else None,
        "destino": str(destino),
        "confianza": round(confianza, 3),
    }


def clasificar_archivo(
    ctx: Contexto, ruta: str | Path, desempate: Callable[[str, list[str]], str | None] | None = None
) -> dict:
    """Clasifica un archivo nuevo y, si hay confianza suficiente, lo mueve.

    Args:
        ruta: archivo a organizar.
        desempate: función opcional que recibe el texto y las materias
            candidatas y devuelve una de ellas (o ``None``). Es el punto
            donde entra el LLM cuando el clasificador duda.

    Returns:
        ``estado`` es ``movido`` si se organizó, o ``duda`` con las materias
        candidatas para preguntarle al usuario.
    """
    ruta = Path(ruta).expanduser()
    if not ruta.is_file():
        raise ErrorSkill(f"No encuentro el archivo {ruta}.")
    if ruta.suffix.lower() not in EXTENSIONES:
        raise ErrorSkill(f"No sé leer archivos {ruta.suffix or 'sin extensión'}.")

    activas = ctx.repo.materias_activas()
    por_nombre = {m["nombre"]: m for m in activas}
    texto = extraer_texto(ruta)
    # El nombre del archivo también informa: "taller_redes_inalambricas.pdf".
    contenido = f"{Path(ruta).stem.replace('_', ' ').replace('-', ' ')} {texto}"
    candidatas = ctx.clasificador.clasificar(contenido, list(por_nombre))

    if candidatas:
        materia, confianza = candidatas[0]
        absoluta = ctx.clasificador.confianza_absoluta(contenido, materia)
        if confianza >= ctx.ajustes.umbral_confianza and absoluta >= CONFIANZA_ABSOLUTA_MINIMA:
            return mover_y_registrar(ctx, ruta, por_nombre[materia], texto, confianza, False)

    opciones = [nombre for nombre, _ in candidatas[:3]] or list(por_nombre)[:3]
    if desempate and texto.strip():
        elegida = desempate(texto[:3000], list(por_nombre))
        if elegida in por_nombre:
            confianza = dict(candidatas).get(elegida, 0.0)
            resultado = mover_y_registrar(ctx, ruta, por_nombre[elegida], texto, confianza, False)
            resultado["decidido_por"] = "llm"
            return resultado

    return {
        "estado": "duda",
        "archivo": ruta.name,
        "ruta": str(ruta),
        "candidatas": opciones,
        "confianza": round(candidatas[0][1], 3) if candidatas else 0.0,
    }


def confirmar_archivo(ctx: Contexto, ruta: str | Path, materia: str) -> dict:
    """Organiza un archivo con la materia que eligió el usuario.

    La respuesta se guarda como ejemplo etiquetado para que el clasificador
    aprenda de ella en el próximo reentrenamiento.
    """
    ruta = Path(ruta).expanduser()
    if not ruta.is_file():
        raise ErrorSkill(f"El archivo {ruta.name} ya no está donde lo dejé.")
    encontrada = ctx.repo.buscar_materia(materia)
    if not encontrada:
        raise ErrorSkill(f"No encuentro la materia '{materia}'.")
    texto = extraer_texto(ruta)
    if texto.strip():
        ctx.repo.agregar_ejemplo_clasificador(texto[:5000], encontrada["nombre"])
    return mover_y_registrar(ctx, ruta, encontrada, texto, 1.0, True)


def consultar_archivos(ctx: Contexto, materia: str, entrega: str | None = None) -> dict:
    """Lista los archivos guardados de una materia y, si se indica, de una entrega."""
    encontrada = ctx.repo.buscar_materia(materia)
    if not encontrada:
        raise ErrorSkill(f"No encuentro la materia '{materia}'.")
    entrega_id = None
    if entrega:
        objetivo = _palabras(entrega)
        for candidata in ctx.repo.entregas_abiertas(encontrada["id"]):
            if objetivo and objetivo <= _palabras(candidata["nombre"]) | _palabras(candidata["descripcion"] or ""):
                entrega_id = candidata["id"]
                break
    archivos = ctx.repo.archivos_de(encontrada["id"], entrega_id)
    return {
        "materia": encontrada["nombre"],
        "archivos": [
            {"nombre": a["nombre"], "entrega": a["entrega"], "ruta": a["ruta"]} for a in archivos
        ],
        "total": len(archivos),
    }


def organizar_archivos(
    ctx: Contexto,
    accion: str,
    ruta: str | None = None,
    materia: str | None = None,
    entrega: str | None = None,
) -> dict:
    """Organiza un archivo por materia o consulta los archivos ya organizados.

    Args:
        accion: ``clasificar`` para ubicar un archivo nuevo, o ``consultar``
            para saber qué archivos hay de una materia o entrega.
        ruta: ruta del archivo (solo para ``clasificar``).
        materia: materia a consultar (solo para ``consultar``).
        entrega: entrega a consultar, opcional.
    """
    if accion == "clasificar":
        if not ruta:
            raise ErrorSkill("Falta la ruta del archivo.")
        return clasificar_archivo(ctx, ruta)
    if accion == "consultar":
        if not materia:
            raise ErrorSkill("Indica de qué materia quieres ver los archivos.")
        return consultar_archivos(ctx, materia, entrega)
    raise ErrorSkill("La acción debe ser 'clasificar' o 'consultar'.")


def reentrenar_clasificador(ctx: Contexto) -> dict | None:
    """Reentrena el clasificador sumando los ejemplos que confirmó el usuario."""
    import pandas as pd

    from ml import clasificador
    from ml.entrenar import RUTA_CORPUS, asegurar_datos

    ejemplos = ctx.repo.ejemplos_clasificador()
    if not ejemplos:
        return None
    asegurar_datos()
    corpus = clasificador.combinar_corpus(pd.read_csv(RUTA_CORPUS), ejemplos)
    metricas = clasificador.entrenar_y_guardar(corpus, ctx.ajustes.carpeta_modelos)
    ctx.clasificador.recargar()
    metricas["ejemplos_confirmados"] = len(ejemplos)
    return metricas
