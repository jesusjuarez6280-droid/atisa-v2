"""Pantallas de Calidad y liberación (blueprint 'calidad', /calidad). Diseño 03, sección 4."""
import io
from datetime import date

from flask import Blueprint, abort, current_app, flash, render_template, request, send_file, session, url_for
from markupsafe import Markup

from nucleo import archivos, firmas, inventario, registro
from nucleo.auth import login_required, requiere_modulo, requiere_facultad, usuario_puede
from nucleo.db import get_db_connection
from nucleo.equipos import ErrorEquipo
from nucleo.parametros import parametro
from nucleo.vistas import render_page, full_page, hx_redirect

from .servicios import (analisis as an, especificaciones as esp, investigaciones as inv, dictamen as dic,
                        retencion as ret, certificado as cert)
from .servicios._comun import ErrorCalidad, lote_fila, sigue_al_granel
from . import reportes

bp = Blueprint('calidad', __name__, url_prefix='/calidad', template_folder='templates')

ERRORES = (ErrorCalidad, firmas.FirmaInvalida, inventario.ErrorInventario, ErrorEquipo, archivos.ErrorArchivo)
TABS = ('muestrear', 'analisis', 'revisar', 'dictaminar', 'retenidos', 'reanalisis', 'terminados')


def _firma():
    return {'numero_empleado': (request.form.get('firma_numero') or '').strip(),
            'pin': (request.form.get('firma_pin') or '').strip()}


def _usuario():
    return session.get('nombre') or session.get('username') or 'sistema'


def _ejecutar(accion, exito, destino):
    """Corre `accion(conn)` en una transacción: commit y aviso de éxito, o rollback y el error tal cual."""
    conn = get_db_connection()
    try:
        resultado = accion(conn)
        conn.commit()
        if exito:
            flash(exito(resultado) if callable(exito) else exito, 'success')
    except ERRORES as e:
        conn.rollback()
        flash(str(e), 'error')
        resultado = None
    finally:
        conn.close()
    return hx_redirect(destino(resultado) if callable(destino) else destino)


def _num(texto_, decimales):
    """Número con los decimales de la especificación (para pantallas)."""
    if texto_ is None:
        return ''
    try:
        return f'{float(texto_):.{int(decimales or 0)}f}'
    except (TypeError, ValueError):
        return str(texto_)


@bp.context_processor
def _ayudas():
    return {'cc_num': _num, 'puede_operar': usuario_puede('calidad_operar')}


# ---------------------------------------------------------------------------
# Bandeja
# ---------------------------------------------------------------------------

@bp.route('')
@login_required
@requiere_modulo('calidad')
def bandeja():
    tab = request.args.get('tab') if request.args.get('tab') in TABS else 'muestrear'
    q = (request.args.get('q') or '').strip()
    conn = get_db_connection()
    datos = an.bandeja(conn, q=q)
    conn.close()
    return render_page('calidad/bandeja.html', tab=tab, q=q, b=datos, k=datos['kpis'])


# ---------------------------------------------------------------------------
# Muestreo
# ---------------------------------------------------------------------------

