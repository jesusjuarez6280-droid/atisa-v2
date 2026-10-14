"""Dictamen del lote: lista de verificación, aprobar, rechazar, retener y extender el reanálisis
(diseño 03, 4.4 y reglas 9 a 12).

Firma crítica con liberar_lotes (la de quien tiene la sesión). Aprobar solo con la lista completa;
rechazar y retener siempre se pueden, con motivo. El cambio de estatus lo hace la base
(cambiar_estatus, con cascada en el granel) y la lista, tal como se vio al firmar, queda guardada.
"""
from nucleo import inventario, registro
from nucleo.parametros import parametro_int

from ._comun import (ErrorCalidad, texto, hoy, fecha, firmar, sumar_meses, nombre_firmante, lote_fila,
                     sigue_al_granel, a_json, de_json)
from . import especificaciones as esp


def _ultimo_analisis(conn, lote_id, tipo=None):
    sql = "SELECT * FROM cc_analisis WHERE lote_id = ? AND estatus <> 'CANCELADO'"
    params = [lote_id]
    if tipo:
        sql += ' AND tipo = ?'
        params.append(tipo)
    return conn.execute(sql + ' ORDER BY id DESC LIMIT 1', params).fetchone()


def _presentaciones(conn, lote_id):
    return conn.execute(
        "SELECT l.*, m.codigo AS material_codigo, m.nombre AS material_nombre FROM lotes l "
        "JOIN materiales m ON m.id = l.material_id WHERE l.lote_padre_id = ? AND m.tipo = 'PT' "
        "AND l.origen <> 'DEVOLUCION' ORDER BY l.id", (lote_id,)).fetchall()


def _tiene_retencion(conn, lote_id):
    return conn.execute('SELECT 1 FROM cc_muestras_retencion WHERE lote_id = ? LIMIT 1', (lote_id,)).fetchone() \
        is not None


