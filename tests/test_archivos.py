"""Pruebas del skill organizar_archivos y de la extracción de texto."""

from datetime import timedelta
from pathlib import Path

import pytest

from ml.extraccion_texto import extraer_texto
from skills import soporte
from skills.agenda import ErrorSkill
from skills.organizar_archivos import (
    clasificar_archivo,
    confirmar_archivo,
    consultar_archivos,
    nombre_de_carpeta,
    organizar_archivos,
)
from tests.conftest import AHORA

TEXTO_SO = (
    "Taller 3. Sincronización de procesos e hilos. Implemente una solución con semáforos y "
    "exclusión mutua para la sección crítica. Use memoria compartida y tuberías y FIFO para la "
    "comunicación entre procesos. Explique el interbloqueo y el cambio de contexto."
)
TEXTO_AJENO = "Factura de servicios públicos. Valor a pagar este mes. Consumo de energía y acueducto del apartamento."


def _archivo(ctx, nombre: str, texto: str) -> Path:
    ruta = ctx.ajustes.carpeta_vigilada / nombre
    ruta.write_text(texto, encoding="utf-8")
    return ruta


def test_nombre_de_carpeta_valido_en_windows():
    assert nombre_de_carpeta('Taller 3: "hilos" / procesos?') == "Taller 3 hilos procesos"
    assert nombre_de_carpeta("Parcial 1.") == "Parcial 1"
    assert nombre_de_carpeta("???") == "Sin nombre"


def test_clasifica_mueve_y_vincula_a_la_entrega(ctx):
    limite = (AHORA + timedelta(days=6)).date().isoformat()
    soporte.registrar_entrega(ctx, "Sistemas Operativos", "Taller 3", limite, 15)
    ruta = _archivo(ctx, "taller3.txt", TEXTO_SO)

    resultado = clasificar_archivo(ctx, ruta)

    assert resultado["estado"] == "movido"
    assert resultado["materia"] == "Sistemas Operativos"
    assert resultado["entrega"] == "Taller 3"
    destino = Path(resultado["destino"])
    assert destino == ctx.ajustes.carpeta_destino / "Sistemas Operativos" / "Taller 3" / "taller3.txt"
    assert destino.exists() and not ruta.exists()
    assert consultar_archivos(ctx, "sistemas operativos")["total"] == 1


def test_no_pisa_un_archivo_con_el_mismo_nombre(ctx):
    primero = clasificar_archivo(ctx, _archivo(ctx, "guia.txt", TEXTO_SO))
    segundo = clasificar_archivo(ctx, _archivo(ctx, "guia.txt", TEXTO_SO))
    assert Path(primero["destino"]).name == "guia.txt"
    assert Path(segundo["destino"]).name == "guia (2).txt"


def test_archivo_no_academico_queda_en_duda_y_no_se_mueve(ctx):
    ruta = _archivo(ctx, "recibo.txt", TEXTO_AJENO)
    resultado = clasificar_archivo(ctx, ruta)
    assert resultado["estado"] == "duda"
    assert ruta.exists()
    assert resultado["candidatas"]


def test_el_desempate_del_llm_decide_cuando_hay_duda(ctx):
    ruta = _archivo(ctx, "apuntes.txt", TEXTO_AJENO)
    resultado = clasificar_archivo(ctx, ruta, desempate=lambda texto, materias: "Redes Inalámbricas")
    assert resultado["estado"] == "movido"
    assert resultado["materia"] == "Redes Inalámbricas"
    assert resultado["decidido_por"] == "llm"


def test_confirmar_guarda_el_ejemplo_para_reentrenar(ctx):
    ruta = _archivo(ctx, "recibo.txt", TEXTO_AJENO)
    resultado = confirmar_archivo(ctx, ruta, "Gestión de Proyectos de TI")
    assert resultado["estado"] == "movido" and resultado["confianza"] == 1.0
    ejemplos = ctx.repo.ejemplos_clasificador()
    assert ejemplos == [{"texto": TEXTO_AJENO, "materia": "Gestión de Proyectos de TI"}]


def test_errores_de_entrada(ctx):
    with pytest.raises(ErrorSkill):
        clasificar_archivo(ctx, ctx.ajustes.carpeta_vigilada / "no_existe.pdf")
    with pytest.raises(ErrorSkill):
        clasificar_archivo(ctx, _archivo(ctx, "instalador.exe", "binario"))
    with pytest.raises(ErrorSkill):
        organizar_archivos(ctx, "borrar")


def test_extraccion_de_word_powerpoint_y_pdf(tmp_path):
    from docx import Document
    from pptx import Presentation
    from pypdf import PdfWriter

    documento = Document()
    documento.add_paragraph("Semáforos y exclusión mutua")
    documento.save(tmp_path / "guia.docx")
    assert "exclusión mutua" in extraer_texto(tmp_path / "guia.docx")

    presentacion = Presentation()
    diapositiva = presentacion.slides.add_slide(presentacion.slide_layouts[0])
    diapositiva.shapes.title.text = "Planificación de procesos"
    presentacion.save(tmp_path / "clase.pptx")
    assert "Planificación de procesos" in extraer_texto(tmp_path / "clase.pptx")

    # Un PDF sin texto (o un archivo dañado) devuelve vacío en lugar de fallar.
    escritor = PdfWriter()
    escritor.add_blank_page(width=200, height=200)
    with open(tmp_path / "vacio.pdf", "wb") as salida:
        escritor.write(salida)
    assert extraer_texto(tmp_path / "vacio.pdf").strip() == ""
    (tmp_path / "roto.pdf").write_bytes(b"esto no es un pdf")
    assert extraer_texto(tmp_path / "roto.pdf") == ""