@bp.route('/lote/<int:lote_id>/muestrear', methods=['GET', 'POST'])
@login_required
@requiere_modulo('calidad_operar')
def muestrear(lote_id):
    if request.method == 'POST':
        f = request.form
        retencion = None
        if f.get('con_retencion'):
            retencion = {'envases': f.get('ret_envases'), 'cantidad': f.get('ret_cantidad'),
                         'ubicacion': f.get('ret_ubicacion')}
        return _ejecutar(
            lambda conn: an.muestrear(conn, lote_id=lote_id, usuario=_usuario(), firma=_firma(),
                                      tipo=f.get('tipo') or 'LIBERACION', muestra_lote_id=f.get('muestra_lote_id'),
                                      cantidad=f.get('cantidad'), en_gramos=f.get('unidad_captura') == 'g',
                                      contenedores=f.get('contenedores'), observaciones=f.get('observaciones'),
                                      retencion=retencion),
            lambda aid: 'Muestreo guardado. Imprime la etiqueta de la muestra y la hoja de trabajo.',
            lambda aid: url_for('calidad.analisis_ver', analisis_id=aid) if aid
            else url_for('calidad.muestrear', lote_id=lote_id, tipo=f.get('tipo') or ''))
    conn = get_db_connection()
    try:
        lote = lote_fila(conn, lote_id)
    except ErrorCalidad:
        conn.close()
        abort(404)
    especificacion = esp.vigente(conn, lote['material_id'])
    presentaciones = conn.execute(
        'SELECT l.id, l.numero_lote, l.existencia, m.codigo, m.unidad FROM lotes l JOIN materiales m '
        "ON m.id = l.material_id WHERE l.lote_padre_id = ? AND l.origen <> 'DEVOLUCION' ORDER BY l.id",
        (lote_id,)).fetchall()
    abierto = an._abierto_de(conn, lote_id)
    granel = lote_fila(conn, lote['lote_padre_id']) if sigue_al_granel(lote) else None
    tipo = request.args.get('tipo') if request.args.get('tipo') in an.TIPOS else (
        'REANALISIS' if lote['estatus'] == 'APROBADO' else 'LIBERACION')
    conn.close()
    return render_page('calidad/muestreo.html', lote=lote, esp=especificacion, presentaciones=presentaciones,
                       abierto=abierto, granel=granel, tipo=tipo)


# ---------------------------------------------------------------------------
# Análisis
# ---------------------------------------------------------------------------

@bp.route('/analisis/<int:analisis_id>')
@login_required
@requiere_modulo('calidad')
def analisis_ver(analisis_id):
    conn = get_db_connection()
    try:
        d = an.detalle(conn, analisis_id)
    except ErrorCalidad:
        conn.close()
        abort(404)
    equipos = {}
    for fila in d['filas']:
        t = fila['prueba']['equipo_tipo']
        if t not in equipos:
            equipos[t] = an.equipos_aptos(conn, t) if t else an.equipos_aptos(conn)
    laboratorios = an.laboratorios(conn)
    conn.close()
    return render_page('calidad/analisis.html', d=d, a=d['a'], lote=d['lote'], equipos=equipos,
                       laboratorios=laboratorios, fuentes=an.FUENTES, hoy=date.today().isoformat(),
                       ficha=registro.enlace('ficha_lote', lote_id=d['lote']['id']))


def _volver_analisis(analisis_id):
    return url_for('calidad.analisis_ver', analisis_id=analisis_id)


@bp.route('/analisis/<int:analisis_id>/capturar/<int:prueba_id>', methods=['POST'])
@login_required
@requiere_modulo('calidad_operar')
def capturar(analisis_id, prueba_id):
    f = request.form

    def exito(r):
        if r['cumple']:
            return 'Resultado guardado: cumple.'
        return 'El resultado NO cumple: se abrió una investigación de resultado fuera de especificación.'
    return _ejecutar(
        lambda conn: an.capturar(conn, analisis_id=analisis_id, prueba_id=prueba_id, usuario=_usuario(),
                                 firma=_firma(), valor=f.get('valor'), texto_obs=f.get('texto'),
                                 cumple=f.get('cumple'), fuente=f.get('fuente'), equipo_id=f.get('equipo_id'),
                                 laboratorio_id=f.get('laboratorio_id'), informe=f.get('informe'),
                                 archivo=request.files.get('archivo'), fecha_analisis=f.get('fecha_analisis')),
        exito,
        lambda r: url_for('calidad.oos', investigacion_id=r['investigacion_id'])
        if r and r['investigacion_id'] else _volver_analisis(analisis_id))


