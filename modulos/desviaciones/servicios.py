"""Reglas de negocio de desviaciones y CAPA (diseño 04-desviaciones-capa.md).

Servicios sin Flask: reciben la conexión abierta y NO hacen commit (el que llama firma
primero, luego llama al servicio y confirma; si algo falla, se revierte todo, incluida
la firma). Los errores se lanzan como ErrorDesviacion con un mensaje listo para flash().

Las firmas las hace la ruta con nucleo.firmas.firmar(); aquí se vuelve a revisar que
quien firmó tenga la facultad que pide cada paso (como hace nucleo.inventario).
"""
from datetime import date, datetime, timedelta

from nucleo import folios, inventario
from nucleo.auth import PERMISOS
from nucleo.firmas import firma_tiene_facultad, roles_de
from nucleo.parametros import parametro_int


class ErrorDesviacion(Exception):
    """El mensaje se puede mostrar tal cual en pantalla (flash)."""


FACULTAD = 'gestionar_desviaciones'

TIPOS = {'DESVIACION': 'DES', 'QUEJA': 'QJA', 'HALLAZGO': 'HAL'}
NOMBRE_TIPO = {'DESVIACION': 'Desviación', 'QUEJA': 'Queja de cliente', 'HALLAZGO': 'Hallazgo de auditoría'}
CLASIFICACIONES = ('CRITICA', 'MAYOR', 'MENOR')
NOMBRE_CLASIFICACION = {'CRITICA': 'Crítica', 'MAYOR': 'Mayor', 'MENOR': 'Menor'}
DEFINICION_CLASIFICACION = {
    'CRITICA': 'Puede afectar la seguridad o eficacia del producto, o el bienestar de los animales; '
               'o es un incumplimiento grave de BPM.',
    'MAYOR': 'Puede afectar la calidad del producto o es un incumplimiento importante de un procedimiento, '
             'sin riesgo directo para la seguridad.',
    'MENOR': 'No afecta la calidad del producto: un error de registro o un incumplimiento menor ya corregido.',
}
CATEGORIAS = ('Mano de obra', 'Método', 'Materiales', 'Máquina', 'Medición', 'Medio ambiente', 'Proveedor', 'Otra')
AREAS = ('Producción', 'Laboratorio', 'Almacén', 'Mantenimiento', 'Calidad', 'Envasado', 'Embarques',
         'Administración', 'Ventas')
ESTATUS = ('REPORTADA', 'EN_INVESTIGACION', 'POR_APROBAR', 'EN_ACCIONES', 'CERRADA', 'ANULADA')
NOMBRE_ESTATUS = {'REPORTADA': 'Por clasificar', 'EN_INVESTIGACION': 'En investigación',
                  'POR_APROBAR': 'Por aprobar', 'EN_ACCIONES': 'Acciones en curso',
                  'CERRADA': 'Cerrada', 'ANULADA': 'Anulada'}
ABIERTAS = ('REPORTADA', 'EN_INVESTIGACION', 'POR_APROBAR', 'EN_ACCIONES')
TIPOS_ACCION = ('CORRECTIVA', 'PREVENTIVA')
IMPACTOS = ('PENDIENTE', 'SIN_IMPACTO', 'CON_IMPACTO')
NOMBRE_IMPACTO = {'PENDIENTE': 'Por evaluar', 'SIN_IMPACTO': 'Sin impacto', 'CON_IMPACTO': 'Con impacto'}
SEIS_M = (('m_mano_obra', 'Mano de obra'), ('m_metodo', 'Método'), ('m_materiales', 'Materiales'),
          ('m_maquina', 'Máquina'), ('m_medicion', 'Medición'), ('m_medio_ambiente', 'Medio ambiente'))

# Nombres de origen (sección 7): el módulo que reporta usa el mismo al consultar.
ORIGENES = {
    'orden': 'ordenes', 'orden_paso': 'ordenes', 'orden_pesada': 'ordenes', 'orden_granel': 'ordenes',
    'orden_conciliacion': 'ordenes',
    'oos': 'calidad',
    'calibracion': 'equipos', 'verificacion_equipo': 'equipos', 'mantenimiento': 'equipos',
    'recepcion': 'almacen', 'conteo': 'almacen', 'devolucion': 'almacen',
    'cinco_s': 'cinco_s',
    'accion_no_eficaz': 'desviaciones',
}

PARAMETROS = [
    ('desviaciones.dias_investigacion_critica', '15', 'Días para investigar una desviación crítica'),
    ('desviaciones.dias_investigacion_mayor', '30', 'Días para investigar una desviación mayor'),
    ('desviaciones.dias_investigacion_menor', '30', 'Días para investigar una desviación menor'),
    ('desviaciones.dias_eficacia', '90', 'Días después de verificar una acción para revisar si funcionó'),
    ('desviaciones.dias_alerta_sin_clasificar', '2', 'Días sin clasificar para avisar en el Tablero'),
    ('desviaciones.codigo_formato', '', 'Código del formato impreso de desviación (ej. F-CAL-010 · v1)'),
]

# Campos que se capturan al reportar (además de los obligatorios).
CAMPOS_REPORTE = ('titulo', 'descripcion', 'fecha_ocurrio', 'fecha_detecto', 'area', 'detecto',
                  'correccion_inmediata', 'origen_modulo', 'origen_tipo', 'origen_id', 'origen_texto',
                  'origen_url', 'documento_folio', 'material_id', 'equipo_id', 'proveedor_id', 'cliente',
                  'cliente_contacto', 'cantidad_reclamada', 'posible_reaccion_adversa', 'auditoria', 'requisito')
CAMPOS_INVESTIGACION = ('porques', 'm_mano_obra', 'm_metodo', 'm_materiales', 'm_maquina', 'm_medicion',
                        'm_medio_ambiente', 'causa_raiz', 'categoria_causa', 'sin_acciones_motivo')


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def hoy():
    return date.today().isoformat()


def _texto(valor):
    return (str(valor).strip() if valor is not None else '') or None


def _fecha(valor, nombre):
    if valor in (None, ''):
        return None
    texto = str(valor).strip()[:10]
    try:
        return datetime.strptime(texto, '%Y-%m-%d').date().isoformat()
    except ValueError:
        raise ErrorDesviacion(f'La fecha de {nombre} no es válida (usa AAAA-MM-DD).')


def _entero(valor):
    if valor in (None, ''):
        return None
    try:
        return int(valor)
    except (TypeError, ValueError):
        raise ErrorDesviacion('Un dato numérico no es válido.')


def sumar_dias(fecha, dias):
    return (date.fromisoformat(fecha[:10]) + timedelta(days=int(dias))).isoformat()


def _exigir_facultad(conn, firma_id, para):
    if not firma_id:
        raise ErrorDesviacion(f'Para {para} se necesita la firma de alguien con la facultad «{FACULTAD}».')
    if not firma_tiene_facultad(conn, firma_id, FACULTAD):
        raise ErrorDesviacion(f'Quien firmó no tiene la facultad «{FACULTAD}» para {para}.')


def _numero_firma(conn, firma_id):
    if not firma_id:
        return None
    fila = conn.execute('SELECT numero_empleado FROM firmas WHERE id = ?', (firma_id,)).fetchone()
    return fila[0] if fila else None


def _historial(conn, desviacion_id, campo, anterior, nuevo, usuario, motivo=None, firma_id=None, accion_id=None):
    conn.execute('INSERT INTO dev_historial (desviacion_id, accion_id, campo, valor_anterior, valor_nuevo, motivo, '
                 'firma_id, usuario) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                 (desviacion_id, accion_id, campo, None if anterior is None else str(anterior),
                  None if nuevo is None else str(nuevo), _texto(motivo), firma_id, usuario or '—'))


def _cambiar_estatus(conn, d, nuevo, usuario, motivo=None, firma_id=None):
    conn.execute('UPDATE dev_desviaciones SET estatus = ? WHERE id = ?', (nuevo, d['id']))
    _historial(conn, d['id'], 'estatus', d['estatus'], nuevo, usuario, motivo, firma_id)


def desviacion(conn, desviacion_id):
    """La desviación con los nombres de su material, equipo, proveedor y responsable; None si no existe."""
    fila = conn.execute(
        'SELECT d.*, m.codigo AS material_codigo, m.nombre AS material_nombre, e.codigo AS equipo_codigo, '
        'e.nombre AS equipo_nombre, p.nombre AS proveedor_nombre, u.nombre_completo AS responsable_nombre '
        'FROM dev_desviaciones d LEFT JOIN materiales m ON m.id = d.material_id '
        'LEFT JOIN equipos e ON e.id = d.equipo_id LEFT JOIN proveedores p ON p.id = d.proveedor_id '
        'LEFT JOIN usuarios u ON u.numero_empleado = d.responsable WHERE d.id = ?', (desviacion_id,)).fetchone()
    return dict(fila) if fila else None


def _exigir(conn, desviacion_id):
    d = desviacion(conn, desviacion_id)
    if not d:
        raise ErrorDesviacion('La desviación no existe.')
    return d


def _exigir_estatus(d, *permitidos, para):
    if d['estatus'] not in permitidos:
        raise ErrorDesviacion(f"{d['folio']} está «{NOMBRE_ESTATUS[d['estatus']]}»: no se puede {para}.")


