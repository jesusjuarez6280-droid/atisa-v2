"""Pantallas de oficina del módulo Equipos (blueprint 'equipos', /equipos)."""
from datetime import date

from flask import Blueprint, abort, current_app, flash, request, send_file, session, url_for

from nucleo import archivos, firmas, registro
from nucleo.auth import login_required, requiere_facultad, requiere_modulo
from nucleo.db import get_db_connection
from nucleo.parametros import parametro
from nucleo.vistas import full_page, hx_redirect, render_page

from . import reportes
from .servicios import calibraciones, catalogo, impactos, mantenimiento, verificaciones
from .servicios.comun import (CRITICIDADES, FACULTAD, MODALIDADES, TIPO_PESA, TIPOS, ErrorEquipos, equipo,
                              programa)

bp = Blueprint('equipos', __name__, url_prefix='/equipos', template_folder='templates')

POR_PAGINA = 50
ERRORES = (ErrorEquipos, firmas.FirmaInvalida, archivos.ErrorArchivo)
FILTROS = {
    'cal_vencida': 'Calibración vencida', 'cal_por_vencer': 'Calibración por vencer',
    'man_vencido': 'Mantenimiento vencido', 'fuera': 'Fuera de servicio',
    'verif_pendiente': 'Sin verificación de hoy', 'sin_programa': 'Instrumentos sin programa',
}


# --- Ayudas ----------------------------------------------------------------------

def _usuario():
    return session.get('nombre') or session.get('username') or '—'


def _firmar(conn, entidad, entidad_id, detalle, critica=False, facultad=None, significado=None):
    """Firma con el número y PIN del formulario (componente nucleo/_firma.html)."""
    return firmas.firmar(conn, numero_empleado=request.form.get('firma_numero'), pin=request.form.get('firma_pin'),
                         significado=significado or request.form.get('firma_significado') or 'Realizó',
                         modulo='equipos', entidad=entidad, entidad_id=entidad_id, detalle=detalle,
                         facultad=facultad, critica=critica)


def _volver(defecto):
    destino = request.form.get('volver') or request.args.get('volver') or ''
    if destino.startswith('/') and not destino.replace('\\', '/').startswith('//'):
        return destino
    return defecto


def _equipo_o_404(conn, equipo_id):
    try:
        return equipo(conn, equipo_id)
    except ErrorEquipos:
        conn.close()
        abort(404)


def _fallo(conn, e):
    conn.rollback()
    flash(str(e), 'error')


def _inesperado(conn, que):
    conn.rollback()
    current_app.logger.exception('Equipos: falló %s', que)
    flash('Algo salió mal y no se guardó. El error quedó registrado.', 'error')


def _lista_form(prefijo, campos):
    """Renglones de un formulario con campos repetidos (pc_nominal[], pc_unidad[]…) → [dict]."""
    columnas = {c: request.form.getlist(f'{prefijo}_{c}') for c in campos}
    n = max((len(v) for v in columnas.values()), default=0)
    return [{c: (columnas[c][i] if i < len(columnas[c]) else '') for c in campos} for i in range(n)]


def _proveedores(conn):
    return calibraciones.laboratorios(conn)


def _guardar_archivo(conn, campo, entidad, entidad_id, descripcion):
    f = request.files.get(campo)
    if f and f.filename:
        return archivos.guardar_archivo(conn, f, modulo='equipos', entidad=entidad, entidad_id=entidad_id,
                                        descripcion=descripcion, usuario=_usuario())
    return None


# --- Lista ------------------------------------------------------------------------

def _indicadores(conn, hoy):
    from datetime import timedelta
    from nucleo.parametros import parametro_int
    en30 = (date.today() + timedelta(days=parametro_int('equipos.dias_aviso_calibracion', conn))).isoformat()
    q = lambda sql, *p: conn.execute(sql, p).fetchone()[0] or 0  # noqa: E731
    hechas, tocan = verificaciones.conteo_hoy(conn)
    return {
        'cal_vencida': q("SELECT COUNT(*) FROM equipos WHERE estatus <> 'BAJA' AND vence_calibracion < ?", hoy),
        'cal_por_vencer': q("SELECT COUNT(*) FROM equipos WHERE estatus <> 'BAJA' AND vence_calibracion BETWEEN ? AND ?",
                            hoy, en30),
        'man_vencido': q("SELECT COUNT(*) FROM equipos WHERE estatus <> 'BAJA' AND vence_mantenimiento < ?", hoy),
        'fuera': q("SELECT COUNT(*) FROM equipos WHERE estatus = 'FUERA_DE_SERVICIO'"),
        'verif_hechas': hechas, 'verif_tocan': tocan,
    }


