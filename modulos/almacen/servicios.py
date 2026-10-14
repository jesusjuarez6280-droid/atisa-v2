"""Reglas de negocio del almacén, sin Flask.

Como los servicios de la base: reciben la conexión abierta, NO hacen commit y lanzan
ErrorAlmacen (o ErrorInventario / FirmaInvalida de la base) con un mensaje listo para
mostrarse. Todo lo que cambia existencias pasa por nucleo.inventario.

Firmas: las funciones que firman reciben `firmar`, una función hecha con firmante()
que se crea ANTES de escribir nada (valida el PIN en ese momento, así los intentos
fallidos cuentan aunque la transacción se revierta) y que se llama ya con el id del
registro firmado: firmar(entidad, entidad_id, detalle) -> firma_id.
"""
from datetime import date, datetime

from nucleo import archivos, firmas, folios, inventario
from nucleo.parametros import parametro_int

MODULO = 'almacen'

TIPOS_MATERIAL = {
    'MP': 'Materia prima',
    'ME': 'Material de envase',
    'EM': 'Material de empaque',
    'GR': 'Granel',
    'PT': 'Producto terminado',
}
TIPOS_UBICACION = ('General', 'Cuarentena', 'Rechazados', 'Refrigeración', 'Producto terminado')
TIPOS_SALIDA = {
    'VENTA': 'Venta',
    'DESTRUCCION': 'Destrucción',
    'DEVOLUCION_PROVEEDOR': 'Devolución al proveedor',
}
MOTIVOS_DEVOLUCION = ('Caducado', 'Dañado', 'Queja', 'Error de pedido')
FILTROS_ESTATUS = ('CUARENTENA', 'APROBADO', 'RECHAZADO', 'RETENIDO', 'CADUCADO')

_EPSILON = 1e-9


class ErrorAlmacen(Exception):
    """El mensaje se puede mostrar tal cual en pantalla (flash)."""


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def hoy():
    return date.today().isoformat()


def _texto(valor):
    return (str(valor).strip() if valor is not None else '') or None


def _fecha(valor, nombre):
    if valor is None or valor == '':
        return None
    if isinstance(valor, datetime):
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    texto = str(valor).strip()[:10]
    try:
        return datetime.strptime(texto, '%Y-%m-%d').date().isoformat()
    except ValueError:
        raise ErrorAlmacen(f'La fecha de {nombre} no es válida (usa AAAA-MM-DD).')


def _numero(valor, nombre, minimo=None, positivo=False):
    try:
        n = float(str(valor).replace(',', '').strip())
    except (TypeError, ValueError):
        raise ErrorAlmacen(f'{nombre} debe ser un número.')
    if positivo and n <= 0:
        raise ErrorAlmacen(f'{nombre} debe ser mayor que cero.')
    if minimo is not None and n < minimo:
        raise ErrorAlmacen(f'{nombre} no puede ser menor que {minimo:g}.')
    return n


def _bool(valor):
    return 1 if valor in (1, True, '1', 'on', 'si', 'sí', 'true', 'True') else 0


def fmt(n):
    """12.5 → '12.5'; 3.0 → '3'."""
    if n is None:
        return ''
    return f'{n:,.4f}'.rstrip('0').rstrip('.')


def firmante(conn, *, numero, pin, significado, facultad=None, critica=False, sesion=None):
    """Valida el PIN ya y devuelve firmar(entidad, entidad_id, detalle) -> firma_id."""
    numero = _texto(numero)
    if not numero or not _texto(pin):
        raise firmas.FirmaInvalida('Escribe tu número de empleado y tu PIN para firmar.')
    firmas.verificar_pin(conn, numero, pin)

    def firmar(entidad, entidad_id, detalle):
        return firmas.firmar(conn, numero_empleado=numero, pin=pin, significado=significado, modulo=MODULO,
                             entidad=entidad, entidad_id=entidad_id, detalle=detalle, facultad=facultad,
                             critica=critica, sesion=sesion)
    return firmar


def _firmar(firmar, entidad, entidad_id, detalle):
    return firmar(entidad, entidad_id, detalle) if firmar else None


def _ultimo_movimiento(conn, lote_id):
    return conn.execute('SELECT MAX(id) FROM lote_movimientos WHERE lote_id = ?', (lote_id,)).fetchone()[0] or 0


def _mover(conn, **kwargs):
    """inventario.mover() devolviendo (saldo, id del movimiento)."""
    saldo = inventario.mover(conn, modulo=MODULO, **kwargs)
    mov_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    return saldo, mov_id


def ubicacion_por_tipo(conn, tipo):
    fila = conn.execute('SELECT codigo FROM alm_ubicaciones WHERE tipo = ? AND activo = 1 ORDER BY id LIMIT 1',
                        (tipo,)).fetchone()
    return fila['codigo'] if fila else None


def _validar_ubicacion(conn, codigo):
    codigo = _texto(codigo)
    if not codigo:
        return None
    if not conn.execute('SELECT 1 FROM alm_ubicaciones WHERE codigo = ? AND activo = 1', (codigo,)).fetchone():
        raise ErrorAlmacen(f'La ubicación {codigo} no existe o está desactivada.')
    return codigo


# ---------------------------------------------------------------------------
# Recepciones
# ---------------------------------------------------------------------------

def aviso_proveedor(estatus):
    if estatus in ('En evaluación', 'Condicionado'):
        return f'Proveedor {estatus.lower()}'
    return None