def accion(conn, accion_id):
    fila = conn.execute(
        'SELECT a.*, d.folio, d.estatus AS desviacion_estatus, d.titulo AS desviacion_titulo, '
        'u.nombre_completo AS responsable_nombre, c.nombre AS curso_nombre '
        'FROM dev_acciones a JOIN dev_desviaciones d ON d.id = a.desviacion_id '
        'LEFT JOIN usuarios u ON u.numero_empleado = a.responsable LEFT JOIN cursos c ON c.id = a.curso_id '
        'WHERE a.id = ?', (accion_id,)).fetchone()
    return dict(fila) if fila else None


def _exigir_accion(conn, accion_id):
    a = accion(conn, accion_id)
    if not a:
        raise ErrorDesviacion('La acción no existe.')
    return a


def usuarios_con_acceso(conn):
    """Personas activas que pueden ver el módulo (entre ellas se elige responsable)."""
    permitidos = set(PERMISOS.get('desviaciones', []))
    filas = conn.execute('SELECT * FROM usuarios WHERE estatus = 1 ORDER BY nombre_completo').fetchall()
    return [{'numero_empleado': u['numero_empleado'], 'nombre': u['nombre_completo'], 'puesto': u['puesto']}
            for u in filas if set(roles_de(u)) & permitidos]


def _exigir_responsable(conn, numero, para='responsable'):
    numero = _texto(numero)
    if not numero:
        raise ErrorDesviacion(f'Falta el {para}.')
    u = conn.execute('SELECT * FROM usuarios WHERE numero_empleado = ? AND estatus = 1', (numero,)).fetchone()
    if not u:
        raise ErrorDesviacion(f'El {para} no es un empleado activo.')
    if not set(roles_de(u)) & set(PERMISOS.get('desviaciones', [])):
        raise ErrorDesviacion(f"{u['nombre_completo']} no tiene acceso al módulo de desviaciones.")
    return numero


def _siguiente_id(conn, tabla):
    """Id que tomará el siguiente renglón (AUTOINCREMENT): la firma se hace antes del INSERT
    y necesita el id de lo que firma."""
    fila = conn.execute('SELECT seq FROM sqlite_sequence WHERE name = ?', (tabla,)).fetchone()
    maximo = conn.execute(f'SELECT COALESCE(MAX(id), 0) FROM {tabla}').fetchone()[0]
    return max(fila[0] if fila else 0, maximo) + 1


def siguiente_id_desviacion(conn):
    return _siguiente_id(conn, 'dev_desviaciones')


def resolver_lotes(conn, texto):
    """'L123 · MP-001, 2610015' → [lote_id]. Un número de lote solo basta si es único."""
    ids = []
    for parte in (texto or '').replace(';', ',').replace('\n', ',').split(','):
        parte = parte.strip()
        if not parte:
            continue
        numero, _, codigo = (x.strip() for x in parte.partition('·'))
        if codigo:
            filas = conn.execute('SELECT l.id FROM lotes l JOIN materiales m ON m.id = l.material_id '
                                 'WHERE l.numero_lote = ? AND m.codigo = ?', (numero, codigo)).fetchall()
        else:
            filas = conn.execute('SELECT id FROM lotes WHERE numero_lote = ?', (numero,)).fetchall()
        if not filas:
            raise ErrorDesviacion(f'No existe el lote {parte}.')
        if len(filas) > 1:
            raise ErrorDesviacion(f'El lote {numero} existe en varios materiales: elígelo de la lista '
                                  '(número · código del material).')
        if filas[0][0] not in ids:
            ids.append(filas[0][0])
    return ids


# ---------------------------------------------------------------------------
# Reportar
# ---------------------------------------------------------------------------

def reportar(conn, *, tipo, datos, firma_id, usuario, lotes=None, fecha=None, desviacion_id=None) -> int:
    """Crea la desviación (estatus REPORTADA) con su folio DES-/QJA-/HAL- y liga los lotes.
    `datos` trae los campos de CAMPOS_REPORTE. Una queja con posible reacción adversa queda
    como crítica de entrada. `desviacion_id`: el id que se usó al firmar (siguiente_id_desviacion)."""
    if tipo not in TIPOS:
        raise ErrorDesviacion('Tipo no válido (desviación, queja o hallazgo).')
    if not firma_id:
        raise ErrorDesviacion('Falta la firma de quien reporta.')
    if not _texto(usuario):
        raise ErrorDesviacion('Falta el usuario que reporta.')
    v = {c: datos.get(c) for c in CAMPOS_REPORTE if c in datos}
    for c in list(v):
        if c not in ('origen_id', 'material_id', 'equipo_id', 'proveedor_id', 'posible_reaccion_adversa'):
            v[c] = _texto(v[c])
    if not v.get('descripcion'):
        raise ErrorDesviacion('Escribe qué pasó.')
    if not v.get('area'):
        raise ErrorDesviacion('Elige el área.')
    v['fecha_detecto'] = _fecha(v.get('fecha_detecto'), 'detección')
    if not v['fecha_detecto']:
        raise ErrorDesviacion('Falta la fecha en que se detectó.')
    v['fecha_ocurrio'] = _fecha(v.get('fecha_ocurrio'), 'cuándo ocurrió')
    if v['fecha_detecto'] > hoy():
        raise ErrorDesviacion('La fecha de detección no puede ser futura.')
    if v['fecha_ocurrio'] and v['fecha_ocurrio'] > v['fecha_detecto']:
        raise ErrorDesviacion('No pudo ocurrir después de detectarse.')
    if not v.get('titulo'):
        primera = v['descripcion'].splitlines()[0]
        v['titulo'] = primera if len(primera) <= 80 else primera[:77].rstrip() + '…'
    for c in ('origen_id', 'material_id', 'equipo_id', 'proveedor_id'):
        v[c] = _entero(v.get(c))
    v['posible_reaccion_adversa'] = 1 if v.get('posible_reaccion_adversa') in (1, True, '1', 'on', 'si', 'sí') else 0
    if v.get('origen_tipo') and not v.get('origen_modulo'):
        v['origen_modulo'] = ORIGENES.get(v['origen_tipo'])
    url = v.get('origen_url')
    if url and (not url.startswith('/') or url.replace('\\', '/').startswith('//')):
        v['origen_url'] = None
    for c, tabla, nombre in (('material_id', 'materiales', 'material'), ('equipo_id', 'equipos', 'equipo'),
                             ('proveedor_id', 'proveedores', 'proveedor')):
        if v[c] and not conn.execute(f'SELECT 1 FROM {tabla} WHERE id = ?', (v[c],)).fetchone():
            raise ErrorDesviacion(f'El {nombre} no existe.')
    if tipo == 'QUEJA':
        if not v.get('cliente'):
            raise ErrorDesviacion('En una queja escribe el cliente.')
        if v['posible_reaccion_adversa']:
            v['clasificacion'] = 'CRITICA'
    else:
        for c in ('cliente', 'cliente_contacto', 'cantidad_reclamada'):
            v.pop(c, None)
        v['posible_reaccion_adversa'] = 0
    if tipo == 'HALLAZGO' and not v.get('auditoria'):
        raise ErrorDesviacion('En un hallazgo escribe de qué auditoría viene.')

    v['folio'] = folios.siguiente_folio(conn, TIPOS[tipo], fecha)
    v['tipo'] = tipo
    v['reporto_firma_id'] = firma_id
    v['reporto'] = usuario
    v['estatus'] = 'REPORTADA'
    if desviacion_id:
        v['id'] = int(desviacion_id)
    cur = conn.execute(f'INSERT INTO dev_desviaciones ({", ".join(v)}) VALUES ({", ".join("?" * len(v))})',
                       tuple(v.values()))
    did = cur.lastrowid
    _historial(conn, did, 'estatus', None, 'REPORTADA', usuario, 'Reporte', firma_id)
    if v.get('clasificacion'):
        _historial(conn, did, 'clasificacion', None, 'CRITICA', usuario, 'Queja con posible reacción adversa')
    for lote_id in lotes or []:
        _ligar(conn, did, lote_id, usuario)
    return did


# ---------------------------------------------------------------------------
# Lotes afectados
# ---------------------------------------------------------------------------

def _ligar(conn, desviacion_id, lote_id, usuario):
    if not conn.execute('SELECT 1 FROM lotes WHERE id = ?', (lote_id,)).fetchone():
        raise ErrorDesviacion('El lote no existe.')
    cur = conn.execute('INSERT OR IGNORE INTO dev_lotes (desviacion_id, lote_id, agregado_por) VALUES (?, ?, ?)',
                       (desviacion_id, lote_id, usuario))
    if cur.rowcount:
        n = conn.execute('SELECT numero_lote FROM lotes WHERE id = ?', (lote_id,)).fetchone()[0]
        _historial(conn, desviacion_id, 'lote', None, n, usuario, 'Lote afectado agregado')
    return bool(cur.rowcount)


