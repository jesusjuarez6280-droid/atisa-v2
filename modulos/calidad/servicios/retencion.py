"""Muestras de retención: guardar, usar y destruir (diseño 03, 4.7 y regla 14).

La cantidad sale del lote como MUESTRA. Se puede destruir a partir de la caducidad + calidad.meses_retencion;
sin caducidad, a partir de la fecha en que se guardó + ese mismo plazo.
"""
from nucleo import inventario

from ._comun import (ErrorCalidad, ROLES_CALIDAD, texto, numero, hoy, firmar, sumar_meses, meses_retencion,
                     lote_fila)


def muestra(conn, muestra_id):
    fila = conn.execute(
        'SELECT r.*, l.numero_lote, l.fecha_caducidad, m.codigo AS material_codigo, m.nombre AS material_nombre, '
        'm.unidad FROM cc_muestras_retencion r JOIN lotes l ON l.id = r.lote_id '
        'JOIN materiales m ON m.id = l.material_id WHERE r.id = ?', (muestra_id,)).fetchone()
    if not fila:
        raise ErrorCalidad('La muestra de retención no existe.')
    return fila


def destruir_desde(conn, lote):
    base = lote['fecha_caducidad'] or hoy()
    return sumar_meses(base, meses_retencion(conn))


def guardar(conn, *, lote_id, envases, cantidad, ubicacion, usuario, firma=None, firma_id=None, analisis_id=None,
            en_gramos=False) -> int:
    """firma (PIN) o firma_id (cuando se guarda junto con el muestreo, con la misma firma)."""
    lote = lote_fila(conn, lote_id)
    envases = numero(envases, 'los envases', entero=True, opcional=False)
    if envases <= 0:
        raise ErrorCalidad('Los envases deben ser al menos uno.')
    cantidad = numero(cantidad, 'la cantidad') or 0
    if cantidad < 0:
        raise ErrorCalidad('La cantidad no puede ser negativa.')
    if en_gramos and (lote['unidad'] or '').lower() == 'kg':
        cantidad = cantidad / 1000.0
    ubicacion = texto(ubicacion)
    if not ubicacion:
        raise ErrorCalidad('Indica la ubicación en la muestroteca.')
    if not firma_id:
        firma_id = firmar(conn, firma, 'Realizó', 'lotes', lote_id,
                          f'Guardó muestra de retención del lote {lote["numero_lote"]}', roles=ROLES_CALIDAD)
    movimiento_id = None
    if cantidad > 0:
        inventario.mover(conn, lote_id=lote_id, tipo='MUESTRA', cantidad=cantidad, usuario=usuario, modulo='calidad',
                         motivo='Muestra de retención', firma_id=firma_id)
        movimiento_id = conn.execute('SELECT MAX(id) FROM lote_movimientos WHERE lote_id = ?', (lote_id,)).fetchone()[0]
    cur = conn.execute(
        'INSERT INTO cc_muestras_retencion (lote_id, analisis_id, envases, envases_actuales, cantidad, ubicacion, '
        'fecha_guardado, destruir_desde, movimiento_id, guardo_firma_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (lote_id, analisis_id, envases, envases, cantidad, ubicacion, hoy(), destruir_desde(conn, lote),
         movimiento_id, firma_id))
    return cur.lastrowid


def usar(conn, *, muestra_id, envases, motivo, firma, folio=None) -> int:
    """Para una queja, una investigación o un reanálisis. No se pueden usar más envases de los que quedan."""
    m = muestra(conn, muestra_id)
    if m['estatus'] != 'GUARDADA':
        raise ErrorCalidad(f'La muestra está {m["estatus"].lower()}.')
    envases = numero(envases, 'los envases', entero=True, opcional=False)
    if envases <= 0:
        raise ErrorCalidad('Los envases deben ser al menos uno.')
    if envases > m['envases_actuales']:
        raise ErrorCalidad(f'Solo quedan {m["envases_actuales"]} envases de la muestra del lote {m["numero_lote"]}.')
    motivo = texto(motivo)
    if not motivo:
        raise ErrorCalidad('Escribe para qué se usa la muestra.')
    fid = firmar(conn, firma, 'Realizó', 'cc_muestras_retencion', muestra_id,
                 f'Usó {envases} envase(s) de la retención del lote {m["numero_lote"]}', roles=ROLES_CALIDAD)
    conn.execute('INSERT INTO cc_muestra_usos (muestra_id, envases, motivo, folio, firma_id) VALUES (?, ?, ?, ?, ?)',
                 (muestra_id, envases, motivo, texto(folio), fid))
    quedan = m['envases_actuales'] - envases
    conn.execute('UPDATE cc_muestras_retencion SET envases_actuales = ?, estatus = ? WHERE id = ?',
                 (quedan, 'AGOTADA' if quedan == 0 else 'GUARDADA', muestra_id))
    return fid