@bp.route('/resultado/<int:resultado_id>/corregir', methods=['POST'])
@login_required
@requiere_modulo('calidad_operar')
def corregir(resultado_id):
    f = request.form
    conn = get_db_connection()
    fila = conn.execute('SELECT analisis_id FROM cc_resultados WHERE id = ?', (resultado_id,)).fetchone()
    conn.close()
    if not fila:
        abort(404)
    return _ejecutar(
        lambda conn: an.corregir(conn, resultado_id=resultado_id, motivo=f.get('motivo'), usuario=_usuario(),
                                 firma=_firma(), valor=f.get('valor'), texto_obs=f.get('texto'),
                                 cumple=f.get('cumple'), fecha_analisis=f.get('fecha_analisis')),
        'Captura corregida: el valor anterior quedó tachado.', _volver_analisis(fila['analisis_id']))


@bp.route('/analisis/<int:analisis_id>/externo', methods=['POST'])
@login_required
@requiere_modulo('calidad_operar')
def enviar_externo(analisis_id):
    f = request.form
    return _ejecutar(
        lambda conn: an.enviar_externo(conn, analisis_id=analisis_id, usuario=_usuario(),
                                       laboratorio_id=f.get('laboratorio_id') or None,
                                       laboratorio_nuevo=f.get('laboratorio_nuevo'), fecha_envio=f.get('fecha')),
        'Envío al laboratorio externo anotado.', _volver_analisis(analisis_id))


@bp.route('/analisis/<int:analisis_id>/revisar', methods=['POST'])
@login_required
@requiere_modulo('calidad_operar')
def revisar(analisis_id):
    return _ejecutar(
        lambda conn: an.revisar(conn, analisis_id=analisis_id, firma=_firma(),
                                observaciones=request.form.get('observaciones')),
        lambda estatus: f'Análisis revisado: {estatus.replace("_", " ").lower()}.', _volver_analisis(analisis_id))


@bp.route('/analisis/<int:analisis_id>/cancelar', methods=['POST'])
@login_required
@requiere_modulo('calidad_operar')
def cancelar(analisis_id):
    return _ejecutar(
        lambda conn: an.cancelar(conn, analisis_id=analisis_id, motivo=request.form.get('motivo'), firma=_firma()),
        'Análisis cancelado.', _volver_analisis(analisis_id))


@bp.route('/analisis/<int:analisis_id>/archivo', methods=['POST'])
@login_required
@requiere_modulo('calidad_operar')
def analisis_archivo(analisis_id):
    return _ejecutar(
        lambda conn: an.guardar_archivo(conn, analisis_id=analisis_id, archivo=request.files.get('archivo'),
                                        descripcion=request.form.get('descripcion'), usuario=_usuario()),
        'Archivo adjuntado.', _volver_analisis(analisis_id))


def _impresion_analisis(analisis_id, plantilla):
    conn = get_db_connection()
    try:
        d = an.detalle(conn, analisis_id)
    except ErrorCalidad:
        conn.close()
        abort(404)
    codigo = parametro('calidad.codigo_formato_analisis', conn)
    conn.close()
    return full_page(plantilla, d=d, a=d['a'], lote=d['lote'], codigo_formato=codigo)


@bp.route('/analisis/<int:analisis_id>/hoja')
@login_required
@requiere_modulo('calidad')
def hoja_trabajo(analisis_id):
    return _impresion_analisis(analisis_id, 'calidad/hoja_trabajo.html')


@bp.route('/analisis/<int:analisis_id>/etiqueta')
@login_required
@requiere_modulo('calidad')
def etiqueta_muestra(analisis_id):
    return _impresion_analisis(analisis_id, 'calidad/etiqueta_muestra.html')


# ---------------------------------------------------------------------------
# Investigación OOS
# ---------------------------------------------------------------------------