def _validar_partida(conn, p, n, hoy_, minimo_dias):
    """Valida y normaliza una partida (en sitio). Devuelve la lista de avisos."""
    avisos = []
    etiqueta = f'Partida {n}'
    try:
        material_id = int(p.get('material_id') or 0)
    except (TypeError, ValueError):
        material_id = 0
    m = conn.execute('SELECT * FROM materiales WHERE id = ?', (material_id,)).fetchone()
    if not m:
        raise ErrorAlmacen(f'{etiqueta}: elige el material.')
    if not m['activo']:
        raise ErrorAlmacen(f'{etiqueta}: el material {m["codigo"]} está desactivado.')
    p['material_id'] = m['id']
    p['lote_proveedor'] = _texto(p.get('lote_proveedor'))
    if not p['lote_proveedor']:
        raise ErrorAlmacen(f'{etiqueta} ({m["codigo"]}): falta el lote del proveedor.')
    p['cantidad'] = _numero(p.get('cantidad'), f'{etiqueta}: la cantidad', positivo=True)
    try:
        p['contenedores'] = int(p.get('contenedores') or 1)
    except (TypeError, ValueError):
        raise ErrorAlmacen(f'{etiqueta}: el número de contenedores debe ser un entero.')
    if p['contenedores'] < 1:
        raise ErrorAlmacen(f'{etiqueta}: el número de contenedores debe ser 1 o más.')
    p['fecha_caducidad'] = _fecha(p.get('fecha_caducidad'), 'caducidad')
    p['fecha_reanalisis'] = _fecha(p.get('fecha_reanalisis'), 'reanálisis')
    for campo in ('empaque_integro', 'identificacion_ok', 'trae_certificado', 'limpio'):
        p[campo] = _bool(p.get(campo))
    p['aceptada'] = 0 if _bool(p.get('no_se_recibe')) or p.get('aceptada') in (0, '0', False) else 1
    p['temperatura'] = _texto(p.get('temperatura'))
    p['observaciones'] = _texto(p.get('observaciones'))
    p['ubicacion'] = _validar_ubicacion(conn, p.get('ubicacion'))
    if not p['aceptada']:
        return avisos
    if m['tipo'] == 'MP' and not p['fecha_caducidad']:
        raise ErrorAlmacen(f'{etiqueta} ({m["codigo"]}): la caducidad es obligatoria para materias primas.')
    if p['fecha_caducidad']:
        if p['fecha_caducidad'] < hoy_:
            raise ErrorAlmacen(f'{etiqueta} ({m["codigo"]}): el lote ya caducó el {p["fecha_caducidad"]}; '
                               'no se puede recibir.')
        dias = (date.fromisoformat(p['fecha_caducidad']) - date.fromisoformat(hoy_)).days
        if dias < minimo_dias:
            avisos.append(f'{m["codigo"]} lote {p["lote_proveedor"]}: caduca en {dias} días '
                          f'(menos de {minimo_dias}).')
    if p['fecha_reanalisis'] and p['fecha_reanalisis'] < hoy_:
        raise ErrorAlmacen(f'{etiqueta} ({m["codigo"]}): la fecha de reanálisis ya pasó.')
    return avisos


def recibir(conn, *, proveedor_id, partidas, usuario, firmar, fecha=None, documento=None,
            transporte=None, observaciones=None):
    """Registra la recepción y crea un lote por partida aceptada (en cuarentena si el
    material requiere análisis). Una partida con no_se_recibe queda registrada sin lote.
    partidas: [{material_id, lote_proveedor, cantidad, contenedores, fecha_caducidad,
    fecha_reanalisis, empaque_integro, identificacion_ok, trae_certificado, limpio,
    temperatura, no_se_recibe, observaciones, ubicacion, certificado (FileStorage)}].
    Devuelve {recepcion_id, folio, lotes, avisos}."""
    prov = conn.execute('SELECT * FROM proveedores WHERE id = ?', (proveedor_id or 0,)).fetchone()
    if not prov:
        raise ErrorAlmacen('Elige el proveedor.')
    if prov['estatus'] == 'No aprobado':
        raise ErrorAlmacen(f'El proveedor {prov["nombre"]} está como «No aprobado»: no se le recibe.')
    if not prov['activo']:
        raise ErrorAlmacen(f'El proveedor {prov["nombre"]} está desactivado.')
    if not partidas:
        raise ErrorAlmacen('Agrega al menos una partida.')
    fecha = _fecha(fecha, 'recepción') or hoy()
    hoy_ = hoy()
    minimo = parametro_int('almacen.vida_util_minima_dias', conn)
    aviso_prov = aviso_proveedor(prov['estatus'])
    avisos = [f'El proveedor {prov["nombre"]} está {prov["estatus"].lower()}.'] if aviso_prov else []
    vistos = set()
    for n, p in enumerate(partidas, 1):
        avisos += _validar_partida(conn, p, n, hoy_, minimo)
        llave = (p['material_id'], p['lote_proveedor'].upper())
        if llave in vistos:
            raise ErrorAlmacen(f'Partida {n}: ese lote del proveedor ya está en otra partida '
                               '(un lote por partida).')
        vistos.add(llave)

    folio = folios.siguiente_folio(conn, 'REC')
    cur = conn.execute(
        'INSERT INTO alm_recepciones (folio, fecha, proveedor_id, documento, transporte, observaciones, recibio) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        (folio, fecha, prov['id'], _texto(documento), _texto(transporte), _texto(observaciones), usuario))
    rec_id = cur.lastrowid
    firma_id = _firmar(firmar, 'alm_recepciones', rec_id, f'Recibió {folio} de {prov["nombre"]}')
    conn.execute('UPDATE alm_recepciones SET firma_id = ? WHERE id = ?', (firma_id, rec_id))

    lotes = []
    cuarentena = ubicacion_por_tipo(conn, 'Cuarentena')
    for p in partidas:
        cur = conn.execute(
            'INSERT INTO alm_recepcion_partidas (recepcion_id, material_id, lote_proveedor, cantidad, contenedores, '
            'fecha_caducidad, fecha_reanalisis, empaque_integro, identificacion_ok, trae_certificado, limpio, '
            'temperatura, aceptada, aviso_proveedor, observaciones) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (rec_id, p['material_id'], p['lote_proveedor'], p['cantidad'], p['contenedores'], p['fecha_caducidad'],
             p['fecha_reanalisis'], p['empaque_integro'], p['identificacion_ok'], p['trae_certificado'], p['limpio'],
             p['temperatura'], p['aceptada'], aviso_prov, p['observaciones']))
        partida_id = cur.lastrowid
        if not p['aceptada']:
            continue
        requiere = conn.execute('SELECT requiere_analisis FROM materiales WHERE id = ?',
                                (p['material_id'],)).fetchone()[0]
        lote_id = inventario.crear_lote(
            conn, material_id=p['material_id'], cantidad=p['cantidad'], origen='RECEPCION', usuario=usuario,
            modulo=MODULO, folio=folio, fecha_entrada=fecha, fecha_caducidad=p['fecha_caducidad'],
            fecha_reanalisis=p['fecha_reanalisis'], lote_proveedor=p['lote_proveedor'], proveedor_id=prov['id'],
            ubicacion=p['ubicacion'] or (cuarentena if requiere else None), firma_id=firma_id)
        conn.execute('UPDATE alm_recepcion_partidas SET lote_id = ? WHERE id = ?', (lote_id, partida_id))
        if p.get('certificado') is not None and getattr(p['certificado'], 'filename', None):
            archivos.guardar_archivo(conn, p['certificado'], modulo=MODULO, entidad='lotes', entidad_id=lote_id,
                                     descripcion=f'Certificado del proveedor ({folio})', usuario=usuario)
        lotes.append(lote_id)
    return {'recepcion_id': rec_id, 'folio': folio, 'lotes': lotes, 'avisos': avisos}


