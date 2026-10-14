"""Tablas propias del módulo Equipos (prefijo eq_). El catálogo `equipos` es de la base."""

TABLAS = '''
-- Datos del equipo que solo usa este módulo (uno a uno con equipos)
CREATE TABLE IF NOT EXISTS eq_fichas (
    equipo_id          INTEGER PRIMARY KEY REFERENCES equipos(id),
    criticidad         TEXT CHECK (criticidad IN ('Alta','Media','Baja')),
    rango_uso          TEXT,
    tolerancia_proceso TEXT,
    valor_nominal      REAL,
    unidad_nominal     TEXT,
    clase              TEXT,
    proveedor_id       INTEGER REFERENCES proveedores(id),
    fecha_alta         TEXT,
    notas              TEXT
);

-- Qué se le hace a cada equipo y cada cuándo
CREATE TABLE IF NOT EXISTS eq_programas (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    equipo_id        INTEGER NOT NULL REFERENCES equipos(id),
    actividad        TEXT NOT NULL CHECK (actividad IN ('CALIBRACION','MANTENIMIENTO')),
    frecuencia_meses INTEGER NOT NULL CHECK (frecuencia_meses > 0),
    modalidad        TEXT NOT NULL DEFAULT 'Externa' CHECK (modalidad IN ('Externa','Interna')),
    proveedor_id     INTEGER REFERENCES proveedores(id),
    tareas           TEXT,
    dias_aviso       INTEGER NOT NULL DEFAULT 30,
    activo           INTEGER NOT NULL DEFAULT 1,
    UNIQUE (equipo_id, actividad)
);

-- Puntos de calibración y de verificación del día. Un punto ya usado no se borra:
-- al cambiar los puntos se desactiva (activo = 0) y se crea otro.
CREATE TABLE IF NOT EXISTS eq_puntos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    equipo_id     INTEGER NOT NULL REFERENCES equipos(id),
    uso           TEXT NOT NULL CHECK (uso IN ('CALIBRACION','VERIFICACION')),
    orden         INTEGER NOT NULL DEFAULT 1,
    nombre        TEXT,
    nominal       REAL,
    tolerancia    REAL,
    minimo        REAL,
    maximo        REAL,
    unidad        TEXT NOT NULL,
    requiere_pesa INTEGER NOT NULL DEFAULT 0,
    activo        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_eq_puntos ON eq_puntos(equipo_id, uso, orden);

CREATE TABLE IF NOT EXISTS eq_calibraciones (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    folio               TEXT UNIQUE NOT NULL,
    equipo_id           INTEGER NOT NULL REFERENCES equipos(id),
    fecha               TEXT NOT NULL,
    modalidad           TEXT NOT NULL CHECK (modalidad IN ('Externa','Interna')),
    proveedor_id        INTEGER REFERENCES proveedores(id),
    acreditacion        TEXT,
    certificado         TEXT,
    patrones            TEXT,
    ajustado            INTEGER NOT NULL DEFAULT 0,
    conforme_encontrado INTEGER NOT NULL,
    conforme_dejado     INTEGER NOT NULL,
    observaciones       TEXT,
    realizo_firma_id    INTEGER NOT NULL REFERENCES firmas(id),
    estatus             TEXT NOT NULL DEFAULT 'POR_DICTAMINAR'
                        CHECK (estatus IN ('POR_DICTAMINAR','APTO','NO_APTO','ANULADA')),
    dictamen_motivo     TEXT,
    proxima             TEXT,
    dictamino_firma_id  INTEGER REFERENCES firmas(id),
    motivo_anulacion    TEXT,
    ts                  TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_eq_cal_equipo ON eq_calibraciones(equipo_id, fecha);

CREATE TABLE IF NOT EXISTS eq_calibracion_puntos (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    calibracion_id      INTEGER NOT NULL REFERENCES eq_calibraciones(id),
    nominal             REAL NOT NULL,
    unidad              TEXT NOT NULL,
    tolerancia          REAL NOT NULL,
    encontrado          REAL NOT NULL,
    dejado              REAL NOT NULL,
    incertidumbre       REAL,
    conforme_encontrado INTEGER NOT NULL,
    conforme_dejado     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eq_cal_puntos ON eq_calibracion_puntos(calibracion_id);

CREATE TABLE IF NOT EXISTS eq_verificaciones (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    equipo_id        INTEGER NOT NULL REFERENCES equipos(id),
    fecha            TEXT NOT NULL,
    intento          INTEGER NOT NULL DEFAULT 1,
    nivelada_limpia  INTEGER NOT NULL DEFAULT 1,
    conforme         INTEGER NOT NULL,
    observaciones    TEXT,
    realizo_firma_id INTEGER NOT NULL REFERENCES firmas(id),
    ts               TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_eq_verif ON eq_verificaciones(equipo_id, fecha);

CREATE TABLE IF NOT EXISTS eq_verificacion_lecturas (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    verificacion_id INTEGER NOT NULL REFERENCES eq_verificaciones(id),
    punto_id        INTEGER NOT NULL REFERENCES eq_puntos(id),
    pesa_id         INTEGER REFERENCES equipos(id),
    lectura         REAL NOT NULL,
    error           REAL,
    conforme        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eq_verif_lect ON eq_verificacion_lecturas(verificacion_id);

CREATE TABLE IF NOT EXISTS eq_ordenes_trabajo (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    folio              TEXT UNIQUE NOT NULL,
    equipo_id          INTEGER NOT NULL REFERENCES equipos(id),
    tipo               TEXT NOT NULL CHECK (tipo IN ('PREVENTIVO','CORRECTIVO')),
    descripcion        TEXT NOT NULL,
    se_puede_usar      INTEGER,
    fecha_programada   TEXT,
    proveedor_id       INTEGER REFERENCES proveedores(id),
    estatus            TEXT NOT NULL DEFAULT 'ABIERTA'
                       CHECK (estatus IN ('ABIERTA','TERMINADA','CANCELADA')),
    tareas_hechas      TEXT,
    trabajo_realizado  TEXT,
    refacciones        TEXT,
    horas              REAL,
    afecta_medicion    INTEGER NOT NULL DEFAULT 0,
    inicio             TEXT,
    fin                TEXT,
    reporto_firma_id   INTEGER REFERENCES firmas(id),
    realizo_firma_id   INTEGER REFERENCES firmas(id),
    motivo_cancelacion TEXT,
    creado_en          TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_eq_ot_equipo ON eq_ordenes_trabajo(equipo_id, estatus);

CREATE TABLE IF NOT EXISTS eq_impactos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    equipo_id  INTEGER NOT NULL REFERENCES equipos(id),
    origen     TEXT NOT NULL CHECK (origen IN ('CALIBRACION','VERIFICACION')),
    origen_id  INTEGER NOT NULL,
    desde      TEXT NOT NULL,
    hasta      TEXT NOT NULL,
    estatus    TEXT NOT NULL DEFAULT 'PENDIENTE' CHECK (estatus IN ('PENDIENTE','EVALUADO')),
    conclusion TEXT,
    firma_id   INTEGER REFERENCES firmas(id),
    ts         TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (origen, origen_id)
);
CREATE INDEX IF NOT EXISTS idx_eq_impactos ON eq_impactos(estatus, equipo_id);

-- Fuera de servicio, regreso y baja. `causa` dice por qué salió de servicio
-- (POR_CALIBRAR, CALIBRACION, VERIFICACION, FALLA, MANUAL). firma_id va vacío solo
-- en el alta de un instrumento, que nace "por calibrar" sin que nadie firme.
CREATE TABLE IF NOT EXISTS eq_estatus_historial (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    equipo_id INTEGER NOT NULL REFERENCES equipos(id),
    anterior  TEXT NOT NULL,
    nuevo     TEXT NOT NULL,
    causa     TEXT,
    motivo    TEXT NOT NULL,
    folio     TEXT,
    firma_id  INTEGER REFERENCES firmas(id),
    usuario   TEXT NOT NULL,
    ts        TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_eq_hist ON eq_estatus_historial(equipo_id, id)
'''

# Lo firmado no se edita a mano ni se borra (regla 10); los servicios cambian solo el estatus.
_SIN_BORRAR = ('eq_calibraciones', 'eq_calibracion_puntos', 'eq_verificaciones', 'eq_verificacion_lecturas',
               'eq_ordenes_trabajo', 'eq_impactos', 'eq_estatus_historial')
_SIN_EDITAR = ('eq_calibracion_puntos', 'eq_verificaciones', 'eq_verificacion_lecturas', 'eq_estatus_historial')


def crear_tablas(conn):
    # Sentencia por sentencia: executescript() confirmaría la transacción de la migración.
    for sentencia in TABLAS.split(';'):
        if sentencia.strip():
            conn.execute(sentencia)
    for tabla in _SIN_BORRAR:
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS trg_{tabla}_no_delete BEFORE DELETE ON {tabla}
                         BEGIN SELECT RAISE(ABORT, '{tabla} no se borra'); END''')
    for tabla in _SIN_EDITAR:
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS trg_{tabla}_no_update BEFORE UPDATE ON {tabla}
                         BEGIN SELECT RAISE(ABORT, '{tabla} no se edita'); END''')


MIGRACIONES = [(1, crear_tablas)]
