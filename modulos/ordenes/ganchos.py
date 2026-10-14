"""Ganchos de Órdenes de producción hacia la base y los demás módulos (diseño 02, sección 7)."""
from datetime import date, datetime, timedelta

from flask import has_request_context, url_for
from werkzeug.security import generate_password_hash

from nucleo import equipos as srv_equipos
from nucleo import firmas as srv_firmas
from nucleo import inventario
from nucleo.parametros import parametro_int

from .servicios import ordenes as srv_ordenes
from .servicios import formulas as srv_formulas
from .servicios import planta as srv_planta


def _es(usuario, *roles):
    return bool(set(usuario.get('roles') or []) & set(roles))


# --- Tablet ---------------------------------------------------------------------
def accesos_planta(conn, usuario):
    if not _es(usuario, 'admin', 'produccion', 'operador'):
        return []
    n = conn.execute("SELECT COUNT(*) FROM op_ordenes WHERE estatus IN ('EMITIDA','EN_PROCESO')").fetchone()[0]
    return [{'titulo': 'Órdenes en proceso', 'icono': 'ri-file-list-3-line', 'url': '/ordenes/planta', 'contador': n}]


# --- Mis pendientes -------------------------------------------------------------
def pendientes(conn, usuario):
    salida = []
    if _es(usuario, 'admin', 'produccion'):
        n = conn.execute("SELECT COUNT(*) FROM op_ordenes WHERE estatus = 'BORRADOR'").fetchone()[0]
        if n and 'emitir_ordenes' in (usuario.get('facultades') or []):
            salida.append({'texto': f'Emitir {n} orden{"es" if n != 1 else ""} en borrador',
                           'url': '/ordenes?pestana=borradores', 'nivel': 'amarillo', 'fecha': None})
        for o in conn.execute("SELECT id, folio, fecha_fin FROM op_ordenes WHERE estatus = 'TERMINADA' "
                              'ORDER BY fecha_fin LIMIT 20').fetchall():
            salida.append({'texto': f'Revisar expediente {o["folio"]}', 'url': f'/ordenes/{o["id"]}',
                           'nivel': 'amarillo', 'fecha': (o['fecha_fin'] or '')[:10]})
        dias = parametro_int('ordenes.dias_alerta_en_proceso', conn)
        limite = (datetime.now() - timedelta(days=dias)).strftime('%Y-%m-%d %H:%M:%S')
        for o in conn.execute("SELECT id, folio, fecha_inicio FROM op_ordenes WHERE estatus = 'EN_PROCESO' "
                              'AND fecha_inicio < ? ORDER BY fecha_inicio LIMIT 20', (limite,)).fetchall():
            d = (date.today() - date.fromisoformat(o['fecha_inicio'][:10])).days
            salida.append({'texto': f'{o["folio"]} lleva {d} días en proceso', 'url': f'/ordenes/{o["id"]}',
                           'nivel': 'rojo', 'fecha': o['fecha_inicio'][:10]})
    if 'aprobar_maestros' in (usuario.get('facultades') or []):
        for f in conn.execute("SELECT f.id, f.version, m.codigo, m.nombre FROM op_formulas f JOIN materiales m "
                              "ON m.id = f.material_id WHERE f.estatus = 'EN_REVISION' AND f.reviso_firma_id IS NOT NULL "
                              'ORDER BY f.id').fetchall():
            salida.append({'texto': f'Aprobar fórmula {f["codigo"]} {f["nombre"]} v{f["version"]}',
                           'url': f'/ordenes/formulas/{f["id"]}', 'nivel': 'amarillo', 'fecha': None})
    return salida


# --- Tablero --------------------------------------------------------------------
def alertas(conn):
    salida = []
    for o in conn.execute("SELECT id FROM op_ordenes WHERE estatus IN ('EN_PROCESO','TERMINADA') "
                          'AND (rendimiento_fuera = 1 OR id IN (SELECT orden_id FROM op_orden_pasos WHERE fuera_de_rango = 1) '
                          'OR id IN (SELECT orden_id FROM op_conciliaciones WHERE ABS(diferencia) > 0.000001))').fetchall():
        oo = srv_ordenes.orden(conn, o['id'])
        for p in srv_ordenes.problemas(conn, oo):
            salida.append({'tipo': 'Orden de producción', 'icono': 'ri-error-warning-line', 'nivel': 'rojo',
                           'nombre': oo['folio'], 'area': 'Producción', 'detalle': p['texto'],
                           'url': f'/ordenes/{oo["id"]}'})
    limite = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d %H:%M:%S')
    n = conn.execute("SELECT COUNT(*) FROM op_ordenes WHERE estatus = 'TERMINADA' AND fecha_fin < ?",
                     (limite,)).fetchone()[0]
    if n:
        salida.append({'tipo': 'Expedientes sin revisar', 'icono': 'ri-file-search-line', 'nivel': 'amarillo',
                       'nombre': f'{n} expediente{"s" if n != 1 else ""}', 'area': 'Producción',
                       'detalle': 'Terminados hace más de 3 días sin revisar', 'url': '/ordenes?pestana=revisar'})
    return salida