@bp.route('/oos/<int:investigacion_id>')
@login_required
@requiere_modulo('calidad')
def oos(investigacion_id):
    conn = get_db_connection()
    try:
        d = inv.detalle(conn, investigacion_id)
    except ErrorCalidad:
        conn.close()
        abort(404)
    desviaciones = []
    try:
        desviaciones = registro.consulta('desviaciones_de', conn, 'oos', investigacion_id, default=[]) or []
    except Exception:
        current_app.logger.exception('Falló la consulta desviaciones_de')
    conn.close()
    reportar = registro.enlace('reportar_desviacion', origen='oos', origen_id=investigacion_id,
                               texto=f'{d["i"]["folio"]}: {d["r"]["prueba"]} fuera de especificación en el lote '
                                     f'{d["lote"]["numero_lote"]}',
                               folio=d['i']['folio'], lotes=[d['lote']['id']], material_id=d['lote']['material_id'],
                               volver=url_for('calidad.oos', investigacion_id=investigacion_id))
    return render_page('calidad/investigacion.html', d=d, i=d['i'], desviaciones=desviaciones, reportar=reportar)


def _volver_oos(investigacion_id):
    return url_for('calidad.oos', investigacion_id=investigacion_id)


def _revision_form():
    return {campo: request.form.get(campo) for campo, _ in inv.REVISION}


@bp.route('/oos/<int:investigacion_id>/guardar', methods=['POST'])
@login_required
@requiere_modulo('calidad_operar')
def oos_guardar(investigacion_id):
    return _ejecutar(lambda conn: inv.guardar(conn, investigacion_id, revision=_revision_form(),
                                              hallazgos=request.form.get('hallazgos')),
                     'Avance de la investigación guardado.', _volver_oos(investigacion_id))


@bp.route('/oos/<int:investigacion_id>/concluir', methods=['POST'])
@login_required
@requiere_modulo('calidad_operar')
def oos_concluir(investigacion_id):
    return _ejecutar(lambda conn: inv.concluir(conn, investigacion_id, conclusion=request.form.get('conclusion'),
                                               firma=_firma(), revision=_revision_form(),
                                               hallazgos=request.form.get('hallazgos')),
                     'Investigación concluida; falta la aprobación.', _volver_oos(investigacion_id))


@bp.route('/oos/<int:investigacion_id>/regresar', methods=['POST'])
@login_required
@requiere_modulo('calidad_operar')
def oos_regresar(investigacion_id):
    return _ejecutar(lambda conn: inv.regresar(conn, investigacion_id),
                     'La investigación regresó a quien la hace.', _volver_oos(investigacion_id))


@bp.route('/oos/<int:investigacion_id>/aprobar', methods=['POST'])
@login_required
@requiere_modulo('calidad_operar')
@requiere_facultad('gestionar_desviaciones')
def oos_aprobar(investigacion_id):
    return _ejecutar(lambda conn: inv.aprobar(conn, investigacion_id, firma=_firma()),
                     'Investigación cerrada.', _volver_oos(investigacion_id))


# ---------------------------------------------------------------------------
# Dictamen
# ---------------------------------------------------------------------------

@bp.route('/lote/<int:lote_id>/dictamen')
@login_required
@requiere_modulo('calidad')
def dictamen(lote_id):
    conn = get_db_connection()
    try:
        lote = lote_fila(conn, lote_id)
    except ErrorCalidad:
        conn.close()
        abort(404)
    v = dic.verificaciones(conn, lote_id, numero_empleado=session.get('numero_empleado'))
    detalle = an.detalle(conn, v['analisis']['id']) if v['analisis'] else None
    secciones = []
    for s in registro.secciones_lote(conn, lote_id):
        if s.get('modulo') == 'calidad':
            continue
        try:
            s['html'] = Markup(render_template(s['plantilla'], **(s.get('contexto') or {})))
        except Exception:
            current_app.logger.exception('No se pudo pintar la sección %s del lote', s.get('modulo'))
            continue
        secciones.append(s)
    contexto = {
        'lote': lote, 'v': v, 'detalle': detalle, 'secciones': secciones,
        'presentaciones': dic.presentaciones_que_cambian(conn, lote_id),
        'propuesta_reanalisis': dic.fecha_reanalisis_propuesta(conn, lote_id),
        'historial': dic.historial(conn, lote_id),
        'reanalisis': conn.execute("SELECT * FROM cc_analisis WHERE lote_id = ? AND tipo = 'REANALISIS' "
                                   "AND estatus <> 'CANCELADO' ORDER BY id DESC LIMIT 1", (lote_id,)).fetchone(),
        'ficha': registro.enlace('ficha_lote', lote_id=lote_id),
    }
    conn.close()
    return render_page('calidad/dictamen.html', **contexto)


