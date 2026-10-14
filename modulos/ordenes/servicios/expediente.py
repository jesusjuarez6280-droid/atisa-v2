"""Expediente de lote: todo lo de una orden, con sus firmas, listo para pantalla e impresión."""
from nucleo import registro

from . import ordenes as srv_ordenes


def _firmas(conn, ids):
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    filas = conn.execute(f'SELECT * FROM firmas WHERE id IN ({",".join("?" * len(ids))})', ids).fetchall()
    return {f['id']: dict(f) for f in filas}


def _calibracion(conn, equipo_id, fecha, cache):
    """Calibración vigente del equipo ese día (si el módulo Equipos está instalado)."""
    if not equipo_id:
        return None
    clave = (equipo_id, (fecha or '')[:10])
    if clave not in cache:
        try:
            cache[clave] = registro.consulta('calibracion_vigente', conn, equipo_id, clave[1], default=None)
        except Exception:
            cache[clave] = None
    return cache[clave]


def armar(conn, orden_id):
    e = srv_ordenes.estado(conn, orden_id)
    o = e['orden']
    pesadas = [dict(p) for p in conn.execute(
        'SELECT p.*, l.numero_lote, l.fecha_caducidad, eq.codigo AS equipo_codigo, eq.nombre AS equipo_nombre, '
        'c.linea, c.tipo, m.codigo, m.nombre, m.unidad FROM op_pesadas p JOIN lotes l ON l.id = p.lote_id '
        'JOIN op_orden_componentes c ON c.id = p.componente_id JOIN materiales m ON m.id = c.material_id '
        'LEFT JOIN equipos eq ON eq.id = p.equipo_id WHERE p.orden_id = ? ORDER BY c.linea, p.id',
        (orden_id,)).fetchall()]
    ids = [o['emitio_firma_id'], o['granel_firma_id'], o['termino_firma_id'], o['reviso_firma_id'],
           o['cancelo_firma_id'], o['rendimiento_justificacion_firma_id']]
    ids += [x for p in pesadas for x in (p['realizo_firma_id'], p['verifico_firma_id'])]
    ids += [x for p in e['pasos'] for x in (p['realizo_firma_id'], p['verifico_firma_id'], p['justificacion_firma_id'])]
    ids += [x for d in e['despejes'].values() for x in (d['realizo_firma_id'], d['verifico_firma_id'])]
    ids += [p['firma_id'] for p in e['presentaciones']]
    ids += [x for c in e['conciliaciones'].values() for x in (c['firma_id'], c['justificacion_firma_id'])]
    firmas = _firmas(conn, ids)
    cache = {}
    for p in pesadas:
        p['calibracion'] = _calibracion(conn, p['equipo_id'], p['ts'], cache)
    for p in e['pasos']:
        p['calibracion'] = _calibracion(conn, p['equipo_id'], p['fin'] or p['inicio'], cache)
    granel_equipo = None
    if o['granel_equipo_id']:
        granel_equipo = conn.execute('SELECT codigo, nombre FROM equipos WHERE id = ?', (o['granel_equipo_id'],)).fetchone()
    desviaciones = []
    for origen, origen_id, que in ([('orden', o['id'], 'Orden'), ('orden_granel', o['id'], 'Rendimiento')]
                                   + [('orden_paso', p['id'], f'Paso {p["numero"]}') for p in e['pasos']]
                                   + [('orden_pesada', p['id'], f'Pesada {p["codigo"]}') for p in pesadas]
                                   + [('orden_conciliacion', c['id'], 'Conciliación')
                                      for c in e['conciliaciones'].values()]):
        for d in srv_ordenes.desviaciones(conn, origen, origen_id):
            desviaciones.append({**d, 'donde': que})
    nombres_material = {r['id']: r for r in conn.execute(
        'SELECT id, codigo, nombre FROM materiales WHERE id IN (SELECT material_id FROM op_conciliaciones '
        'WHERE orden_id = ?)', (orden_id,)).fetchall()}
    return {**e, 'pesadas': pesadas, 'firmas': firmas, 'granel_equipo': granel_equipo,
            'desviaciones': desviaciones, 'nombres_material': nombres_material,
            'formula': conn.execute('SELECT * FROM op_formulas WHERE id = ?', (o['formula_id'],)).fetchone()}