def anular_recepcion(conn, recepcion_id, *, motivo, usuario, firmar):
    """Solo si sus lotes no tienen otros movimientos. Deja los lotes en cero con un
    AJUSTE_MENOS firmado por alguien con autorizar_ajustes. No se borra nada."""
    rec = conn.execute('SELECT * FROM alm_recepciones WHERE id = ?', (recepcion_id,)).fetchone()
    if not rec:
        raise ErrorAlmacen('La recepción no existe.')
    if rec['estatus'] == 'Anulada':
        raise ErrorAlmacen(f'La recepción {rec["folio"]} ya está anulada.')
    motivo = _texto(motivo)
    if not motivo:
        raise ErrorAlmacen('Escribe el motivo de la anulación.')
    lotes = conn.execute('SELECT l.id, l.numero_lote, l.existencia FROM alm_recepcion_partidas p '
                         'JOIN lotes l ON l.id = p.lote_id WHERE p.recepcion_id = ?', (recepcion_id,)).fetchall()
    for l in lotes:
        n = conn.execute('SELECT COUNT(*) FROM lote_movimientos WHERE lote_id = ?', (l['id'],)).fetchone()[0]
        hijos = conn.execute('SELECT 1 FROM lotes WHERE lote_padre_id = ? LIMIT 1', (l['id'],)).fetchone()
        if n > 1 or hijos:
            raise ErrorAlmacen(f'No se puede anular {rec["folio"]}: el lote {l["numero_lote"]} ya tuvo '
                               'movimientos. Corrígelo con un conteo.')
    firma_id = _firmar(firmar, 'alm_recepciones', recepcion_id, f'Anuló {rec["folio"]}: {motivo}')
    for l in lotes:
        if l['existencia'] > _EPSILON:
            _mover(conn, lote_id=l['id'], tipo='AJUSTE_MENOS', cantidad=l['existencia'], usuario=usuario,
                   folio=rec['folio'], motivo=f'Anulación de {rec["folio"]}: {motivo}', firma_id=firma_id)
    conn.execute("UPDATE alm_recepciones SET estatus = 'Anulada', motivo_anulacion = ?, anulo_firma_id = ? "
                 'WHERE id = ?', (motivo, firma_id, recepcion_id))


# ---------------------------------------------------------------------------
# Salidas y devoluciones
# ---------------------------------------------------------------------------

def lotes_para_salida(conn, tipo):
    """Lotes que se pueden elegir en una salida de ese tipo, en orden de caducidad."""
    h = hoy()
    if tipo == 'VENTA':
        sql = ("m.tipo = 'PT' AND l.estatus = 'APROBADO' AND (l.fecha_caducidad IS NULL OR l.fecha_caducidad >= :h) "
               'AND (l.fecha_reanalisis IS NULL OR l.fecha_reanalisis >= :h)')
    else:
        sql = "(l.estatus = 'RECHAZADO' OR (l.fecha_caducidad IS NOT NULL AND l.fecha_caducidad < :h))"
    return [dict(r) for r in conn.execute(
        'SELECT l.id, l.numero_lote, l.lote_proveedor, l.existencia, l.fecha_caducidad, l.estatus, l.proveedor_id, '
        'm.id AS material_id, m.codigo AS material_codigo, m.nombre AS material_nombre, m.unidad '
        'FROM lotes l JOIN materiales m ON m.id = l.material_id '
        f'WHERE l.existencia > 0 AND {sql} '
        'ORDER BY m.codigo, l.fecha_caducidad IS NULL, l.fecha_caducidad, l.fecha_entrada, l.id', {'h': h}).fetchall()]