def _volver_dictamen(lote_id):
    return url_for('calidad.dictamen', lote_id=lote_id)


@bp.route('/lote/<int:lote_id>/aprobar', methods=['POST'])
@login_required
@requiere_modulo('calidad')
@requiere_facultad('liberar_lotes')
def aprobar(lote_id):
    return _ejecutar(lambda conn: dic.aprobar(conn, lote_id=lote_id, firma=_firma(), usuario=_usuario(),
                                              motivo=request.form.get('motivo'),
                                              fecha_reanalisis=request.form.get('fecha_reanalisis')),
                     lambda ids: 'Lote aprobado' + (f' con {len(ids) - 1} presentación(es).' if len(ids) > 1 else '.'),
                     _volver_dictamen(lote_id))


@bp.route('/lote/<int:lote_id>/rechazar', methods=['POST'])
@login_required
@requiere_modulo('calidad')
@requiere_facultad('liberar_lotes')
def rechazar(lote_id):
    return _ejecutar(lambda conn: dic.rechazar(conn, lote_id=lote_id, motivo=request.form.get('motivo'),
                                               firma=_firma(), usuario=_usuario(), folio=request.form.get('folio')),
                     'Lote rechazado. A Almacén le toca destruirlo o devolverlo al proveedor.',
                     _volver_dictamen(lote_id))


@bp.route('/lote/<int:lote_id>/retener', methods=['POST'])
@login_required
@requiere_modulo('calidad')
@requiere_facultad('liberar_lotes')
def retener(lote_id):
    volver = request.form.get('volver') or ''
    if not volver.startswith('/calidad/'):
        volver = _volver_dictamen(lote_id)
    return _ejecutar(lambda conn: dic.retener(conn, lote_id=lote_id, motivo=request.form.get('motivo'),
                                              firma=_firma(), usuario=_usuario(), folio=request.form.get('folio')),
                     'Lote retenido.', volver)


@bp.route('/lote/<int:lote_id>/extender', methods=['POST'])
@login_required
@requiere_modulo('calidad')
@requiere_facultad('liberar_lotes')
def extender(lote_id):
    return _ejecutar(lambda conn: dic.extender_reanalisis(conn, lote_id=lote_id,
                                                          nueva_fecha=request.form.get('nueva_fecha'),
                                                          firma=_firma(), usuario=_usuario(),
                                                          motivo=request.form.get('motivo')),
                     'Fecha de reanálisis extendida.', _volver_dictamen(lote_id))


@bp.route('/lote/<int:lote_id>/certificado')
@login_required
@requiere_modulo('calidad')
def certificado(lote_id):
    conn = get_db_connection()
    try:
        c = cert.armar(conn, lote_id)
    except ErrorCalidad as e:
        existe = conn.execute('SELECT 1 FROM lotes WHERE id = ?', (lote_id,)).fetchone()
        conn.close()
        if not existe:
            abort(404)
        flash(str(e), 'error')
        return hx_redirect(_volver_dictamen(lote_id))
    conn.close()
    return full_page('calidad/certificado.html', c=c)


# ---------------------------------------------------------------------------
# Especificaciones
# ---------------------------------------------------------------------------

@bp.route('/especificaciones')
@login_required
@requiere_modulo('calidad')
def especificaciones():
    tab = 'sin' if request.args.get('tab') == 'sin' else 'lista'
    conn = get_db_connection()
    filas = esp.lista(conn)
    sin = esp.sin_especificacion(conn)
    materiales = conn.execute('SELECT id, codigo, nombre, tipo FROM materiales WHERE activo = 1 '
                              'ORDER BY tipo, codigo').fetchall()
    vigentes = conn.execute("SELECT e.id, m.codigo, e.version FROM cc_especificaciones e JOIN materiales m "
                            "ON m.id = e.material_id WHERE e.estatus = 'VIGENTE' ORDER BY m.codigo").fetchall()
    conn.close()
    return render_page('calidad/especificaciones.html', tab=tab, filas=filas, sin=sin, materiales=materiales,
                       vigentes=vigentes)


