"""Ganchos del almacén para el Tablero, Mis pendientes, enlaces entre módulos y humo."""
from flask import url_for

from nucleo.auth import PERMISOS
from nucleo.parametros import parametro_int

from . import servicios


def _url(endpoint, **valores):
    return url_for(endpoint, **valores)


def url_ficha_lote(lote_id):
    return _url('almacen.ficha_lote', lote_id=lote_id)


def url_material(material_id):
    return _url('almacen.kardex_material', material_id=material_id)


def url_recepcion(recepcion_id):
    return _url('almacen.recepcion_detalle', recepcion_id=recepcion_id)


def alertas(conn):
    h = servicios.hoy()
    salida = []
    for r in conn.execute(
            'SELECT l.id, l.numero_lote, l.existencia, l.fecha_caducidad, m.codigo, m.nombre, m.unidad '
            'FROM lotes l JOIN materiales m ON m.id = l.material_id '
            'WHERE l.existencia > 0 AND l.fecha_caducidad < ? ORDER BY l.fecha_caducidad', (h,)).fetchall():
        salida.append({'tipo': 'Lote caducado con existencia', 'icono': 'ri-error-warning-line', 'nivel': 'rojo',
                       'nombre': f'{r["codigo"]} · lote {r["numero_lote"]}', 'area': 'Almacén',
                       'detalle': f'Caducó el {r["fecha_caducidad"]}; quedan {servicios.fmt(r["existencia"])} '
                                  f'{r["unidad"]}', 'url': _url('almacen.existencias', f='caducados')})
    ind = servicios.indicadores(conn)
    dias_cua = parametro_int('almacen.dias_alerta_cuarentena', conn)
    reanalisis = conn.execute('SELECT COUNT(*) FROM lotes WHERE existencia > 0 AND fecha_reanalisis < ?',
                              (h,)).fetchone()[0]
    cuarentena_larga = conn.execute(
        "SELECT COUNT(*) FROM lotes WHERE existencia > 0 AND estatus = 'CUARENTENA' "
        "AND fecha_entrada < date('now', 'localtime', ?)", (f'-{dias_cua} days',)).fetchone()[0]
    for n, tipo, detalle, filtro, vista in (
            (ind['por_caducar'], 'Lotes por caducar', f'lotes caducan en los próximos {ind["dias"]} días',
             'por_caducar', None),
            (reanalisis, 'Reanálisis vencido', 'lotes con el reanálisis vencido', 'reanalisis', None),
            (ind['bajo_minimo'], 'Bajo mínimo', 'materiales bajo el stock mínimo', 'bajo_minimo', 'material'),
            (cuarentena_larga, 'Cuarentena larga', f'lotes en cuarentena hace más de {dias_cua} días',
             'cuarentena_larga', None)):
        if n:
            salida.append({'tipo': tipo, 'icono': 'ri-archive-2-line', 'nivel': 'amarillo', 'nombre': 'Almacén',
                           'area': 'Almacén', 'detalle': f'{n} {detalle}',
                           'url': _url('almacen.existencias', f=filtro, vista=vista) if vista
                           else _url('almacen.existencias', f=filtro)})
    return salida


def kpis(conn):
    ind = servicios.indicadores(conn)
    return [
        {'titulo': 'Lotes en cuarentena', 'valor': ind['cuarentena'], 'detalle': 'Esperan dictamen de Calidad',
         'nivel': 'amarillo' if ind['cuarentena'] else 'verde', 'url': _url('almacen.existencias', f='cuarentena')},
        {'titulo': 'Lotes por caducar', 'valor': ind['por_caducar'], 'detalle': f'Próximos {ind["dias"]} días',
         'nivel': 'amarillo' if ind['por_caducar'] else 'verde', 'url': _url('almacen.existencias', f='por_caducar')},
        {'titulo': 'Caducados con existencia', 'valor': ind['caducados'], 'detalle': 'Destruir o devolver',
         'nivel': 'rojo' if ind['caducados'] else 'verde', 'url': _url('almacen.existencias', f='caducados')},
        {'titulo': 'Materiales bajo mínimo', 'valor': ind['bajo_minimo'], 'detalle': 'Disponible < stock mínimo',
         'nivel': 'amarillo' if ind['bajo_minimo'] else 'verde',
         'url': _url('almacen.existencias', f='bajo_minimo', vista='material')},
    ]