def verificaciones(conn, lote_id, numero_empleado=None) -> dict:
    """Lista de verificación para aprobar. {'calidad': [{'texto', 'ok', 'detalle'}], 'modulos': [motivo],
    'motivos': [lo que falta], 'completa': bool, 'analisis': dict | None, 'especificacion': dict | None}."""
    lote = lote_fila(conn, lote_id)
    base_id = lote['lote_padre_id'] if sigue_al_granel(lote) else lote['id']
    base = lote_fila(conn, base_id) if base_id != lote['id'] else lote
    items = []

    def item(txt, ok, detalle=''):
        items.append({'texto': txt, 'ok': bool(ok), 'detalle': detalle})

    a = _ultimo_analisis(conn, base_id, 'LIBERACION')
    e = esp.especificacion(conn, a['especificacion_id']) if a else esp.vigente(conn, base['material_id'])
    sigue = f' (del granel {base["numero_lote"]})' if base_id != lote['id'] else ''
    if not a:
        item('Análisis de liberación conforme y revisado', False, f'No hay análisis de liberación{sigue}.')
    else:
        revisado = bool(a['reviso_firma_id']) or (e is not None and not e['requiere_revision'])
        if a['estatus'] == 'CONFORME' and revisado:
            quien = nombre_firmante(conn, a['reviso_firma_id'])
            item('Análisis de liberación conforme y revisado', True,
                 f'{a["folio"]} conforme' + (f' · revisó {quien}' if quien else '') + sigue)
        elif a['estatus'] == 'NO_CONFORME':
            item('Análisis de liberación conforme y revisado', False, f'{a["folio"]} quedó NO CONFORME{sigue}.')
        else:
            item('Análisis de liberación conforme y revisado', False,
                 f'{a["folio"]} está {a["estatus"].replace("_", " ").lower()}{sigue}.')
    posterior = conn.execute(
        "SELECT folio, tipo, estatus FROM cc_analisis WHERE lote_id IN (?, ?) AND tipo <> 'LIBERACION' "
        "AND estatus <> 'CANCELADO' ORDER BY id DESC LIMIT 1", (base_id, lote['id'])).fetchone()
    if posterior and posterior['estatus'] != 'CONFORME':
        item('Análisis posteriores conformes', False,
             f'{posterior["folio"]} ({posterior["tipo"].lower()}) está {posterior["estatus"].replace("_", " ").lower()}.')
    abiertas = conn.execute(
        "SELECT i.folio FROM cc_investigaciones i JOIN cc_analisis a ON a.id = i.analisis_id "
        "WHERE a.lote_id IN (?, ?) AND i.estatus = 'ABIERTA' ORDER BY i.id", (base_id, lote['id'])).fetchall()
    item('Sin investigaciones abiertas', not abiertas,
         ', '.join(f'{x["folio"]} abierta' for x in abiertas) if abiertas else 'Ninguna abierta')
    caducado = bool(lote['fecha_caducidad']) and lote['fecha_caducidad'] < hoy()
    item('Lote no caducado', not caducado,
         f'Caducó el {lote["fecha_caducidad"]}' if caducado else (lote['fecha_caducidad'] or 'Sin caducidad'))
    if e is not None and e['pide_retencion']:
        faltan = []
        if lote['material_tipo'] == 'GR':
            hijos = _presentaciones(conn, lote['id'])
            if hijos and parametro_int('calidad.retencion_presentaciones', conn):
                faltan = [h['numero_lote'] + f' ({h["material_codigo"]})' for h in hijos
                          if not _tiene_retencion(conn, h['id'])]
            elif not hijos and not _tiene_retencion(conn, lote['id']):
                faltan = [lote['numero_lote']]
        elif not _tiene_retencion(conn, lote['id']):
            faltan = [lote['numero_lote']]
        item('Muestras de retención guardadas', not faltan,
             ('Falta la de ' + ', '.join(faltan)) if faltan else 'Guardadas')
    else:
        item('Muestras de retención guardadas', True, 'La especificación no las pide')
    if numero_empleado and a and parametro_int('calidad.libera_distinto_de_analista', conn):
        from .analisis import capturistas
        capturo = str(numero_empleado) in capturistas(conn, a['id'])
        item('Quien dictamina no capturó resultados del análisis', not capturo,
             'Tú capturaste resultados de este análisis' if capturo else 'Correcto')

    modulos = list(registro.verificaciones_liberacion(conn, lote['id']))
    if base_id != lote['id']:
        modulos += [m for m in registro.verificaciones_liberacion(conn, base_id) if m not in modulos]
    motivos = [f'{i["texto"]}: {i["detalle"]}' for i in items if not i['ok']] + modulos
    return {'calidad': items, 'modulos': modulos, 'motivos': motivos, 'completa': not motivos,
            'analisis': dict(a) if a else None, 'especificacion': dict(e) if e else None,
            'lote_analizado_id': base_id}


def _registrar(conn, *, lote_id, analisis_id, decision, anterior, motivo, v, cascada, firma_id, nueva_fecha=None):
    conn.execute('INSERT INTO cc_dictamenes (lote_id, analisis_id, decision, estatus_anterior, motivo, '
                 'verificaciones, lotes_cascada, nueva_fecha_reanalisis, firma_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                 (lote_id, analisis_id, decision, anterior, motivo,
                  a_json({'calidad': v['calidad'], 'modulos': v['modulos']}),
                  a_json(cascada) if cascada else None, nueva_fecha, firma_id))


def _numeros(conn, ids):
    if not ids:
        return []
    return [r[0] for r in conn.execute(
        f'SELECT numero_lote FROM lotes WHERE id IN ({",".join("?" * len(ids))}) ORDER BY id', ids).fetchall()]


def fecha_reanalisis_propuesta(conn, lote_id):
    """Al aprobar por primera vez una materia prima con meses de reanálisis en su especificación."""
    lote = lote_fila(conn, lote_id)
    if lote['material_tipo'] != 'MP' or lote['fecha_reanalisis']:
        return None
    a = _ultimo_analisis(conn, lote_id, 'LIBERACION')
    e = esp.especificacion(conn, a['especificacion_id']) if a else esp.vigente(conn, lote['material_id'])
    if not e or not e['meses_reanalisis']:
        return None
    propuesta = sumar_meses(hoy(), e['meses_reanalisis'])
    if lote['fecha_caducidad'] and propuesta > lote['fecha_caducidad']:
        propuesta = lote['fecha_caducidad']
    return propuesta


