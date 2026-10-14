"""Tablas propias del módulo de calidad (prefijo cc_). Diseño 03, sección 5."""

TABLAS = '''
-- Especificación (una versión por renglón)
CREATE TABLE IF NOT EXISTS cc_especificaciones (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id        INTEGER NOT NULL REFERENCES materiales(id),
    version            INTEGER NOT NULL,
    codigo_documento   TEXT,
    referencia         TEXT,
    documento_id       INTEGER REFERENCES documentos_legales(id),
    cantidad_muestra   REAL,
    plan_muestreo      TEXT,
    pide_retencion     INTEGER NOT NULL DEFAULT 0,
    cantidad_retencion REAL,
    meses_reanalisis   INTEGER,
    requiere_revision  INTEGER NOT NULL DEFAULT 1,
    estatus            TEXT NOT NULL DEFAULT 'BORRADOR'
                       CHECK (estatus IN ('BORRADOR','EN_REVISION','VIGENTE','OBSOLETA')),
    cambios            TEXT,
    elaboro            TEXT,
    reviso_firma_id    INTEGER REFERENCES firmas(id),
    aprobo_firma_id    INTEGER REFERENCES firmas(id),
    vigente_desde      TEXT,
    creado_en          TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (material_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cc_esp_vigente ON cc_especificaciones(material_id) WHERE estatus = 'VIGENTE';

CREATE TABLE IF NOT EXISTS cc_especificacion_pruebas (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    especificacion_id    INTEGER NOT NULL REFERENCES cc_especificaciones(id),
    orden                INTEGER NOT NULL,
    prueba               TEXT NOT NULL,
    tipo                 TEXT NOT NULL CHECK (tipo IN ('NUMERICA','CUALITATIVA')),
    limite_min           REAL,
    limite_max           REAL,
    unidad               TEXT,
    decimales            INTEGER NOT NULL DEFAULT 1,
    texto_especificacion TEXT NOT NULL,
    metodo               TEXT,
    fuente_default       TEXT NOT NULL DEFAULT 'Interno'
                         CHECK (fuente_default IN ('Interno','Externo','Certificado del proveedor')),
    equipo_tipo          TEXT,
    cada_lote            INTEGER NOT NULL DEFAULT 1,
    en_certificado       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_cc_esp_pruebas ON cc_especificacion_pruebas(especificacion_id, orden);

-- Análisis de un lote
CREATE TABLE IF NOT EXISTS cc_analisis (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    folio                  TEXT UNIQUE NOT NULL,
    lote_id                INTEGER NOT NULL REFERENCES lotes(id),
    especificacion_id      INTEGER NOT NULL REFERENCES cc_especificaciones(id),
    tipo                   TEXT NOT NULL DEFAULT 'LIBERACION'
                           CHECK (tipo IN ('LIBERACION','REANALISIS','ESPECIAL')),
    estatus                TEXT NOT NULL DEFAULT 'EN_ANALISIS'
                           CHECK (estatus IN ('EN_ANALISIS','POR_REVISAR','CONFORME','NO_CONFORME','CANCELADO')),
    muestra_lote_id        INTEGER NOT NULL REFERENCES lotes(id),
    cantidad_muestra       REAL NOT NULL DEFAULT 0,
    contenedores           TEXT,
    muestreo_obs           TEXT,
    muestreo_firma_id      INTEGER NOT NULL REFERENCES firmas(id),
    movimiento_id          INTEGER REFERENCES lote_movimientos(id),
    externo_laboratorio_id INTEGER REFERENCES proveedores(id),
    externo_enviado        TEXT,
    reviso_firma_id        INTEGER REFERENCES firmas(id),
    observaciones          TEXT,
    motivo_cancelacion     TEXT,
    cancelo_firma_id       INTEGER REFERENCES firmas(id),
    creo                   TEXT NOT NULL,
    creado_en              TEXT DEFAULT (datetime('now','localtime')),
    terminado_en           TEXT
);
CREATE INDEX IF NOT EXISTS idx_cc_analisis_lote    ON cc_analisis(lote_id);
CREATE INDEX IF NOT EXISTS idx_cc_analisis_estatus ON cc_analisis(estatus);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cc_analisis_abierto ON cc_analisis(lote_id)
    WHERE estatus IN ('EN_ANALISIS','POR_REVISAR');

-- Resultados: nunca se editan ni se borran (una corrección o una repetición es un renglón nuevo)
CREATE TABLE IF NOT EXISTS cc_resultados (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    analisis_id      INTEGER NOT NULL REFERENCES cc_analisis(id),
    prueba_id        INTEGER NOT NULL REFERENCES cc_especificacion_pruebas(id),
    valor            REAL,
    valor_redondeado REAL,
    texto            TEXT,
    cumple           INTEGER NOT NULL,
    fuente           TEXT NOT NULL CHECK (fuente IN ('Interno','Externo','Certificado del proveedor')),
    equipo_id        INTEGER REFERENCES equipos(id),
    informe_externo  TEXT,
    fecha_analisis   TEXT NOT NULL,
    realizo_firma_id INTEGER NOT NULL REFERENCES firmas(id),
    valido           INTEGER NOT NULL DEFAULT 1,
    motivo_anulacion TEXT,
    anulo_firma_id   INTEGER REFERENCES firmas(id),
    ts               TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_cc_res_analisis ON cc_resultados(analisis_id, prueba_id);
CREATE INDEX IF NOT EXISTS idx_cc_res_equipo   ON cc_resultados(equipo_id, fecha_analisis);

-- Investigación de un resultado fuera de especificación (OOS)
CREATE TABLE IF NOT EXISTS cc_investigaciones (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    folio            TEXT UNIQUE NOT NULL,
    analisis_id      INTEGER NOT NULL REFERENCES cc_analisis(id),
    resultado_id     INTEGER NOT NULL REFERENCES cc_resultados(id),
    calculos_ok      INTEGER,
    equipo_ok        INTEGER,
    metodo_ok        INTEGER,
    reactivos_ok     INTEGER,
    muestra_ok       INTEGER,
    analista_ok      INTEGER,
    hallazgos        TEXT,
    conclusion       TEXT CHECK (conclusion IN ('ERROR_LABORATORIO','CONFIRMADO')),
    estatus          TEXT NOT NULL DEFAULT 'ABIERTA' CHECK (estatus IN ('ABIERTA','CERRADA')),
    realizo_firma_id INTEGER REFERENCES firmas(id),
    aprobo_firma_id  INTEGER REFERENCES firmas(id),
    abierta_en       TEXT DEFAULT (datetime('now','localtime')),
    cerrada_en       TEXT
);
CREATE INDEX IF NOT EXISTS idx_cc_inv_analisis ON cc_investigaciones(analisis_id);

-- Dictamen: lo que se revisó y se decidió (el cambio de estatus queda además en lote_historial)
CREATE TABLE IF NOT EXISTS cc_dictamenes (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    lote_id                INTEGER NOT NULL REFERENCES lotes(id),
    analisis_id            INTEGER REFERENCES cc_analisis(id),
    decision               TEXT NOT NULL
                           CHECK (decision IN ('APROBADO','RECHAZADO','RETENIDO','REANALISIS_EXTENDIDO')),
    estatus_anterior       TEXT NOT NULL,
    motivo                 TEXT,
    verificaciones         TEXT NOT NULL,
    lotes_cascada          TEXT,
    nueva_fecha_reanalisis TEXT,
    firma_id               INTEGER NOT NULL REFERENCES firmas(id),
    ts                     TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_cc_dict_lote ON cc_dictamenes(lote_id, id);

-- Muestras de retención
CREATE TABLE IF NOT EXISTS cc_muestras_retencion (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    lote_id           INTEGER NOT NULL REFERENCES lotes(id),
    analisis_id       INTEGER REFERENCES cc_analisis(id),
    envases           INTEGER NOT NULL CHECK (envases > 0),
    envases_actuales  INTEGER NOT NULL,
    cantidad          REAL NOT NULL,
    ubicacion         TEXT NOT NULL,
    fecha_guardado    TEXT NOT NULL,
    destruir_desde    TEXT NOT NULL,
    estatus           TEXT NOT NULL DEFAULT 'GUARDADA' CHECK (estatus IN ('GUARDADA','AGOTADA','DESTRUIDA')),
    movimiento_id     INTEGER REFERENCES lote_movimientos(id),
    guardo_firma_id   INTEGER NOT NULL REFERENCES firmas(id),
    destruyo_firma_id INTEGER REFERENCES firmas(id),
    ts                TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_cc_ret_lote ON cc_muestras_retencion(lote_id);

CREATE TABLE IF NOT EXISTS cc_muestra_usos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    muestra_id INTEGER NOT NULL REFERENCES cc_muestras_retencion(id),
    envases    INTEGER NOT NULL CHECK (envases > 0),
    motivo     TEXT NOT NULL,
    folio      TEXT,
    firma_id   INTEGER NOT NULL REFERENCES firmas(id),
    ts         TEXT DEFAULT (datetime('now','localtime'))
)
'''