def _puede(usuario, permiso):
    return bool(set(usuario.get('roles') or []) & set(PERMISOS.get(permiso, [])))


def pendientes(conn, usuario):
    salida = []
    if _puede(usuario, 'almacen_operar'):
        reetiquetar = servicios.pendientes_reetiquetar(conn)
        if reetiquetar:
            salida.append({'texto': f'Cambiar etiqueta de {len(reetiquetar)} lote'
                                    f'{"" if len(reetiquetar) == 1 else "s"}',
                           'url': _url('almacen.reetiquetar'), 'nivel': 'amarillo', 'fecha': None})
        for c in conn.execute("SELECT id, folio, fecha FROM alm_conteos WHERE estatus = 'Abierto' ORDER BY id"):
            salida.append({'texto': f'Conteo {c["folio"]} abierto', 'nivel': 'azul', 'fecha': c['fecha'],
                           'url': _url('almacen.conteo_detalle', conteo_id=c['id'])})
        n = conn.execute("SELECT COUNT(*) FROM lotes WHERE existencia > 0 AND (estatus = 'RECHAZADO' "
                         'OR fecha_caducidad < ?)', (servicios.hoy(),)).fetchone()[0]
        if n:
            salida.append({'texto': f'Destruir o devolver {n} lote{"" if n == 1 else "s"} rechazado'
                                    f'{"" if n == 1 else "s"} o caducado{"" if n == 1 else "s"}',
                           'url': _url('almacen.existencias', f='rechazados_caducados'), 'nivel': 'rojo',
                           'fecha': None})
    if 'autorizar_ajustes' in (usuario.get('facultades') or []):
        for c in conn.execute("SELECT id, folio, fecha FROM alm_conteos WHERE estatus = 'Capturado' ORDER BY id"):
            salida.append({'texto': f'Autorizar ajustes del conteo {c["folio"]}', 'nivel': 'amarillo',
                           'fecha': c['fecha'], 'url': _url('almacen.conteo_detalle', conteo_id=c['id'])})
    if _puede(usuario, 'almacen_proveedores'):
        n = conn.execute("SELECT COUNT(*) FROM proveedores WHERE activo = 1 AND estatus = 'En evaluación'").fetchone()[0]
        if n:
            salida.append({'texto': f'{n} proveedor{"" if n == 1 else "es"} en evaluación', 'nivel': 'azul',
                           'fecha': None, 'url': _url('almacen.catalogos', pestana='proveedores')})
    return salida


def datos_humo(conn):
    """Un proveedor, materiales, una recepción con su lote, una venta, una devolución y un
    conteo, para que las pruebas de humo abran todas las pantallas del almacén."""
    from nucleo import inventario
    prov = inventario.crear_proveedor(conn, nombre='Proveedor humo almacén', usuario='humo', estatus='Aprobado')
    mp = inventario.crear_material(conn, codigo='MP-HUMO-ALM', nombre='Materia prima humo', tipo='MP',
                                   unidad='kg', usuario='humo', stock_minimo=500)
    pt = inventario.crear_material(conn, codigo='PT-HUMO-ALM', nombre='Producto humo', tipo='PT',
                                   unidad='pza', usuario='humo', requiere_analisis=0)
    rec = servicios.recibir(conn, proveedor_id=prov, usuario='humo', firmar=None, documento='F-HUMO', partidas=[
        {'material_id': mp, 'lote_proveedor': 'PROV-HUMO', 'cantidad': 25, 'contenedores': 2,
         'fecha_caducidad': '2099-12-31', 'trae_certificado': 1}])
    lote_pt = inventario.crear_lote(conn, material_id=pt, cantidad=100, origen='INICIAL', usuario='humo',
                                    modulo='almacen', numero_lote='HUMO-PT-1', fecha_caducidad='2099-12-31')
    sal = servicios.registrar_salida(conn, tipo='VENTA', destino='Cliente humo', usuario='humo', firmar=None,
                                     partidas=[{'lote_id': lote_pt, 'cantidad': 10}])
    servicios.registrar_devolucion(conn, cliente='Cliente humo', lote_vendido_id=lote_pt, cantidad=1,
                                   motivo='Dañado', usuario='humo')
    cnt = servicios.abrir_conteo(conn, usuario='humo', material_ids=[mp])
    return {'lote_id': rec['lotes'][0], 'material_id': mp, 'recepcion_id': rec['recepcion_id'],
            'salida_id': sal['salida_id'], 'conteo_id': cnt['conteo_id']}
