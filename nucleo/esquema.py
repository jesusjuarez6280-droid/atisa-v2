"""Tablas compartidas y migraciones versionadas por módulo.

Cada módulo declara MIGRACIONES = [(1, fn), (2, fn), ...] donde fn(conn) crea o
cambia SUS tablas. aplicar_migraciones() corre, en orden y cada una en su propia
transacción, las que falten de 'nucleo' y de cada módulo registrado, y anota la
versión en esquema_versiones. Correrla dos veces no hace nada.
Una migración nunca borra columnas ni datos.
"""
import sqlite3

from flask import has_app_context, current_app


TABLAS_COMPARTIDAS = '''
-- Catálogo único de materiales y productos
CREATE TABLE IF NOT EXISTS materiales (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo              TEXT UNIQUE NOT NULL,
    nombre              TEXT NOT NULL,
    tipo                TEXT NOT NULL CHECK (tipo IN ('MP','ME','EM','GR','PT')),
    unidad              TEXT NOT NULL,
    material_padre_id   INTEGER REFERENCES materiales(id),
    contenido_por_pieza REAL,
    registro_sanitario  TEXT,
    forma_farmaceutica  TEXT,
    vida_util_meses     INTEGER,
    requiere_analisis   INTEGER NOT NULL DEFAULT 1,
    es_impreso          INTEGER NOT NULL DEFAULT 0,
    condiciones_almacen TEXT,
    stock_minimo        REAL NOT NULL DEFAULT 0,
    activo              INTEGER NOT NULL DEFAULT 1,
    creado_por          TEXT,
    creado_en           TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_materiales_tipo ON materiales(tipo, activo);

CREATE TABLE IF NOT EXISTS proveedores (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre    TEXT NOT NULL,
    rfc       TEXT,
    tipo      TEXT NOT NULL DEFAULT 'Materiales' CHECK (tipo IN ('Materiales','Servicios','Ambos')),
    contacto  TEXT, telefono TEXT, correo TEXT,
    estatus   TEXT NOT NULL DEFAULT 'En evaluación'
              CHECK (estatus IN ('Aprobado','Condicionado','En evaluación','No aprobado')),
    notas     TEXT,
    activo    INTEGER NOT NULL DEFAULT 1,
    creado_en TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS lotes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id       INTEGER NOT NULL REFERENCES materiales(id),
    numero_lote       TEXT NOT NULL,
    lote_proveedor    TEXT,
    proveedor_id      INTEGER REFERENCES proveedores(id),
    lote_padre_id     INTEGER REFERENCES lotes(id),
    origen            TEXT NOT NULL CHECK (origen IN ('RECEPCION','PRODUCCION','INICIAL','DEVOLUCION')),
    origen_folio      TEXT,
    fecha_entrada     TEXT NOT NULL,
    fecha_fabricacion TEXT,
    fecha_caducidad   TEXT,
    fecha_reanalisis  TEXT,
    estatus           TEXT NOT NULL DEFAULT 'CUARENTENA'
                      CHECK (estatus IN ('CUARENTENA','APROBADO','RECHAZADO','RETENIDO')),
    existencia        REAL NOT NULL DEFAULT 0 CHECK (existencia >= 0),
    ubicacion         TEXT,
    notas             TEXT,
    creado_por        TEXT,
    creado_en         TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (material_id, numero_lote)
);
CREATE INDEX IF NOT EXISTS idx_lotes_material_estatus ON lotes(material_id, estatus);
CREATE INDEX IF NOT EXISTS idx_lotes_caducidad        ON lotes(fecha_caducidad);
CREATE INDEX IF NOT EXISTS idx_lotes_padre            ON lotes(lote_padre_id);
CREATE INDEX IF NOT EXISTS idx_lotes_origen_folio     ON lotes(origen_folio);

-- Kárdex: nunca se edita ni se borra, se corrige con otro movimiento.
CREATE TABLE IF NOT EXISTS lote_movimientos (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    lote_id   INTEGER NOT NULL REFERENCES lotes(id),
    tipo      TEXT NOT NULL CHECK (tipo IN (
                'ENTRADA_RECEPCION','ENTRADA_PRODUCCION','ENTRADA_INICIAL','ENTRADA_DEVOLUCION',
                'DEVOLUCION_PRODUCCION',
                'CONSUMO_PRODUCCION','MUESTRA','VENTA','DEVOLUCION_PROVEEDOR','DESTRUCCION',
                'AJUSTE_MAS','AJUSTE_MENOS')),
    cantidad  REAL NOT NULL,
    saldo     REAL NOT NULL,
    folio     TEXT,
    motivo    TEXT,
    modulo    TEXT NOT NULL,
    usuario   TEXT NOT NULL,
    firma_id  INTEGER REFERENCES firmas(id),
    ts        TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_mov_lote  ON lote_movimientos(lote_id, id);
CREATE INDEX IF NOT EXISTS idx_mov_folio ON lote_movimientos(folio);

CREATE TABLE IF NOT EXISTS lote_historial (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lote_id         INTEGER NOT NULL REFERENCES lotes(id),
    campo           TEXT NOT NULL,
    valor_anterior  TEXT,
    valor_nuevo     TEXT,
    motivo          TEXT NOT NULL,
    folio           TEXT,
    firma_id        INTEGER REFERENCES firmas(id),
    usuario         TEXT NOT NULL,
    ts              TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_lote_hist ON lote_historial(lote_id, id);

CREATE TABLE IF NOT EXISTS equipos (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo               TEXT UNIQUE NOT NULL,
    nombre               TEXT NOT NULL,
    tipo                 TEXT NOT NULL,
    area                 TEXT,
    marca TEXT, modelo TEXT, serie TEXT, ubicacion TEXT,
    capacidad            TEXT,
    resolucion           TEXT,
    es_instrumento       INTEGER NOT NULL DEFAULT 0,
    verificacion_diaria  INTEGER NOT NULL DEFAULT 0,
    competencia_tarea_id INTEGER REFERENCES competencias_tareas(id),
    activo_id            INTEGER REFERENCES inventario_activos(id),
    estatus              TEXT NOT NULL DEFAULT 'EN_SERVICIO'
                         CHECK (estatus IN ('EN_SERVICIO','FUERA_DE_SERVICIO','BAJA')),
    vence_calibracion    TEXT,
    vence_mantenimiento  TEXT,
    ultima_verificacion  TEXT,
    creado_en            TEXT DEFAULT (datetime('now','localtime'))
);

-- Registro de firmas (inmutable)
CREATE TABLE IF NOT EXISTS firmas (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    numero_empleado TEXT NOT NULL REFERENCES usuarios(numero_empleado),
    nombre          TEXT NOT NULL,
    puesto          TEXT,
    significado     TEXT NOT NULL,
    modulo          TEXT NOT NULL,
    entidad         TEXT NOT NULL,
    entidad_id      INTEGER NOT NULL,
    detalle         TEXT,
    sesion_usuario  TEXT,
    ip              TEXT,
    ts              TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_firmas_entidad ON firmas(entidad, entidad_id);

CREATE TABLE IF NOT EXISTS folios (
    serie  TEXT NOT NULL,
    anio   INTEGER NOT NULL,
    ultimo INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (serie, anio)
);

-- Archivos adjuntos privados (fuera de static/: nunca se sirven sin sesión)
CREATE TABLE IF NOT EXISTS archivos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    modulo          TEXT NOT NULL,
    entidad         TEXT NOT NULL,
    entidad_id      INTEGER NOT NULL,
    descripcion     TEXT,
    nombre_original TEXT NOT NULL,
    ruta            TEXT NOT NULL,
    tamano          INTEGER,
    subido_por      TEXT,
    ts              TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_archivos_entidad ON archivos(entidad, entidad_id);

CREATE TABLE IF NOT EXISTS parametros (
    clave           TEXT PRIMARY KEY,
    valor           TEXT NOT NULL,
    modulo          TEXT NOT NULL,
    descripcion     TEXT,
    actualizado_por TEXT,
    actualizado_en  TEXT DEFAULT (datetime('now','localtime'))
);
'''