def ligar_lote(conn, desviacion_id, lote_id, usuario) -> bool:
    """Agrega un lote afectado (impacto por evaluar). True si era nuevo."""
    d = _exigir(conn, desviacion_id)
    _exigir_estatus(d, 'REPORTADA', 'EN_INVESTIGACION', 'EN_ACCIONES', para='agregar lotes')
    return _ligar(conn, desviacion_id, lote_id, usuario)


def lotes_de_documento(conn, documento_folio):
    """Lotes que nacieron del documento de origen (orden o recepción)."""
    if not documento_folio:
        return []
    return [r[0] for r in conn.execute('SELECT id FROM lotes WHERE origen_folio = ? ORDER BY id',
                                       (documento_folio,)).fetchall()]


def lotes(conn, desviacion_id):
    """Lotes afectados con su material, estatus y evaluación."""
    return [dict(r) for r in conn.execute(
        'SELECT dl.*, l.numero_lote, l.estatus, l.origen_folio, l.lote_padre_id, l.existencia, '
        'l.fecha_caducidad, m.codigo AS material_codigo, m.nombre AS material_nombre, m.tipo AS material_tipo, '
        'm.unidad, f.nombre AS retuvo_nombre FROM dev_lotes dl JOIN lotes l ON l.id = dl.lote_id '
        'JOIN materiales m ON m.id = l.material_id LEFT JOIN firmas f ON f.id = dl.retuvo_firma_id '
        'WHERE dl.desviacion_id = ? ORDER BY dl.id', (desviacion_id,)).fetchall()]


def lotes_sin_agregar(conn, d):
    """Lotes del documento de origen que nadie ha agregado a la desviación."""
    ligados = {r[0] for r in conn.execute('SELECT lote_id FROM dev_lotes WHERE desviacion_id = ?', (d['id'],))}
    return [lid for lid in lotes_de_documento(conn, d['documento_folio']) if lid not in ligados]


def retener(conn, desviacion_id, lote_id, *, firma_id, usuario) -> list:
    """Retiene un lote APROBADO (y, si es granel, sus presentaciones) con el servicio de la base.
    Devuelve los ids de lotes retenidos."""
    d = _exigir(conn, desviacion_id)
    _exigir_estatus(d, *ABIERTAS, para='retener lotes')
    _exigir_facultad(conn, firma_id, 'retener un lote')
    l = inventario.lote(conn, lote_id)
    if not l:
        raise ErrorDesviacion('El lote no existe.')
    if l['estatus'] != 'APROBADO':
        raise ErrorDesviacion(f"El lote {l['numero_lote']} está en {l['estatus']}: solo se retiene un lote aprobado.")
    _ligar(conn, desviacion_id, lote_id, usuario)
    try:
        afectados = inventario.cambiar_estatus(conn, lote_id=lote_id, nuevo='RETENIDO', usuario=usuario,
                                               motivo=f"Desviación {d['folio']}: {d['titulo']}",
                                               firma_id=firma_id, folio=d['folio'])
    except inventario.ErrorInventario as e:
        raise ErrorDesviacion(str(e))
    for lid in afectados:
        _ligar(conn, desviacion_id, lid, usuario)
        conn.execute('UPDATE dev_lotes SET retenido = 1, retuvo_firma_id = ? WHERE desviacion_id = ? AND lote_id = ?',
                     (firma_id, desviacion_id, lid))
        n = conn.execute('SELECT numero_lote FROM lotes WHERE id = ?', (lid,)).fetchone()[0]
        _historial(conn, desviacion_id, 'retencion', 'APROBADO', 'RETENIDO', usuario, f'Lote {n} retenido', firma_id)
    return afectados


def evaluar_lote(conn, desviacion_id, lote_id, *, impacto, justificacion, usuario):
    """Sin impacto / con impacto, con justificación. Ya evaluado, solo se cambia mientras se investiga."""
    d = _exigir(conn, desviacion_id)
    _exigir_estatus(d, 'REPORTADA', 'EN_INVESTIGACION', 'EN_ACCIONES', para='evaluar lotes')
    if impacto not in ('SIN_IMPACTO', 'CON_IMPACTO'):
        raise ErrorDesviacion('Elige si el lote tuvo impacto o no.')
    justificacion = _texto(justificacion)
    if not justificacion:
        raise ErrorDesviacion('Escribe por qué el lote tuvo o no impacto.')
    if lote_id in lotes_sin_agregar(conn, d):
        _ligar(conn, desviacion_id, lote_id, usuario)
    fila = conn.execute('SELECT * FROM dev_lotes WHERE desviacion_id = ? AND lote_id = ?',
                        (desviacion_id, lote_id)).fetchone()
    if not fila:
        raise ErrorDesviacion('Ese lote no está ligado a la desviación.')
    if fila['impacto'] != 'PENDIENTE' and d['estatus'] == 'EN_ACCIONES':
        raise ErrorDesviacion('La investigación ya se aprobó: esa evaluación ya no se cambia.')
    conn.execute('UPDATE dev_lotes SET impacto = ?, justificacion = ? WHERE id = ?', (impacto, justificacion, fila['id']))
    n = conn.execute('SELECT numero_lote FROM lotes WHERE id = ?', (lote_id,)).fetchone()[0]
    _historial(conn, desviacion_id, f'impacto lote {n}', fila['impacto'], impacto, usuario, justificacion)


def bloqueos_de_lote(conn, lote_id) -> list:
    """Regla 5: motivos que impiden liberar el lote (o el granel del que salió una presentación)."""
    fila = conn.execute('SELECT id, numero_lote, origen_folio, lote_padre_id FROM lotes WHERE id = ?',
                        (lote_id,)).fetchone()
    if not fila:
        return []
    revisar = [(fila, 'este lote')]
    if fila['lote_padre_id']:
        padre = conn.execute('SELECT id, numero_lote, origen_folio, lote_padre_id FROM lotes WHERE id = ?',
                             (fila['lote_padre_id'],)).fetchone()
        if padre:
            revisar.append((padre, f"el granel {padre['numero_lote']}"))
    motivos = []
    for l, cual in revisar:
        for r in conn.execute(
                "SELECT d.folio, dl.impacto FROM dev_lotes dl JOIN dev_desviaciones d ON d.id = dl.desviacion_id "
                "WHERE dl.lote_id = ? AND d.estatus <> 'ANULADA' AND dl.impacto IN ('PENDIENTE', 'CON_IMPACTO') "
                'ORDER BY d.id', (l['id'],)).fetchall():
            if r['impacto'] == 'PENDIENTE':
                motivos.append(f"{r['folio']}: falta evaluar el impacto en {cual}.")
            else:
                motivos.append(f"{r['folio']}: la desviación afectó a {cual}; solo se puede rechazar.")
        if l['origen_folio']:
            for r in conn.execute(
                    "SELECT folio FROM dev_desviaciones WHERE documento_folio = ? AND estatus NOT IN ('ANULADA', 'CERRADA') "
                    'AND id NOT IN (SELECT desviacion_id FROM dev_lotes WHERE lote_id = ?) ORDER BY id',
                    (l['origen_folio'], l['id'])).fetchall():
                motivos.append(f"{r['folio']}: falta evaluar el impacto en {cual} (viene de {l['origen_folio']}).")
    return motivos


def desviaciones_de_lote(conn, lote_id):
    """Las desviaciones que afectan al lote: ligadas o por su documento de origen."""
    l = conn.execute('SELECT origen_folio FROM lotes WHERE id = ?', (lote_id,)).fetchone()
    if not l:
        return []
    filas = conn.execute(
        'SELECT d.id, d.folio, d.titulo, d.estatus, d.clasificacion, dl.impacto, dl.retenido '
        'FROM dev_desviaciones d LEFT JOIN dev_lotes dl ON dl.desviacion_id = d.id AND dl.lote_id = ? '
        "WHERE dl.id IS NOT NULL OR (d.documento_folio IS NOT NULL AND d.documento_folio = ? "
        "AND d.estatus NOT IN ('ANULADA', 'CERRADA')) ORDER BY d.id DESC",
        (lote_id, l['origen_folio'])).fetchall()
    return [dict(r, impacto=r['impacto'] or 'PENDIENTE') for r in filas]


def alcance(conn, desviacion_id) -> list:
    """Trazabilidad hacia adelante de cada lote afectado: presentaciones, órdenes donde se usó,
    lotes producidos y ventas (cliente y cantidad)."""
    renglones, vistos = [], set()
    for l in lotes(conn, desviacion_id):
        try:
            traza = inventario.trazabilidad(conn, l['lote_id'])
        except inventario.ErrorInventario:
            continue
        for r in traza['hacia_adelante']:
            clave = (r['relacion'], r['lote_id'], r['folio'], r['cliente'], r['cantidad'], r['fecha'])
            if clave in vistos:
                continue
            vistos.add(clave)
            renglones.append({**r, 'desde_numero_lote': l['numero_lote']})
    return renglones


def agregar_alcance(conn, desviacion_id, usuario) -> int:
    """"Agregar todos": liga los lotes del alcance (presentaciones y lotes producidos)."""
    d = _exigir(conn, desviacion_id)
    _exigir_estatus(d, 'REPORTADA', 'EN_INVESTIGACION', 'EN_ACCIONES', para='agregar lotes')
    n = 0
    for r in alcance(conn, desviacion_id):
        if r['relacion'] in ('PRESENTACION', 'PRODUCIDO') and r['lote_id']:
            n += 1 if _ligar(conn, desviacion_id, r['lote_id'], usuario) else 0
    return n