@bp.route('/')
@bp.route('')
@login_required
@requiere_modulo('equipos')
def lista():
    from datetime import timedelta
    from nucleo.parametros import parametro_int
    conn = get_db_connection()
    hoy = date.today().isoformat()
    f = {k: (request.args.get(k) or '').strip() for k in ('tipo', 'area', 'estatus', 'instrumentos', 'filtro', 'q')}
    where, params = [], []
    if f['estatus']:
        where.append('e.estatus = ?')
        params.append(f['estatus'])
    else:
        where.append("e.estatus <> 'BAJA'")
    if f['tipo']:
        where.append('e.tipo = ?')
        params.append(f['tipo'])
    if f['area']:
        where.append('e.area = ?')
        params.append(f['area'])
    if f['instrumentos']:
        where.append('e.es_instrumento = 1')
    if f['q']:
        where.append('(e.codigo LIKE ? OR e.nombre LIKE ? OR e.serie LIKE ?)')
        params += [f'%{f["q"]}%'] * 3
    filtro = f['filtro']
    if filtro == 'cal_vencida':
        where.append('e.vence_calibracion < ?')
        params.append(hoy)
    elif filtro == 'cal_por_vencer':
        en = (date.today() + timedelta(days=parametro_int('equipos.dias_aviso_calibracion', conn))).isoformat()
        where.append('e.vence_calibracion BETWEEN ? AND ?')
        params += [hoy, en]
    elif filtro == 'man_vencido':
        where.append('e.vence_mantenimiento < ?')
        params.append(hoy)
    elif filtro == 'fuera':
        where.append("e.estatus = 'FUERA_DE_SERVICIO'")
    elif filtro == 'verif_pendiente':
        where.append("e.verificacion_diaria = 1 AND e.estatus = 'EN_SERVICIO' "
                     "AND COALESCE(substr(e.ultima_verificacion, 1, 10), '') <> ?")
        params.append(hoy)
    elif filtro == 'sin_programa':
        where.append("e.es_instrumento = 1 AND NOT EXISTS (SELECT 1 FROM eq_programas p WHERE p.equipo_id = e.id "
                     "AND p.actividad = 'CALIBRACION' AND p.activo = 1)")
    try:
        pagina = max(1, int(request.args.get('pagina') or 1))
    except ValueError:
        pagina = 1
    sql_where = ' WHERE ' + ' AND '.join(where)
    total = conn.execute(f'SELECT COUNT(*) FROM equipos e{sql_where}', params).fetchone()[0]
    filas = conn.execute(f'SELECT e.* FROM equipos e{sql_where} ORDER BY e.area, e.codigo LIMIT ? OFFSET ?',
                         (*params, POR_PAGINA, (pagina - 1) * POR_PAGINA)).fetchall()
    equipos = [dict(e, verif=verificaciones.estado_hoy(conn, e)) for e in filas]
    areas = [r[0] for r in conn.execute("SELECT DISTINCT area FROM equipos WHERE area IS NOT NULL AND area <> '' "
                                        'ORDER BY area').fetchall()]
    tipos = sorted({*TIPOS, *(r[0] for r in conn.execute('SELECT DISTINCT tipo FROM equipos').fetchall())})
    ind = _indicadores(conn, hoy)
    conn.close()
    return render_page('equipos/lista.html', equipos=equipos, f=f, filtros=FILTROS, areas=areas, tipos=tipos,
                       ind=ind, pagina=pagina, paginas=max(1, -(-total // POR_PAGINA)), total=total, hoy=hoy)


@bp.route('/exportar')
@login_required
@requiere_modulo('equipos')
def exportar():
    conn = get_db_connection()
    filas = conn.execute("SELECT * FROM equipos ORDER BY estatus = 'BAJA', area, codigo").fetchall()
    conn.close()
    return send_file(reportes.excel_equipos(filas), as_attachment=True,
                     download_name=f'equipos_{date.today():%Y-%m-%d}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# --- Alta y edición -------------------------------------------------------------------

def _datos_form():
    datos = {k: request.form.get(k, '') for k in ('codigo', 'nombre', 'tipo', 'area', 'marca', 'modelo', 'serie',
                                                  'ubicacion', 'capacidad', 'resolucion')}
    datos['tipo'] = (request.form.get('tipo_otro') or '').strip() or datos['tipo']
    datos['es_instrumento'] = request.form.get('es_instrumento')
    datos['verificacion_diaria'] = request.form.get('verificacion_diaria')
    for campo in ('competencia_tarea_id', 'activo_id'):
        valor = (request.form.get(campo) or '').strip()
        datos[campo] = int(valor) if valor.isdigit() else None
    ficha = {k: request.form.get(k, '') for k in catalogo.CAMPOS_FICHA if k != 'fecha_alta'}
    return datos, ficha


def _programas_form(conn, equipo_id):
    for actividad, pre in (('CALIBRACION', 'cal'), ('MANTENIMIENTO', 'man')):
        frecuencia = (request.form.get(f'{pre}_frecuencia') or '').strip()
        if not frecuencia:
            if programa(conn, equipo_id, actividad):
                catalogo.desactivar_programa(conn, equipo_id, actividad, usuario=_usuario())
            continue
        catalogo.guardar_programa(
            conn, equipo_id, actividad, frecuencia_meses=frecuencia,
            modalidad=request.form.get(f'{pre}_modalidad') or 'Externa',
            proveedor_id=request.form.get(f'{pre}_proveedor_id'), tareas=request.form.get(f'{pre}_tareas'),
            dias_aviso=request.form.get(f'{pre}_dias_aviso'), proximo=request.form.get(f'{pre}_proximo'),
            usuario=_usuario())
    catalogo.guardar_puntos(conn, equipo_id, 'CALIBRACION',
                            _lista_form('pc', ('nombre', 'nominal', 'tolerancia', 'unidad')), usuario=_usuario())
    catalogo.guardar_puntos(conn, equipo_id, 'VERIFICACION',
                            _lista_form('pv', ('nombre', 'nominal', 'tolerancia', 'minimo', 'maximo', 'unidad',
                                               'requiere_pesa')), usuario=_usuario())
    _guardar_archivo(conn, 'foto', 'equipos', equipo_id, 'Foto')
    _guardar_archivo(conn, 'manual', 'equipos', equipo_id, 'Manual')


def _contexto_editar(conn, e=None):
    eid = e['id'] if e else 0
    try:
        tareas = conn.execute('SELECT id, nombre FROM competencias_tareas WHERE activo = 1 ORDER BY orden, id').fetchall()
    except Exception:
        tareas = []
    try:
        activos = conn.execute('SELECT id, categoria, descripcion FROM inventario_activos ORDER BY categoria, id').fetchall()
    except Exception:
        activos = []
    ficha = conn.execute('SELECT * FROM eq_fichas WHERE equipo_id = ?', (eid,)).fetchone()
    return {'e': e, 'ficha': ficha, 'tipos': TIPOS, 'criticidades': CRITICIDADES, 'modalidades': MODALIDADES,
            'tareas': tareas, 'activos': activos, 'proveedores': _proveedores(conn),
            'prog_cal': programa(conn, eid, 'CALIBRACION'), 'prog_man': programa(conn, eid, 'MANTENIMIENTO'),
            'puntos_cal': catalogo.puntos(conn, eid, 'CALIBRACION'),
            'puntos_ver': catalogo.puntos(conn, eid, 'VERIFICACION'), 'TIPO_PESA': TIPO_PESA,
            'dias_cal': parametro('equipos.dias_aviso_calibracion', conn),
            'dias_man': parametro('equipos.dias_aviso_mantenimiento', conn)}


@bp.route('/nuevo', methods=['GET', 'POST'])
@login_required
@requiere_modulo('equipos_operar')
def nuevo():
    conn = get_db_connection()
    if request.method == 'POST':
        try:
            datos, ficha = _datos_form()
            equipo_id = catalogo.alta(conn, datos=datos, ficha=ficha, usuario=_usuario())
            _programas_form(conn, equipo_id)
            conn.commit()
            flash(f'Equipo {datos["codigo"]} dado de alta.', 'success')
            conn.close()
            return hx_redirect(url_for('equipos.ficha', equipo_id=equipo_id))
        except ERRORES as e:
            _fallo(conn, e)
        except Exception:
            _inesperado(conn, 'alta de equipo')
    ctx = _contexto_editar(conn)
    conn.close()
    return render_page('equipos/editar.html', form=request.form, **ctx)


@bp.route('/<int:equipo_id>/editar', methods=['GET', 'POST'])
@login_required
@requiere_modulo('equipos_operar')
def editar(equipo_id):
    conn = get_db_connection()
    e = _equipo_o_404(conn, equipo_id)
    if request.method == 'POST':
        try:
            datos, ficha = _datos_form()
            catalogo.editar(conn, equipo_id, datos=datos, ficha=ficha, usuario=_usuario())
            _programas_form(conn, equipo_id)
            conn.commit()
            flash('Cambios guardados.', 'success')
            conn.close()
            return hx_redirect(url_for('equipos.ficha', equipo_id=equipo_id))
        except ERRORES as ex:
            _fallo(conn, ex)
        except Exception:
            _inesperado(conn, 'edición de equipo')
        e = equipo(conn, equipo_id)
    ctx = _contexto_editar(conn, e)
    conn.close()
    return render_page('equipos/editar.html', form=request.form, **ctx)


@bp.route('/laboratorio', methods=['POST'])
@login_required
@requiere_modulo('equipos_operar')
def laboratorio():
    conn = get_db_connection()
    try:
        calibraciones.crear_laboratorio(conn, nombre=request.form.get('nombre'), contacto=request.form.get('contacto'),
                                        telefono=request.form.get('telefono'), correo=request.form.get('correo'),
                                        usuario=_usuario())
        conn.commit()
        flash('Laboratorio o taller agregado (queda "En evaluación" hasta que lo aprueben).', 'success')
    except ERRORES as e:
        _fallo(conn, e)
    finally:
        conn.close()
    return hx_redirect(_volver(url_for('equipos.lista')))


# --- Ficha -------------------------------------------------------------------------

@bp.route('/<int:equipo_id>')
@login_required
@requiere_modulo('equipos')
def ficha(equipo_id):
    conn = get_db_connection()
    e = _equipo_o_404(conn, equipo_id)
    tab = request.args.get('tab') or 'calibracion'
    ctx = {'e': e, 'tab': tab, 'hoy': date.today().isoformat(),
           'ficha': conn.execute('SELECT f.*, p.nombre AS proveedor_nombre FROM eq_fichas f LEFT JOIN proveedores p '
                                 'ON p.id = f.proveedor_id WHERE f.equipo_id = ?', (equipo_id,)).fetchone(),
           'apto': calibraciones.apto(conn, equipo_id), 'causa': catalogo.causa_actual(conn, e),
           'verif': verificaciones.estado_hoy(conn, e), 'calificados': catalogo.personal_calificado(conn, e),
           'archivos': archivos.archivos_de(conn, 'equipos', equipo_id),
           'prog_cal': programa(conn, equipo_id, 'CALIBRACION'), 'prog_man': programa(conn, equipo_id, 'MANTENIMIENTO'),
           'no_regresar': catalogo.motivos_para_no_regresar(conn, equipo_id) if e['estatus'] == 'FUERA_DE_SERVICIO' else [],
           'impactos': conn.execute('SELECT * FROM eq_impactos WHERE equipo_id = ? ORDER BY id DESC', (equipo_id,)).fetchall()}
    ctx['tarea'] = None
    if e['competencia_tarea_id']:
        ctx['tarea'] = conn.execute('SELECT nombre FROM competencias_tareas WHERE id = ?',
                                    (e['competencia_tarea_id'],)).fetchone()
    ctx['activo'] = None
    if e['activo_id']:
        ctx['activo'] = conn.execute('SELECT * FROM inventario_activos WHERE id = ?', (e['activo_id'],)).fetchone()
    if tab == 'calibracion':
        ctx['puntos'] = catalogo.puntos(conn, equipo_id, 'CALIBRACION')
        ctx['calibraciones'] = conn.execute(
            'SELECT c.*, p.nombre AS proveedor_nombre FROM eq_calibraciones c LEFT JOIN proveedores p '
            'ON p.id = c.proveedor_id WHERE c.equipo_id = ? ORDER BY c.fecha DESC, c.id DESC', (equipo_id,)).fetchall()
    elif tab == 'verificaciones':
        ctx['puntos'] = catalogo.puntos(conn, equipo_id, 'VERIFICACION')
        ctx['historial'] = verificaciones.historial(conn, equipo_id)
    elif tab == 'mantenimiento':
        ctx['ordenes'] = conn.execute('SELECT * FROM eq_ordenes_trabajo WHERE equipo_id = ? ORDER BY id DESC',
                                      (equipo_id,)).fetchall()
    elif tab == 'uso':
        desde = (e['creado_en'] or '2000-01-01')[:10]
        ctx['usos'] = impactos.usos_en_ventana(conn, equipo_id, desde)
    elif tab == 'historial':
        ctx['historial'] = conn.execute('SELECT h.*, f.nombre AS firmante FROM eq_estatus_historial h LEFT JOIN firmas f '
                                        'ON f.id = h.firma_id WHERE h.equipo_id = ? ORDER BY h.id DESC',
                                        (equipo_id,)).fetchall()
    conn.close()
    return render_page('equipos/ficha.html', **ctx)


@bp.route('/<int:equipo_id>/fuera', methods=['POST'])
@login_required
@requiere_modulo('equipos')
def fuera(equipo_id):
    conn = get_db_connection()
    _equipo_o_404(conn, equipo_id)
    try:
        motivo = request.form.get('motivo')
        fid = _firmar(conn, 'equipos', equipo_id, f'Puso fuera de servicio: {motivo or ""}')
        catalogo.poner_fuera(conn, equipo_id, motivo=motivo, firma_id=fid, usuario=_usuario())
        conn.commit()
        flash('El equipo quedó fuera de servicio.', 'success')
    except ERRORES as e:
        _fallo(conn, e)
    finally:
        conn.close()
    return hx_redirect(url_for('equipos.ficha', equipo_id=equipo_id))


@bp.route('/<int:equipo_id>/regresar', methods=['POST'])
@login_required
@requiere_modulo('equipos_operar')
@requiere_facultad(FACULTAD)
def regresar(equipo_id):
    conn = get_db_connection()
    _equipo_o_404(conn, equipo_id)
    try:
        motivo = request.form.get('motivo')
        fid = _firmar(conn, 'equipos', equipo_id, f'Regresó a servicio: {motivo or ""}', critica=True,
                      facultad=FACULTAD, significado='Aprobó')
        catalogo.regresar_a_servicio(conn, equipo_id, motivo=motivo, firma_id=fid, usuario=_usuario())
        conn.commit()
        flash('El equipo regresó a servicio.', 'success')
    except ERRORES as e:
        _fallo(conn, e)
    finally:
        conn.close()
    return hx_redirect(url_for('equipos.ficha', equipo_id=equipo_id))


@bp.route('/<int:equipo_id>/baja', methods=['POST'])
@login_required
@requiere_modulo('equipos_operar')
@requiere_facultad(FACULTAD)
def baja(equipo_id):
    conn = get_db_connection()
    _equipo_o_404(conn, equipo_id)
    try:
        motivo = request.form.get('motivo')
        fid = _firmar(conn, 'equipos', equipo_id, f'Dio de baja: {motivo or ""}', critica=True, facultad=FACULTAD,
                      significado='Aprobó')
        catalogo.dar_de_baja(conn, equipo_id, motivo=motivo, firma_id=fid, usuario=_usuario())
        conn.commit()
        flash('El equipo quedó dado de baja; su historial se conserva.', 'success')
    except ERRORES as e:
        _fallo(conn, e)
    finally:
        conn.close()
    return hx_redirect(url_for('equipos.ficha', equipo_id=equipo_id))


@bp.route('/<int:equipo_id>/etiqueta')
@login_required
@requiere_modulo('equipos')
def etiqueta(equipo_id):
    conn = get_db_connection()
    e = _equipo_o_404(conn, equipo_id)
    ultima = conn.execute("SELECT * FROM eq_calibraciones WHERE equipo_id = ? AND estatus = 'APTO' "
                          'ORDER BY fecha DESC, id DESC LIMIT 1', (equipo_id,)).fetchone()
    motivo = conn.execute("SELECT motivo, ts FROM eq_estatus_historial WHERE equipo_id = ? AND nuevo = ? "
                          'ORDER BY id DESC LIMIT 1', (equipo_id, e['estatus'])).fetchone()
    codigo = parametro('equipos.codigo_formato_etiqueta', conn)
    conn.close()
    return full_page('equipos/etiqueta.html', e=e, ultima=ultima, motivo=motivo, codigo_formato=codigo)


# --- Calibraciones ------------------------------------------------------------------

@bp.route('/calibraciones')
@login_required
@requiere_modulo('equipos')
def calibraciones_lista():
    from datetime import timedelta
    conn = get_db_connection()
    hoy = date.today()
    en90 = (hoy + timedelta(days=90)).isoformat()
    base = ("SELECT e.*, p.frecuencia_meses, p.modalidad, pr.nombre AS proveedor_nombre FROM equipos e "
            "JOIN eq_programas p ON p.equipo_id = e.id AND p.actividad = 'CALIBRACION' AND p.activo = 1 "
            "LEFT JOIN proveedores pr ON pr.id = p.proveedor_id WHERE e.estatus <> 'BAJA' ")
    ctx = {
        'por_dictaminar': conn.execute("SELECT c.*, e.codigo, e.nombre AS equipo_nombre FROM eq_calibraciones c "
                                       "JOIN equipos e ON e.id = c.equipo_id WHERE c.estatus = 'POR_DICTAMINAR' "
                                       'ORDER BY c.fecha').fetchall(),
        'vencidas': conn.execute(base + 'AND (e.vence_calibracion IS NULL OR e.vence_calibracion < ?) '
                                 'ORDER BY e.vence_calibracion', (hoy.isoformat(),)).fetchall(),
        'proximas': conn.execute(base + 'AND e.vence_calibracion BETWEEN ? AND ? ORDER BY e.vence_calibracion',
                                 (hoy.isoformat(), en90)).fetchall(),
        'recientes': conn.execute("SELECT c.*, e.codigo, e.nombre AS equipo_nombre FROM eq_calibraciones c "
                                  "JOIN equipos e ON e.id = c.equipo_id WHERE c.estatus <> 'POR_DICTAMINAR' "
                                  'ORDER BY c.id DESC LIMIT 30').fetchall(),
        'mes': hoy.isoformat()[:7],
    }
    conn.close()
    return render_page('equipos/calibraciones.html', **ctx)


@bp.route('/<int:equipo_id>/calibracion/nueva', methods=['GET', 'POST'])
@login_required
@requiere_modulo('equipos_operar')
def calibracion_nueva(equipo_id):
    conn = get_db_connection()
    e = _equipo_o_404(conn, equipo_id)
    if request.method == 'POST':
        try:
            pdf = request.files.get('certificado_pdf')
            fid = _firmar(conn, 'equipos', equipo_id, f'Registró calibración de {e["codigo"]}')
            res = calibraciones.registrar(
                conn, equipo_id, fecha=request.form.get('fecha'), modalidad=request.form.get('modalidad'),
                proveedor_id=request.form.get('proveedor_id'), acreditacion=request.form.get('acreditacion'),
                certificado=request.form.get('certificado'), con_certificado_pdf=bool(pdf and pdf.filename),
                patrones=request.form.getlist('patrones'), ajustado=request.form.get('ajustado'),
                observaciones=request.form.get('observaciones'),
                puntos=_lista_form('p', ('nominal', 'tolerancia', 'unidad', 'encontrado', 'dejado', 'incertidumbre')),
                firma_id=fid, usuario=_usuario())
            _guardar_archivo(conn, 'certificado_pdf', 'eq_calibraciones', res['id'], 'Certificado de calibración')
            conn.commit()
            for aviso in res['avisos']:
                flash(aviso, 'warning')
            flash(f'Calibración {res["folio"]} registrada; falta su dictamen.', 'success')
            conn.close()
            return hx_redirect(url_for('equipos.calibracion', calibracion_id=res['id']))
        except ERRORES as ex:
            _fallo(conn, ex)
        except Exception:
            _inesperado(conn, 'registro de calibración')
    prog = programa(conn, equipo_id, 'CALIBRACION')
    ctx = {'e': e, 'prog': prog, 'puntos': catalogo.puntos(conn, equipo_id, 'CALIBRACION'),
           'proveedores': _proveedores(conn), 'patrones': calibraciones.patrones_aptos(conn),
           'hoy': date.today().isoformat(), 'form': request.form}
    conn.close()
    return render_page('equipos/calibracion_nueva.html', **ctx)


@bp.route('/calibracion/<int:calibracion_id>')
@login_required
@requiere_modulo('equipos')
def calibracion(calibracion_id):
    conn = get_db_connection()
    try:
        c = calibraciones.calibracion(conn, calibracion_id)
    except ErrorEquipos:
        conn.close()
        abort(404)
    ctx = {'c': c, 'puntos': calibraciones.puntos_de(conn, calibracion_id),
           'archivos': archivos.archivos_de(conn, 'eq_calibraciones', calibracion_id),
           'realizo': firmas.firma(conn, c['realizo_firma_id']),
           'dictamino': firmas.firma(conn, c['dictamino_firma_id']) if c['dictamino_firma_id'] else None,
           'impacto': conn.execute("SELECT * FROM eq_impactos WHERE origen = 'CALIBRACION' AND origen_id = ?",
                                   (calibracion_id,)).fetchone()}
    conn.close()
    return render_page('equipos/calibracion.html', **ctx)


@bp.route('/calibracion/<int:calibracion_id>/dictaminar', methods=['POST'])
@login_required
@requiere_modulo('equipos_operar')
@requiere_facultad(FACULTAD)
def dictaminar(calibracion_id):
    conn = get_db_connection()
    try:
        c = calibraciones.calibracion(conn, calibracion_id)
    except ErrorEquipos:
        conn.close()
        abort(404)
    es_apto = request.form.get('dictamen') == 'APTO'
    try:
        fid = _firmar(conn, 'eq_calibraciones', calibracion_id,
                      f'Dictaminó {c["folio"]} ({c["codigo"]}): {"apto" if es_apto else "no apto"}',
                      critica=True, facultad=FACULTAD, significado='Aprobó' if es_apto else 'Rechazó')
        res = calibraciones.dictaminar(conn, calibracion_id, es_apto=es_apto, motivo=request.form.get('motivo'),
                                       firma_id=fid, usuario=_usuario())
        conn.commit()
        flash(f'{c["folio"]} dictaminada {"apta" if es_apto else "no apta"}.', 'success')
        if res['impacto_id']:
            flash(f'{c["codigo"]} llegó fuera de tolerancia: evalúa el impacto en los lotes.', 'warning')
            return hx_redirect(url_for('equipos.impacto', impacto_id=res['impacto_id']))
    except ERRORES as e:
        _fallo(conn, e)
    finally:
        conn.close()
    return hx_redirect(url_for('equipos.calibracion', calibracion_id=calibracion_id))


@bp.route('/calibracion/<int:calibracion_id>/anular', methods=['POST'])
@login_required
@requiere_modulo('equipos_operar')
def anular_calibracion(calibracion_id):
    conn = get_db_connection()
    try:
        c = calibraciones.calibracion(conn, calibracion_id)
    except ErrorEquipos:
        conn.close()
        abort(404)
    try:
        motivo = request.form.get('motivo')
        fid = _firmar(conn, 'eq_calibraciones', calibracion_id, f'Anuló {c["folio"]}: {motivo or ""}',
                      significado='Anuló')
        calibraciones.anular(conn, calibracion_id, motivo=motivo, firma_id=fid, usuario=_usuario())
        conn.commit()
        flash(f'{c["folio"]} anulada. Regístrala de nuevo si hace falta.', 'success')
    except ERRORES as e:
        _fallo(conn, e)
    finally:
        conn.close()
    return hx_redirect(url_for('equipos.calibracion', calibracion_id=calibracion_id))


# --- Evaluación de impacto ----------------------------------------------------------

@bp.route('/impacto/<int:impacto_id>')
@login_required
@requiere_modulo('equipos')
def impacto(impacto_id):
    conn = get_db_connection()
    try:
        i = impactos.impacto(conn, impacto_id)
    except ErrorEquipos:
        conn.close()
        abort(404)
    usos = impactos.usos_en_ventana(conn, i['equipo_id'], i['desde'], i['hasta'])
    origen_folio = None
    if i['origen'] == 'CALIBRACION':
        fila = conn.execute('SELECT folio FROM eq_calibraciones WHERE id = ?', (i['origen_id'],)).fetchone()
        origen_folio = fila['folio'] if fila else None
    firma = firmas.firma(conn, i['firma_id']) if i['firma_id'] else None
    conn.close()
    lotes = impactos.lote_ids(usos)
    desviacion = registro.enlace(
        'reportar_desviacion', origen='calibracion' if i['origen'] == 'CALIBRACION' else 'verificacion_equipo',
        origen_id=i['origen_id'], equipo_id=i['equipo_id'], lotes=lotes, folio=origen_folio,
        texto=f'{i["codigo"]} se encontró fuera de tolerancia el {i["hasta"]}; '
              f'se usó desde el {i["desde"]} en {len(lotes)} lote(s).',
        volver=url_for('equipos.impacto', impacto_id=impacto_id))
    return render_page('equipos/impacto.html', i=i, usos=usos, lotes=lotes, firma=firma, desviacion=desviacion,
                       origen_folio=origen_folio)


@bp.route('/impacto/<int:impacto_id>/evaluar', methods=['POST'])
@login_required
@requiere_modulo('equipos_operar')
@requiere_facultad(FACULTAD)
def evaluar_impacto(impacto_id):
    conn = get_db_connection()
    try:
        i = impactos.impacto(conn, impacto_id)
    except ErrorEquipos:
        conn.close()
        abort(404)
    try:
        fid = _firmar(conn, 'eq_impactos', impacto_id, f'Evaluó el impacto de {i["codigo"]}', critica=True,
                      facultad=FACULTAD, significado='Aprobó')
        impactos.evaluar(conn, impacto_id, conclusion=request.form.get('conclusion'), firma_id=fid, usuario=_usuario())
        conn.commit()
        flash('Evaluación firmada: los lotes de la ventana ya no se bloquean por este equipo.', 'success')
    except ERRORES as e:
        _fallo(conn, e)
    finally:
        conn.close()
    return hx_redirect(url_for('equipos.impacto', impacto_id=impacto_id))


# --- Mantenimiento --------------------------------------------------------------------

@bp.route('/mantenimiento')
@login_required
@requiere_modulo('equipos')
def mantenimiento_lista():
    tab = request.args.get('tab') or 'por_hacer'
    conn = get_db_connection()
    ctx = {'tab': tab, 'hoy': date.today().isoformat(),
           'cuentas': {t: conn.execute('SELECT COUNT(*) FROM eq_ordenes_trabajo WHERE estatus = ?', (t,)).fetchone()[0]
                       for t in ('ABIERTA', 'TERMINADA', 'CANCELADA')}}
    ctx['por_hacer'] = mantenimiento.por_hacer(conn)
    if tab in ('abiertas', 'terminadas', 'canceladas'):
        estatus = {'abiertas': 'ABIERTA', 'terminadas': 'TERMINADA', 'canceladas': 'CANCELADA'}[tab]
        ctx['ordenes'] = mantenimiento.ordenes(conn, estatus, limite=POR_PAGINA)
    conn.close()
    return render_page('equipos/mantenimiento.html', **ctx)


@bp.route('/mantenimiento/abrir', methods=['POST'])
@login_required
@requiere_modulo('equipos_operar')
def abrir_ot():
    conn = get_db_connection()
    try:
        equipo_id = request.form.get('equipo_id', type=int) or 0
        ot_id = mantenimiento.abrir_ot(conn, equipo_id, tipo='PREVENTIVO', usuario=_usuario(),
                                       fecha_programada=request.form.get('fecha_programada') or None)
        conn.commit()
        flash('Orden de trabajo abierta.', 'success')
        return hx_redirect(url_for('equipos.ot', ot_id=ot_id))
    except ERRORES as e:
        _fallo(conn, e)
    finally:
        conn.close()
    return hx_redirect(url_for('equipos.mantenimiento_lista'))


@bp.route('/ot/<int:ot_id>')
@login_required
@requiere_modulo('equipos')
def ot(ot_id):
    conn = get_db_connection()
    try:
        o = mantenimiento.ot(conn, ot_id)
    except ErrorEquipos:
        conn.close()
        abort(404)
    ctx = {'o': o, 'tareas': mantenimiento.tareas_de(o['descripcion']) if o['tipo'] == 'PREVENTIVO' else [],
           'hechas': mantenimiento.tareas_hechas(o), 'archivos': archivos.archivos_de(conn, 'eq_ordenes_trabajo', ot_id),
           'reporto': firmas.firma(conn, o['reporto_firma_id']) if o['reporto_firma_id'] else None,
           'realizo': firmas.firma(conn, o['realizo_firma_id']) if o['realizo_firma_id'] else None,
           'proveedores': _proveedores(conn), 'hoy': date.today().isoformat(),
           'codigo_formato': parametro('equipos.codigo_formato_ot', conn)}
    conn.close()
    if request.args.get('imprimir') is not None:
        return full_page('equipos/ot_imprimir.html', **ctx)
    return render_page('equipos/ot.html', **ctx)


@bp.route('/ot/<int:ot_id>/terminar', methods=['POST'])
@login_required
@requiere_modulo('equipos_operar')
def terminar_ot(ot_id):
    conn = get_db_connection()
    try:
        o = mantenimiento.ot(conn, ot_id)
    except ErrorEquipos:
        conn.close()
        abort(404)
    try:
        tareas = []
        for n, t in enumerate(mantenimiento.tareas_de(o['descripcion'])):
            tareas.append({'tarea': t, 'hecha': bool(request.form.get(f'tarea_{n}')),
                           'nota': request.form.get(f'nota_{n}')})
        proveedor = (request.form.get('proveedor_id') or '').strip()
        if proveedor.isdigit() and int(proveedor) != (o['proveedor_id'] or 0):
            conn.execute('UPDATE eq_ordenes_trabajo SET proveedor_id = ? WHERE id = ?', (int(proveedor), ot_id))
        fid = _firmar(conn, 'eq_ordenes_trabajo', ot_id, f'Terminó {o["folio"]} ({o["codigo"]})')
        res = mantenimiento.terminar_ot(conn, ot_id, tareas=tareas, trabajo_realizado=request.form.get('trabajo_realizado'),
                                        refacciones=request.form.get('refacciones'), horas=request.form.get('horas'),
                                        afecta_medicion=request.form.get('afecta_medicion'),
                                        inicio=request.form.get('inicio'), fin=request.form.get('fin'),
                                        firma_id=fid, usuario=_usuario())
        _guardar_archivo(conn, 'reporte', 'eq_ordenes_trabajo', ot_id, 'Reporte del taller')
        _guardar_archivo(conn, 'foto', 'eq_ordenes_trabajo', ot_id, 'Foto')
        conn.commit()
        flash(f'{o["folio"]} terminada.' + (f' Próximo mantenimiento: {res["proximo"]}.' if res['proximo'] else ''),
              'success')
        if o['equipo_estatus'] == 'FUERA_DE_SERVICIO':
            flash('El equipo sigue fuera de servicio hasta que quien dictamina equipos lo regrese.', 'warning')
    except ERRORES as e:
        _fallo(conn, e)
    except Exception:
        _inesperado(conn, 'cierre de orden de trabajo')
    finally:
        conn.close()
    return hx_redirect(url_for('equipos.ot', ot_id=ot_id))


@bp.route('/ot/<int:ot_id>/cancelar', methods=['POST'])
@login_required
@requiere_modulo('equipos_operar')
def cancelar_ot(ot_id):
    conn = get_db_connection()
    try:
        mantenimiento.cancelar_ot(conn, ot_id, motivo=request.form.get('motivo'), usuario=_usuario())
        conn.commit()
        flash('Orden cancelada.', 'success')
    except ERRORES as e:
        _fallo(conn, e)
    finally:
        conn.close()
    return hx_redirect(url_for('equipos.ot', ot_id=ot_id))


@bp.route('/falla', methods=['GET', 'POST'])
@login_required
@requiere_modulo('equipos')
def falla():
    """Reportar una falla desde la oficina (en la tablet: /equipos/planta/falla)."""
    from .rutas_planta import procesar_falla
    conn = get_db_connection()
    if request.method == 'POST':
        res = procesar_falla(conn)
        if res:
            conn.close()
            return hx_redirect(url_for('equipos.ot', ot_id=res['ot_id']))
    equipos = conn.execute("SELECT id, codigo, nombre, area, estatus FROM equipos WHERE estatus <> 'BAJA' "
                           'ORDER BY area, codigo').fetchall()
    conn.close()
    return render_page('equipos/falla.html', equipos=equipos, elegido=request.values.get('equipo_id', type=int),
                       form=request.form)


# --- Programa anual -------------------------------------------------------------------

def _anio():
    try:
        return max(2000, min(2100, int(request.args.get('anio') or date.today().year)))
    except ValueError:
        return date.today().year


@bp.route('/programa')
@login_required
@requiere_modulo('equipos')
def programa_anual():
    anio = _anio()
    conn = get_db_connection()
    filas = reportes.programa_anual(conn, anio)
    conn.close()
    return render_page('equipos/programa_anual.html', filas=filas, anio=anio, meses=reportes.MESES)


@bp.route('/programa/excel')
@login_required
@requiere_modulo('equipos')
def programa_excel():
    anio = _anio()
    conn = get_db_connection()
    filas = reportes.programa_anual(conn, anio)
    conn.close()
    return send_file(reportes.excel_programa(filas, anio), as_attachment=True,
                     download_name=f'programa_equipos_{anio}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