# Los resultados no se editan ni se borran: solo se anulan (valido, motivo y firma de quien anuló).
_DISPARADORES = [
    """CREATE TRIGGER IF NOT EXISTS trg_cc_resultados_no_delete BEFORE DELETE ON cc_resultados
       BEGIN SELECT RAISE(ABORT, 'cc_resultados no se borra'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_cc_resultados_solo_anular BEFORE UPDATE ON cc_resultados
       WHEN NEW.analisis_id IS NOT OLD.analisis_id OR NEW.prueba_id IS NOT OLD.prueba_id
         OR NEW.valor IS NOT OLD.valor OR NEW.valor_redondeado IS NOT OLD.valor_redondeado
         OR NEW.texto IS NOT OLD.texto OR NEW.cumple IS NOT OLD.cumple OR NEW.fuente IS NOT OLD.fuente
         OR NEW.equipo_id IS NOT OLD.equipo_id OR NEW.realizo_firma_id IS NOT OLD.realizo_firma_id
         OR NEW.fecha_analisis IS NOT OLD.fecha_analisis OR (OLD.valido = 0 AND NEW.valido = 1)
       BEGIN SELECT RAISE(ABORT, 'cc_resultados no se edita: se anula y se captura de nuevo'); END""",
]


def crear_tablas(conn):
    for sentencia in TABLAS.split(';'):
        if sentencia.strip():
            conn.execute(sentencia)
    for sentencia in _DISPARADORES:
        conn.execute(sentencia)


MIGRACIONES = [(1, crear_tablas)]