@bp.route('/especificaciones/nueva', methods=['POST'])
@login_required
@requiere_modulo('calidad_operar')
def especificacion_nueva():
    f = request.form

    def crear(conn):
        material_id = int(f.get('material_id') or 0)
        if f.get('copiar_de'):
            return esp.copiar(conn, desde_id=int(f['copiar_de']), material_id=material_id, usuario=_usuario())
        return esp.crear(conn, material_id=material_id, usuario=_usuario())
    return _ejecutar(crear, 'Especificación creada en borrador.',
                     lambda eid: url_for('calidad.especificacion_editor', especificacion_id=eid) if eid
                     else url_for('calidad.especificaciones'))


def _pruebas_del_form():
    f = request.form
    lista = []
    for i, nombre in enumerate(f.getlist('p_prueba')):
        if not (nombre or '').strip():
            continue
        fila = {campo: (f.getlist('p_' + campo) + [''] * (i + 1))[i]
                for campo in esp.CAMPOS_PRUEBA if campo not in ('cada_lote', 'en_certificado')}
        fila['cada_lote'] = (f.getlist('p_cada_lote') + [''] * (i + 1))[i]
        fila['en_certificado'] = (f.getlist('p_en_certificado') + [''] * (i + 1))[i]
        lista.append(fila)
    return lista


@bp.route('/especificaciones/<int:especificacion_id>', methods=['GET', 'POST'])
@login_required
@requiere_modulo('calidad')
def especificacion_editor(especificacion_id):
    volver = url_for('calidad.especificacion_editor', especificacion_id=especificacion_id)
    if request.method == 'POST':
        if not usuario_puede('calidad_operar'):
            flash('No tienes permiso para editar especificaciones.', 'error')
            return hx_redirect(volver)
        datos = {k: request.form.get(k) for k in esp.CAMPOS}
        for k in ('pide_retencion', 'requiere_revision'):
            datos[k] = request.form.get(k) or '0'
        return _ejecutar(lambda conn: esp.guardar(conn, especificacion_id, usuario=_usuario(), datos=datos,
                                                  pruebas_lista=_pruebas_del_form()),
                         'Especificación guardada.', volver)
    conn = get_db_connection()
    try:
        e = esp.especificacion(conn, especificacion_id)
    except ErrorCalidad:
        conn.close()
        abort(404)
    contexto = {
        'e': e, 'pruebas': esp.pruebas(conn, especificacion_id),
        'revisor': conn.execute('SELECT * FROM firmas WHERE id = ?', (e['reviso_firma_id'],)).fetchone()
        if e['reviso_firma_id'] else None,
        'aprobador': conn.execute('SELECT * FROM firmas WHERE id = ?', (e['aprobo_firma_id'],)).fetchone()
        if e['aprobo_firma_id'] else None,
        'versiones': conn.execute('SELECT id, version, estatus, vigente_desde FROM cc_especificaciones '
                                  'WHERE material_id = ? ORDER BY version DESC', (e['material_id'],)).fetchall(),
        'documentos': conn.execute("SELECT id, codigo, nombre FROM documentos_legales ORDER BY codigo").fetchall(),
        'archivos': archivos.archivos_de(conn, 'cc_especificaciones', especificacion_id),
        'tipos_equipo': [r[0] for r in conn.execute('SELECT DISTINCT tipo FROM equipos ORDER BY tipo').fetchall()],
        'fuentes': esp.FUENTES,
    }
    conn.close()
    return render_page('calidad/especificacion_editor.html', **contexto)