# ---------------------------------------------------------------------------
# Clasificar
# ---------------------------------------------------------------------------

def dias_investigacion(conn, clasificacion):
    return parametro_int(f'desviaciones.dias_investigacion_{clasificacion.lower()}', conn)


def clasificar(conn, desviacion_id, *, clasificacion, responsable, vence=None, firma_id, usuario, lotes=None):
    """REPORTADA → EN_INVESTIGACION con clasificación, responsable y fecha límite (por defecto sale
    de la clasificación). Liga los lotes que se indiquen (los del documento de origen se proponen)."""
    d = _exigir(conn, desviacion_id)
    _exigir_estatus(d, 'REPORTADA', para='clasificar')
    _exigir_facultad(conn, firma_id, 'clasificar')
    if clasificacion not in CLASIFICACIONES:
        raise ErrorDesviacion('Elige la clasificación (crítica, mayor o menor).')
    if d['posible_reaccion_adversa'] and clasificacion != 'CRITICA':
        raise ErrorDesviacion('Una queja con posible reacción adversa es crítica.')
    responsable = _exigir_responsable(conn, responsable, 'responsable de investigar')
    vence = _fecha(vence, 'fecha límite') or sumar_dias(hoy(), dias_investigacion(conn, clasificacion))
    if vence < hoy():
        raise ErrorDesviacion('La fecha límite no puede ser pasada.')
    for lote_id in lotes or []:
        _ligar(conn, desviacion_id, lote_id, usuario)
    conn.execute('UPDATE dev_desviaciones SET clasificacion = ?, responsable = ?, vence_investigacion = ?, '
                 'clasifico_firma_id = ? WHERE id = ?', (clasificacion, responsable, vence, firma_id, desviacion_id))
    if d['clasificacion'] != clasificacion:
        _historial(conn, desviacion_id, 'clasificacion', d['clasificacion'], clasificacion, usuario, None, firma_id)
    _historial(conn, desviacion_id, 'responsable', None, responsable, usuario, None, firma_id)
    _historial(conn, desviacion_id, 'vence_investigacion', None, vence, usuario, None, firma_id)
    _cambiar_estatus(conn, d, 'EN_INVESTIGACION', usuario, 'Clasificada', firma_id)


def cerrar_al_clasificar(conn, desviacion_id, *, justificacion, firma_id, usuario):
    """Regla 3: una menor sin causa que investigar se cierra al clasificarla, con la corrección
    hecha y una justificación; si tiene lotes, igual se evalúa cada uno."""
    d = _exigir(conn, desviacion_id)
    _exigir_estatus(d, 'REPORTADA', para='cerrar al clasificar')
    _exigir_facultad(conn, firma_id, 'cerrar')
    if d['posible_reaccion_adversa']:
        raise ErrorDesviacion('Una queja con posible reacción adversa es crítica: se investiga.')
    if d['tipo'] == 'QUEJA':
        raise ErrorDesviacion('Una queja de cliente se investiga y se le responde al cliente antes de cerrar.')
    justificacion = _texto(justificacion)
    if not justificacion:
        raise ErrorDesviacion('Escribe por qué no hace falta investigar.')
    if not d['correccion_inmediata']:
        raise ErrorDesviacion('Solo se cierra al clasificar si ya se hizo la corrección inmediata; '
                              'si no, clasifícala e investígala.')
    if lotes_sin_agregar(conn, d) or any(l['impacto'] == 'PENDIENTE' for l in lotes(conn, desviacion_id)):
        raise ErrorDesviacion('Antes de cerrar evalúa el impacto en cada lote (también los del documento de origen).')
    ahora = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute("UPDATE dev_desviaciones SET clasificacion = 'MENOR', clasifico_firma_id = ?, conclusion = ?, "
                 'cerro_firma_id = ?, cerrada_en = ? WHERE id = ?',
                 (firma_id, justificacion, firma_id, ahora, desviacion_id))
    _historial(conn, desviacion_id, 'clasificacion', d['clasificacion'], 'MENOR', usuario, None, firma_id)
    _cambiar_estatus(conn, d, 'CERRADA', usuario, f'Cerrada al clasificar: {justificacion}', firma_id)


# ---------------------------------------------------------------------------
# Investigación
# ---------------------------------------------------------------------------

def puede_investigar(d, numero_empleado, facultades):
    return d['estatus'] == 'EN_INVESTIGACION' and (
        str(d['responsable'] or '') == str(numero_empleado or '') or FACULTAD in (facultades or []))


def guardar_investigacion(conn, desviacion_id, datos, usuario):
    d = _exigir(conn, desviacion_id)
    _exigir_estatus(d, 'EN_INVESTIGACION', para='editar la investigación')
    v = {c: _texto(datos.get(c)) for c in CAMPOS_INVESTIGACION if c in datos}
    if v.get('categoria_causa') and v['categoria_causa'] not in CATEGORIAS:
        raise ErrorDesviacion('Categoría de causa no válida.')
    if 'descripcion_ampliada' in datos and _texto(datos.get('descripcion_ampliada')):
        _historial(conn, desviacion_id, 'comentario', None, _texto(datos['descripcion_ampliada']), usuario,
                   'Descripción ampliada')
    cambios = {k: x for k, x in v.items() if d[k] != x}
    if not cambios:
        return
    conn.execute(f'UPDATE dev_desviaciones SET {", ".join(f"{k} = ?" for k in cambios)} WHERE id = ?',
                 (*cambios.values(), desviacion_id))
    for k in ('causa_raiz', 'categoria_causa'):
        if k in cambios:
            _historial(conn, desviacion_id, k, d[k], cambios[k], usuario)


def parecidas(conn, d, meses=12) -> list:
    """Otras desviaciones de los últimos 12 meses con el mismo material o equipo, o la misma
    área y categoría de causa (sin las anuladas)."""
    desde = (date.today() - timedelta(days=round(meses * 30.44))).isoformat()
    condiciones, params = [], []
    if d.get('material_id'):
        condiciones.append('material_id = ?')
        params.append(d['material_id'])
    if d.get('equipo_id'):
        condiciones.append('equipo_id = ?')
        params.append(d['equipo_id'])
    if d.get('categoria_causa'):
        condiciones.append('(area = ? AND categoria_causa = ?)')
        params += [d['area'], d['categoria_causa']]
    if not condiciones:
        return []
    filas = conn.execute(
        f"SELECT id, folio, titulo, area, estatus, clasificacion, categoria_causa, fecha_detecto, material_id, "
        f"equipo_id FROM dev_desviaciones WHERE id <> ? AND estatus <> 'ANULADA' AND fecha_detecto >= ? "
        f"AND ({' OR '.join(condiciones)}) ORDER BY fecha_detecto DESC, id DESC",
        (d['id'], desde, *params)).fetchall()
    salida = []
    for r in filas:
        por = []
        if d.get('material_id') and r['material_id'] == d['material_id']:
            por.append('mismo material')
        if d.get('equipo_id') and r['equipo_id'] == d['equipo_id']:
            por.append('mismo equipo')
        if d.get('categoria_causa') and r['area'] == d['area'] and r['categoria_causa'] == d['categoria_causa']:
            por.append('misma área y causa')
        salida.append({**dict(r), 'por': ', '.join(por)})
    return salida


def relacionadas(conn, desviacion_id):
    return [dict(r) for r in conn.execute(
        'SELECT d.id, d.folio, d.titulo, d.estatus, r.motivo FROM dev_relacionadas r '
        'JOIN dev_desviaciones d ON d.id = CASE WHEN r.desviacion_id = ? THEN r.relacionada_id ELSE r.desviacion_id END '
        'WHERE r.desviacion_id = ? OR r.relacionada_id = ? ORDER BY d.id',
        (desviacion_id, desviacion_id, desviacion_id)).fetchall()]


def relacionar(conn, desviacion_id, otra_id, motivo, usuario):
    d = _exigir(conn, desviacion_id)
    otra = _exigir(conn, otra_id)
    if d['id'] == otra['id']:
        raise ErrorDesviacion('Una desviación no se liga consigo misma.')
    if conn.execute('SELECT 1 FROM dev_relacionadas WHERE (desviacion_id = ? AND relacionada_id = ?) '
                    'OR (desviacion_id = ? AND relacionada_id = ?)', (d['id'], otra['id'], otra['id'], d['id'])).fetchone():
        return
    conn.execute('INSERT INTO dev_relacionadas (desviacion_id, relacionada_id, motivo) VALUES (?, ?, ?)',
                 (d['id'], otra['id'], _texto(motivo) or 'Parecida'))
    _historial(conn, d['id'], 'relacionada', None, otra['folio'], usuario, motivo)