def resumen(conn):
    mes = date.today().strftime('%Y-%m')
    en_proceso = conn.execute("SELECT COUNT(*) FROM op_ordenes WHERE estatus IN ('EMITIDA','EN_PROCESO')").fetchone()[0]
    por_revisar = conn.execute("SELECT COUNT(*) FROM op_ordenes WHERE estatus = 'TERMINADA'").fetchone()[0]
    fila = conn.execute("SELECT COUNT(*), AVG(rendimiento_pct) FROM op_ordenes WHERE estatus IN ('TERMINADA','REVISADA') "
                        'AND substr(fecha_fin, 1, 7) = ?', (mes,)).fetchone()
    return {'en_proceso': en_proceso, 'por_revisar': por_revisar, 'lotes_mes': fila[0],
            'rendimiento_mes': round(fila[1], 1) if fila[1] is not None else None}


def kpis(conn):
    r = resumen(conn)
    return [
        {'titulo': 'Órdenes en proceso', 'valor': r['en_proceso'], 'detalle': 'emitidas o en planta', 'nivel': 'azul',
         'url': '/ordenes'},
        {'titulo': 'Expedientes por revisar', 'valor': r['por_revisar'], 'detalle': 'órdenes terminadas',
         'nivel': 'amarillo' if r['por_revisar'] else 'verde', 'url': '/ordenes?pestana=revisar'},
        {'titulo': 'Lotes producidos', 'valor': r['lotes_mes'], 'detalle': 'este mes', 'nivel': 'verde', 'url': '/ordenes'},
        {'titulo': 'Rendimiento promedio', 'valor': f'{r["rendimiento_mes"]} %' if r['rendimiento_mes'] is not None else '—',
         'detalle': 'órdenes terminadas este mes', 'nivel': 'azul', 'url': '/ordenes/reporte'},
    ]


# --- Liberación y ficha del lote ------------------------------------------------
def verificaciones_liberacion(conn, lote_id):
    o = srv_ordenes.lote_de_orden(conn, lote_id)
    if not o:
        return []
    motivos = []
    if o['estatus'] != 'REVISADA':
        motivos.append(f'{o["folio"]}: la orden está {srv_ordenes.NOMBRE_ESTATUS[o["estatus"]].lower()}; '
                       'falta que producción revise el expediente.')
    motivos += [f'{o["folio"]}: {p["texto"]}.' for p in srv_ordenes.problemas(conn, o)]
    return motivos


def seccion_lote(conn, lote_id):
    lote = conn.execute('SELECT l.*, m.tipo FROM lotes l JOIN materiales m ON m.id = l.material_id WHERE l.id = ?',
                        (lote_id,)).fetchone()
    if not lote:
        return None
    if lote['tipo'] in ('GR', 'PT'):
        o = srv_ordenes.lote_de_orden(conn, lote_id)
        if not o:
            return None
        datos = {'fabricado': {'id': o['id'], 'folio': o['folio'], 'rendimiento': o['rendimiento_pct'],
                               'estatus': srv_ordenes.NOMBRE_ESTATUS[o['estatus']]}, 'usado': []}
    else:
        usado = [dict(r) for r in conn.execute(
            'SELECT o.id, o.folio, o.numero_lote, SUM(p.cantidad) AS cantidad FROM op_pesadas p '
            'JOIN op_ordenes o ON o.id = p.orden_id WHERE p.lote_id = ? GROUP BY o.id HAVING SUM(p.cantidad) <> 0 '
            'ORDER BY o.id DESC LIMIT 50', (lote_id,)).fetchall()]
        if not usado:
            return None
        datos = {'fabricado': None, 'usado': usado, 'unidad': None}
    return {'titulo': 'Órdenes de producción', 'plantilla': 'ordenes/_seccion_lote.html',
            'contexto': {'datos': datos, 'lote_id': lote_id}}


