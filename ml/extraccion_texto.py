"""Extracción de texto de archivos académicos (PDF, Word, PowerPoint, texto plano)."""

from __future__ import annotations

import logging
from pathlib import Path

registro = logging.getLogger(__name__)

EXTENSIONES = {".pdf", ".docx", ".pptx", ".txt", ".md"}
MAX_CARACTERES = 20_000  # suficiente para clasificar; evita leer libros enteros
MAX_PAGINAS = 15


def _pdf(ruta: Path) -> str:
    from pypdf import PdfReader

    lector = PdfReader(str(ruta))
    partes = []
    for pagina in lector.pages[:MAX_PAGINAS]:
        partes.append(pagina.extract_text() or "")
        if sum(len(p) for p in partes) > MAX_CARACTERES:
            break
    return "\n".join(partes)


def _docx(ruta: Path) -> str:
    from docx import Document

    documento = Document(str(ruta))
    partes = [p.text for p in documento.paragraphs]
    for tabla in documento.tables:
        for fila in tabla.rows:
            partes.extend(celda.text for celda in fila.cells)
    return "\n".join(partes)


def _pptx(ruta: Path) -> str:
    from pptx import Presentation

    partes = []
    for diapositiva in Presentation(str(ruta)).slides:
        for forma in diapositiva.shapes:
            if forma.has_text_frame:
                partes.append(forma.text_frame.text)
    return "\n".join(partes)


def extraer_texto(ruta: Path | str) -> str:
    """Devuelve el texto del archivo, o una cadena vacía si no se puede leer.

    Un archivo dañado, protegido o escaneado sin texto no debe detener al
    agente: se devuelve vacío y quien llama decide qué hacer.
    """
    ruta = Path(ruta)
    extension = ruta.suffix.lower()
    try:
        if extension == ".pdf":
            texto = _pdf(ruta)
        elif extension == ".docx":
            texto = _docx(ruta)
        elif extension == ".pptx":
            texto = _pptx(ruta)
        elif extension in (".txt", ".md"):
            texto = ruta.read_text(encoding="utf-8", errors="ignore")
        else:
            return ""
    except Exception as error:
        registro.warning("No se pudo leer %s: %s", ruta.name, error)
        return ""
    return texto[:MAX_CARACTERES]