def faltantes_envio(conn, desviacion_id) -> list:
    """Regla 6: lo que falta para enviar la investigación a aprobación."""
    d = _exigir(conn, desviacion_id)
    faltan = []
    if not d['causa_raiz']:
        faltan.append('Escribe la causa raíz.')
    if not d['categoria_causa']:
        faltan.append('Elige la categoría de la causa.')
    for l in lotes(conn, desviacion_id):
        if l['impacto'] == 'PENDIENTE' or not l['justificacion']:
            faltan.append(f"Evalúa el impacto en el lote {l['numero_lote']}.")
    for lid in lotes_sin_agregar(conn, d):
        n = conn.execute('SELECT numero_lote FROM lotes WHERE id = ?', (lid,)).fetchone()[0]
        faltan.append(f"Evalúa el impacto en el lote {n} (viene de {d['documento_folio']}).")
    activas = acciones(conn, desviacion_id, activas=True)
    if not activas:
        if d['clasificacion'] == 'CRITICA':
            faltan.append('Una desviación crítica necesita al menos una acción.')
        elif d['categoria_causa'] == 'Mano de obra' and not d['sin_acciones_motivo']:
            faltan.append('La causa es de mano de obra: agrega una acción (por ejemplo, capacitación) '
                          'o explica por qué no hace falta.')
        elif not d['sin_acciones_motivo']:
            faltan.append('Agrega al menos una acción o explica por qué no hacen falta.')
    if len(parecidas(conn, d)) >= 2 and not any(a['tipo'] == 'PREVENTIVA' for a in activas):
        faltan.append('Ya pasó algo parecido 2 o más veces en 12 meses: agrega al menos una acción preventiva.')
    return faltan


def enviar(conn, desviacion_id, *, firma_id, usuario):
    """EN_INVESTIGACION → POR_APROBAR, con la firma de quien investigó."""
    d = _exigir(conn, desviacion_id)
    _exigir_estatus(d, 'EN_INVESTIGACION', para='enviar a aprobación')
    if not firma_id:
        raise ErrorDesviacion('Falta la firma de quien investigó.')
    faltan = faltantes_envio(conn, desviacion_id)
    if faltan:
        raise ErrorDesviacion('Falta para enviar: ' + ' '.join(faltan))
    conn.execute('UPDATE dev_desviaciones SET investigo_firma_id = ?, comentarios_aprobacion = NULL WHERE id = ?',
                 (firma_id, desviacion_id))
    _cambiar_estatus(conn, d, 'POR_APROBAR', usuario, 'Investigación enviada a aprobación', firma_id)


def aprobar(conn, desviacion_id, *, firma_id, usuario):
    """Regla 7: POR_APROBAR → EN_ACCIONES, firmado por gestionar_desviaciones, distinto de quien investigó."""
    d = _exigir(conn, desviacion_id)
    _exigir_estatus(d, 'POR_APROBAR', para='aprobar')
    _exigir_facultad(conn, firma_id, 'aprobar la investigación')
    if _numero_firma(conn, firma_id) == _numero_firma(conn, d['investigo_firma_id']):
        raise ErrorDesviacion('Quien investigó no puede aprobar su propia investigación.')
    conn.execute('UPDATE dev_desviaciones SET aprobo_firma_id = ? WHERE id = ?', (firma_id, desviacion_id))
    _cambiar_estatus(conn, d, 'EN_ACCIONES', usuario, 'Investigación aprobada', firma_id)


def regresar(conn, desviacion_id, *, comentarios, usuario):
    d = _exigir(conn, desviacion_id)
    _exigir_estatus(d, 'POR_APROBAR', para='regresar')
    comentarios = _texto(comentarios)
    if not comentarios:
        raise ErrorDesviacion('Escribe qué le falta a la investigación.')
    conn.execute('UPDATE dev_desviaciones SET comentarios_aprobacion = ?, investigo_firma_id = NULL WHERE id = ?',
                 (comentarios, desviacion_id))
    _cambiar_estatus(conn, d, 'EN_INVESTIGACION', usuario, f'Regresada: {comentarios}')


# ---------------------------------------------------------------------------
# Acciones (CAPA)
# ---------------------------------------------------------------------------

def acciones(conn, desviacion_id, activas=False):
    sql = ('SELECT a.*, u.nombre_completo AS responsable_nombre, c.nombre AS curso_nombre, '
           'fh.nombre AS hecha_nombre, fh.ts AS hecha_ts, fv.nombre AS verifico_nombre, fv.ts AS verifico_ts, '
           'fe.nombre AS eficacia_nombre, fe.ts AS eficacia_ts '
           'FROM dev_acciones a LEFT JOIN usuarios u ON u.numero_empleado = a.responsable '
           'LEFT JOIN cursos c ON c.id = a.curso_id LEFT JOIN firmas fh ON fh.id = a.hecha_firma_id '
           'LEFT JOIN firmas fv ON fv.id = a.verifico_firma_id LEFT JOIN firmas fe ON fe.id = a.eficacia_firma_id '
           'WHERE a.desviacion_id = ?')
    if activas:
        sql += " AND a.estatus <> 'CANCELADA'"
    return [dict(r) for r in conn.execute(sql + ' ORDER BY a.numero', (desviacion_id,)).fetchall()]


def agregar_accion(conn, desviacion_id, *, tipo, descripcion, responsable, fecha_compromiso, usuario,
                   curso_id=None, personas=None, eficacia_fecha=None, eficacia_criterio=None) -> int:
    d = _exigir(conn, desviacion_id)
    _exigir_estatus(d, 'EN_INVESTIGACION', 'EN_ACCIONES', para='agregar acciones')
    if tipo not in TIPOS_ACCION:
        raise ErrorDesviacion('Elige si la acción es correctiva o preventiva.')
    descripcion = _texto(descripcion)
    if not descripcion:
        raise ErrorDesviacion('Escribe qué se va a hacer.')
    responsable = _exigir_responsable(conn, responsable, 'responsable de la acción')
    fecha_compromiso = _fecha(fecha_compromiso, 'compromiso')
    if not fecha_compromiso:
        raise ErrorDesviacion('Falta la fecha compromiso.')
    if fecha_compromiso < hoy():
        raise ErrorDesviacion('La fecha compromiso no puede ser pasada.')
    curso_id = _entero(curso_id)
    personas = [p for p in dict.fromkeys(str(x).strip() for x in (personas or [])) if p]
    if curso_id:
        if not conn.execute('SELECT 1 FROM cursos WHERE id = ?', (curso_id,)).fetchone():
            raise ErrorDesviacion('El curso no existe.')
        if not personas:
            raise ErrorDesviacion('Elige a quién se va a capacitar.')
        for p in personas:
            if not conn.execute('SELECT 1 FROM usuarios WHERE numero_empleado = ? AND estatus = 1', (p,)).fetchone():
                raise ErrorDesviacion(f'El empleado {p} no existe o no está activo.')
    else:
        personas = []
    eficacia_fecha = _fecha(eficacia_fecha, 'revisión de eficacia')
    eficacia_criterio = _texto(eficacia_criterio)
    if eficacia_criterio and not eficacia_fecha:
        eficacia_fecha = sumar_dias(fecha_compromiso, parametro_int('desviaciones.dias_eficacia', conn))
    if eficacia_fecha and not eficacia_criterio:
        raise ErrorDesviacion('Escribe con qué criterio se va a revisar si la acción funcionó.')
    if eficacia_fecha and eficacia_fecha < fecha_compromiso:
        raise ErrorDesviacion('La revisión de eficacia va después de la fecha compromiso.')
    numero = conn.execute('SELECT COALESCE(MAX(numero), 0) + 1 FROM dev_acciones WHERE desviacion_id = ?',
                          (desviacion_id,)).fetchone()[0]
    cur = conn.execute('INSERT INTO dev_acciones (desviacion_id, numero, tipo, descripcion, responsable, '
                       'fecha_compromiso, curso_id, eficacia_fecha, eficacia_criterio) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (desviacion_id, numero, tipo, descripcion, responsable, fecha_compromiso, curso_id,
                        eficacia_fecha, eficacia_criterio))
    aid = cur.lastrowid
    for p in personas:
        conn.execute('INSERT INTO dev_accion_personas (accion_id, numero_empleado) VALUES (?, ?)', (aid, p))
    _historial(conn, desviacion_id, 'accion', None, f'{numero}. {descripcion}', usuario,
               f'{tipo.capitalize()} · {responsable} · {fecha_compromiso}', accion_id=aid)
    return aid


def avance_capacitacion(conn, accion_id):
    """Personas de una acción de capacitación y si ya tienen el curso «Tomado» con fecha posterior
    a la acción. None si la acción no es de capacitación."""
    a = conn.execute('SELECT curso_id, creado_en FROM dev_acciones WHERE id = ?', (accion_id,)).fetchone()
    if not a or not a['curso_id']:
        return None
    desde = (a['creado_en'] or '')[:10]
    personas = []
    for r in conn.execute(
            'SELECT p.numero_empleado, u.nombre_completo, cr.estatus, cr.fecha FROM dev_accion_personas p '
            'LEFT JOIN usuarios u ON u.numero_empleado = p.numero_empleado '
            'LEFT JOIN capacitacion_registro cr ON cr.numero_empleado = p.numero_empleado AND cr.curso_id = ? '
            'WHERE p.accion_id = ? ORDER BY u.nombre_completo', (a['curso_id'], accion_id)).fetchall():
        tomado = (r['estatus'] or '').lower() == 'tomado' and bool(r['fecha']) and r['fecha'][:10] >= desde
        personas.append({'numero_empleado': r['numero_empleado'], 'nombre': r['nombre_completo'],
                         'fecha': r['fecha'] if tomado else None, 'tomado': tomado})
    hechas = sum(1 for p in personas if p['tomado'])
    return {'personas': personas, 'tomadas': hechas, 'total': len(personas),
            'completa': bool(personas) and hechas == len(personas)}