@bp.route('/especificaciones/<int:especificacion_id>/<accion>', methods=['POST'])
@login_required
@requiere_modulo('calidad_operar')
def especificacion_accion(especificacion_id, accion):
    volver = url_for('calidad.especificacion_editor', especificacion_id=especificacion_id)
    acciones = {
        'enviar': (lambda conn: esp.enviar_revision(conn, especificacion_id, usuario=_usuario()), 'Enviada a revisión.'),
        'regresar': (lambda conn: esp.regresar_borrador(conn, especificacion_id, usuario=_usuario()),
                     'Regresó a borrador.'),
        'revisar': (lambda conn: esp.revisar(conn, especificacion_id, firma=_firma()), 'Especificación revisada.'),
        'aprobar': (lambda conn: esp.aprobar(conn, especificacion_id, firma=_firma()),
                    'Especificación aprobada: ya está vigente.'),
        'nueva-version': (lambda conn: esp.nueva_version(conn, especificacion_id, usuario=_usuario()),
                          'Versión nueva creada en borrador.'),
        'arte': (lambda conn: archivos.guardar_archivo(
            conn, request.files.get('archivo'), modulo='calidad', entidad='cc_especificaciones',
            entidad_id=especificacion_id, descripcion=request.form.get('descripcion') or 'Arte aprobado',
            usuario=_usuario()), 'Archivo adjuntado.'),
    }
    if accion not in acciones:
        abort(404)
    if accion == 'aprobar' and 'aprobar_maestros' not in (session.get('facultades') or []):
        flash('Para aprobar especificaciones necesitas la facultad «aprobar_maestros».', 'error')
        return hx_redirect(volver)
    fn, msg = acciones[accion]
    if accion == 'nueva-version':
        return _ejecutar(fn, msg, lambda eid: url_for('calidad.especificacion_editor', especificacion_id=eid)
                         if eid else volver)
    return _ejecutar(fn, msg, volver)


@bp.route('/especificaciones/bloque', methods=['POST'])
@login_required
@requiere_modulo('calidad_operar')
def especificaciones_bloque():
    accion = request.form.get('accion')
    ids = [i for i in request.form.getlist('ids') if i.isdigit()]
    if accion not in ('revisar', 'aprobar') or not ids:
        flash('Marca las especificaciones y elige revisar o aprobar.', 'error')
        return hx_redirect(url_for('calidad.especificaciones'))
    if accion == 'aprobar' and 'aprobar_maestros' not in (session.get('facultades') or []):
        flash('Para aprobar especificaciones necesitas la facultad «aprobar_maestros».', 'error')
        return hx_redirect(url_for('calidad.especificaciones'))
    conn = get_db_connection()
    resultado = esp.en_bloque(conn, accion, ids, firma=_firma())
    conn.commit()
    conn.close()
    bien = sum(1 for _, err in resultado if not err)
    if bien:
        flash(f'{bien} especificación(es) {"revisadas" if accion == "revisar" else "aprobadas"}.', 'success')
    for eid, err in resultado:
        if err:
            flash(f'Especificación {eid}: {err}', 'error')
    return hx_redirect(url_for('calidad.especificaciones'))


@bp.route('/especificaciones/carga', methods=['GET', 'POST'])
@login_required
@requiere_modulo('calidad_operar')
def especificaciones_carga():
    if request.method == 'POST':
        archivo = request.files.get('archivo')
        if not archivo or not archivo.filename:
            flash('Selecciona el archivo de Excel.', 'error')
            return hx_redirect(url_for('calidad.especificaciones_carga'))
        conn = get_db_connection()
        try:
            r = esp.importar_excel(conn, io.BytesIO(archivo.read()), usuario=_usuario())
            conn.commit()
        except ErrorCalidad as e:
            conn.rollback()
            conn.close()
            flash(str(e), 'error')
            return hx_redirect(url_for('calidad.especificaciones_carga'))
        conn.close()
        if r['creadas']:
            flash(f'{len(r["creadas"])} especificación(es) creadas en borrador: '
                  + ', '.join(c for c, _ in r['creadas']) + '.', 'success')
        for err in r['errores']:
            flash(err, 'error')
        return hx_redirect(url_for('calidad.especificaciones'))
    return render_page('calidad/carga_especificaciones.html', columnas=esp.COLUMNAS_EXCEL)


