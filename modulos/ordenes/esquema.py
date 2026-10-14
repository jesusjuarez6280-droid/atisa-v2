"""Tablas propias del módulo Órdenes de producción (prefijo op_).

Diseño: 02-ordenes-produccion.md, sección 5. Además de lo del diseño, la orden y la
conciliación guardan su justificación firmada (cuando el rendimiento o la conciliación
salen mal y no está instalado el módulo de Desviaciones), igual que ya la tenían los pasos.
"""

TABLAS = '''
-- Fórmula maestra (una versión por renglón)
CREATE TABLE IF NOT EXISTS op_formulas (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id      INTEGER NOT NULL REFERENCES materiales(id),
    version          INTEGER NOT NULL,
    codigo_documento TEXT,
    tamano_lote      REAL NOT NULL CHECK (tamano_lote > 0),
    rendimiento_min  REAL NOT NULL DEFAULT 98,
    rendimiento_max  REAL NOT NULL DEFAULT 102,
    receta_id        INTEGER REFERENCES recetas(id) ON DELETE SET NULL,
    estatus          TEXT NOT NULL DEFAULT 'BORRADOR'
                     CHECK (estatus IN ('BORRADOR','EN_REVISION','VIGENTE','OBSOLETA')),
    cambios          TEXT,
    elaboro          TEXT,
    reviso_firma_id  INTEGER REFERENCES firmas(id),
    aprobo_firma_id  INTEGER REFERENCES firmas(id),
    vigente_desde    TEXT,
    creado_en        TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (material_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_op_formula_vigente ON op_formulas(material_id) WHERE estatus = 'VIGENTE';

CREATE TABLE IF NOT EXISTS op_formula_componentes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    formula_id     INTEGER NOT NULL REFERENCES op_formulas(id),
    orden          INTEGER NOT NULL,
    material_id    INTEGER NOT NULL REFERENCES materiales(id),
    cantidad       REAL NOT NULL CHECK (cantidad > 0),
    tolerancia_pct REAL NOT NULL DEFAULT 1,
    es_activo      INTEGER NOT NULL DEFAULT 0,
    notas          TEXT
);
CREATE INDEX IF NOT EXISTS idx_op_fcomp ON op_formula_componentes(formula_id, orden);

CREATE TABLE IF NOT EXISTS op_formula_pasos (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    formula_id           INTEGER NOT NULL REFERENCES op_formulas(id),
    numero               INTEGER NOT NULL,
    fase                 TEXT NOT NULL DEFAULT 'Fabricación' CHECK (fase IN ('Fabricación','Envasado')),
    instruccion          TEXT NOT NULL,
    minutos              INTEGER NOT NULL DEFAULT 0,
    imagen_ruta          TEXT,
    es_critico           INTEGER NOT NULL DEFAULT 0,
    equipo_tipo          TEXT,
    competencia_tarea_id INTEGER REFERENCES competencias_tareas(id)
);
CREATE INDEX IF NOT EXISTS idx_op_fpasos ON op_formula_pasos(formula_id, numero);

CREATE TABLE IF NOT EXISTS op_formula_parametros (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    paso_id   INTEGER NOT NULL REFERENCES op_formula_pasos(id),
    nombre    TEXT NOT NULL,
    unidad    TEXT,
    valor_min REAL,
    valor_max REAL
);
CREATE INDEX IF NOT EXISTS idx_op_fparam ON op_formula_parametros(paso_id);

CREATE TABLE IF NOT EXISTS op_formula_empaque (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    formula_id         INTEGER NOT NULL REFERENCES op_formulas(id),
    presentacion_id    INTEGER NOT NULL REFERENCES materiales(id),
    material_id        INTEGER NOT NULL REFERENCES materiales(id),
    cantidad_por_pieza REAL NOT NULL CHECK (cantidad_por_pieza > 0),
    tolerancia_pct     REAL NOT NULL DEFAULT 2
);
CREATE INDEX IF NOT EXISTS idx_op_fempaque ON op_formula_empaque(formula_id, presentacion_id);

-- Orden de producción
CREATE TABLE IF NOT EXISTS op_ordenes (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    folio              TEXT UNIQUE NOT NULL,
    formula_id         INTEGER NOT NULL REFERENCES op_formulas(id),
    material_id        INTEGER NOT NULL REFERENCES materiales(id),
    numero_lote        TEXT UNIQUE,
    tamano_lote        REAL NOT NULL CHECK (tamano_lote > 0),
    factor             REAL NOT NULL,
    fecha_programada   TEXT,
    fecha_emision      TEXT,
    fecha_inicio       TEXT,
    fecha_fin          TEXT,
    fecha_revision     TEXT,
    fecha_caducidad    TEXT,
    estatus            TEXT NOT NULL DEFAULT 'BORRADOR'
                       CHECK (estatus IN ('BORRADOR','EMITIDA','EN_PROCESO','TERMINADA','REVISADA','CANCELADA')),
    granel_obtenido    REAL,
    granel_equipo_id   INTEGER REFERENCES equipos(id),
    granel_ts          TEXT,
    rendimiento_pct    REAL,
    rendimiento_fuera  INTEGER NOT NULL DEFAULT 0,
    rendimiento_justificacion        TEXT,
    rendimiento_justificacion_firma_id INTEGER REFERENCES firmas(id),
    granel_lote_id     INTEGER REFERENCES lotes(id),
    merma_granel       REAL,
    motivo_merma       TEXT,
    observaciones      TEXT,
    creo               TEXT NOT NULL,
    emitio_firma_id    INTEGER REFERENCES firmas(id),
    granel_firma_id    INTEGER REFERENCES firmas(id),
    termino_firma_id   INTEGER REFERENCES firmas(id),
    reviso_firma_id    INTEGER REFERENCES firmas(id),
    cancelo_firma_id   INTEGER REFERENCES firmas(id),
    motivo_cancelacion TEXT,
    creado_en          TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_op_ordenes_estatus ON op_ordenes(estatus);
CREATE INDEX IF NOT EXISTS idx_op_ordenes_granel  ON op_ordenes(granel_lote_id);

CREATE TABLE IF NOT EXISTS op_orden_presentaciones (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    orden_id          INTEGER NOT NULL REFERENCES op_ordenes(id),
    presentacion_id   INTEGER NOT NULL REFERENCES materiales(id),
    piezas_planeadas  INTEGER NOT NULL DEFAULT 0,
    piezas_buenas     INTEGER NOT NULL DEFAULT 0,
    piezas_rechazadas INTEGER NOT NULL DEFAULT 0,
    lote_id           INTEGER REFERENCES lotes(id),
    firma_id          INTEGER REFERENCES firmas(id),
    UNIQUE (orden_id, presentacion_id)
);
CREATE INDEX IF NOT EXISTS idx_op_opres_lote ON op_orden_presentaciones(lote_id);

CREATE TABLE IF NOT EXISTS op_orden_componentes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    orden_id         INTEGER NOT NULL REFERENCES op_ordenes(id),
    linea            INTEGER NOT NULL,
    material_id      INTEGER NOT NULL REFERENCES materiales(id),
    tipo             TEXT NOT NULL CHECK (tipo IN ('MP','ME','EM')),
    presentacion_id  INTEGER REFERENCES materiales(id),
    cantidad_teorica REAL NOT NULL,
    tolerancia_pct   REAL NOT NULL,
    es_activo        INTEGER NOT NULL DEFAULT 0,
    es_impreso       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_op_ocomp ON op_orden_componentes(orden_id, linea);

CREATE TABLE IF NOT EXISTS op_pesadas (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    orden_id                  INTEGER NOT NULL REFERENCES op_ordenes(id),
    componente_id             INTEGER NOT NULL REFERENCES op_orden_componentes(id),
    lote_id                   INTEGER NOT NULL REFERENCES lotes(id),
    cantidad                  REAL NOT NULL,
    equipo_id                 INTEGER REFERENCES equipos(id),
    lote_sugerido_id          INTEGER REFERENCES lotes(id),
    motivo_otro_lote          TEXT,
    movimiento_id             INTEGER REFERENCES lote_movimientos(id),
    realizo_firma_id          INTEGER NOT NULL REFERENCES firmas(id),
    verifico_firma_id         INTEGER REFERENCES firmas(id),
    registro_posterior        INTEGER NOT NULL DEFAULT 0,
    motivo_registro_posterior TEXT,
    ts                        TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_op_pesadas_orden  ON op_pesadas(orden_id, componente_id);
CREATE INDEX IF NOT EXISTS idx_op_pesadas_equipo ON op_pesadas(equipo_id, ts);
CREATE INDEX IF NOT EXISTS idx_op_pesadas_lote   ON op_pesadas(lote_id);

CREATE TABLE IF NOT EXISTS op_orden_pasos (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    orden_id                  INTEGER NOT NULL REFERENCES op_ordenes(id),
    numero                    INTEGER NOT NULL,
    fase                      TEXT NOT NULL,
    instruccion               TEXT NOT NULL,
    minutos                   INTEGER NOT NULL DEFAULT 0,
    imagen_ruta               TEXT,
    es_critico                INTEGER NOT NULL DEFAULT 0,
    equipo_tipo               TEXT,
    competencia_tarea_id      INTEGER,
    estatus                   TEXT NOT NULL DEFAULT 'Pendiente' CHECK (estatus IN ('Pendiente','En curso','Hecho')),
    equipo_id                 INTEGER REFERENCES equipos(id),
    inicio                    TEXT,
    fin                       TEXT,
    realizo_firma_id          INTEGER REFERENCES firmas(id),
    verifico_firma_id         INTEGER REFERENCES firmas(id),
    nivel_competencia         INTEGER,
    fuera_de_rango            INTEGER NOT NULL DEFAULT 0,
    comentario                TEXT,
    justificacion             TEXT,
    justificacion_firma_id    INTEGER REFERENCES firmas(id),
    registro_posterior        INTEGER NOT NULL DEFAULT 0,
    motivo_registro_posterior TEXT
);
CREATE INDEX IF NOT EXISTS idx_op_pasos_orden  ON op_orden_pasos(orden_id, numero);
CREATE INDEX IF NOT EXISTS idx_op_pasos_equipo ON op_orden_pasos(equipo_id);

CREATE TABLE IF NOT EXISTS op_paso_valores (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    paso_id   INTEGER NOT NULL REFERENCES op_orden_pasos(id),
    nombre    TEXT NOT NULL,
    unidad    TEXT,
    valor_min REAL,
    valor_max REAL,
    valor     REAL,
    en_rango  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_op_pvalores ON op_paso_valores(paso_id);

CREATE TABLE IF NOT EXISTS op_despejes (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    orden_id               INTEGER NOT NULL REFERENCES op_ordenes(id),
    momento                TEXT NOT NULL CHECK (momento IN ('Fabricación','Envasado')),
    area_limpia            INTEGER NOT NULL,
    sin_materiales_previos INTEGER NOT NULL,
    equipos_limpios        INTEGER NOT NULL,
    documentos_retirados   INTEGER NOT NULL,
    observaciones          TEXT,
    realizo_firma_id       INTEGER NOT NULL REFERENCES firmas(id),
    verifico_firma_id      INTEGER NOT NULL REFERENCES firmas(id),
    ts                     TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (orden_id, momento)
);

CREATE TABLE IF NOT EXISTS op_conciliaciones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    orden_id    INTEGER NOT NULL REFERENCES op_ordenes(id),
    material_id INTEGER NOT NULL REFERENCES materiales(id),
    entregadas  REAL NOT NULL,
    usadas      REAL NOT NULL,
    danadas     REAL NOT NULL DEFAULT 0,
    devueltas   REAL NOT NULL DEFAULT 0,
    diferencia  REAL NOT NULL,
    firma_id    INTEGER NOT NULL REFERENCES firmas(id),
    justificacion          TEXT,
    justificacion_firma_id INTEGER REFERENCES firmas(id),
    ts          TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (orden_id, material_id)
);
'''


def crear_tablas(conn):
    for sentencia in TABLAS.split(';'):
        if sentencia.strip():
            conn.execute(sentencia)


MIGRACIONES = [(1, crear_tablas)]
