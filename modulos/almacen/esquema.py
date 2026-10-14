"""Tablas propias del almacén (prefijo alm_). Las compartidas (materiales, proveedores,
lotes, lote_movimientos, lote_historial, firmas, archivos) son de la base."""

_TABLAS = '''
CREATE TABLE IF NOT EXISTS alm_ubicaciones (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo TEXT UNIQUE NOT NULL,
    nombre TEXT NOT NULL,
    tipo   TEXT NOT NULL DEFAULT 'General'
           CHECK (tipo IN ('General','Cuarentena','Rechazados','Refrigeración','Producto terminado')),
    activo INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS alm_recepciones (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    folio            TEXT UNIQUE NOT NULL,
    fecha            TEXT NOT NULL,
    proveedor_id     INTEGER NOT NULL REFERENCES proveedores(id),
    documento        TEXT,
    transporte       TEXT,
    observaciones    TEXT,
    estatus          TEXT NOT NULL DEFAULT 'Registrada' CHECK (estatus IN ('Registrada','Anulada')),
    recibio          TEXT NOT NULL,
    firma_id         INTEGER REFERENCES firmas(id),
    motivo_anulacion TEXT,
    anulo_firma_id   INTEGER REFERENCES firmas(id),
    creado_en        TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS alm_recepcion_partidas (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    recepcion_id      INTEGER NOT NULL REFERENCES alm_recepciones(id),
    material_id       INTEGER NOT NULL REFERENCES materiales(id),
    lote_proveedor    TEXT NOT NULL,
    cantidad          REAL NOT NULL CHECK (cantidad > 0),
    contenedores      INTEGER NOT NULL DEFAULT 1,
    fecha_caducidad   TEXT,
    fecha_reanalisis  TEXT,
    empaque_integro   INTEGER NOT NULL DEFAULT 1,
    identificacion_ok INTEGER NOT NULL DEFAULT 1,
    trae_certificado  INTEGER NOT NULL DEFAULT 0,
    limpio            INTEGER NOT NULL DEFAULT 1,
    temperatura       TEXT,
    aceptada          INTEGER NOT NULL DEFAULT 1,
    aviso_proveedor   TEXT,
    observaciones     TEXT,
    lote_id           INTEGER REFERENCES lotes(id)
);
CREATE INDEX IF NOT EXISTS idx_alm_rec_part ON alm_recepcion_partidas(recepcion_id);

CREATE TABLE IF NOT EXISTS alm_salidas (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    folio         TEXT UNIQUE NOT NULL,
    tipo          TEXT NOT NULL CHECK (tipo IN ('VENTA','DESTRUCCION','DEVOLUCION_PROVEEDOR')),
    fecha         TEXT NOT NULL,
    destino       TEXT NOT NULL,
    documento     TEXT,
    observaciones TEXT,
    usuario       TEXT NOT NULL,
    firma_id      INTEGER REFERENCES firmas(id),
    creado_en     TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS alm_salida_partidas (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    salida_id     INTEGER NOT NULL REFERENCES alm_salidas(id),
    lote_id       INTEGER NOT NULL REFERENCES lotes(id),
    cantidad      REAL NOT NULL CHECK (cantidad > 0),
    motivo_lote   TEXT,
    movimiento_id INTEGER REFERENCES lote_movimientos(id)
);
CREATE INDEX IF NOT EXISTS idx_alm_sal_part ON alm_salida_partidas(salida_id);

CREATE TABLE IF NOT EXISTS alm_devoluciones (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    folio           TEXT UNIQUE NOT NULL,
    fecha           TEXT NOT NULL,
    cliente         TEXT NOT NULL,
    lote_vendido_id INTEGER NOT NULL REFERENCES lotes(id),
    cantidad        REAL NOT NULL CHECK (cantidad > 0),
    motivo          TEXT NOT NULL,
    lote_id         INTEGER REFERENCES lotes(id),
    usuario         TEXT NOT NULL,
    creado_en       TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS alm_conteos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    folio         TEXT UNIQUE NOT NULL,
    fecha         TEXT NOT NULL,
    alcance       TEXT NOT NULL,
    estatus       TEXT NOT NULL DEFAULT 'Abierto'
                  CHECK (estatus IN ('Abierto','Capturado','Ajustado','Cancelado')),
    conto         TEXT,
    observaciones TEXT,
    firma_id      INTEGER REFERENCES firmas(id),
    creado_en     TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS alm_conteo_partidas (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    conteo_id               INTEGER NOT NULL REFERENCES alm_conteos(id),
    lote_id                 INTEGER NOT NULL REFERENCES lotes(id),
    cantidad_contada        REAL,
    existencia_al_capturar  REAL,
    ultimo_movimiento_id    INTEGER,
    motivo_diferencia       TEXT,
    movimiento_id           INTEGER REFERENCES lote_movimientos(id),
    UNIQUE (conteo_id, lote_id)
);

CREATE TABLE IF NOT EXISTS alm_etiquetas (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    lote_id INTEGER NOT NULL REFERENCES lotes(id),
    estatus TEXT NOT NULL,
    copias  INTEGER NOT NULL,
    usuario TEXT NOT NULL,
    ts      TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_alm_etiq ON alm_etiquetas(lote_id, id);
'''

UBICACIONES_INICIALES = [
    ('CUA-01', 'Cuarentena', 'Cuarentena'),
    ('MP-A1', 'Materias primas A1', 'General'),
    ('PT-01', 'Producto terminado', 'Producto terminado'),
    ('RECH-01', 'Rechazados', 'Rechazados'),
    ('REF-01', 'Refrigeración 2-8 °C', 'Refrigeración'),
]


def crear_tablas(conn):
    for sentencia in _TABLAS.split(';'):
        if sentencia.strip():
            conn.execute(sentencia)
    for codigo, nombre, tipo in UBICACIONES_INICIALES:
        conn.execute('INSERT OR IGNORE INTO alm_ubicaciones (codigo, nombre, tipo) VALUES (?, ?, ?)',
                     (codigo, nombre, tipo))


MIGRACIONES = [(1, crear_tablas)]
