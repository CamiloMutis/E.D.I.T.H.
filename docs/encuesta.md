# Encuesta de tiempos de estudio

El predictor de tiempo arranca con datos sintéticos. Esta encuesta los reemplaza por datos reales de estudiantes y permite responder dos preguntas del proyecto:

- ¿En qué porcentaje subestiman los estudiantes el tiempo de sus tareas, y varía por tipo de tarea?
- ¿Cuántos estudiantes han perdido o entregado tarde algo por mala planificación en el último semestre?

**Meta:** 30 a 50 personas, unas 300 filas (cada persona registra entre 5 y 10 tareas).

## Formulario (Google Forms)

Texto de presentación:

> Somos estudiantes de Ingeniería de Sistemas y estamos entrenando un modelo que predice cuánto se demora de verdad una tarea universitaria. La encuesta es anónima y toma unos 5 minutos. Piensa en tareas que hayas terminado en las últimas semanas.

Sección 1, una sola vez por persona:

| Pregunta | Tipo de respuesta |
|---|---|
| Semestre que cursas | Lista del 1 al 10 |
| En el último semestre, ¿entregaste algo tarde o no lo entregaste por falta de tiempo? | Nunca / 1 o 2 veces / 3 o más veces |
| ¿Usas alguna herramienta para planear tus entregas? | Ninguna / Agenda o cuaderno / Calendario digital / App de tareas |

Sección 2, se repite por cada tarea (el formulario permite "agregar otra tarea"):

| Pregunta | Tipo de respuesta | Columna |
|---|---|---|
| Materia | Lista con las materias del pénsum | `materia` |
| Tipo de tarea | Informe / Taller / Lectura / Código / Estudio para examen / Presentación | `tipo_tarea` |
| ¿Qué tan difícil te pareció? | Escala de 1 a 5 | `dificultad` |
| Antes de empezar, ¿cuántas horas creíste que te tomaría? | Número | `estimado_h` |
| ¿Cuántas horas te tomó en realidad? | Número | `real_h` |
| ¿Qué día de la semana la hiciste principalmente? | Lunes a domingo | `dia_semana` |
| ¿En qué momento del día? | Mañana / Tarde / Noche | `franja` |

## Del formulario al modelo

1. En Google Sheets, deja una fila por tarea con las columnas de la tabla anterior.
2. Normaliza los valores: `tipo_tarea` en minúsculas y sin tildes (`informe`, `taller`, `lectura`, `codigo`, `estudio`, `presentacion`), `dia_semana` sin tildes (`miercoles`, `sabado`) y `franja` como `manana`, `tarde` o `noche`.
3. Borra cualquier dato que identifique a una persona (correo, nombre, marca de tiempo).
4. Descarga la hoja como CSV y guárdala en `data/encuesta_tiempos.csv`.
5. Ejecuta `python -m ml.entrenar`. El entrenamiento une la encuesta con los datos sintéticos y con el historial de uso, y marca cada fila con su fuente.
6. Vuelve a ejecutar los notebooks 01 y 02 para actualizar las métricas y las gráficas del pitch.

Cuando la encuesta tenga suficientes filas, conviene entrenar solo con datos reales: basta con filtrar por `fuente` en el notebook 02 y comparar.