def aprobar(conn, *, lote_id, firma, usuario, motivo=None, fecha_reanalisis=None) -> list:
    """Solo con la lista de verificación completa. Devuelve los ids de lotes aprobados (granel + presentaciones)."""
    lote = lote_fila(conn, lote_id)
    if lote['estatus'] not in ('CUARENTENA', 'RETENIDO'):
        raise ErrorCalidad(f'El lote {lote["numero_lote"]} está {lote["estatus"]}: no se puede aprobar.')
    v = verificaciones(conn, lote_id, numero_empleado=(firma or {}).get('numero_empleado'))
    if not v['completa']:
        raise ErrorCalidad('No se puede aprobar: ' + '; '.join(v['motivos']))
    propuesta = fecha_reanalisis_propuesta(conn, lote_id)
    nueva = fecha(fecha_reanalisis, 'fecha de reanálisis') or propuesta
    if nueva and lote['fecha_caducidad'] and nueva > lote['fecha_caducidad']:
        raise ErrorCalidad('La fecha de reanálisis no puede pasar de la caducidad.')
    if nueva and nueva <= hoy():
        raise ErrorCalidad('La fecha de reanálisis debe ser posterior a hoy.')
    a = v['analisis']
    fid = firmar(conn, firma, 'Liberó', 'lotes', lote_id, f'Aprobó el lote {lote["numero_lote"]} '
                 f'({lote["material_codigo"]})', facultad='liberar_lotes', critica=True)
    motivo = texto(motivo) or (f'Análisis {a["folio"]} conforme' if a else 'Dictamen de calidad')
    afectados = inventario.cambiar_estatus(conn, lote_id=lote_id, nuevo='APROBADO', usuario=usuario, motivo=motivo,
                                           firma_id=fid, folio=a['folio'] if a else None)
    if nueva and nueva != lote['fecha_reanalisis']:
        inventario.corregir_lote(conn, lote_id=lote_id, campo='fecha_reanalisis', valor=nueva, usuario=usuario,
                                 motivo='Fecha de reanálisis al aprobar', firma_id=fid)
    _registrar(conn, lote_id=lote_id, analisis_id=a['id'] if a else None, decision='APROBADO',
               anterior=lote['estatus'], motivo=motivo, v=v, cascada=_numeros(conn, afectados[1:]), firma_id=fid,
               nueva_fecha=nueva)
    return afectados


def rechazar(conn, *, lote_id, motivo, firma, usuario, folio=None) -> list:
    """Siempre se puede, con motivo. Un lote aprobado se retiene y se rechaza con la misma firma."""
    lote = lote_fila(conn, lote_id)
    if lote['estatus'] == 'RECHAZADO':
        raise ErrorCalidad(f'El lote {lote["numero_lote"]} ya está rechazado.')
    motivo = texto(motivo)
    if not motivo:
        raise ErrorCalidad('Escribe el motivo del rechazo.')
    v = verificaciones(conn, lote_id)
    fid = firmar(conn, firma, 'Rechazó', 'lotes', lote_id, f'Rechazó el lote {lote["numero_lote"]}: {motivo}',
                 facultad='liberar_lotes', critica=True)
    a = _ultimo_analisis(conn, v['lote_analizado_id'])
    folio = texto(folio) or (a['folio'] if a else None)
    afectados = []
    if lote['estatus'] == 'APROBADO':
        afectados = inventario.cambiar_estatus(conn, lote_id=lote_id, nuevo='RETENIDO', usuario=usuario,
                                               motivo=f'{motivo} (se retiene para rechazarlo)', firma_id=fid, folio=folio)
    mas = inventario.cambiar_estatus(conn, lote_id=lote_id, nuevo='RECHAZADO', usuario=usuario, motivo=motivo,
                                     firma_id=fid, folio=folio)
    afectados = list(dict.fromkeys(afectados + mas))
    _registrar(conn, lote_id=lote_id, analisis_id=a['id'] if a else None, decision='RECHAZADO',
               anterior=lote['estatus'], motivo=motivo, v=v, cascada=_numeros(conn, afectados[1:]), firma_id=fid)
    return afectados


