-- Esquema de la base de datos del E.D.I.T.H. (SQLite).
-- Las fechas se guardan como texto ISO 8601 con zona horaria.

CREATE TABLE IF NOT EXISTS materias (
  id            INTEGER PRIMARY KEY,
  nombre        TEXT NOT NULL UNIQUE,
  semestre      INTEGER,
  creditos      INTEGER,
  area          TEXT,
  activa        INTEGER DEFAULT 0,     -- 1 si el estudiante la cursa este semestre
  carpeta       TEXT                   -- ruta en Universidad/<Materia>
);

CREATE TABLE IF NOT EXISTS entregas (
  id            INTEGER PRIMARY KEY,
  materia_id    INTEGER NOT NULL REFERENCES materias(id),
  nombre        TEXT NOT NULL,
  descripcion   TEXT,
  fecha_limite  TEXT NOT NULL,
  peso_nota     REAL,                  -- 0.15 = 15 %
  estado        TEXT DEFAULT 'abierta',   -- abierta | entregada
  riesgo        TEXT,                  -- tranquila | ajustada | en_riesgo
  holgura       REAL
);

CREATE TABLE IF NOT EXISTS subtareas (
  id               INTEGER PRIMARY KEY,
  entrega_id       INTEGER NOT NULL REFERENCES entregas(id),
  nombre           TEXT NOT NULL,
  tipo_tarea       TEXT,               -- informe | taller | lectura | codigo | estudio | presentacion
  dificultad       INTEGER,            -- 1 a 5
  horas_usuario    REAL,               -- lo que el usuario cree
  horas_predichas  REAL,               -- lo que predice el modelo
  horas_restantes  REAL,
  horas_hechas     REAL DEFAULT 0,
  estado           TEXT DEFAULT 'pendiente'  -- pendiente | en_curso | hecha
);

CREATE TABLE IF NOT EXISTS bloques (
  id              INTEGER PRIMARY KEY,
  subtarea_id     INTEGER NOT NULL REFERENCES subtareas(id),
  inicio          TEXT NOT NULL,
  fin             TEXT NOT NULL,
  estado          TEXT DEFAULT 'pendiente', -- pendiente | hecho | a_medias | perdido
  calendar_event  TEXT                -- id del evento en el calendario
);

CREATE TABLE IF NOT EXISTS historial_tiempos (
  id              INTEGER PRIMARY KEY,
  subtarea_id     INTEGER REFERENCES subtareas(id),
  materia         TEXT,
  tipo_tarea      TEXT,
  dificultad      INTEGER,
  estimado_h      REAL,
  real_h          REAL,
  dia_semana      TEXT,
  franja          TEXT,
  fuente          TEXT                -- encuesta | sintetico | uso
);

CREATE TABLE IF NOT EXISTS archivos (
  id              INTEGER PRIMARY KEY,
  ruta            TEXT NOT NULL,
  nombre          TEXT,
  materia_id      INTEGER REFERENCES materias(id),
  entrega_id      INTEGER REFERENCES entregas(id),
  confianza       REAL,               -- probabilidad del clasificador
  confirmado      INTEGER DEFAULT 0,  -- 1 si el usuario lo confirmó
  registrado_en   TEXT
);

-- Ejemplos etiquetados por el usuario cuando el clasificador dudó.
-- Se suman al corpus en el siguiente reentrenamiento.
CREATE TABLE IF NOT EXISTS ejemplos_clasificador (
  id              INTEGER PRIMARY KEY,
  texto           TEXT NOT NULL,
  materia         TEXT NOT NULL
);

-- Calendario local: se usa cuando no hay credenciales de Google.
CREATE TABLE IF NOT EXISTS eventos_locales (
  id              INTEGER PRIMARY KEY,
  titulo          TEXT NOT NULL,
  inicio          TEXT NOT NULL,
  fin             TEXT NOT NULL,
  descripcion     TEXT
);

CREATE TABLE IF NOT EXISTS preferencias (
  clave           TEXT PRIMARY KEY,
  valor           TEXT
);

CREATE INDEX IF NOT EXISTS idx_bloques_inicio ON bloques(inicio);
CREATE INDEX IF NOT EXISTS idx_entregas_estado ON entregas(estado);
