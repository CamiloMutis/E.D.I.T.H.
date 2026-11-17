"""Pruebas de los modelos de ML y de los datos con que se entrenan."""

import json

import pandas as pd

from ajustes import RAIZ
from ml import clasificador, predictor_tiempo
from ml.corpus_sintetico import generar_corpus
from ml.datos_sinteticos import TIPOS_TAREA, generar_tiempos

PENSUM = RAIZ / "data" / "pensum.json"


def test_pensum_completo():
    materias = json.loads(PENSUM.read_text(encoding="utf-8"))["materias"]
    assert len(materias) == 51
    assert {m["semestre"] for m in materias} == set(range(1, 11))
    assert len({m["nombre"] for m in materias}) == len(materias)
    septimo = {m["nombre"] for m in materias if m["semestre"] == 7}
    assert {"Sistemas Operativos", "Inteligencia Artificial", "Arquitectura de Software"} <= septimo


def test_datos_sinteticos_reproducibles_y_dentro_de_rango():
    datos = generar_tiempos(PENSUM, filas=200, semilla=1)
    assert datos.equals(generar_tiempos(PENSUM, filas=200, semilla=1))
    assert set(datos["tipo_tarea"]) <= set(TIPOS_TAREA)
    assert datos["real_h"].between(0.25, 40).all() and datos["dificultad"].between(1, 5).all()
    assert (datos["fuente"] == "sintetico").all()
    # La falacia de la planificación está en los datos: en promedio se tarda más de lo estimado.
    assert datos["real_h"].mean() > datos["estimado_h"].mean()


def test_limpieza_descarta_valores_absurdos():
    datos = generar_tiempos(PENSUM, filas=50, semilla=2)
    datos.loc[0, "real_h"] = 90
    datos.loc[1, "estimado_h"] = None
    assert len(predictor_tiempo.limpiar(datos)) == 48


def test_el_predictor_supera_a_la_linea_base(modelos):
    predictor, _ = modelos
    assert predictor.metricas["MAE"] < predictor.metricas["MAE_linea_base"]
    facil = predictor.predecir("Sistemas Operativos", "codigo", 1, 2.0, "martes", "tarde")
    dificil = predictor.predecir("Sistemas Operativos", "codigo", 5, 2.0, "martes", "tarde")
    assert dificil > facil > 0


def test_el_clasificador_reconoce_materias_y_respeta_las_permitidas(modelos):
    _, modelo = modelos
    texto = "Taller sobre el estándar IEEE 802.11, puntos de acceso, ganancia de antena y zona de Fresnel"
    assert modelo.clasificar(texto)[0][0] == "Redes Inalámbricas"

    permitidas = ["Sistemas Operativos", "Inteligencia Artificial"]
    restringido = modelo.clasificar(texto, permitidas)
    assert {materia for materia, _ in restringido} == set(permitidas)
    assert abs(sum(p for _, p in restringido) - 1) < 1e-6
    assert modelo.clasificar("hola") == []                       # texto demasiado corto
    assert modelo.clasificar("zzzz qqqq wwww xxxx yyyy kkkk") == []  # sin vocabulario conocido


def test_el_corpus_cubre_las_materias_con_vocabulario():
    corpus = generar_corpus(PENSUM, por_materia=5)
    assert corpus["materia"].nunique() == 44
    assert not corpus["materia"].str.startswith("Electiva").any()


def test_los_ejemplos_confirmados_se_suman_al_corpus():
    base = pd.DataFrame({"texto": ["a"], "materia": ["X"]})
    combinado = clasificador.combinar_corpus(base, [{"texto": "b", "materia": "Y"}])
    assert (combinado["materia"] == "Y").sum() == 5
    assert clasificador.combinar_corpus(base, []) is base
