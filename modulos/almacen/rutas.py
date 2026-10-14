"""Pantallas del almacén (blueprint 'almacen', /almacen)."""
import json
import re

from flask import Blueprint, abort, current_app, flash, request, send_file, session, url_for, render_template
from markupsafe import Markup

from nucleo import archivos, firmas, inventario, registro
from nucleo.archivos import ErrorArchivo
from nucleo.auth import login_required, requiere_modulo, usuario_puede
from nucleo.db import get_db_connection
from nucleo.firmas import FirmaInvalida
from nucleo.inventario import ErrorInventario
from nucleo.parametros import parametro, parametro_int
from nucleo.vistas import full_page, hx_redirect, render_page

from . import reportes, servicios
from .servicios import ErrorAlmacen, TIPOS_MATERIAL, TIPOS_SALIDA

bp = Blueprint('almacen', __name__, url_prefix='/almacen', template_folder='templates')

ERRORES = (ErrorAlmacen, ErrorInventario, FirmaInvalida, ErrorArchivo)
POR_PAGINA = 50
XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


@bp.app_template_filter('cant')
def _cant(n):
    return servicios.fmt(n)


@bp.context_processor
def _ayudas():
    return {'TIPOS_MATERIAL': TIPOS_MATERIAL, 'TIPOS_SALIDA': TIPOS_SALIDA,
            'puede_operar': usuario_puede('almacen_operar')}


def _usuario():
    return session.get('nombre') or session.get('username') or 'sistema'


def _firmante(conn, significado, facultad=None):
    return servicios.firmante(conn, numero=request.form.get('firma_numero'), pin=request.form.get('firma_pin'),
                              significado=significado, facultad=facultad)


def _pagina():
    try:
        return max(1, int(request.args.get('pagina', 1)))
    except ValueError:
        return 1


def _error_inesperado(conn, que):
    conn.rollback()
    current_app.logger.exception('Almacén: falló %s', que)
    flash(f'No se pudo {que}. Avisa a sistemas.', 'error')


def _ubicaciones(conn, solo_activas=True):
    sql = 'SELECT * FROM alm_ubicaciones' + (' WHERE activo = 1' if solo_activas else '') + ' ORDER BY codigo'
    return conn.execute(sql).fetchall()


def _excel(contenido, nombre):
    return send_file(contenido, mimetype=XLSX, as_attachment=True, download_name=nombre)


# ---------------------------------------------------------------------------
# Existencias
# ---------------------------------------------------------------------------