# Inmutables: el kárdex, el historial del lote y las firmas no se editan ni se borran.
_DISPARADORES_INMUTABLES = [
    (tabla, accion)
    for tabla in ('lote_movimientos', 'lote_historial', 'firmas')
    for accion in ('UPDATE', 'DELETE')
]


def _migracion_1(conn):
    cols = {r[1] for r in conn.execute('PRAGMA table_info(usuarios)').fetchall()}
    for col, tipo in [('pin_hash', 'TEXT'), ('pin_fallos', 'INTEGER DEFAULT 0'),
                      ('pin_bloqueado_hasta', 'TEXT'), ('roles_extra', "TEXT DEFAULT ''"),
                      ('facultades', "TEXT DEFAULT ''")]:
        if col not in cols:
            conn.execute(f'ALTER TABLE usuarios ADD COLUMN {col} {tipo}')
    for sentencia in TABLAS_COMPARTIDAS.split(';'):
        if sentencia.strip():
            conn.execute(sentencia)
    for tabla, accion in _DISPARADORES_INMUTABLES:
        conn.execute(f'''
            CREATE TRIGGER IF NOT EXISTS trg_{tabla}_no_{accion.lower()}
            BEFORE {accion} ON {tabla}
            BEGIN SELECT RAISE(ABORT, '{tabla} no se edita ni se borra'); END''')


MIGRACIONES_NUCLEO = [(1, _migracion_1)]


def _crear_tabla_versiones(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS esquema_versiones (
            modulo   TEXT PRIMARY KEY,
            version  INTEGER NOT NULL,
            aplicado TEXT DEFAULT (datetime('now','localtime'))
        )''')
    conn.commit()


def version_actual(conn, modulo):
    fila = conn.execute('SELECT version FROM esquema_versiones WHERE modulo = ?', (modulo,)).fetchone()
    return fila[0] if fila else 0


def aplicar_migraciones_de(conn, modulo, migraciones):
    """Corre las migraciones pendientes de UN módulo. Devuelve cuántas aplicó."""
    _crear_tabla_versiones(conn)
    actual = version_actual(conn, modulo)
    aplicadas = 0
    for version, fn in sorted(migraciones, key=lambda m: m[0]):
        if version <= actual:
            continue
        conn.execute('BEGIN')
        try:
            fn(conn)
            conn.execute('INSERT INTO esquema_versiones (modulo, version) VALUES (?, ?) '
                         'ON CONFLICT(modulo) DO UPDATE SET version = excluded.version, '
                         "aplicado = datetime('now','localtime')", (modulo, version))
            conn.commit()
        except Exception:
            conn.rollback()
            if has_app_context():
                current_app.logger.exception('Falló la migración %s v%s', modulo, version)
            raise
        actual = version
        aplicadas += 1
    return aplicadas


def aplicar_migraciones(conn):
    """Migraciones de la base y de cada módulo registrado (en orden de registro)."""
    from nucleo.registro import modulos_activos
    aplicar_migraciones_de(conn, 'nucleo', MIGRACIONES_NUCLEO)
    for m in modulos_activos():
        if m.migraciones:
            aplicar_migraciones_de(conn, m.clave, m.migraciones)
    # Parámetros: se guarda el default de los que aún no existan (no pisa lo ya editado).
    from nucleo.parametros import sembrar_parametros
    sembrar_parametros(conn)
    conn.commit()