def reprogramar(conn, accion_id, *, motivo, firma_id, usuario, fecha_compromiso=None, responsable=None):
    """Regla 8: cambiar fecha o responsable pide motivo y firma de gestionar_desviaciones."""
    a = _exigir_accion(conn, accion_id)
    if a['estatus'] != 'PENDIENTE':
        raise ErrorDesviacion('Solo se reprograma una acción pendiente.')
    if a['desviacion_estatus'] not in ('EN_INVESTIGACION', 'POR_APROBAR', 'EN_ACCIONES'):
        raise ErrorDesviacion('La desviación ya no está abierta.')
    _exigir_facultad(conn, firma_id, 'reprogramar una acción')
    motivo = _texto(motivo)
    if not motivo:
        raise ErrorDesviacion('Escribe el motivo del cambio.')
    cambios = {}
    fecha_compromiso = _fecha(fecha_compromiso, 'compromiso')
    if fecha_compromiso and fecha_compromiso != a['fecha_compromiso']:
        if fecha_compromiso < hoy():
            raise ErrorDesviacion('La fecha compromiso no puede ser pasada.')
        cambios['fecha_compromiso'] = fecha_compromiso
    if _texto(responsable) and _texto(responsable) != a['responsable']:
        cambios['responsable'] = _exigir_responsable(conn, responsable, 'responsable de la acción')
    if not cambios:
        raise ErrorDesviacion('No cambiaste la fecha ni el responsable.')
    conn.execute(f'UPDATE dev_acciones SET {", ".join(f"{k} = ?" for k in cambios)} WHERE id = ?',
                 (*cambios.values(), accion_id))
    for k, x in cambios.items():
        _historial(conn, a['desviacion_id'], k, a[k], x, usuario, motivo, firma_id, accion_id)


def marcar_hecha(conn, accion_id, *, como_se_hizo, firma_id, usuario):
    """Marcar hecha: cómo se hizo y la firma del responsable de la acción."""
    a = _exigir_accion(conn, accion_id)
    if a['estatus'] != 'PENDIENTE':
        raise ErrorDesviacion('La acción no está pendiente.')
    if a['desviacion_estatus'] not in ('EN_INVESTIGACION', 'POR_APROBAR', 'EN_ACCIONES'):
        raise ErrorDesviacion('La desviación ya no está abierta.')
    como_se_hizo = _texto(como_se_hizo)
    if not como_se_hizo:
        raise ErrorDesviacion('Escribe cómo se hizo.')
    if _numero_firma(conn, firma_id) != a['responsable']:
        raise ErrorDesviacion('La acción la firma su responsable.')
    conn.execute("UPDATE dev_acciones SET estatus = 'HECHA', como_se_hizo = ?, hecha_firma_id = ? WHERE id = ?",
                 (como_se_hizo, firma_id, accion_id))
    _historial(conn, a['desviacion_id'], 'estatus accion', 'PENDIENTE', 'HECHA', usuario, como_se_hizo, firma_id,
               accion_id)


def _es_de_calidad(conn, numero):
    u = conn.execute('SELECT * FROM usuarios WHERE numero_empleado = ?', (numero,)).fetchone()
    return bool(u) and 'calidad' in roles_de(u)


def verificar(conn, accion_id, *, firma_id, usuario, comentario=None):
    """Regla 8: la verifica alguien de calidad distinto del responsable. Una acción de capacitación
    completa (todas sus personas con el curso «Tomado») se puede verificar sin marcarla hecha."""
    a = _exigir_accion(conn, accion_id)
    if a['desviacion_estatus'] not in ('EN_INVESTIGACION', 'POR_APROBAR', 'EN_ACCIONES'):
        raise ErrorDesviacion('La desviación ya no está abierta.')
    avance = avance_capacitacion(conn, accion_id)
    capacitacion_lista = a['estatus'] == 'PENDIENTE' and avance and avance['completa']
    if a['estatus'] != 'HECHA' and not capacitacion_lista:
        raise ErrorDesviacion('Solo se verifica una acción hecha.')
    numero = _numero_firma(conn, firma_id)
    if not numero:
        raise ErrorDesviacion('Falta la firma de quien verifica.')
    if numero in (a['responsable'], _numero_firma(conn, a['hecha_firma_id'])):
        raise ErrorDesviacion('Quien hizo la acción no la puede verificar.')
    if not _es_de_calidad(conn, numero):
        raise ErrorDesviacion('La verificación la firma alguien de calidad.')
    if capacitacion_lista:
        como = 'Capacitación registrada por RH: ' + ', '.join(f"{p['nombre']} ({p['fecha']})" for p in avance['personas'])
        conn.execute('UPDATE dev_acciones SET como_se_hizo = ? WHERE id = ?', (como, accion_id))
    eficacia = a['eficacia_fecha']
    conn.execute("UPDATE dev_acciones SET estatus = 'VERIFICADA', verifico_firma_id = ? WHERE id = ?",
                 (firma_id, accion_id))
    _historial(conn, a['desviacion_id'], 'estatus accion', a['estatus'], 'VERIFICADA', usuario,
               _texto(comentario) or (f'Revisar eficacia el {eficacia}' if eficacia else None), firma_id, accion_id)


def cancelar_accion(conn, accion_id, *, motivo, firma_id, usuario):
    a = _exigir_accion(conn, accion_id)
    if a['estatus'] not in ('PENDIENTE', 'HECHA'):
        raise ErrorDesviacion('Esa acción ya no se puede cancelar.')
    if a['desviacion_estatus'] not in ('EN_INVESTIGACION', 'POR_APROBAR', 'EN_ACCIONES'):
        raise ErrorDesviacion('La desviación ya no está abierta.')
    _exigir_facultad(conn, firma_id, 'cancelar una acción')
    motivo = _texto(motivo)
    if not motivo:
        raise ErrorDesviacion('Escribe por qué se cancela.')
    conn.execute("UPDATE dev_acciones SET estatus = 'CANCELADA', motivo_cancelacion = ?, cancelo_firma_id = ? "
                 'WHERE id = ?', (motivo, firma_id, accion_id))
    _historial(conn, a['desviacion_id'], 'estatus accion', a['estatus'], 'CANCELADA', usuario, motivo, firma_id,
               accion_id)


def eficacias_por_revisar(conn, fecha=None):
    return [dict(r) for r in conn.execute(
        "SELECT a.*, d.folio FROM dev_acciones a JOIN dev_desviaciones d ON d.id = a.desviacion_id "
        "WHERE a.estatus = 'VERIFICADA' AND a.eficacia_fecha IS NOT NULL AND a.eficaz IS NULL "
        "AND a.eficacia_fecha <= ? AND d.estatus <> 'ANULADA' ORDER BY a.eficacia_fecha, a.id",
        (fecha or hoy(),)).fetchall()]


def revisar_eficacia(conn, accion_id, *, eficaz, comentario, firma_id, usuario, nueva_id=None):
    """Regla 10: al llegar la fecha se revisa si la acción funcionó. Si no, se abre una desviación
    nueva ligada (origen accion_no_eficaz). Devuelve el id de la nueva o None."""
    a = _exigir_accion(conn, accion_id)
    if a['estatus'] != 'VERIFICADA' or not a['eficacia_fecha'] or a['eficaz'] is not None:
        raise ErrorDesviacion('Esa acción no tiene una revisión de eficacia pendiente.')
    if a['eficacia_fecha'] > hoy():
        raise ErrorDesviacion(f"La revisión de eficacia toca el {a['eficacia_fecha']}.")
    comentario = _texto(comentario)
    if not comentario:
        raise ErrorDesviacion('Escribe qué se revisó.')
    numero = _numero_firma(conn, firma_id)
    if not numero or not _es_de_calidad(conn, numero):
        raise ErrorDesviacion('La revisión de eficacia la firma alguien de calidad.')
    eficaz = 1 if eficaz in (1, True, '1', 'si', 'sí') else 0
    conn.execute('UPDATE dev_acciones SET eficaz = ?, eficacia_comentario = ?, eficacia_firma_id = ? WHERE id = ?',
                 (eficaz, comentario, firma_id, accion_id))
    _historial(conn, a['desviacion_id'], 'eficacia', None, 'Funcionó' if eficaz else 'No funcionó', usuario,
               comentario, firma_id, accion_id)
    if eficaz:
        return None
    d = _exigir(conn, a['desviacion_id'])
    nueva = reportar(conn, tipo='DESVIACION', firma_id=firma_id, usuario=usuario, desviacion_id=nueva_id, datos={
        'titulo': f"Acción no eficaz de {d['folio']}",
        'descripcion': f"La acción {a['numero']} de {d['folio']} («{a['descripcion']}») no funcionó. "
                       f"Criterio: {a['eficacia_criterio']}. Revisión: {comentario}",
        'fecha_detecto': hoy(), 'area': d['area'], 'origen_modulo': 'desviaciones', 'origen_tipo': 'accion_no_eficaz',
        'origen_id': accion_id, 'origen_texto': f"{d['folio']} · acción {a['numero']}",
        'origen_url': f"/desviaciones/{d['id']}", 'material_id': d['material_id'], 'equipo_id': d['equipo_id'],
        'proveedor_id': d['proveedor_id'],
    })
    conn.execute('INSERT INTO dev_relacionadas (desviacion_id, relacionada_id, motivo) VALUES (?, ?, ?)',
                 (nueva, d['id'], f"Acción {a['numero']} no eficaz"))
    folio = conn.execute('SELECT folio FROM dev_desviaciones WHERE id = ?', (nueva,)).fetchone()[0]
    _historial(conn, d['id'], 'relacionada', None, folio, usuario, f"Acción {a['numero']} no eficaz")
    return nueva


