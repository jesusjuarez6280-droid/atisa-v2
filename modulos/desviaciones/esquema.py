"""Tablas propias del módulo de desviaciones y CAPA (prefijo dev_).

Diseño: 04-desviaciones-capa.md, sección 5. Una migración nunca borra columnas ni datos.
"""

TABLAS = '''
CREATE TABLE IF NOT EXISTS dev_desviaciones (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    folio                    TEXT UNIQUE NOT NULL,     -- DES-2026-0001 · QJA-2026-0001 (queja) · HAL-2026-0001 (hallazgo)
    tipo                     TEXT NOT NULL CHECK (tipo IN ('DESVIACION','QUEJA','HALLAZGO')),
    titulo                   TEXT NOT NULL,
    descripcion              TEXT NOT NULL,            -- qué pasó
    fecha_ocurrio            TEXT,
    fecha_detecto            TEXT NOT NULL,
    area                     TEXT NOT NULL,            -- Producción, Laboratorio, Almacén, Mantenimiento…
    detecto                  TEXT,                     -- quién lo detectó
    correccion_inmediata     TEXT,                     -- lo que se hizo en el momento
    -- de dónde viene (lo llena el módulo que manda reportar)
    origen_modulo            TEXT,                     -- ordenes · calidad · equipos · almacen · cinco_s
    origen_tipo              TEXT,                     -- ver "Nombres de origen" en la sección 7
    origen_id                INTEGER,
    origen_texto             TEXT,                     -- legible: "OP-2026-0031 · paso 5"
    origen_url               TEXT,                     -- para regresar al origen
    documento_folio          TEXT,                     -- folio cuyos lotes pueden estar afectados: OP-…, REC-…
    -- de qué trata (opcionales)
    material_id              INTEGER REFERENCES materiales(id),
    equipo_id                INTEGER REFERENCES equipos(id),
    proveedor_id             INTEGER REFERENCES proveedores(id),
    -- quejas
    cliente                  TEXT,
    cliente_contacto         TEXT,
    cantidad_reclamada       TEXT,
    posible_reaccion_adversa INTEGER NOT NULL DEFAULT 0,
    respuesta_cliente        TEXT,
    fecha_respuesta          TEXT,
    -- hallazgos
    auditoria                TEXT,                     -- "Visita SENASICA sep 2026", "5S Almacén 2026-10-02"
    requisito                TEXT,                     -- numeral o requisito incumplido
    -- clasificación
    clasificacion            TEXT CHECK (clasificacion IN ('CRITICA','MAYOR','MENOR')),
    responsable              TEXT REFERENCES usuarios(numero_empleado),   -- quién investiga
    vence_investigacion      TEXT,
    clasifico_firma_id       INTEGER REFERENCES firmas(id),
    -- investigación
    porques                  TEXT,                     -- los 5 porqués, uno por renglón
    m_mano_obra              TEXT,                     -- las 6M: lo que se revisó de cada una
    m_metodo                 TEXT,
    m_materiales             TEXT,
    m_maquina                TEXT,
    m_medicion               TEXT,
    m_medio_ambiente         TEXT,
    causa_raiz               TEXT,
    categoria_causa          TEXT CHECK (categoria_causa IN ('Mano de obra','Método','Materiales','Máquina',
                                                             'Medición','Medio ambiente','Proveedor','Otra')),
    sin_acciones_motivo      TEXT,                     -- si no requiere acciones, por qué (no en críticas)
    investigo_firma_id       INTEGER REFERENCES firmas(id),
    comentarios_aprobacion   TEXT,                     -- si se regresó, qué falta
    aprobo_firma_id          INTEGER REFERENCES firmas(id),
    -- estatus y cierre
    estatus                  TEXT NOT NULL DEFAULT 'REPORTADA'
                             CHECK (estatus IN ('REPORTADA','EN_INVESTIGACION','POR_APROBAR','EN_ACCIONES',
                                                'CERRADA','ANULADA')),
    conclusion               TEXT,
    cerro_firma_id           INTEGER REFERENCES firmas(id),
    cerrada_en               TEXT,
    motivo_anulacion         TEXT,
    anulo_firma_id           INTEGER REFERENCES firmas(id),
    reporto_firma_id         INTEGER NOT NULL REFERENCES firmas(id),
    reporto                  TEXT NOT NULL,            -- usuario de la sesión donde se reportó
    creado_en                TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_dev_estatus   ON dev_desviaciones(estatus);
CREATE INDEX IF NOT EXISTS idx_dev_origen    ON dev_desviaciones(origen_tipo, origen_id);
CREATE INDEX IF NOT EXISTS idx_dev_documento ON dev_desviaciones(documento_folio);

CREATE TABLE IF NOT EXISTS dev_lotes (                  -- lotes afectados y su evaluación
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    desviacion_id   INTEGER NOT NULL REFERENCES dev_desviaciones(id),
    lote_id         INTEGER NOT NULL REFERENCES lotes(id),
    impacto         TEXT NOT NULL DEFAULT 'PENDIENTE'
                    CHECK (impacto IN ('PENDIENTE','SIN_IMPACTO','CON_IMPACTO')),
    justificacion   TEXT,
    retenido        INTEGER NOT NULL DEFAULT 0,         -- 1 = esta desviación lo retuvo
    retuvo_firma_id INTEGER REFERENCES firmas(id),
    agregado_por    TEXT NOT NULL,
    ts              TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (desviacion_id, lote_id)
);
CREATE INDEX IF NOT EXISTS idx_dev_lotes_lote ON dev_lotes(lote_id);

CREATE TABLE IF NOT EXISTS dev_acciones (               -- CAPA
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    desviacion_id       INTEGER NOT NULL REFERENCES dev_desviaciones(id),
    numero              INTEGER NOT NULL,
    tipo                TEXT NOT NULL CHECK (tipo IN ('CORRECTIVA','PREVENTIVA')),
    descripcion         TEXT NOT NULL,
    responsable         TEXT NOT NULL REFERENCES usuarios(numero_empleado),
    fecha_compromiso    TEXT NOT NULL,
    curso_id            INTEGER REFERENCES cursos(id),  -- si la acción es capacitar (catálogo de Capacitación)
    estatus             TEXT NOT NULL DEFAULT 'PENDIENTE'
                        CHECK (estatus IN ('PENDIENTE','HECHA','VERIFICADA','CANCELADA')),
    como_se_hizo        TEXT,
    hecha_firma_id      INTEGER REFERENCES firmas(id),
    verifico_firma_id   INTEGER REFERENCES firmas(id),
    eficacia_fecha      TEXT,                           -- cuándo revisar si funcionó (NULL = no aplica)
    eficacia_criterio   TEXT,                           -- "no se repite en 3 meses", "0 errores en 20 pesadas"
    eficaz              INTEGER,                        -- 1 sí · 0 no
    eficacia_comentario TEXT,
    eficacia_firma_id   INTEGER REFERENCES firmas(id),
    motivo_cancelacion  TEXT,
    cancelo_firma_id    INTEGER REFERENCES firmas(id),
    creado_en           TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (desviacion_id, numero)
);
CREATE INDEX IF NOT EXISTS idx_dev_acc_resp ON dev_acciones(responsable, estatus);

CREATE TABLE IF NOT EXISTS dev_accion_personas (        -- a quién se capacita en una acción de capacitación
    accion_id       INTEGER NOT NULL REFERENCES dev_acciones(id),
    numero_empleado TEXT NOT NULL REFERENCES usuarios(numero_empleado),
    PRIMARY KEY (accion_id, numero_empleado)
);

CREATE TABLE IF NOT EXISTS dev_relacionadas (           -- parecidas, recurrentes o nacidas de una acción no eficaz
    desviacion_id  INTEGER NOT NULL REFERENCES dev_desviaciones(id),
    relacionada_id INTEGER NOT NULL REFERENCES dev_desviaciones(id),
    motivo         TEXT,
    PRIMARY KEY (desviacion_id, relacionada_id)
);

CREATE TABLE IF NOT EXISTS dev_historial (              -- cada cambio, con quién y por qué
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    desviacion_id  INTEGER NOT NULL REFERENCES dev_desviaciones(id),
    accion_id      INTEGER REFERENCES dev_acciones(id),
    campo          TEXT NOT NULL,       -- estatus, clasificacion, responsable, fecha_compromiso…
    valor_anterior TEXT,
    valor_nuevo    TEXT,
    motivo         TEXT,
    firma_id       INTEGER REFERENCES firmas(id),
    usuario        TEXT NOT NULL,
    ts             TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_dev_hist ON dev_historial(desviacion_id, id);
'''


def _sentencias(sql):
    """Separa por ';' ignorando los comentarios '--' (algunos llevan ';' o comas)."""
    limpio = '\n'.join(linea.split('--', 1)[0] for linea in sql.splitlines())
    return [s.strip() for s in limpio.split(';') if s.strip()]


def crear_tablas(conn):
    for sentencia in _sentencias(TABLAS):
        conn.execute(sentencia)


MIGRACIONES = [(1, crear_tablas)]