def destruir(conn, *, muestra_id, firma) -> int:
    m = muestra(conn, muestra_id)
    if m['estatus'] == 'DESTRUIDA':
        raise ErrorCalidad('La muestra ya está destruida.')
    if m['destruir_desde'] > hoy():
        raise ErrorCalidad(f'La muestra del lote {m["numero_lote"]} se puede destruir a partir del '
                           f'{m["destruir_desde"]}.')
    fid = firmar(conn, firma, 'Realizó', 'cc_muestras_retencion', muestra_id,
                 f'Destruyó la retención del lote {m["numero_lote"]}', roles=ROLES_CALIDAD)
    conn.execute("UPDATE cc_muestras_retencion SET estatus = 'DESTRUIDA', destruyo_firma_id = ? WHERE id = ?",
                 (fid, muestra_id))
    return fid


def lista(conn, *, filtro=None, material=None, limite=200):
    sql = ('SELECT r.*, l.numero_lote, m.codigo AS material_codigo, m.nombre AS material_nombre, m.unidad, '
           "(r.destruir_desde <= date('now','localtime') AND r.estatus <> 'DESTRUIDA') AS por_destruir "
           'FROM cc_muestras_retencion r JOIN lotes l ON l.id = r.lote_id JOIN materiales m ON m.id = l.material_id ')
    where, params = [], []
    if filtro == 'por_destruir':
        where.append("r.destruir_desde <= date('now','localtime') AND r.estatus <> 'DESTRUIDA'")
    elif filtro == 'guardadas':
        where.append("r.estatus = 'GUARDADA'")
    if texto(material):
        where.append('(m.codigo LIKE ? OR m.nombre LIKE ? OR l.numero_lote LIKE ?)')
        params += [f'%{material.strip()}%'] * 3
    if where:
        sql += 'WHERE ' + ' AND '.join(where) + ' '
    return conn.execute(sql + 'ORDER BY r.id DESC LIMIT ?', (*params, limite)).fetchall()


def usos(conn, muestra_id):
    return conn.execute('SELECT u.*, f.nombre AS firmo FROM cc_muestra_usos u JOIN firmas f ON f.id = u.firma_id '
                        'WHERE u.muestra_id = ? ORDER BY u.id', (muestra_id,)).fetchall()


def lotes_sin_retencion(conn):
    """Lotes aprobados o por dictaminar cuya especificación pide retención y aún no tienen muestra
    (en un granel con presentaciones, cada presentación). Para Mis pendientes."""
    filas = conn.execute(
        "SELECT l.id, l.numero_lote, l.lote_padre_id, m.tipo, l.material_id FROM lotes l "
        "JOIN materiales m ON m.id = l.material_id WHERE l.estatus IN ('CUARENTENA','APROBADO') "
        'AND l.existencia > 0 AND NOT EXISTS (SELECT 1 FROM cc_muestras_retencion r WHERE r.lote_id = l.id) '
        "AND l.fecha_entrada >= date('now','localtime','-180 days')").fetchall()
    salida = []
    for f in filas:
        material_id = f['material_id']
        if f['tipo'] == 'PT' and f['lote_padre_id']:
            padre = conn.execute('SELECT material_id FROM lotes WHERE id = ?', (f['lote_padre_id'],)).fetchone()
            material_id = padre['material_id'] if padre else material_id
        elif f['tipo'] == 'GR' and conn.execute('SELECT 1 FROM lotes WHERE lote_padre_id = ? LIMIT 1',
                                                (f['id'],)).fetchone():
            continue        # la retención va en sus presentaciones
        e = conn.execute("SELECT pide_retencion FROM cc_especificaciones WHERE material_id = ? AND estatus = 'VIGENTE'",
                         (material_id,)).fetchone()
        if e and e['pide_retencion']:
            salida.append(dict(f))
    return salida