# ---------------------------------------------------------------------------
# Quejas, cierre, anulación y comentarios
# ---------------------------------------------------------------------------

def registrar_respuesta(conn, desviacion_id, *, respuesta, fecha, usuario):
    d = _exigir(conn, desviacion_id)
    if d['tipo'] != 'QUEJA':
        raise ErrorDesviacion('Solo las quejas llevan respuesta al cliente.')
    _exigir_estatus(d, *ABIERTAS, para='registrar la respuesta')
    respuesta = _texto(respuesta)
    if not respuesta:
        raise ErrorDesviacion('Escribe la respuesta que se le dio al cliente.')
    fecha = _fecha(fecha, 'respuesta') or hoy()
    conn.execute('UPDATE dev_desviaciones SET respuesta_cliente = ?, fecha_respuesta = ? WHERE id = ?',
                 (respuesta, fecha, desviacion_id))
    _historial(conn, desviacion_id, 'respuesta_cliente', d['respuesta_cliente'], respuesta, usuario, fecha)


def faltantes_cierre(conn, desviacion_id) -> list:
    """Regla 9: lo que falta para cerrar."""
    d = _exigir(conn, desviacion_id)
    faltan = []
    if d['estatus'] != 'EN_ACCIONES' or not d['aprobo_firma_id']:
        faltan.append('Falta aprobar la investigación.')
    for a in acciones(conn, desviacion_id):
        if a['estatus'] not in ('VERIFICADA', 'CANCELADA'):
            faltan.append(f"Acción {a['numero']}: falta {'verificarla' if a['estatus'] == 'HECHA' else 'hacerla y verificarla'}.")
    for l in lotes(conn, desviacion_id):
        if l['impacto'] == 'PENDIENTE':
            faltan.append(f"Falta evaluar el impacto en el lote {l['numero_lote']}.")
    for lid in lotes_sin_agregar(conn, d):
        n = conn.execute('SELECT numero_lote FROM lotes WHERE id = ?', (lid,)).fetchone()[0]
        faltan.append(f"Falta evaluar el impacto en el lote {n} (viene de {d['documento_folio']}).")
    if d['tipo'] == 'QUEJA' and not d['respuesta_cliente']:
        faltan.append('Falta registrar la respuesta al cliente.')
    return faltan


def cerrar(conn, desviacion_id, *, conclusion, firma_id, usuario):
    d = _exigir(conn, desviacion_id)
    _exigir_estatus(d, 'EN_ACCIONES', para='cerrar')
    _exigir_facultad(conn, firma_id, 'cerrar')
    faltan = faltantes_cierre(conn, desviacion_id)
    if faltan:
        raise ErrorDesviacion('No se puede cerrar: ' + ' '.join(faltan))
    conclusion = _texto(conclusion)
    if not conclusion:
        raise ErrorDesviacion('Escribe la conclusión.')
    conn.execute('UPDATE dev_desviaciones SET conclusion = ?, cerro_firma_id = ?, cerrada_en = ? WHERE id = ?',
                 (conclusion, firma_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), desviacion_id))
    _cambiar_estatus(conn, d, 'CERRADA', usuario, conclusion, firma_id)


def anular(conn, desviacion_id, *, motivo, firma_id, usuario):
    """Reportada por error, duplicada o no procede. Los lotes retenidos siguen retenidos."""
    d = _exigir(conn, desviacion_id)
    _exigir_estatus(d, *ABIERTAS, para='anular')
    _exigir_facultad(conn, firma_id, 'anular')
    motivo = _texto(motivo)
    if not motivo:
        raise ErrorDesviacion('Escribe por qué se anula.')
    conn.execute('UPDATE dev_desviaciones SET motivo_anulacion = ?, anulo_firma_id = ? WHERE id = ?',
                 (motivo, firma_id, desviacion_id))
    _cambiar_estatus(conn, d, 'ANULADA', usuario, motivo, firma_id)


def comentar(conn, desviacion_id, texto, usuario):
    _exigir(conn, desviacion_id)
    texto = _texto(texto)
    if not texto:
        raise ErrorDesviacion('Escribe el comentario.')
    _historial(conn, desviacion_id, 'comentario', None, texto, usuario)


def historial(conn, desviacion_id):
    return [dict(r) for r in conn.execute(
        'SELECT h.*, a.numero AS accion_numero, f.nombre AS firma_nombre, f.significado AS firma_significado '
        'FROM dev_historial h LEFT JOIN dev_acciones a ON a.id = h.accion_id LEFT JOIN firmas f ON f.id = h.firma_id '
        'WHERE h.desviacion_id = ? ORDER BY h.id', (desviacion_id,)).fetchall()]


def firmas_de(conn, d):
    """{nombre_columna: fila de firmas} de las firmas de la desviación."""
    salida = {}
    for c in ('reporto_firma_id', 'clasifico_firma_id', 'investigo_firma_id', 'aprobo_firma_id',
              'cerro_firma_id', 'anulo_firma_id'):
        if d.get(c):
            f = conn.execute('SELECT * FROM firmas WHERE id = ?', (d[c],)).fetchone()
            salida[c[:-9]] = dict(f) if f else None
    return salida


# ---------------------------------------------------------------------------
# Consultas para otros módulos, pendientes e indicadores
# ---------------------------------------------------------------------------

def desviaciones_de(conn, origen, origen_id):
    """Consulta 'desviaciones_de': folio, estatus, clasificación y URL de las desviaciones de un origen."""
    return [{'id': r['id'], 'folio': r['folio'], 'estatus': r['estatus'], 'clasificacion': r['clasificacion'],
             'titulo': r['titulo'], 'url': f"/desviaciones/{r['id']}"}
            for r in conn.execute('SELECT id, folio, estatus, clasificacion, titulo FROM dev_desviaciones '
                                  'WHERE origen_tipo = ? AND origen_id = ? ORDER BY id', (origen, origen_id)).fetchall()]


def acciones_vencidas(conn, fecha=None):
    return conn.execute(
        "SELECT COUNT(*) FROM dev_acciones a JOIN dev_desviaciones d ON d.id = a.desviacion_id "
        "WHERE a.estatus = 'PENDIENTE' AND a.fecha_compromiso < ? AND d.estatus IN "
        "('EN_INVESTIGACION', 'POR_APROBAR', 'EN_ACCIONES')", (fecha or hoy(),)).fetchone()[0]


def investigaciones_vencidas(conn, fecha=None):
    return conn.execute("SELECT COUNT(*) FROM dev_desviaciones WHERE estatus = 'EN_INVESTIGACION' "
                        'AND vence_investigacion < ?', (fecha or hoy(),)).fetchone()[0]


def _a_tiempo(conn, d):
    """Cerrada a tiempo: investigación enviada antes de su fecha límite y cada acción hecha
    antes de su fecha compromiso."""
    if d['vence_investigacion'] and d['investigo_firma_id']:
        ts = conn.execute('SELECT ts FROM firmas WHERE id = ?', (d['investigo_firma_id'],)).fetchone()
        if ts and ts[0][:10] > d['vence_investigacion']:
            return False
    for a in conn.execute("SELECT a.fecha_compromiso, f.ts FROM dev_acciones a LEFT JOIN firmas f "
                          "ON f.id = COALESCE(a.hecha_firma_id, a.verifico_firma_id) "
                          "WHERE a.desviacion_id = ? AND a.estatus <> 'CANCELADA'", (d['id'],)).fetchall():
        if a['ts'] and a['ts'][:10] > a['fecha_compromiso']:
            return False
    return True