# --- Enlaces y consultas ----------------------------------------------------------
def url_orden(orden_id):
    return url_for('ordenes.detalle', orden_id=orden_id)


def url_expediente(orden_id):
    return url_for('ordenes.expediente', orden_id=orden_id)


def lotes_por_equipo(conn, equipo_id, desde=None):
    """Lotes donde se usó un equipo desde una fecha (para el impacto de una calibración no conforme).
    Una fila por lote producido (granel y presentaciones); una orden que aún no tiene lote sale con
    lote_id None. Campos: lote_id, fecha, numero_lote, folio, material, url, orden_id, estatus, usos."""
    desde = str(desde or '0000-00-00')[:10]
    usos = conn.execute(
        "SELECT orden_id, 'Pesada' AS uso, ts AS cuando FROM op_pesadas WHERE equipo_id = ? AND substr(ts, 1, 10) >= ? "
        "UNION ALL SELECT orden_id, 'Paso ' || numero, COALESCE(fin, inicio) FROM op_orden_pasos "
        'WHERE equipo_id = ? AND substr(COALESCE(fin, inicio), 1, 10) >= ? '
        "UNION ALL SELECT id, 'Granel', granel_ts FROM op_ordenes WHERE granel_equipo_id = ? "
        'AND substr(granel_ts, 1, 10) >= ? ORDER BY 3',
        (equipo_id, desde, equipo_id, desde, equipo_id, desde)).fetchall()
    por_orden = {}
    for u in usos:
        d = por_orden.setdefault(u['orden_id'], {'usos': [], 'primera': u['cuando'], 'ultima': u['cuando']})
        if u['uso'] not in d['usos']:
            d['usos'].append(u['uso'])
        d['ultima'] = max(d['ultima'] or '', u['cuando'] or '')
    salida = []
    for orden_id, d in por_orden.items():
        o = srv_ordenes.orden(conn, orden_id)
        url = url_for('ordenes.expediente', orden_id=orden_id) if has_request_context() else \
            f'/ordenes/{orden_id}/expediente'
        comun = {'orden_id': orden_id, 'folio': o['folio'], 'numero_lote': o['numero_lote'], 'estatus': o['estatus'],
                 'usos': d['usos'], 'fecha': (d['primera'] or '')[:10], 'primera': d['primera'],
                 'ultima': d['ultima'], 'url': url}
        lotes = []
        if o['granel_lote_id']:
            lotes.append((o['granel_lote_id'], f'{o["producto_codigo"]} {o["producto_nombre"]}'))
        lotes += [(r['lote_id'], f'{r["codigo"]} {r["nombre"]}') for r in conn.execute(
            'SELECT p.lote_id, m.codigo, m.nombre FROM op_orden_presentaciones p JOIN materiales m '
            'ON m.id = p.presentacion_id WHERE p.orden_id = ? AND p.lote_id IS NOT NULL', (orden_id,)).fetchall()]
        if not lotes:
            lotes = [(None, f'{o["producto_codigo"]} {o["producto_nombre"]} (lote aún sin producir)')]
        salida += [{**comun, 'lote_id': lote_id, 'material': material} for lote_id, material in lotes]
    return salida


def demanda_material(conn, material_id):
    """La orden (borrador, emitida o en proceso) más próxima que necesita el material y cuánto le falta de
    existencia aprobada: {orden_id, folio, fecha_programada, requerido, existencia_aprobada, faltante} o None."""
    candidatas = []
    for o in conn.execute("SELECT id, folio, estatus, formula_id, factor, fecha_programada, creado_en FROM op_ordenes "
                          "WHERE estatus IN ('BORRADOR','EMITIDA','EN_PROCESO')").fetchall():
        if o['estatus'] == 'BORRADOR':
            piezas = {p['presentacion_id']: p['piezas_planeadas'] for p in srv_ordenes.presentaciones(conn, o['id'])}
            req = sum(r['cantidad'] for r in srv_ordenes.requerimientos(conn, o['formula_id'], o['factor'], piezas)
                      if r['material_id'] == material_id)
        else:
            req = sum(c['falta'] for c in srv_ordenes.componentes(conn, o['id']) if c['material_id'] == material_id)
        if req > 0:
            candidatas.append((o['fecha_programada'] or '9999-12-31', o['creado_en'] or '', o, req))
    if not candidatas:
        return None
    candidatas.sort(key=lambda x: (x[0], x[1]))
    _, _, o, req = candidatas[0]
    aprobado = inventario.existencia_material(conn, material_id)
    return {'orden_id': o['id'], 'folio': o['folio'], 'fecha_programada': o['fecha_programada'],
            'requerido': round(req, 6), 'existencia_aprobada': aprobado,
            'faltante': max(0.0, round(req - aprobado, 6))}