def retener(conn, *, lote_id, motivo, firma, usuario, folio=None) -> list:
    lote = lote_fila(conn, lote_id)
    if lote['estatus'] not in ('CUARENTENA', 'APROBADO'):
        raise ErrorCalidad(f'El lote {lote["numero_lote"]} está {lote["estatus"]}: no se puede retener.')
    motivo = texto(motivo)
    if not motivo:
        raise ErrorCalidad('Escribe el motivo de la retención.')
    v = verificaciones(conn, lote_id)
    fid = firmar(conn, firma, 'Retuvo', 'lotes', lote_id, f'Retuvo el lote {lote["numero_lote"]}: {motivo}',
                 facultad='liberar_lotes', critica=True)
    a = _ultimo_analisis(conn, v['lote_analizado_id'])
    afectados = inventario.cambiar_estatus(conn, lote_id=lote_id, nuevo='RETENIDO', usuario=usuario, motivo=motivo,
                                           firma_id=fid, folio=texto(folio) or (a['folio'] if a else None))
    _registrar(conn, lote_id=lote_id, analisis_id=a['id'] if a else None, decision='RETENIDO',
               anterior=lote['estatus'], motivo=motivo, v=v, cascada=_numeros(conn, afectados[1:]), firma_id=fid)
    return afectados


def extender_reanalisis(conn, *, lote_id, nueva_fecha, firma, usuario, motivo=None) -> int:
    """Exige un reanálisis CONFORME posterior a la última extensión y una fecha que no pase la caducidad."""
    lote = lote_fila(conn, lote_id)
    if lote['estatus'] != 'APROBADO':
        raise ErrorCalidad(f'El lote {lote["numero_lote"]} está {lote["estatus"]}: solo se extiende un lote aprobado.')
    nueva = fecha(nueva_fecha, 'nueva fecha de reanálisis')
    if not nueva:
        raise ErrorCalidad('Escribe la nueva fecha de reanálisis.')
    if nueva <= hoy():
        raise ErrorCalidad('La nueva fecha de reanálisis debe ser posterior a hoy.')
    if lote['fecha_caducidad'] and nueva > lote['fecha_caducidad']:
        raise ErrorCalidad(f'La fecha de reanálisis no puede pasar de la caducidad ({lote["fecha_caducidad"]}).')
    a = _ultimo_analisis(conn, lote_id, 'REANALISIS')
    usado = conn.execute("SELECT 1 FROM cc_dictamenes WHERE lote_id = ? AND decision = 'REANALISIS_EXTENDIDO' "
                         "AND analisis_id = ?", (lote_id, a['id'] if a else None)).fetchone()
    if not a or a['estatus'] != 'CONFORME' or usado:
        raise ErrorCalidad('Para extender la fecha se necesita un análisis de reanálisis CONFORME.')
    v = verificaciones(conn, lote_id)
    fid = firmar(conn, firma, 'Liberó', 'lotes', lote_id,
                 f'Extendió el reanálisis de {lote["numero_lote"]} al {nueva}', facultad='liberar_lotes', critica=True)
    motivo = texto(motivo) or f'Reanálisis {a["folio"]} conforme'
    inventario.corregir_lote(conn, lote_id=lote_id, campo='fecha_reanalisis', valor=nueva, usuario=usuario,
                             motivo=motivo, firma_id=fid)
    _registrar(conn, lote_id=lote_id, analisis_id=a['id'], decision='REANALISIS_EXTENDIDO', anterior=lote['estatus'],
               motivo=motivo, v=v, cascada=None, firma_id=fid, nueva_fecha=nueva)
    return fid


def historial(conn, lote_id):
    filas = conn.execute('SELECT d.*, f.nombre AS firmo, a.folio AS analisis_folio FROM cc_dictamenes d '
                         'JOIN firmas f ON f.id = d.firma_id LEFT JOIN cc_analisis a ON a.id = d.analisis_id '
                         'WHERE d.lote_id = ? ORDER BY d.id DESC', (lote_id,)).fetchall()
    salida = []
    for f in filas:
        d = dict(f)
        d['verificaciones'] = de_json(d['verificaciones'], {})
        d['lotes_cascada'] = de_json(d['lotes_cascada'], [])
        salida.append(d)
    return salida


def presentaciones_que_cambian(conn, lote_id):
    lote = lote_fila(conn, lote_id)
    if lote['material_tipo'] != 'GR':
        return []
    return [dict(h) for h in _presentaciones(conn, lote_id) if h['estatus'] == lote['estatus']]