def indicadores(conn, fecha=None):
    fecha = fecha or hoy()
    mes = fecha[:7]
    abiertas = conn.execute(f"SELECT COUNT(*), SUM(clasificacion = 'CRITICA') FROM dev_desviaciones "
                            f"WHERE estatus IN {ABIERTAS}").fetchone()
    cerradas_mes = conn.execute("SELECT * FROM dev_desviaciones WHERE estatus = 'CERRADA' "
                                'AND substr(cerrada_en, 1, 7) = ?', (mes,)).fetchall()
    a_tiempo = sum(1 for d in cerradas_mes if _a_tiempo(conn, d))
    desde = (date.fromisoformat(fecha) - timedelta(days=365)).isoformat()
    promedio = conn.execute("SELECT AVG(julianday(cerrada_en) - julianday(creado_en)) FROM dev_desviaciones "
                            "WHERE estatus = 'CERRADA' AND cerrada_en >= ?", (desde,)).fetchone()[0]
    return {
        'abiertas': abiertas[0] or 0, 'criticas': abiertas[1] or 0,
        'investigaciones_vencidas': investigaciones_vencidas(conn, fecha),
        'acciones_vencidas': acciones_vencidas(conn, fecha),
        'cerradas_mes': len(cerradas_mes),
        'a_tiempo_pct': round(100 * a_tiempo / len(cerradas_mes)) if cerradas_mes else None,
        'dias_promedio': round(promedio, 1) if promedio is not None else None,
    }


PESTANAS = {
    'clasificar': ('Por clasificar', "d.estatus = 'REPORTADA'"),
    'investigacion': ('En investigación', "d.estatus = 'EN_INVESTIGACION'"),
    'aprobar': ('Por aprobar', "d.estatus = 'POR_APROBAR'"),
    'acciones': ('Acciones en curso', "d.estatus = 'EN_ACCIONES'"),
    'cerradas': ('Cerradas', "d.estatus = 'CERRADA'"),
    'anuladas': ('Anuladas', "d.estatus = 'ANULADA'"),
    'todas': ('Todas', '1 = 1'),
}


def buscar(conn, *, pestana='todas', tipo=None, clasificacion=None, area=None, categoria=None, desde=None,
           hasta=None, texto=None, pagina=1, por_pagina=50):
    """Lista filtrada y paginada: (renglones, total)."""
    where = [PESTANAS.get(pestana, PESTANAS['todas'])[1]]
    params = []
    for valor, sql in ((tipo, 'd.tipo = ?'), (clasificacion, 'd.clasificacion = ?'), (area, 'd.area = ?'),
                       (categoria, 'd.categoria_causa = ?'), (desde, 'd.fecha_detecto >= ?'),
                       (hasta, 'd.fecha_detecto <= ?')):
        if valor:
            where.append(sql)
            params.append(valor)
    if texto:
        where.append('(d.folio LIKE ? OR d.titulo LIKE ? OR d.descripcion LIKE ?)')
        params += [f'%{texto}%'] * 3
    w = ' AND '.join(where)
    total = conn.execute(f'SELECT COUNT(*) FROM dev_desviaciones d WHERE {w}', params).fetchone()[0]
    pagina = max(1, int(pagina or 1))
    filas = conn.execute(
        f"SELECT d.*, u.nombre_completo AS responsable_nombre, "
        f"(SELECT GROUP_CONCAT(l.numero_lote, ', ') FROM dev_lotes dl JOIN lotes l ON l.id = dl.lote_id "
        f" WHERE dl.desviacion_id = d.id) AS lotes_texto, "
        f"(SELECT MIN(a.fecha_compromiso) FROM dev_acciones a WHERE a.desviacion_id = d.id AND a.estatus = 'PENDIENTE') "
        f" AS proxima_accion "
        f"FROM dev_desviaciones d LEFT JOIN usuarios u ON u.numero_empleado = d.responsable WHERE {w} "
        f"ORDER BY d.id DESC LIMIT ? OFFSET ?", (*params, por_pagina, (pagina - 1) * por_pagina)).fetchall()
    renglones = []
    for r in filas:
        r = dict(r)
        if r['estatus'] == 'EN_INVESTIGACION':
            r['fecha_limite'] = r['vence_investigacion']
        elif r['estatus'] in ('POR_APROBAR', 'EN_ACCIONES'):
            r['fecha_limite'] = r['proxima_accion']
        else:
            r['fecha_limite'] = None
        renglones.append(r)
    return renglones, total


def conteo_pestanas(conn):
    filas = conn.execute('SELECT estatus, COUNT(*) FROM dev_desviaciones GROUP BY estatus').fetchall()
    por = {r[0]: r[1] for r in filas}
    return {'clasificar': por.get('REPORTADA', 0), 'investigacion': por.get('EN_INVESTIGACION', 0),
            'aprobar': por.get('POR_APROBAR', 0), 'acciones': por.get('EN_ACCIONES', 0),
            'cerradas': por.get('CERRADA', 0), 'anuladas': por.get('ANULADA', 0), 'todas': sum(por.values())}


def mis_acciones(conn, numero_empleado):
    filas = conn.execute(
        "SELECT a.*, d.folio, d.titulo AS desviacion_titulo, c.nombre AS curso_nombre FROM dev_acciones a "
        "JOIN dev_desviaciones d ON d.id = a.desviacion_id LEFT JOIN cursos c ON c.id = a.curso_id "
        "WHERE a.responsable = ? AND a.estatus = 'PENDIENTE' AND d.estatus IN "
        "('EN_INVESTIGACION', 'POR_APROBAR', 'EN_ACCIONES') ORDER BY a.fecha_compromiso, a.id",
        (numero_empleado,)).fetchall()
    salida = []
    for r in filas:
        r = dict(r)
        r['capacitacion'] = avance_capacitacion(conn, r['id'])
        salida.append(r)
    return salida


def acciones_por_verificar(conn, excepto=None):
    filas = conn.execute(
        "SELECT a.*, d.folio, d.titulo AS desviacion_titulo, u.nombre_completo AS responsable_nombre "
        "FROM dev_acciones a JOIN dev_desviaciones d ON d.id = a.desviacion_id "
        "LEFT JOIN usuarios u ON u.numero_empleado = a.responsable "
        "WHERE a.estatus IN ('HECHA', 'PENDIENTE') AND d.estatus IN ('EN_INVESTIGACION', 'POR_APROBAR', 'EN_ACCIONES') "
        "ORDER BY a.fecha_compromiso, a.id").fetchall()
    salida = []
    for r in filas:
        r = dict(r)
        if excepto and r['responsable'] == excepto:
            continue
        if r['estatus'] == 'PENDIENTE':
            avance = avance_capacitacion(conn, r['id'])
            if not (avance and avance['completa']):
                continue
            r['capacitacion'] = avance
        salida.append(r)
    return salida


# --- Tendencias -------------------------------------------------------------

def _meses(fecha, n=12):
    a, m = int(fecha[:4]), int(fecha[5:7])
    salida = []
    for _ in range(n):
        salida.append(f'{a:04d}-{m:02d}')
        m -= 1
        if m == 0:
            a, m = a - 1, 12
    return list(reversed(salida))


def tendencias(conn, fecha=None):
    """Últimos 12 meses (sin anuladas): por mes, área, categoría de causa y clasificación; causas que se
    repiten; acciones a tiempo contra vencidas."""
    fecha = fecha or hoy()
    meses = _meses(fecha)
    desde = meses[0] + '-01'
    base = "FROM dev_desviaciones WHERE estatus <> 'ANULADA' AND fecha_detecto >= ?"

    def agrupar(columna):
        return [{'etiqueta': r[0] or 'Sin dato', 'n': r[1]} for r in conn.execute(
            f'SELECT {columna}, COUNT(*) {base} GROUP BY {columna} ORDER BY COUNT(*) DESC, {columna}',
            (desde,)).fetchall()]

    por_mes_db = {r[0]: r[1] for r in conn.execute(f'SELECT substr(fecha_detecto, 1, 7), COUNT(*) {base} '
                                                   'GROUP BY 1', (desde,)).fetchall()}
    por_mes = [{'etiqueta': m, 'n': por_mes_db.get(m, 0)} for m in meses]
    repetidas = [dict(r) for r in conn.execute(
        f"SELECT area, categoria_causa, COUNT(*) AS n, GROUP_CONCAT(folio, ', ') AS folios {base} "
        "AND categoria_causa IS NOT NULL GROUP BY area, categoria_causa HAVING COUNT(*) >= 2 ORDER BY n DESC",
        (desde,)).fetchall()]
    hoy_ = fecha
    acc = {'a_tiempo': 0, 'tarde': 0, 'vencidas': 0, 'en_plazo': 0}
    for a in conn.execute(
            "SELECT a.estatus, a.fecha_compromiso, f.ts FROM dev_acciones a JOIN dev_desviaciones d "
            "ON d.id = a.desviacion_id LEFT JOIN firmas f ON f.id = COALESCE(a.hecha_firma_id, a.verifico_firma_id) "
            "WHERE a.estatus <> 'CANCELADA' AND d.estatus <> 'ANULADA' AND a.fecha_compromiso >= ?",
            (desde,)).fetchall():
        if a['estatus'] == 'PENDIENTE':
            acc['vencidas' if a['fecha_compromiso'] < hoy_ else 'en_plazo'] += 1
        elif a['ts'] and a['ts'][:10] > a['fecha_compromiso']:
            acc['tarde'] += 1
        else:
            acc['a_tiempo'] += 1
    return {'desde': desde, 'hasta': fecha, 'por_mes': por_mes, 'por_area': agrupar('area'),
            'por_categoria': agrupar('categoria_causa'), 'por_clasificacion': agrupar('clasificacion'),
            'por_tipo': agrupar('tipo'), 'repetidas': repetidas, 'acciones': acc}