@bp.route('')
@bp.route('/')
@login_required
@requiere_modulo('almacen')
def existencias():
    conn = get_db_connection()
    filtros = {k: request.args.get(k, '') for k in ('f', 'tipo', 'estatus', 'ubicacion', 'q')}
    vista = 'material' if request.args.get('vista') == 'material' else 'lote'
    pagina = _pagina()
    lotes, total, materiales = [], 0, []
    if vista == 'lote':
        lotes, total = servicios.existencias_por_lote(conn, filtros, POR_PAGINA, (pagina - 1) * POR_PAGINA)
    else:
        materiales = servicios.existencias_por_material(conn, filtros)
    ind = servicios.indicadores(conn)
    ubicaciones = _ubicaciones(conn)
    conn.close()
    return render_page('almacen/existencias.html', vista=vista, filtros=filtros, lotes=lotes, total=total,
                       materiales=materiales, ind=ind, ubicaciones=ubicaciones, pagina=pagina,
                       paginas=max(1, -(-total // POR_PAGINA)), estatuses=servicios.FILTROS_ESTATUS)


@bp.route('/exportar')
@login_required
@requiere_modulo('almacen')
def exportar():
    conn = get_db_connection()
    cual = request.args.get('r', 'existencias')
    hoy = servicios.hoy()
    if cual == 'por_caducar':
        archivo = reportes.lotes_filtrados(conn, 'por_caducar', 'Por caducar')
    elif cual == 'cuarentena':
        archivo = reportes.lotes_filtrados(conn, 'cuarentena', 'En cuarentena')
    else:
        cual = 'existencias'
        archivo = reportes.existencias(conn)
    conn.close()
    return _excel(archivo, f'almacen_{cual}_{hoy}.xlsx')


# ---------------------------------------------------------------------------
# Ficha del lote, correcciones y etiquetas
# ---------------------------------------------------------------------------

def _lote_o_404(conn, lote_id):
    lote = inventario.lote(conn, lote_id)
    if not lote:
        conn.close()
        abort(404)
    return lote


@bp.route('/lote/<int:lote_id>')
@login_required
@requiere_modulo('almacen')
def ficha_lote(lote_id):
    conn = get_db_connection()
    lote = _lote_o_404(conn, lote_id)
    proveedor = conn.execute('SELECT * FROM proveedores WHERE id = ?', (lote['proveedor_id'],)).fetchone() \
        if lote['proveedor_id'] else None
    partida = conn.execute(
        'SELECT p.*, r.id AS recepcion_id, r.folio, r.fecha, r.documento, r.recibio, r.estatus AS rec_estatus '
        'FROM alm_recepcion_partidas p JOIN alm_recepciones r ON r.id = p.recepcion_id WHERE p.lote_id = ?',
        (lote_id,)).fetchone()
    padre = inventario.lote(conn, lote['lote_padre_id']) if lote['lote_padre_id'] else None
    devolucion = conn.execute('SELECT * FROM alm_devoluciones WHERE lote_id = ?', (lote_id,)).fetchone()
    traza = inventario.trazabilidad(conn, lote_id)
    secciones = []
    for s in registro.secciones_lote(conn, lote_id):
        try:
            secciones.append({'titulo': s['titulo'], 'modulo': s['modulo'],
                              'html': Markup(render_template(s['plantilla'], **(s.get('contexto') or {})))})
        except Exception:
            current_app.logger.exception('No se pudo pintar la sección %s en la ficha del lote', s.get('modulo'))
    datos = {
        'lote': lote, 'proveedor': proveedor, 'partida': partida, 'padre': padre, 'devolucion': devolucion,
        'kardex': servicios.kardex_lote(conn, lote_id), 'historial': servicios.historial_lote(conn, lote_id),
        'traza': traza, 'secciones': secciones, 'certificados': archivos.archivos_de(conn, 'lotes', lote_id),
        'etiquetas': conn.execute('SELECT * FROM alm_etiquetas WHERE lote_id = ? ORDER BY id DESC LIMIT 5',
                                  (lote_id,)).fetchall(),
        'ubicaciones': _ubicaciones(conn),
        'dias_aviso': parametro_int('almacen.dias_alerta_caducidad', conn),
    }
    conn.close()
    return render_page('almacen/ficha_lote.html', **datos)


@bp.route('/lote/<int:lote_id>/corregir', methods=['POST'])
@login_required
@requiere_modulo('almacen')
def corregir_lote(lote_id):
    campo = request.form.get('campo')
    destino = url_for('almacen.ficha_lote', lote_id=lote_id)
    if campo not in ('ubicacion', 'fecha_caducidad', 'fecha_reanalisis'):
        flash('Ese dato del lote no se corrige aquí.', 'error')
        return hx_redirect(destino)
    if campo == 'ubicacion' and not usuario_puede('almacen_operar'):
        flash('La ubicación la cambia el almacén.', 'error')
        return hx_redirect(destino)
    conn = get_db_connection()
    _lote_o_404(conn, lote_id)
    try:
        firma_id = None
        if campo != 'ubicacion':
            firmar = _firmante(conn, 'Aprobó', facultad='liberar_lotes')
            firma_id = firmar('lotes', lote_id, f'Corrigió {campo.replace("_", " ")} del lote')
        valor = request.form.get('valor')
        if campo == 'ubicacion':
            servicios._validar_ubicacion(conn, valor)
        inventario.corregir_lote(conn, lote_id=lote_id, campo=campo, valor=valor, usuario=_usuario(),
                                 motivo=request.form.get('motivo'), firma_id=firma_id)
        conn.commit()
        flash('Lote corregido.', 'success')
    except ERRORES as e:
        conn.rollback()
        flash(str(e), 'error')
    except Exception:
        _error_inesperado(conn, 'corregir el lote')
    finally:
        conn.close()
    return hx_redirect(destino)


@bp.route('/lote/<int:lote_id>/etiqueta')
@login_required
@requiere_modulo('almacen')
def etiqueta(lote_id):
    conn = get_db_connection()
    lote = _lote_o_404(conn, lote_id)
    proveedor = conn.execute('SELECT nombre FROM proveedores WHERE id = ?', (lote['proveedor_id'],)).fetchone() \
        if lote['proveedor_id'] else None
    partida = conn.execute('SELECT contenedores, cantidad FROM alm_recepcion_partidas WHERE lote_id = ?',
                           (lote_id,)).fetchone()
    padre = inventario.lote(conn, lote['lote_padre_id']) \
        if lote['origen'] == 'DEVOLUCION' and lote['lote_padre_id'] else None
    codigo = parametro('almacen.codigo_formato_etiqueta', conn)
    conn.close()
    contenedores = partida['contenedores'] if partida else 1
    formato = 'individual' if request.args.get('formato') == 'individual' else 'carta'
    return full_page('almacen/etiqueta.html', lote=lote, proveedor=proveedor, contenedores=contenedores,
                     formato=formato, codigo_formato=codigo, padre=padre, impreso=servicios.hoy())


@bp.route('/lote/<int:lote_id>/etiqueta', methods=['POST'])
@login_required
@requiere_modulo('almacen_operar')
def etiqueta_imprimir(lote_id):
    conn = get_db_connection()
    try:
        servicios.registrar_impresion(conn, lote_id, copias=request.form.get('copias'), usuario=_usuario())
        conn.commit()
    except ERRORES as e:
        conn.rollback()
        conn.close()
        flash(str(e), 'error')
        return hx_redirect(url_for('almacen.existencias'))
    conn.close()
    formato = request.form.get('formato') or 'carta'
    return hx_redirect(url_for('almacen.etiqueta', lote_id=lote_id, formato=formato, imprimir=1))


@bp.route('/reetiquetar')
@login_required
@requiere_modulo('almacen')
def reetiquetar():
    conn = get_db_connection()
    lotes = servicios.pendientes_reetiquetar(conn)
    conn.close()
    return render_page('almacen/reetiquetar.html', lotes=lotes)


@bp.route('/material/<int:material_id>')
@login_required
@requiere_modulo('almacen')
def kardex_material(material_id):
    conn = get_db_connection()
    material = conn.execute('SELECT * FROM materiales WHERE id = ?', (material_id,)).fetchone()
    if not material:
        conn.close()
        abort(404)
    if request.args.get('excel'):
        archivo = reportes.kardex(conn, material)
        conn.close()
        return _excel(archivo, f'kardex_{material["codigo"]}.xlsx')
    lotes, movimientos, totales = servicios.kardex_material(conn, material_id)
    conn.close()
    return render_page('almacen/kardex_material.html', material=material, lotes=lotes,
                       movimientos=movimientos[-300:], n_movimientos=len(movimientos), totales=totales)


# ---------------------------------------------------------------------------
# Recepciones
# ---------------------------------------------------------------------------

@bp.route('/recepciones')
@login_required
@requiere_modulo('almacen')
def recepciones():
    conn = get_db_connection()
    pagina = _pagina()
    total = conn.execute('SELECT COUNT(*) FROM alm_recepciones').fetchone()[0]
    filas = conn.execute(
        'SELECT r.*, p.nombre AS proveedor_nombre, '
        '(SELECT COUNT(*) FROM alm_recepcion_partidas x WHERE x.recepcion_id = r.id) AS partidas, '
        '(SELECT COUNT(*) FROM alm_recepcion_partidas x WHERE x.recepcion_id = r.id AND x.aceptada = 0) AS rechazadas '
        'FROM alm_recepciones r JOIN proveedores p ON p.id = r.proveedor_id ORDER BY r.id DESC LIMIT ? OFFSET ?',
        (POR_PAGINA, (pagina - 1) * POR_PAGINA)).fetchall()
    conn.close()
    return render_page('almacen/recepciones.html', recepciones=filas, pagina=pagina,
                       paginas=max(1, -(-total // POR_PAGINA)))


_CAMPOS_PARTIDA = ('material_id', 'lote_proveedor', 'cantidad', 'contenedores', 'fecha_caducidad',
                   'fecha_reanalisis', 'empaque_integro', 'identificacion_ok', 'trae_certificado', 'limpio',
                   'temperatura', 'no_se_recibe', 'observaciones', 'ubicacion')


def _partidas_del_form(prefijo, campos):
    indices = sorted({int(m.group(1)) for k in request.form
                      for m in [re.match(rf'{prefijo}-(\d+)-', k)] if m})
    filas = []
    for i in indices:
        fila = {c: request.form.get(f'{prefijo}-{i}-{c}') for c in campos}
        if not any(v for c, v in fila.items() if c not in ('empaque_integro', 'identificacion_ok', 'limpio',
                                                           'contenedores')):
            continue
        fila['_i'] = i
        filas.append(fila)
    return filas


def _form_recepcion(conn, previo=None, partidas=None):
    materiales = conn.execute('SELECT id, codigo, nombre, tipo, unidad, condiciones_almacen FROM materiales '
                              "WHERE activo = 1 AND tipo IN ('MP','ME','EM') ORDER BY codigo").fetchall()
    proveedores = conn.execute("SELECT id, nombre, estatus FROM proveedores WHERE activo = 1 "
                               "AND tipo IN ('Materiales','Ambos') ORDER BY nombre").fetchall()
    return render_page('almacen/recepcion_nueva.html', materiales=materiales, proveedores=proveedores,
                       ubicaciones=_ubicaciones(conn), previo=previo or {}, partidas=partidas or [{}],
                       hoy=servicios.hoy(), minimo=parametro_int('almacen.vida_util_minima_dias', conn))


@bp.route('/recepciones/nueva', methods=['GET', 'POST'])
@login_required
@requiere_modulo('almacen_operar')
def recepcion_nueva():
    conn = get_db_connection()
    if request.method == 'GET':
        resp = _form_recepcion(conn)
        conn.close()
        return resp
    partidas = _partidas_del_form('p', _CAMPOS_PARTIDA)
    for p in partidas:
        p['certificado'] = request.files.get(f'p-{p["_i"]}-certificado')
    previo = {k: request.form.get(k) for k in ('proveedor_id', 'fecha', 'documento', 'transporte', 'observaciones')}
    try:
        firmar = _firmante(conn, 'Realizó')
        r = servicios.recibir(conn, proveedor_id=request.form.get('proveedor_id'), partidas=partidas,
                              usuario=_usuario(), firmar=firmar, fecha=previo['fecha'], documento=previo['documento'],
                              transporte=previo['transporte'], observaciones=previo['observaciones'])
        conn.commit()
    except ERRORES as e:
        conn.rollback()
        flash(str(e), 'error')
        for p in partidas:
            p.pop('certificado', None)
        resp = _form_recepcion(conn, previo, partidas or [{}])
        conn.close()
        return resp
    except Exception:
        _error_inesperado(conn, 'registrar la recepción')
        conn.close()
        return hx_redirect(url_for('almacen.recepciones'))
    conn.close()
    for aviso in r['avisos']:
        flash(aviso, 'warning')
    flash(f'Recepción {r["folio"]} registrada: {len(r["lotes"])} lote{"" if len(r["lotes"]) == 1 else "s"} '
          'creados. Imprime y pega las etiquetas.', 'success')
    return hx_redirect(url_for('almacen.recepcion_detalle', recepcion_id=r['recepcion_id']))


def _recepcion(conn, recepcion_id):
    rec = conn.execute('SELECT r.*, p.nombre AS proveedor_nombre, p.estatus AS proveedor_estatus '
                       'FROM alm_recepciones r JOIN proveedores p ON p.id = r.proveedor_id WHERE r.id = ?',
                       (recepcion_id,)).fetchone()
    if not rec:
        conn.close()
        abort(404)
    partidas = [dict(p) for p in conn.execute(
        'SELECT p.*, m.codigo AS material_codigo, m.nombre AS material_nombre, m.unidad, l.numero_lote, '
        'l.estatus AS lote_estatus, l.existencia FROM alm_recepcion_partidas p '
        'JOIN materiales m ON m.id = p.material_id LEFT JOIN lotes l ON l.id = p.lote_id '
        'WHERE p.recepcion_id = ? ORDER BY p.id', (recepcion_id,)).fetchall()]
    for p in partidas:
        p['certificados'] = archivos.archivos_de(conn, 'lotes', p['lote_id']) if p['lote_id'] else []
    return rec, partidas


@bp.route('/recepciones/<int:recepcion_id>')
@login_required
@requiere_modulo('almacen')
def recepcion_detalle(recepcion_id):
    conn = get_db_connection()
    rec, partidas = _recepcion(conn, recepcion_id)
    firma = firmas.firma(conn, rec['firma_id']) if rec['firma_id'] else None
    firma_anulo = firmas.firma(conn, rec['anulo_firma_id']) if rec['anulo_firma_id'] else None
    conn.close()
    return render_page('almacen/recepcion_detalle.html', rec=rec, partidas=partidas, firma=firma,
                       firma_anulo=firma_anulo)


@bp.route('/recepciones/<int:recepcion_id>/imprimir')
@login_required
@requiere_modulo('almacen')
def recepcion_imprimir(recepcion_id):
    conn = get_db_connection()
    rec, partidas = _recepcion(conn, recepcion_id)
    firma = firmas.firma(conn, rec['firma_id']) if rec['firma_id'] else None
    codigo = parametro('almacen.codigo_formato_recepcion', conn)
    conn.close()
    return full_page('almacen/recepcion_imprimir.html', rec=rec, partidas=partidas, firma=firma,
                     codigo_formato=codigo)


@bp.route('/recepciones/<int:recepcion_id>/anular', methods=['POST'])
@login_required
@requiere_modulo('almacen_operar')
def recepcion_anular(recepcion_id):
    conn = get_db_connection()
    try:
        firmar = _firmante(conn, 'Anuló', facultad='autorizar_ajustes')
        servicios.anular_recepcion(conn, recepcion_id, motivo=request.form.get('motivo'), usuario=_usuario(),
                                   firmar=firmar)
        conn.commit()
        flash('Recepción anulada; sus lotes quedaron en cero.', 'success')
    except ERRORES as e:
        conn.rollback()
        flash(str(e), 'error')
    except Exception:
        _error_inesperado(conn, 'anular la recepción')
    finally:
        conn.close()
    return hx_redirect(url_for('almacen.recepcion_detalle', recepcion_id=recepcion_id))


# ---------------------------------------------------------------------------
# Salidas
# ---------------------------------------------------------------------------

@bp.route('/salidas')
@login_required
@requiere_modulo('almacen')
def salidas():
    conn = get_db_connection()
    pagina = _pagina()
    tipo = request.args.get('tipo') if request.args.get('tipo') in TIPOS_SALIDA else None
    where, params = ('WHERE s.tipo = ?', [tipo]) if tipo else ('', [])
    total = conn.execute(f'SELECT COUNT(*) FROM alm_salidas s {where}', params).fetchone()[0]
    filas = conn.execute(
        'SELECT s.*, (SELECT COUNT(*) FROM alm_salida_partidas x WHERE x.salida_id = s.id) AS partidas '
        f'FROM alm_salidas s {where} ORDER BY s.id DESC LIMIT ? OFFSET ?',
        params + [POR_PAGINA, (pagina - 1) * POR_PAGINA]).fetchall()
    conn.close()
    return render_page('almacen/salidas.html', salidas=filas, tipo=tipo, pagina=pagina,
                       paginas=max(1, -(-total // POR_PAGINA)))


def _form_salida(conn, tipo, previo=None, partidas=None):
    return render_page('almacen/salida_nueva.html', tipo=tipo, lotes=servicios.lotes_para_salida(conn, tipo),
                       previo=previo or {}, partidas=partidas or [{} for _ in range(5)], hoy=servicios.hoy())


@bp.route('/salidas/nueva', methods=['GET', 'POST'])
@login_required
@requiere_modulo('almacen_operar')
def salida_nueva():
    tipo = request.values.get('tipo') if request.values.get('tipo') in TIPOS_SALIDA else 'VENTA'
    conn = get_db_connection()
    if request.method == 'GET':
        resp = _form_salida(conn, tipo)
        conn.close()
        return resp
    partidas = _partidas_del_form('s', ('lote_id', 'cantidad', 'motivo_lote'))
    previo = {k: request.form.get(k) for k in ('destino', 'documento', 'observaciones', 'fecha')}
    try:
        firmar = _firmante(conn, 'Realizó' if tipo == 'VENTA' else 'Autorizó',
                           facultad=None if tipo == 'VENTA' else 'autorizar_ajustes')
        r = servicios.registrar_salida(conn, tipo=tipo, destino=previo['destino'], partidas=partidas,
                                       usuario=_usuario(), firmar=firmar, documento=previo['documento'],
                                       observaciones=previo['observaciones'], fecha=previo['fecha'])
        conn.commit()
    except ERRORES as e:
        conn.rollback()
        flash(str(e), 'error')
        resp = _form_salida(conn, tipo, previo, partidas + [{}] if partidas else None)
        conn.close()
        return resp
    except Exception:
        _error_inesperado(conn, 'registrar la salida')
        conn.close()
        return hx_redirect(url_for('almacen.salidas'))
    conn.close()
    flash(f'Salida {r["folio"]} registrada.', 'success')
    return hx_redirect(url_for('almacen.salida_detalle', salida_id=r['salida_id']))


@bp.route('/salidas/<int:salida_id>')
@login_required
@requiere_modulo('almacen')
def salida_detalle(salida_id):
    conn = get_db_connection()
    s = conn.execute('SELECT * FROM alm_salidas WHERE id = ?', (salida_id,)).fetchone()
    if not s:
        conn.close()
        abort(404)
    partidas = conn.execute(
        'SELECT p.*, l.numero_lote, l.lote_proveedor, l.fecha_caducidad, l.estatus, m.codigo AS material_codigo, '
        'm.nombre AS material_nombre, m.unidad FROM alm_salida_partidas p JOIN lotes l ON l.id = p.lote_id '
        'JOIN materiales m ON m.id = l.material_id WHERE p.salida_id = ? ORDER BY p.id', (salida_id,)).fetchall()
    firma = firmas.firma(conn, s['firma_id']) if s['firma_id'] else None
    codigo = parametro('almacen.codigo_formato_salida', conn)
    conn.close()
    return full_page('almacen/salida_imprimir.html', s=s, partidas=partidas, firma=firma, codigo_formato=codigo)


# ---------------------------------------------------------------------------
# Devoluciones de cliente
# ---------------------------------------------------------------------------

@bp.route('/devoluciones')
@login_required
@requiere_modulo('almacen')
def devoluciones():
    conn = get_db_connection()
    filas = conn.execute(
        'SELECT d.*, lv.numero_lote AS lote_vendido, l.numero_lote, l.estatus, m.codigo AS material_codigo, '
        'm.nombre AS material_nombre, m.unidad FROM alm_devoluciones d JOIN lotes lv ON lv.id = d.lote_vendido_id '
        'LEFT JOIN lotes l ON l.id = d.lote_id JOIN materiales m ON m.id = lv.material_id '
        'ORDER BY d.id DESC LIMIT 200').fetchall()
    vendidos = servicios.lotes_vendidos(conn) if usuario_puede('almacen_operar') else []
    conn.close()
    return render_page('almacen/devoluciones.html', devoluciones=filas, vendidos=vendidos,
                       motivos=servicios.MOTIVOS_DEVOLUCION, hoy=servicios.hoy())


@bp.route('/devoluciones/nueva', methods=['POST'])
@login_required
@requiere_modulo('almacen_operar')
def devolucion_nueva():
    conn = get_db_connection()
    try:
        r = servicios.registrar_devolucion(conn, cliente=request.form.get('cliente'),
                                           lote_vendido_id=request.form.get('lote_vendido_id'),
                                           cantidad=request.form.get('cantidad'), motivo=request.form.get('motivo'),
                                           usuario=_usuario(), fecha=request.form.get('fecha'))
        conn.commit()
        flash(f'Devolución {r["folio"]} registrada; el lote quedó en cuarentena. Imprime su etiqueta.', 'success')
        destino = url_for('almacen.ficha_lote', lote_id=r['lote_id'])
    except ERRORES as e:
        conn.rollback()
        flash(str(e), 'error')
        destino = url_for('almacen.devoluciones')
    except Exception:
        _error_inesperado(conn, 'registrar la devolución')
        destino = url_for('almacen.devoluciones')
    finally:
        conn.close()
    return hx_redirect(destino)


# ---------------------------------------------------------------------------
# Conteos
# ---------------------------------------------------------------------------

@bp.route('/conteos')
@login_required
@requiere_modulo('almacen')
def conteos():
    conn = get_db_connection()
    pagina = _pagina()
    total = conn.execute('SELECT COUNT(*) FROM alm_conteos').fetchone()[0]
    filas = conn.execute(
        'SELECT c.*, (SELECT COUNT(*) FROM alm_conteo_partidas p WHERE p.conteo_id = c.id) AS lotes '
        'FROM alm_conteos c ORDER BY c.id DESC LIMIT ? OFFSET ?', (POR_PAGINA, (pagina - 1) * POR_PAGINA)).fetchall()
    materiales = conn.execute('SELECT id, codigo, nombre FROM materiales WHERE activo = 1 ORDER BY codigo').fetchall()
    ubicaciones = _ubicaciones(conn)
    conn.close()
    return render_page('almacen/conteos.html', conteos=filas, materiales=materiales, ubicaciones=ubicaciones,
                       pagina=pagina, paginas=max(1, -(-total // POR_PAGINA)))


@bp.route('/conteos/nuevo', methods=['POST'])
@login_required
@requiere_modulo('almacen_operar')
def conteo_nuevo():
    conn = get_db_connection()
    alcance = request.form.get('alcance')
    try:
        r = servicios.abrir_conteo(
            conn, usuario=_usuario(),
            tipo_material=request.form.get('tipo') if alcance == 'tipo' else None,
            ubicacion=request.form.get('ubicacion') if alcance == 'ubicacion' else None,
            material_ids=request.form.getlist('material_ids') if alcance == 'materiales' else None)
        if alcance == 'materiales' and not request.form.getlist('material_ids'):
            raise ErrorAlmacen('Elige al menos un material.')
        conn.commit()
        flash(f'Conteo {r["folio"]} abierto con {r["lotes"]} lotes. Imprime la hoja o captura en pantalla.',
              'success')
        destino = url_for('almacen.conteo_detalle', conteo_id=r['conteo_id'])
    except ERRORES as e:
        conn.rollback()
        flash(str(e), 'error')
        destino = url_for('almacen.conteos')
    finally:
        conn.close()
    return hx_redirect(destino)


def _conteo(conn, conteo_id):
    c = conn.execute('SELECT * FROM alm_conteos WHERE id = ?', (conteo_id,)).fetchone()
    if not c:
        conn.close()
        abort(404)
    return c


@bp.route('/conteos/<int:conteo_id>')
@login_required
@requiere_modulo('almacen')
def conteo_detalle(conteo_id):
    conn = get_db_connection()
    c = _conteo(conn, conteo_id)
    partidas = servicios.partidas_conteo(conn, conteo_id)
    firma = firmas.firma(conn, c['firma_id']) if c['firma_id'] else None
    conn.close()
    # Conteo ciego: mientras se captura, la existencia del sistema no viaja a la pantalla.
    if c['estatus'] == 'Abierto':
        for p in partidas:
            p['existencia'] = p['existencia_al_capturar'] = p['diferencia'] = None
    return render_page('almacen/conteo_captura.html', c=c, partidas=partidas, firma=firma,
                       recontar=sum(1 for p in partidas if p['recontar']),
                       diferencias=sum(1 for p in partidas if p['diferencia']))


@bp.route('/conteos/<int:conteo_id>/hoja')
@login_required
@requiere_modulo('almacen')
def conteo_hoja(conteo_id):
    conn = get_db_connection()
    c = _conteo(conn, conteo_id)
    partidas = servicios.partidas_conteo(conn, conteo_id)
    conn.close()
    return full_page('almacen/conteo_hoja.html', c=c, partidas=partidas)


@bp.route('/conteos/<int:conteo_id>/capturar', methods=['POST'])
@login_required
@requiere_modulo('almacen_operar')
def conteo_capturar(conteo_id):
    conn = get_db_connection()
    cantidades = {k[len('contado-'):]: v for k, v in request.form.items() if k.startswith('contado-')}
    try:
        faltan = servicios.capturar_conteo(conn, conteo_id, cantidades=cantidades, usuario=_usuario(),
                                           observaciones=request.form.get('observaciones'))
        conn.commit()
        flash(f'Captura guardada; faltan {faltan} lotes.' if faltan else
              'Conteo capturado. Revisa las diferencias y pide la autorización de los ajustes.',
              'warning' if faltan else 'success')
    except ERRORES as e:
        conn.rollback()
        flash(str(e), 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('almacen.conteo_detalle', conteo_id=conteo_id))


@bp.route('/conteos/<int:conteo_id>/ajustar', methods=['POST'])
@login_required
@requiere_modulo('almacen')
def conteo_ajustar(conteo_id):
    conn = get_db_connection()
    motivos = {k[len('motivo-'):]: v for k, v in request.form.items() if k.startswith('motivo-')}
    try:
        firmar = _firmante(conn, 'Autorizó', facultad='autorizar_ajustes')
        n = servicios.ajustar_conteo(conn, conteo_id, motivos=motivos, usuario=_usuario(), firmar=firmar)
        conn.commit()
        flash(f'Conteo cerrado con {n} ajuste{"" if n == 1 else "s"}.', 'success')
    except ERRORES as e:
        conn.rollback()
        flash(str(e), 'error')
    except Exception:
        _error_inesperado(conn, 'autorizar los ajustes')
    finally:
        conn.close()
    return hx_redirect(url_for('almacen.conteo_detalle', conteo_id=conteo_id))


@bp.route('/conteos/<int:conteo_id>/cancelar', methods=['POST'])
@login_required
@requiere_modulo('almacen_operar')
def conteo_cancelar(conteo_id):
    conn = get_db_connection()
    try:
        servicios.cancelar_conteo(conn, conteo_id, usuario=_usuario())
        conn.commit()
        flash('Conteo cancelado.', 'success')
    except ERRORES as e:
        conn.rollback()
        flash(str(e), 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('almacen.conteos'))


# ---------------------------------------------------------------------------
# Catálogos
# ---------------------------------------------------------------------------

@bp.route('/catalogos')
@login_required
@requiere_modulo('almacen')
def catalogos():
    pestana = request.args.get('pestana') if request.args.get('pestana') in ('proveedores', 'ubicaciones') \
        else 'materiales'
    conn = get_db_connection()
    q = (request.args.get('q') or '').strip()
    materiales = conn.execute(
        'SELECT m.*, p.codigo AS padre_codigo, (SELECT COUNT(*) FROM lotes l WHERE l.material_id = m.id) AS n_lotes '
        'FROM materiales m LEFT JOIN materiales p ON p.id = m.material_padre_id '
        'WHERE (? = \'\' OR m.codigo LIKE ? OR m.nombre LIKE ?) ORDER BY m.activo DESC, m.codigo',
        (q, f'%{q}%', f'%{q}%')).fetchall()
    proveedores = conn.execute('SELECT * FROM proveedores ORDER BY activo DESC, nombre').fetchall()
    ubicaciones = _ubicaciones(conn, solo_activas=False)
    graneles = conn.execute("SELECT id, codigo, nombre FROM materiales WHERE tipo = 'GR' AND activo = 1 "
                            'ORDER BY codigo').fetchall()
    editar = None
    if request.args.get('editar'):
        tabla = {'materiales': 'materiales', 'proveedores': 'proveedores', 'ubicaciones': 'alm_ubicaciones'}[pestana]
        editar = conn.execute(f'SELECT * FROM {tabla} WHERE id = ?', (request.args.get('editar'),)).fetchone()
    conn.close()
    return render_page('almacen/catalogos.html', pestana=pestana, materiales=materiales, proveedores=proveedores,
                       ubicaciones=ubicaciones, graneles=graneles, editar=editar, q=q,
                       tipos_proveedor=inventario.TIPOS_PROVEEDOR, estatus_proveedor=inventario.ESTATUS_PROVEEDOR,
                       tipos_ubicacion=servicios.TIPOS_UBICACION,
                       puede_proveedores=usuario_puede('almacen_proveedores'))


@bp.route('/catalogos/material', methods=['POST'])
@login_required
@requiere_modulo('almacen_operar')
def catalogo_material():
    f = request.form
    material_id = f.get('id')
    campos = {k: f.get(k) for k in ('nombre', 'stock_minimo', 'condiciones_almacen', 'vida_util_meses',
                                    'material_padre_id', 'contenido_por_pieza', 'registro_sanitario',
                                    'forma_farmaceutica')}
    campos['requiere_analisis'] = f.get('requiere_analisis')
    campos['es_impreso'] = f.get('es_impreso')
    campos['activo'] = f.get('activo') if material_id else '1'
    conn = get_db_connection()
    try:
        if material_id:
            actual = conn.execute('SELECT * FROM materiales WHERE id = ?', (material_id,)).fetchone()
            if not actual:
                raise ErrorAlmacen('El material no existe.')
            firma_id = None
            if actual['requiere_analisis'] and not f.get('requiere_analisis') and f.get('firma_numero'):
                firmar = _firmante(conn, 'Aprobó', facultad='aprobar_maestros')
                firma_id = firmar('materiales', int(material_id), f'Quitó el análisis obligatorio a {actual["codigo"]}')
            inventario.editar_material(conn, int(material_id), usuario=_usuario(), firma_id=firma_id,
                                       codigo=f.get('codigo'), tipo=f.get('tipo'), unidad=f.get('unidad'), **campos)
            flash(f'Material {f.get("codigo")} actualizado.', 'success')
        else:
            nombre = campos.pop('nombre')
            extra = {k: v for k, v in campos.items() if v not in (None, '')}
            extra['requiere_analisis'] = f.get('requiere_analisis') or 0
            inventario.crear_material(conn, codigo=f.get('codigo'), nombre=nombre, tipo=f.get('tipo'),
                                      unidad=f.get('unidad'), usuario=_usuario(), **extra)
            flash(f'Material {f.get("codigo")} dado de alta.', 'success')
        conn.commit()
    except ERRORES as e:
        conn.rollback()
        flash(str(e), 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('almacen.catalogos', pestana='materiales'))


@bp.route('/catalogos/proveedor', methods=['POST'])
@login_required
@requiere_modulo('almacen_operar')
def catalogo_proveedor():
    f = request.form
    campos = {k: f.get(k) or None for k in ('rfc', 'contacto', 'telefono', 'correo')}
    conn = get_db_connection()
    try:
        if f.get('id'):
            inventario.editar_proveedor(conn, int(f['id']), usuario=_usuario(), nombre=f.get('nombre'),
                                        tipo=f.get('tipo'), activo=f.get('activo'), **campos)
            flash('Proveedor actualizado.', 'success')
        else:
            inventario.crear_proveedor(conn, nombre=f.get('nombre'), tipo=f.get('tipo') or 'Materiales',
                                       usuario=_usuario(), **campos)
            flash('Proveedor dado de alta (queda «En evaluación» hasta que Calidad lo apruebe).', 'success')
        conn.commit()
    except ERRORES as e:
        conn.rollback()
        flash(str(e), 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('almacen.catalogos', pestana='proveedores'))


@bp.route('/catalogos/proveedor/<int:proveedor_id>/estatus', methods=['POST'])
@login_required
@requiere_modulo('almacen_proveedores')
def proveedor_estatus(proveedor_id):
    conn = get_db_connection()
    try:
        inventario.editar_proveedor(conn, proveedor_id, usuario=_usuario(), estatus=request.form.get('estatus'),
                                    motivo=request.form.get('motivo'))
        conn.commit()
        flash('Estatus del proveedor actualizado.', 'success')
    except ERRORES as e:
        conn.rollback()
        flash(str(e), 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('almacen.catalogos', pestana='proveedores'))


@bp.route('/catalogos/ubicacion', methods=['POST'])
@login_required
@requiere_modulo('almacen_operar')
def catalogo_ubicacion():
    f = request.form
    conn = get_db_connection()
    try:
        servicios.guardar_ubicacion(conn, codigo=f.get('codigo'), nombre=f.get('nombre'), tipo=f.get('tipo'),
                                    ubicacion_id=f.get('id') or None, activo=f.get('activo', '0'))
        conn.commit()
        flash('Ubicación guardada.', 'success')
    except ERRORES as e:
        conn.rollback()
        flash(str(e), 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('almacen.catalogos', pestana='ubicaciones'))


# ---------------------------------------------------------------------------
# Carga inicial
# ---------------------------------------------------------------------------

@bp.route('/carga-inicial', methods=['GET', 'POST'])
@login_required
@requiere_modulo('almacen_operar')
def carga_inicial():
    if request.method == 'GET':
        return render_page('almacen/carga_inicial.html', vista_previa=None)
    conn = get_db_connection()
    try:
        materiales = reportes.leer_hoja(request.files.get('materiales'), servicios.COLUMNAS_MATERIALES)
        existencias = reportes.leer_hoja(request.files.get('existencias'), servicios.COLUMNAS_EXISTENCIAS)
        if not materiales and not existencias:
            raise ErrorAlmacen('Sube al menos una de las dos plantillas.')
        errores = servicios.validar_carga(conn, materiales, existencias)
    except ErrorAlmacen as e:
        conn.close()
        flash(str(e), 'error')
        return render_page('almacen/carga_inicial.html', vista_previa=None)
    conn.close()
    return render_page('almacen/carga_inicial.html', vista_previa={
        'materiales': materiales, 'existencias': existencias, 'errores': errores,
        'requiere_firma': servicios.carga_requiere_firma(existencias),
        'json': json.dumps({'materiales': materiales, 'existencias': existencias})})


@bp.route('/carga-inicial/confirmar', methods=['POST'])
@login_required
@requiere_modulo('almacen_operar')
def carga_inicial_confirmar():
    try:
        datos = json.loads(request.form.get('datos') or '{}')
        materiales = servicios.normalizar_renglones(datos.get('materiales') or [], servicios.COLUMNAS_MATERIALES)
        existencias = servicios.normalizar_renglones(datos.get('existencias') or [], servicios.COLUMNAS_EXISTENCIAS)
    except (ValueError, AttributeError, TypeError):
        flash('No se pudo leer la vista previa; vuelve a subir las plantillas.', 'error')
        return hx_redirect(url_for('almacen.carga_inicial'))
    conn = get_db_connection()
    try:
        firmar = None
        if servicios.carga_requiere_firma(existencias):
            firmar = _firmante(conn, 'Liberó', facultad='liberar_lotes')
        r = servicios.importar_carga_inicial(conn, materiales=materiales, existencias=existencias,
                                             usuario=_usuario(), firmar=firmar)
        conn.commit()
        flash(f'Carga {r["folio"]} lista: {r["materiales"]} materiales y {r["lotes"]} lotes.', 'success')
        destino = url_for('almacen.existencias')
    except ERRORES as e:
        conn.rollback()
        flash(str(e), 'error')
        destino = url_for('almacen.carga_inicial')
    except Exception:
        _error_inesperado(conn, 'hacer la carga inicial')
        destino = url_for('almacen.carga_inicial')
    finally:
        conn.close()
    return hx_redirect(destino)


@bp.route('/carga-inicial/plantilla-materiales')
@login_required
@requiere_modulo('almacen_operar')
def plantilla_materiales():
    return _excel(reportes.plantilla_materiales(), 'plantilla_materiales.xlsx')


@bp.route('/carga-inicial/plantilla-existencias')
@login_required
@requiere_modulo('almacen_operar')
def plantilla_existencias():
    return _excel(reportes.plantilla_existencias(), 'plantilla_existencias.xlsx')