# --- Pruebas de humo ------------------------------------------------------------
def _usuario_humo(conn, numero, facultades):
    if not conn.execute('SELECT 1 FROM usuarios WHERE numero_empleado = ?', (numero,)).fetchone():
        conn.execute("INSERT INTO usuarios (numero_empleado, nombre_completo, username, password_hash, rol, "
                     "debe_cambiar_password, facultades) VALUES (?, ?, ?, ?, 'produccion', 0, ?)",
                     (numero, f'Humo órdenes {numero}', f'humo_op_{numero}',
                      generate_password_hash(f'humo-{numero}-{datetime.now():%f}'), facultades))
    srv_firmas.fijar_pin(conn, numero, '2580', 'humo')


def datos_humo(conn):
    """Producto con fórmula vigente y una orden emitida con una pesada."""
    _usuario_humo(conn, 'OPH1', 'emitir_ordenes,aprobar_maestros')
    _usuario_humo(conn, 'OPH2', 'emitir_ordenes,aprobar_maestros')
    sesion = {'username': 'humo', 'numero_empleado': None, 'ip': None}

    def firma(numero, significado):
        return srv_firmas.firmar(conn, numero_empleado=numero, pin='2580', significado=significado, modulo='ordenes',
                                 entidad='humo', entidad_id=0, sesion=sesion)

    sufijo = datetime.now().strftime('%H%M%S%f')
    mp = inventario.crear_material(conn, codigo=f'MP-OPH-{sufijo}', nombre='Materia prima humo', tipo='MP',
                                   unidad='kg', usuario='humo', requiere_analisis=0)
    lote = inventario.crear_lote(conn, material_id=mp, cantidad=50, origen='RECEPCION', usuario='humo', modulo='ordenes')
    gid = srv_formulas.crear_producto(conn, granel={'codigo': f'GR-OPH-{sufijo}', 'nombre': 'Producto humo',
                                                     'unidad': 'kg', 'vida_util_meses': 24},
                                      presentaciones=[{'codigo': f'PT-OPH-{sufijo}', 'nombre': 'Producto humo 1 kg',
                                                       'unidad': 'pza', 'contenido_por_pieza': 1}], usuario='humo')
    fid = srv_formulas.crear_formula(conn, material_id=gid, tamano_lote=10, usuario='humo')
    srv_formulas.agregar_componente(conn, fid, material_id=mp, cantidad=10, tolerancia_pct=1)
    srv_formulas.agregar_paso(conn, fid, instruccion='Mezclar 10 minutos', minutos=10,
                              parametros=[{'nombre': 'Temperatura', 'unidad': '°C', 'valor_min': 20, 'valor_max': 30}])
    srv_formulas.enviar_revision(conn, fid)
    srv_formulas.revisar(conn, fid, firma('OPH1', 'Revisó'))
    srv_formulas.aprobar(conn, fid, firma('OPH2', 'Aprobó'))
    pt = srv_formulas.presentaciones(conn, gid)[0]['id']
    orden_id, _ = srv_ordenes.crear(conn, formula_id=fid, tamano_lote=10, usuario='humo', piezas={pt: 10})
    srv_ordenes.emitir(conn, orden_id, firma('OPH1', 'Autorizó'))
    bascula = srv_equipos.crear_equipo(conn, codigo=f'BAS-OPH-{sufijo}', nombre='Báscula humo', tipo='Báscula',
                                       usuario='humo', es_instrumento=1)
    checks = {k: 1 for k, _ in srv_planta.CHECKS_DESPEJE}
    srv_planta.despeje(conn, orden_id, momento='Fabricación', checks=checks, realizo_firma_id=firma('OPH1', 'Realizó'),
                       verifico_firma_id=firma('OPH2', 'Verificó'))
    comp = srv_ordenes.componentes(conn, orden_id)[0]
    srv_planta.pesar(conn, orden_id, comp['id'], lote_id=lote, cantidad=5, usuario='humo', equipo_id=bascula,
                     realizo_firma_id=firma('OPH1', 'Realizó'), verifico_firma_id=firma('OPH2', 'Verificó'))
    paso_id = srv_ordenes.pasos(conn, orden_id)[0]['id']
    return {'orden_id': orden_id, 'formula_id': fid, 'componente_id': comp['id'], 'paso_id': paso_id}