def registrar_salida(conn, *, tipo, destino, partidas, usuario, firmar, documento=None, observaciones=None,
                     fecha=None):
    """VENTA (solo PT liberado y vigente; si no se toma el lote que sugiere el sistema por
    caducidad, pide motivo) · DESTRUCCION y DEVOLUCION_PROVEEDOR (solo rechazados o
    caducados, con firma de autorizar_ajustes). partidas: [{lote_id, cantidad, motivo_lote}].
    Devuelve {salida_id, folio}."""
    if tipo not in TIPOS_SALIDA:
        raise ErrorAlmacen('Tipo de salida no válido.')
    destino = _texto(destino)
    if not destino:
        raise ErrorAlmacen('Escribe el cliente.' if tipo == 'VENTA' else
                           'Escribe a quién se entrega (proveedor o empresa que destruye).')
    partidas = [p for p in partidas if p.get('lote_id')]
    if not partidas:
        raise ErrorAlmacen('Agrega al menos un lote.')
    fecha = _fecha(fecha, 'salida') or hoy()
    h = hoy()
    filas = []
    por_material = {}
    for n, p in enumerate(partidas, 1):
        try:
            lote = inventario.lote(conn, int(p['lote_id']))
        except (TypeError, ValueError):
            lote = None
        if not lote:
            raise ErrorAlmacen(f'Partida {n}: el lote no existe.')
        cantidad = _numero(p.get('cantidad'), f'Partida {n}: la cantidad', positivo=True)
        if tipo == 'VENTA':
            ok, motivo_no = inventario.lote_disponible(conn, lote['id'], uso='venta')
            if not ok:
                raise ErrorAlmacen(motivo_no)
        elif lote['estatus'] != 'RECHAZADO' and not (lote['fecha_caducidad'] and lote['fecha_caducidad'] < h):
            raise ErrorAlmacen(f'El lote {lote["numero_lote"]} no está rechazado ni caducado: no se puede '
                               f'{"destruir" if tipo == "DESTRUCCION" else "devolver al proveedor"}.')
        filas.append({'lote': lote, 'cantidad': cantidad, 'motivo_lote': _texto(p.get('motivo_lote'))})
        por_material[lote['material_id']] = por_material.get(lote['material_id'], 0) + cantidad
    if tipo == 'VENTA':
        for f in filas:
            sugeridos = {s['lote_id'] for s in inventario.sugerir_lotes(conn, f['lote']['material_id'],
                                                                        por_material[f['lote']['material_id']])
                         if 'lote_id' in s}
            if f['lote']['id'] not in sugeridos and not f['motivo_lote']:
                raise ErrorAlmacen(f'El lote {f["lote"]["numero_lote"]} no es el que caduca primero: escribe '
                                   'por qué se surte ese lote.')

    folio = folios.siguiente_folio(conn, 'SAL')
    cur = conn.execute('INSERT INTO alm_salidas (folio, tipo, fecha, destino, documento, observaciones, usuario) '
                       'VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (folio, tipo, fecha, destino, _texto(documento), _texto(observaciones), usuario))
    salida_id = cur.lastrowid
    verbo = {'VENTA': 'Surtió venta', 'DESTRUCCION': 'Autorizó destrucción',
             'DEVOLUCION_PROVEEDOR': 'Autorizó devolución al proveedor'}[tipo]
    firma_id = _firmar(firmar, 'alm_salidas', salida_id, f'{verbo} {folio} ({destino})')
    conn.execute('UPDATE alm_salidas SET firma_id = ? WHERE id = ?', (firma_id, salida_id))
    for f in filas:
        motivo = destino if tipo == 'VENTA' else f'{TIPOS_SALIDA[tipo]}: {destino}'
        _, mov_id = _mover(conn, lote_id=f['lote']['id'], tipo=tipo, cantidad=f['cantidad'], usuario=usuario,
                           folio=folio, motivo=motivo, firma_id=firma_id)
        conn.execute('INSERT INTO alm_salida_partidas (salida_id, lote_id, cantidad, motivo_lote, movimiento_id) '
                     'VALUES (?, ?, ?, ?, ?)', (salida_id, f['lote']['id'], f['cantidad'], f['motivo_lote'], mov_id))
    return {'salida_id': salida_id, 'folio': folio}


def lotes_vendidos(conn):
    """Lotes con ventas, con lo vendido y lo ya devuelto, para elegir en una devolución."""
    return [dict(r) for r in conn.execute(
        'SELECT l.id, l.numero_lote, l.fecha_caducidad, m.codigo AS material_codigo, m.nombre AS material_nombre, '
        'm.unidad, -SUM(mv.cantidad) AS vendido, '
        '(SELECT COALESCE(SUM(d.cantidad), 0) FROM alm_devoluciones d WHERE d.lote_vendido_id = l.id) AS devuelto, '
        "GROUP_CONCAT(DISTINCT mv.motivo) AS clientes "
        'FROM lote_movimientos mv JOIN lotes l ON l.id = mv.lote_id JOIN materiales m ON m.id = l.material_id '
        "WHERE mv.tipo = 'VENTA' GROUP BY l.id ORDER BY MAX(mv.id) DESC").fetchall()]


def registrar_devolucion(conn, *, cliente, lote_vendido_id, cantidad, motivo, usuario, fecha=None):
    """Crea un lote aparte en CUARENTENA (p. ej. 2610015-D1) ligado al lote vendido.
    Devuelve {devolucion_id, folio, lote_id}."""
    cliente = _texto(cliente)
    if not cliente:
        raise ErrorAlmacen('Escribe el cliente que devuelve.')
    if motivo not in MOTIVOS_DEVOLUCION:
        raise ErrorAlmacen('Elige el motivo de la devolución.')
    cantidad = _numero(cantidad, 'La cantidad', positivo=True)
    try:
        vendido = inventario.lote(conn, int(lote_vendido_id))
    except (TypeError, ValueError):
        vendido = None
    if not vendido:
        raise ErrorAlmacen('Elige el lote que se vendió.')
    total = conn.execute("SELECT -COALESCE(SUM(cantidad), 0) FROM lote_movimientos WHERE lote_id = ? "
                         "AND tipo = 'VENTA'", (vendido['id'],)).fetchone()[0]
    if total <= _EPSILON:
        raise ErrorAlmacen(f'El lote {vendido["numero_lote"]} no tiene ventas registradas.')
    devuelto = conn.execute('SELECT COALESCE(SUM(cantidad), 0) FROM alm_devoluciones WHERE lote_vendido_id = ?',
                            (vendido['id'],)).fetchone()[0]
    if cantidad > total - devuelto + _EPSILON:
        raise ErrorAlmacen(f'Del lote {vendido["numero_lote"]} se vendieron {fmt(total)} {vendido["unidad"]} y ya '
                           f'se devolvieron {fmt(devuelto)}: no se pueden devolver {fmt(cantidad)}.')
    fecha = _fecha(fecha, 'devolución') or hoy()
    n = conn.execute("SELECT COUNT(*) FROM lotes WHERE lote_padre_id = ? AND origen = 'DEVOLUCION'",
                     (vendido['id'],)).fetchone()[0] + 1
    while conn.execute('SELECT 1 FROM lotes WHERE material_id = ? AND numero_lote = ?',
                       (vendido['material_id'], f'{vendido["numero_lote"]}-D{n}')).fetchone():
        n += 1
    numero = f'{vendido["numero_lote"]}-D{n}'
    folio = folios.siguiente_folio(conn, 'DEV')
    lote_id = inventario.crear_lote(
        conn, material_id=vendido['material_id'], cantidad=cantidad, origen='DEVOLUCION', usuario=usuario,
        modulo=MODULO, numero_lote=numero, folio=folio, fecha_entrada=fecha,
        fecha_fabricacion=vendido['fecha_fabricacion'], fecha_caducidad=vendido['fecha_caducidad'],
        fecha_reanalisis=vendido['fecha_reanalisis'], lote_padre_id=vendido['id'],
        ubicacion=ubicacion_por_tipo(conn, 'Cuarentena'))
    cur = conn.execute('INSERT INTO alm_devoluciones (folio, fecha, cliente, lote_vendido_id, cantidad, motivo, '
                       'lote_id, usuario) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       (folio, fecha, cliente, vendido['id'], cantidad, motivo, lote_id, usuario))
    return {'devolucion_id': cur.lastrowid, 'folio': folio, 'lote_id': lote_id}


# ---------------------------------------------------------------------------
# Conteos físicos
# ---------------------------------------------------------------------------

def abrir_conteo(conn, *, usuario, tipo_material=None, ubicacion=None, material_ids=None, fecha=None):
    """Conteo de todo, de un tipo de material, de una ubicación o de materiales elegidos.
    Entran los lotes con existencia. Devuelve {conteo_id, folio, lotes}."""
    where, params, alcance = ['l.existencia > 0'], [], []
    if tipo_material:
        if tipo_material not in TIPOS_MATERIAL:
            raise ErrorAlmacen('Tipo de material no válido.')
        where.append('m.tipo = ?')
        params.append(tipo_material)
        alcance.append(TIPOS_MATERIAL[tipo_material])
    if ubicacion:
        where.append('l.ubicacion = ?')
        params.append(ubicacion)
        alcance.append(f'Ubicación {ubicacion}')
    ids = [int(x) for x in (material_ids or []) if str(x).strip().isdigit()]
    if ids:
        where.append(f'm.id IN ({",".join("?" * len(ids))})')
        params += ids
        codigos = [r[0] for r in conn.execute(f'SELECT codigo FROM materiales WHERE id IN ({",".join("?" * len(ids))}) '
                                              'ORDER BY codigo', ids).fetchall()]
        alcance.append('Materiales: ' + ', '.join(codigos))
    lotes = conn.execute('SELECT l.id FROM lotes l JOIN materiales m ON m.id = l.material_id '
                         f'WHERE {" AND ".join(where)} ORDER BY m.codigo, l.numero_lote', params).fetchall()
    if not lotes:
        raise ErrorAlmacen('No hay lotes con existencia en ese alcance.')
    folio = folios.siguiente_folio(conn, 'CNT')
    cur = conn.execute('INSERT INTO alm_conteos (folio, fecha, alcance, conto) VALUES (?, ?, ?, ?)',
                       (folio, _fecha(fecha, 'conteo') or hoy(), ' · '.join(alcance) or 'Todo el almacén', usuario))
    conteo_id = cur.lastrowid
    conn.executemany('INSERT INTO alm_conteo_partidas (conteo_id, lote_id) VALUES (?, ?)',
                     [(conteo_id, l['id']) for l in lotes])
    return {'conteo_id': conteo_id, 'folio': folio, 'lotes': len(lotes)}


def _conteo(conn, conteo_id):
    c = conn.execute('SELECT * FROM alm_conteos WHERE id = ?', (conteo_id,)).fetchone()
    if not c:
        raise ErrorAlmacen('El conteo no existe.')
    return c


def partidas_conteo(conn, conteo_id):
    """Partidas con datos del lote, diferencia y la marca recontar (el lote se movió
    después de capturarse). La existencia del sistema NO se manda a la hoja de captura."""
    filas = []
    for r in conn.execute(
            'SELECT p.*, l.numero_lote, l.lote_proveedor, l.ubicacion, l.existencia, l.fecha_caducidad, l.estatus, '
            'm.codigo AS material_codigo, m.nombre AS material_nombre, m.unidad, '
            '(SELECT MAX(id) FROM lote_movimientos WHERE lote_id = l.id) AS ultimo_mov '
            'FROM alm_conteo_partidas p JOIN lotes l ON l.id = p.lote_id JOIN materiales m ON m.id = l.material_id '
            'WHERE p.conteo_id = ? ORDER BY l.ubicacion IS NULL, l.ubicacion, m.codigo, l.numero_lote',
            (conteo_id,)).fetchall():
        d = dict(r)
        capturada = d['cantidad_contada'] is not None
        d['diferencia'] = round(d['cantidad_contada'] - d['existencia_al_capturar'], 6) if capturada else None
        d['recontar'] = bool(capturada and d['movimiento_id'] is None
                             and (d['ultimo_mov'] or 0) > (d['ultimo_movimiento_id'] or 0))
        filas.append(d)
    return filas


def capturar_conteo(conn, conteo_id, *, cantidades, usuario, observaciones=None):
    """cantidades: {lote_id: contado}. Guarda la existencia al momento de capturar (sin
    mostrarla) y el último movimiento del lote. Con todo capturado pasa a 'Capturado'."""
    c = _conteo(conn, conteo_id)
    if c['estatus'] not in ('Abierto', 'Capturado'):
        raise ErrorAlmacen(f'El conteo {c["folio"]} está {c["estatus"].lower()}: ya no se captura.')
    partidas = conn.execute('SELECT p.id, p.lote_id, l.numero_lote, l.existencia FROM alm_conteo_partidas p '
                            'JOIN lotes l ON l.id = p.lote_id WHERE p.conteo_id = ?', (conteo_id,)).fetchall()
    cantidades = {str(k): v for k, v in (cantidades or {}).items()}
    for p in partidas:
        valor = cantidades.get(str(p['lote_id']))
        if valor is None or str(valor).strip() == '':
            continue
        contado = _numero(valor, f'Lo contado del lote {p["numero_lote"]}', minimo=0)
        conn.execute('UPDATE alm_conteo_partidas SET cantidad_contada = ?, existencia_al_capturar = ?, '
                     'ultimo_movimiento_id = ? WHERE id = ?',
                     (contado, p['existencia'], _ultimo_movimiento(conn, p['lote_id']), p['id']))
    faltan = conn.execute('SELECT COUNT(*) FROM alm_conteo_partidas WHERE conteo_id = ? AND cantidad_contada IS NULL',
                          (conteo_id,)).fetchone()[0]
    obs = _texto(observaciones)
    conn.execute('UPDATE alm_conteos SET estatus = ?, conto = ?, observaciones = COALESCE(?, observaciones) '
                 'WHERE id = ?', ('Abierto' if faltan else 'Capturado', usuario, obs, conteo_id))
    return faltan


def ajustar_conteo(conn, conteo_id, *, motivos, usuario, firmar):
    """Genera los ajustes con el folio del conteo, con motivo por diferencia y la firma de
    alguien con autorizar_ajustes. motivos: {partida_id: motivo}. Devuelve cuántos ajustes."""
    c = _conteo(conn, conteo_id)
    if c['estatus'] != 'Capturado':
        raise ErrorAlmacen(f'El conteo {c["folio"]} no está capturado completo.' if c['estatus'] == 'Abierto'
                           else f'El conteo {c["folio"]} está {c["estatus"].lower()}.')
    partidas = partidas_conteo(conn, conteo_id)
    recontar = [p['numero_lote'] for p in partidas if p['recontar']]
    if recontar:
        raise ErrorAlmacen('Estos lotes se movieron después de contarse; vuelve a contarlos: '
                           + ', '.join(recontar) + '.')
    motivos = {str(k): _texto(v) for k, v in (motivos or {}).items()}
    diferencias = [p for p in partidas if abs(p['diferencia']) > _EPSILON]
    for p in diferencias:
        if not motivos.get(str(p['id'])):
            raise ErrorAlmacen(f'Escribe el motivo de la diferencia del lote {p["numero_lote"]}.')
    firma_id = _firmar(firmar, 'alm_conteos', conteo_id,
                       f'Autorizó ajustes del conteo {c["folio"]} ({len(diferencias)} diferencias)')
    if firma_id is None or not firmas.firma_tiene_facultad(conn, firma_id, 'autorizar_ajustes'):
        raise ErrorAlmacen('Los ajustes los autoriza alguien con la facultad «autorizar_ajustes».')
    for p in diferencias:
        motivo = motivos[str(p['id'])]
        _, mov_id = _mover(conn, lote_id=p['lote_id'], tipo='AJUSTE_MAS' if p['diferencia'] > 0 else 'AJUSTE_MENOS',
                           cantidad=abs(p['diferencia']), usuario=usuario, folio=c['folio'],
                           motivo=f'Conteo {c["folio"]}: {motivo}', firma_id=firma_id)
        conn.execute('UPDATE alm_conteo_partidas SET motivo_diferencia = ?, movimiento_id = ? WHERE id = ?',
                     (motivo, mov_id, p['id']))
    conn.execute("UPDATE alm_conteos SET estatus = 'Ajustado', firma_id = ? WHERE id = ?", (firma_id, conteo_id))
    return len(diferencias)


def cancelar_conteo(conn, conteo_id, *, usuario):
    c = _conteo(conn, conteo_id)
    if c['estatus'] not in ('Abierto', 'Capturado'):
        raise ErrorAlmacen(f'El conteo {c["folio"]} ya está {c["estatus"].lower()}.')
    conn.execute("UPDATE alm_conteos SET estatus = 'Cancelado', observaciones = TRIM(COALESCE(observaciones, '') "
                 "|| ' Cancelado por ' || ?) WHERE id = ?", (usuario, conteo_id))


# ---------------------------------------------------------------------------
# Etiquetas
# ---------------------------------------------------------------------------

_ESTATUS_ETIQUETADO = (
    'COALESCE((SELECT e.estatus FROM alm_etiquetas e WHERE e.lote_id = l.id ORDER BY e.id DESC LIMIT 1), '
    "(SELECT h.valor_nuevo FROM lote_historial h WHERE h.lote_id = l.id AND h.campo = 'estatus' "
    'ORDER BY h.id LIMIT 1))')


def registrar_impresion(conn, lote_id, *, copias, usuario):
    lote = inventario.lote(conn, lote_id)
    if not lote:
        raise ErrorAlmacen('El lote no existe.')
    try:
        copias = int(copias)
    except (TypeError, ValueError):
        copias = 1
    copias = max(1, min(copias, 200))
    conn.execute('INSERT INTO alm_etiquetas (lote_id, estatus, copias, usuario) VALUES (?, ?, ?, ?)',
                 (lote_id, lote['estatus'], copias, usuario))
    return copias


def pendientes_reetiquetar(conn):
    """Lotes con existencia cuya última etiqueta (o, si nunca se imprimió, la que se puso
    al recibir) lleva un estatus distinto del vigente."""
    return [dict(r) for r in conn.execute(
        f'SELECT l.id, l.numero_lote, l.estatus, l.ubicacion, {_ESTATUS_ETIQUETADO} AS etiquetado, '
        'm.codigo AS material_codigo, m.nombre AS material_nombre '
        'FROM lotes l JOIN materiales m ON m.id = l.material_id '
        f'WHERE l.existencia > 0 AND l.estatus <> {_ESTATUS_ETIQUETADO} ORDER BY l.id').fetchall()]


# ---------------------------------------------------------------------------
# Catálogos
# ---------------------------------------------------------------------------

def guardar_ubicacion(conn, *, codigo, nombre, tipo, ubicacion_id=None, activo=1):
    codigo = (_texto(codigo) or '').upper()
    nombre = _texto(nombre)
    if not codigo or not nombre:
        raise ErrorAlmacen('Escribe el código y el nombre de la ubicación.')
    if tipo not in TIPOS_UBICACION:
        raise ErrorAlmacen('Tipo de ubicación no válido.')
    otro = conn.execute('SELECT id FROM alm_ubicaciones WHERE codigo = ?', (codigo,)).fetchone()
    if otro and str(otro['id']) != str(ubicacion_id or ''):
        raise ErrorAlmacen(f'Ya existe la ubicación {codigo}.')
    if ubicacion_id:
        actual = conn.execute('SELECT * FROM alm_ubicaciones WHERE id = ?', (ubicacion_id,)).fetchone()
        if not actual:
            raise ErrorAlmacen('La ubicación no existe.')
        if actual['codigo'] != codigo and conn.execute('SELECT 1 FROM lotes WHERE ubicacion = ? LIMIT 1',
                                                       (actual['codigo'],)).fetchone():
            raise ErrorAlmacen(f'No se puede cambiar el código de {actual["codigo"]}: tiene lotes.')
        conn.execute('UPDATE alm_ubicaciones SET codigo = ?, nombre = ?, tipo = ?, activo = ? WHERE id = ?',
                     (codigo, nombre, tipo, _bool(activo), ubicacion_id))
        return int(ubicacion_id)
    return conn.execute('INSERT INTO alm_ubicaciones (codigo, nombre, tipo) VALUES (?, ?, ?)',
                        (codigo, nombre, tipo)).lastrowid


# ---------------------------------------------------------------------------
# Carga inicial
# ---------------------------------------------------------------------------

COLUMNAS_MATERIALES = ['codigo', 'nombre', 'tipo', 'unidad', 'stock_minimo', 'requiere_analisis',
                       'vida_util_meses', 'condiciones_almacen']
COLUMNAS_EXISTENCIAS = ['codigo_material', 'unidad', 'lote', 'lote_proveedor', 'proveedor', 'cantidad',
                        'fecha_caducidad', 'fecha_reanalisis', 'estatus', 'ubicacion']

# Las demás columnas de las plantillas son opcionales.
COLUMNAS_REQUERIDAS = ('codigo', 'nombre', 'tipo', 'unidad', 'codigo_material', 'lote', 'cantidad')


def _celda(valor):
    if valor is None:
        return ''
    if isinstance(valor, float) and valor != valor:   # NaN de pandas
        return ''
    if isinstance(valor, (datetime, date)):
        return _fecha(valor, 'celda')
    texto = str(valor).strip()
    if texto.lower() in ('nan', 'nat', 'none'):
        return ''
    if len(texto) >= 19 and texto[4] == '-' and texto[10] == ' ':   # '2027-01-31 00:00:00'
        return texto[:10]
    if texto.endswith('.0') and texto[:-2].isdigit():
        return texto[:-2]
    return texto


def normalizar_renglones(renglones, columnas):
    return [{c: _celda(r.get(c)) for c in columnas} for r in renglones
            if any(_celda(r.get(c)) for c in columnas)]


def validar_carga(conn, materiales, existencias):
    """Revisa las dos hojas sin escribir nada. Devuelve la lista de errores
    [{'hoja', 'renglon', 'error'}] (renglón como en Excel: el 2 es el primero de datos)."""
    errores = []

    def error(hoja, i, texto):
        errores.append({'hoja': hoja, 'renglon': i + 2, 'error': texto})

    nuevos = {}
    for i, r in enumerate(materiales):
        codigo = r['codigo']
        if not codigo or not r['nombre']:
            error('Materiales', i, 'Falta el código o el nombre.')
            continue
        if r['tipo'].upper() not in TIPOS_MATERIAL:
            error('Materiales', i, f'Tipo «{r["tipo"]}» no válido (MP, ME, EM, GR o PT).')
            continue
        if not r['unidad']:
            error('Materiales', i, 'Falta la unidad.')
            continue
        if codigo in nuevos:
            error('Materiales', i, f'El código {codigo} está repetido en la hoja.')
            continue
        for campo in ('stock_minimo', 'vida_util_meses'):
            if r[campo]:
                try:
                    float(r[campo])
                except ValueError:
                    error('Materiales', i, f'{campo.replace("_", " ").capitalize()} debe ser un número.')
        existente = conn.execute('SELECT * FROM materiales WHERE codigo = ?', (codigo,)).fetchone()
        if existente and (existente['unidad'] != r['unidad'] or existente['tipo'] != r['tipo'].upper()):
            error('Materiales', i, f'{codigo} ya existe con otro tipo o unidad ({existente["tipo"]}, '
                                   f'{existente["unidad"]}).')
        nuevos[codigo] = {'unidad': r['unidad'], 'tipo': r['tipo'].upper()}

    vistos = set()
    for i, r in enumerate(existencias):
        codigo = r['codigo_material']
        m = conn.execute('SELECT * FROM materiales WHERE codigo = ?', (codigo,)).fetchone()
        unidad = m['unidad'] if m else (nuevos.get(codigo) or {}).get('unidad')
        tipo = m['tipo'] if m else (nuevos.get(codigo) or {}).get('tipo')
        if not unidad:
            error('Existencias', i, f'El material {codigo or "(vacío)"} no existe ni viene en la hoja de materiales.')
            continue
        if r['unidad'] and r['unidad'] != unidad:
            error('Existencias', i, f'Unidad distinta: {codigo} se maneja en {unidad}, no en {r["unidad"]}.')
        if not r['lote']:
            error('Existencias', i, 'Falta el número de lote.')
        elif (codigo, r['lote']) in vistos:
            error('Existencias', i, f'El lote {r["lote"]} de {codigo} está repetido en la hoja.')
        elif m and conn.execute('SELECT 1 FROM lotes WHERE material_id = ? AND numero_lote = ?',
                                (m['id'], r['lote'])).fetchone():
            error('Existencias', i, f'El lote {r["lote"]} de {codigo} ya existe en el sistema.')
        vistos.add((codigo, r['lote']))
        try:
            if float(r['cantidad']) <= 0:
                error('Existencias', i, 'La cantidad debe ser mayor que cero.')
        except ValueError:
            error('Existencias', i, 'La cantidad debe ser un número.')
        for campo, nombre in (('fecha_caducidad', 'caducidad'), ('fecha_reanalisis', 'reanálisis')):
            if r[campo]:
                try:
                    _fecha(r[campo], nombre)
                except ErrorAlmacen:
                    error('Existencias', i, f'Fecha de {nombre} inválida «{r[campo]}» (usa AAAA-MM-DD).')
        if tipo == 'MP' and not r['fecha_caducidad']:
            error('Existencias', i, 'La caducidad es obligatoria para materias primas.')
        estatus = (r['estatus'] or 'CUARENTENA').upper()
        if estatus not in inventario.ESTATUS_LOTE:
            error('Existencias', i, f'Estatus «{r["estatus"]}» no válido.')
        if r['ubicacion'] and not conn.execute('SELECT 1 FROM alm_ubicaciones WHERE codigo = ? AND activo = 1',
                                               (r['ubicacion'],)).fetchone():
            error('Existencias', i, f'La ubicación {r["ubicacion"]} no existe.')
        if r['proveedor'] and not conn.execute('SELECT 1 FROM proveedores WHERE nombre = ?',
                                               (r['proveedor'],)).fetchone():
            error('Existencias', i, f'El proveedor «{r["proveedor"]}» no existe en el catálogo.')
    return errores


def carga_requiere_firma(existencias):
    return any((r['estatus'] or 'CUARENTENA').upper() != 'CUARENTENA' for r in existencias)


def importar_carga_inicial(conn, *, materiales, existencias, usuario, firmar=None):
    """Si hay un solo error no crea nada. Crea los materiales nuevos y los lotes con origen
    INICIAL y folio INI-AAAA-0001; los que no vienen en CUARENTENA exigen la firma de
    alguien con liberar_lotes. Devuelve {folio, materiales, lotes}."""
    errores = validar_carga(conn, materiales, existencias)
    if errores:
        raise ErrorAlmacen(f'La carga tiene {len(errores)} errores; corrígelos y vuelve a subirla.')
    if not existencias and not materiales:
        raise ErrorAlmacen('Las plantillas vienen vacías.')
    folio = folios.siguiente_folio(conn, 'INI')
    firma_id = None
    if carga_requiere_firma(existencias):
        if not firmar:
            raise ErrorAlmacen('Hay lotes que vienen dictaminados: firma alguien con la facultad «liberar_lotes».')
        firma_id = firmar('alm_carga_inicial', int(folio.rsplit('-', 1)[-1]),
                          f'Autorizó carga inicial {folio} con lotes dictaminados')
    creados_m = 0
    for r in materiales:
        if conn.execute('SELECT 1 FROM materiales WHERE codigo = ?', (r['codigo'],)).fetchone():
            continue
        extra = {k: r[k] for k in ('stock_minimo', 'vida_util_meses', 'condiciones_almacen') if r[k]}
        if 'vida_util_meses' in extra:
            extra['vida_util_meses'] = int(float(extra['vida_util_meses']))
        if r['requiere_analisis']:
            extra['requiere_analisis'] = 0 if r['requiere_analisis'].strip().lower() in ('0', 'no', 'n', 'false') else 1
        inventario.crear_material(conn, codigo=r['codigo'], nombre=r['nombre'], tipo=r['tipo'].upper(),
                                  unidad=r['unidad'], usuario=usuario, **extra)
        creados_m += 1
    creados_l = 0
    for r in existencias:
        m = conn.execute('SELECT id FROM materiales WHERE codigo = ?', (r['codigo_material'],)).fetchone()
        prov = conn.execute('SELECT id FROM proveedores WHERE nombre = ?', (r['proveedor'],)).fetchone() \
            if r['proveedor'] else None
        estatus = (r['estatus'] or 'CUARENTENA').upper()
        inventario.crear_lote(
            conn, material_id=m['id'], cantidad=float(r['cantidad']), origen='INICIAL', usuario=usuario,
            modulo=MODULO, numero_lote=r['lote'], folio=folio, fecha_caducidad=r['fecha_caducidad'] or None,
            fecha_reanalisis=r['fecha_reanalisis'] or None, lote_proveedor=r['lote_proveedor'] or None,
            proveedor_id=prov['id'] if prov else None, ubicacion=r['ubicacion'] or None,
            estatus=estatus, firma_id=firma_id if estatus != 'CUARENTENA' else None)
        creados_l += 1
    return {'folio': folio, 'materiales': creados_m, 'lotes': creados_l}


# ---------------------------------------------------------------------------
# Consultas para pantallas, ganchos y reportes
# ---------------------------------------------------------------------------

def indicadores(conn):
    h = hoy()
    dias = parametro_int('almacen.dias_alerta_caducidad', conn)
    limite = date.fromordinal(date.today().toordinal() + dias).isoformat()
    uno = lambda sql, *p: conn.execute(sql, p).fetchone()[0]  # noqa: E731
    return {
        'cuarentena': uno("SELECT COUNT(*) FROM lotes WHERE existencia > 0 AND estatus = 'CUARENTENA'"),
        'por_caducar': uno('SELECT COUNT(*) FROM lotes WHERE existencia > 0 AND fecha_caducidad >= ? '
                           'AND fecha_caducidad <= ?', h, limite),
        'caducados': uno('SELECT COUNT(*) FROM lotes WHERE existencia > 0 AND fecha_caducidad < ?', h),
        'bajo_minimo': len(materiales_bajo_minimo(conn)),
        'dias': dias,
    }


def materiales_bajo_minimo(conn):
    h = hoy()
    return [dict(r) for r in conn.execute(
        "SELECT m.id, m.codigo, m.nombre, m.unidad, m.stock_minimo, COALESCE(SUM(CASE WHEN l.estatus = 'APROBADO' "
        'AND (l.fecha_caducidad IS NULL OR l.fecha_caducidad >= ?) AND (l.fecha_reanalisis IS NULL OR '
        'l.fecha_reanalisis >= ?) THEN l.existencia END), 0) AS disponible '
        'FROM materiales m LEFT JOIN lotes l ON l.material_id = m.id '
        'WHERE m.activo = 1 AND m.stock_minimo > 0 GROUP BY m.id HAVING disponible < m.stock_minimo '
        'ORDER BY m.codigo', (h, h)).fetchall()]


def _filtros_lote(conn, filtros):
    h = hoy()
    where, params = ['l.existencia > 0'], []
    f = filtros.get('f')
    if f == 'cuarentena':
        where.append("l.estatus = 'CUARENTENA'")
    elif f == 'por_caducar':
        dias = parametro_int('almacen.dias_alerta_caducidad', conn)
        where.append('l.fecha_caducidad >= ? AND l.fecha_caducidad <= ?')
        params += [h, date.fromordinal(date.today().toordinal() + dias).isoformat()]
    elif f == 'caducados':
        where.append('l.fecha_caducidad < ?')
        params.append(h)
    elif f == 'reanalisis':
        where.append('l.fecha_reanalisis < ?')
        params.append(h)
    elif f == 'cuarentena_larga':
        dias = parametro_int('almacen.dias_alerta_cuarentena', conn)
        where.append("l.estatus = 'CUARENTENA' AND l.fecha_entrada < ?")
        params.append(date.fromordinal(date.today().toordinal() - dias).isoformat())
    elif f == 'rechazados_caducados':
        where.append("(l.estatus = 'RECHAZADO' OR l.fecha_caducidad < ?)")
        params.append(h)
    if filtros.get('tipo') in TIPOS_MATERIAL:
        where.append('m.tipo = ?')
        params.append(filtros['tipo'])
    estatus = filtros.get('estatus')
    if estatus == 'CADUCADO':
        where.append('l.fecha_caducidad < ?')
        params.append(h)
    elif estatus in inventario.ESTATUS_LOTE:
        where.append('l.estatus = ?')
        params.append(estatus)
    if filtros.get('ubicacion'):
        where.append('l.ubicacion = ?')
        params.append(filtros['ubicacion'])
    q = _texto(filtros.get('q'))
    if q:
        where.append('(m.codigo LIKE ? OR m.nombre LIKE ? OR l.numero_lote LIKE ? OR l.lote_proveedor LIKE ?)')
        params += [f'%{q}%'] * 4
    return ' AND '.join(where), params


def existencias_por_lote(conn, filtros, limite=50, desde=0):
    where, params = _filtros_lote(conn, filtros)
    base = 'FROM lotes l JOIN materiales m ON m.id = l.material_id LEFT JOIN proveedores p ON p.id = l.proveedor_id '
    total = conn.execute(f'SELECT COUNT(*) {base} WHERE {where}', params).fetchone()[0]
    filas = conn.execute(
        'SELECT l.*, m.codigo AS material_codigo, m.nombre AS material_nombre, m.tipo AS material_tipo, m.unidad, '
        f'p.nombre AS proveedor_nombre {base} WHERE {where} '
        'ORDER BY l.fecha_caducidad IS NULL, l.fecha_caducidad, m.codigo, l.id LIMIT ? OFFSET ?',
        params + [limite, desde]).fetchall()
    h = hoy()
    salida = []
    for r in filas:
        d = dict(r)
        d['caducado'] = bool(d['fecha_caducidad'] and d['fecha_caducidad'] < h)
        salida.append(d)
    return salida, total


def existencias_por_material(conn, filtros):
    h = hoy()
    where, params = ['1 = 1'], [h, h, h, h]
    if filtros.get('tipo') in TIPOS_MATERIAL:
        where.append('m.tipo = ?')
        params.append(filtros['tipo'])
    q = _texto(filtros.get('q'))
    if q:
        where.append('(m.codigo LIKE ? OR m.nombre LIKE ?)')
        params += [f'%{q}%'] * 2
    if filtros.get('f') == 'bajo_minimo':
        ids = [m['id'] for m in materiales_bajo_minimo(conn)] or [0]
        where.append(f'm.id IN ({",".join("?" * len(ids))})')
        params += ids
    vigente = '(l.fecha_caducidad IS NULL OR l.fecha_caducidad >= ?)'
    filas = conn.execute(
        'SELECT m.id, m.codigo, m.nombre, m.tipo, m.unidad, m.stock_minimo, m.activo, '
        f"COALESCE(SUM(CASE WHEN l.estatus = 'APROBADO' AND {vigente} AND (l.fecha_reanalisis IS NULL "
        'OR l.fecha_reanalisis >= ?) THEN l.existencia END), 0) AS disponible, '
        f"COALESCE(SUM(CASE WHEN l.estatus = 'CUARENTENA' AND {vigente} THEN l.existencia END), 0) AS cuarentena, "
        "COALESCE(SUM(CASE WHEN l.estatus = 'RETENIDO' THEN l.existencia END), 0) AS retenido, "
        "COALESCE(SUM(CASE WHEN l.estatus = 'RECHAZADO' OR l.fecha_caducidad < ? THEN l.existencia END), 0) "
        'AS rechazado, COALESCE(SUM(l.existencia), 0) AS total '
        'FROM materiales m LEFT JOIN lotes l ON l.material_id = m.id '
        f'WHERE m.activo = 1 AND {" AND ".join(where)} GROUP BY m.id ORDER BY m.codigo',
        [h, h, h, h] + params[4:]).fetchall()
    salida = []
    for r in filas:
        d = dict(r)
        d['bajo_minimo'] = bool(d['stock_minimo'] and d['disponible'] < d['stock_minimo'])
        salida.append(d)
    return salida


def kardex_lote(conn, lote_id):
    return [dict(r) for r in conn.execute(
        'SELECT mv.*, f.nombre AS firma_nombre, f.significado AS firma_significado FROM lote_movimientos mv '
        'LEFT JOIN firmas f ON f.id = mv.firma_id WHERE mv.lote_id = ? ORDER BY mv.id', (lote_id,)).fetchall()]


def historial_lote(conn, lote_id):
    return [dict(r) for r in conn.execute(
        'SELECT h.*, f.nombre AS firma_nombre, f.significado AS firma_significado FROM lote_historial h '
        'LEFT JOIN firmas f ON f.id = h.firma_id WHERE h.lote_id = ? ORDER BY h.id', (lote_id,)).fetchall()]


def kardex_material(conn, material_id):
    lotes = [dict(r) for r in conn.execute(
        'SELECT l.*, p.nombre AS proveedor_nombre FROM lotes l LEFT JOIN proveedores p ON p.id = l.proveedor_id '
        'WHERE l.material_id = ? ORDER BY l.fecha_caducidad IS NULL, l.fecha_caducidad, l.id',
        (material_id,)).fetchall()]
    movimientos = [dict(r) for r in conn.execute(
        'SELECT mv.*, l.numero_lote FROM lote_movimientos mv JOIN lotes l ON l.id = mv.lote_id '
        'WHERE l.material_id = ? ORDER BY mv.id', (material_id,)).fetchall()]
    entradas = sum(m['cantidad'] for m in movimientos if m['cantidad'] > 0)
    salidas = -sum(m['cantidad'] for m in movimientos if m['cantidad'] < 0)
    return lotes, movimientos, {'entradas': entradas, 'salidas': salidas,
                                'existencia': sum(l['existencia'] for l in lotes)}