@bp.route('/especificaciones/plantilla.xlsx')
@login_required
@requiere_modulo('calidad_operar')
def especificaciones_plantilla():
    return send_file(io.BytesIO(esp.plantilla_excel()), as_attachment=True,
                     download_name='plantilla_especificaciones.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ---------------------------------------------------------------------------
# Muestras de retención
# ---------------------------------------------------------------------------

@bp.route('/retencion')
@login_required
@requiere_modulo('calidad')
def retencion():
    filtro = request.args.get('filtro') if request.args.get('filtro') in ('por_destruir', 'guardadas') else None
    material = (request.args.get('material') or '').strip()
    conn = get_db_connection()
    filas = ret.lista(conn, filtro=filtro, material=material)
    sin = ret.lotes_sin_retencion(conn)
    lotes = conn.execute(
        "SELECT l.id, l.numero_lote, l.existencia, m.codigo, m.unidad FROM lotes l JOIN materiales m "
        "ON m.id = l.material_id WHERE l.estatus IN ('CUARENTENA','APROBADO','RETENIDO') AND l.existencia > 0 "
        'ORDER BY l.id DESC LIMIT 300').fetchall()
    conn.close()
    return render_page('calidad/retencion.html', filas=filas, filtro=filtro, material=material, sin=sin, lotes=lotes)


@bp.route('/retencion/guardar', methods=['POST'])
@login_required
@requiere_modulo('calidad_operar')
def retencion_guardar():
    f = request.form
    return _ejecutar(lambda conn: ret.guardar(conn, lote_id=int(f.get('lote_id') or 0), envases=f.get('envases'),
                                              cantidad=f.get('cantidad'), ubicacion=f.get('ubicacion'),
                                              usuario=_usuario(), firma=_firma(),
                                              en_gramos=f.get('unidad_captura') == 'g'),
                     'Muestra de retención guardada.', url_for('calidad.retencion'))


@bp.route('/retencion/<int:muestra_id>/usar', methods=['POST'])
@login_required
@requiere_modulo('calidad_operar')
def retencion_usar(muestra_id):
    f = request.form
    return _ejecutar(lambda conn: ret.usar(conn, muestra_id=muestra_id, envases=f.get('envases'),
                                           motivo=f.get('motivo'), folio=f.get('folio'), firma=_firma()),
                     'Uso de la muestra registrado.', url_for('calidad.retencion'))


@bp.route('/retencion/<int:muestra_id>/destruir', methods=['POST'])
@login_required
@requiere_modulo('calidad_operar')
def retencion_destruir(muestra_id):
    return _ejecutar(lambda conn: ret.destruir(conn, muestra_id=muestra_id, firma=_firma()),
                     'Muestra destruida.', url_for('calidad.retencion'))


# ---------------------------------------------------------------------------
# Reportes
# ---------------------------------------------------------------------------

@bp.route('/reportes')
@login_required
@requiere_modulo('calidad')
def reportes_inicio():
    return render_page('calidad/reportes.html', reportes=reportes.REPORTES)


@bp.route('/reportes/<tipo>.xlsx')
@login_required
@requiere_modulo('calidad')
def reporte_excel(tipo):
    if tipo not in reportes.REPORTES:
        abort(404)
    conn = get_db_connection()
    try:
        contenido = reportes.excel(conn, tipo, desde=request.args.get('desde'), hasta=request.args.get('hasta'),
                                   material=request.args.get('material'), prueba=request.args.get('prueba'))
    except ErrorCalidad as e:
        conn.close()
        flash(str(e), 'error')
        return hx_redirect(url_for('calidad.reportes_inicio'))
    conn.close()
    return send_file(io.BytesIO(contenido), as_attachment=True, download_name=f'calidad_{tipo}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
