"""Pantallas de desviaciones y CAPA (blueprint 'desviaciones', /desviaciones)."""
from datetime import datetime

from flask import Blueprint, abort, current_app, flash, request, send_file, session, url_for

from nucleo import archivos, firmas, inventario
from nucleo.auth import login_required, requiere_facultad, requiere_modulo, tiene_facultad
from nucleo.db import get_db_connection
from nucleo.parametros import parametro
from nucleo.vistas import full_page, hx_redirect, render_page

from . import reportes
from . import servicios as sv

bp = Blueprint('desviaciones', __name__, url_prefix='/desviaciones', template_folder='templates')

POR_PAGINA = 50
ERRORES = (sv.ErrorDesviacion, firmas.FirmaInvalida, archivos.ErrorArchivo, inventario.ErrorInventario)


# ---------------------------------------------------------------------------
# Ayudas
# ---------------------------------------------------------------------------

def _usuario():
    return session.get('nombre') or session.get('username') or '—'


def _volver_seguro(url, defecto):
    url = (url or '').strip()
    if url.startswith('/') and not url.replace('\\', '/').startswith('//'):
        return url
    return defecto


def _firmar(conn, significado, entidad, entidad_id, detalle, facultad=None, critica=False, distinto_de=None,
            roles=None):
    """Firma con el número de empleado + PIN del formulario. Va ANTES de las demás escrituras."""
    return firmas.firmar(conn, numero_empleado=request.form.get('firma_numero'), pin=request.form.get('firma_pin'),
                         significado=significado, modulo='desviaciones', entidad=entidad, entidad_id=entidad_id,
                         detalle=detalle, facultad=facultad, critica=critica, distinto_de=distinto_de, roles=roles)


def _ejecutar(paso, destino, accion, exito):
    """Corre `accion(conn)` en una transacción: confirma y avisa, o revierte y muestra el motivo."""
    conn = get_db_connection()
    try:
        resultado = accion(conn)
        conn.commit()
        if exito:
            flash(exito(resultado) if callable(exito) else exito, 'success')
    except ERRORES as e:
        conn.rollback()
        flash(str(e), 'error')
    except Exception:
        conn.rollback()
        current_app.logger.exception('Desviaciones: falló %s', paso)
        flash(f'No se pudo {paso}. Avisa a sistemas.', 'error')
    finally:
        conn.close()
    return hx_redirect(destino)


def _detalle_url(desviacion_id, ancla=''):
    return url_for('desviaciones.detalle', desviacion_id=desviacion_id) + (f'#{ancla}' if ancla else '')


def _desviacion_o_404(conn, desviacion_id):
    d = sv.desviacion(conn, desviacion_id)
    if not d:
        conn.close()
        abort(404)
    return d


def _guardar_archivos(conn, lista, entidad, entidad_id, descripcion):
    for f in lista:
        if f and f.filename:
            archivos.guardar_archivo(conn, f, modulo='desviaciones', entidad=entidad, entidad_id=entidad_id,
                                     descripcion=descripcion, usuario=_usuario())


def _lotes_de_parametro(conn, texto):
    """'3,5' (ids) → filas de lote existentes."""
    ids = []
    for x in (texto or '').split(','):
        x = x.strip()
        if x.isdigit() and int(x) not in ids:
            ids.append(int(x))
    return [l for l in (inventario.lote(conn, i) for i in ids) if l]


def _opciones_lotes(conn):
    return [dict(r) for r in conn.execute(
        "SELECT l.id, l.numero_lote, l.estatus, m.codigo, m.nombre FROM lotes l JOIN materiales m ON m.id = l.material_id "
        "WHERE l.estatus <> 'RECHAZADO' ORDER BY l.id DESC LIMIT 300").fetchall()]


def _catalogos(conn):
    return {
        'materiales': conn.execute('SELECT id, codigo, nombre FROM materiales WHERE activo = 1 ORDER BY codigo').fetchall(),
        'equipos': conn.execute("SELECT id, codigo, nombre FROM equipos WHERE estatus <> 'BAJA' ORDER BY codigo").fetchall(),
        'proveedores': conn.execute('SELECT id, nombre FROM proveedores WHERE activo = 1 ORDER BY nombre').fetchall(),
        'opciones_lotes': _opciones_lotes(conn),
    }


def _prellenado(conn):
    """Datos que manda el módulo de origen por la URL (enlace reportar_desviacion)."""
    a = request.args
    origen = a.get('origen') if a.get('origen') in sv.ORIGENES else None
    volver = _volver_seguro(a.get('volver'), '')
    datos = {'origen_tipo': origen, 'origen_modulo': sv.ORIGENES.get(origen) if origen else None,
             'origen_id': a.get('origen_id', type=int) if origen else None,
             'origen_texto': (a.get('texto') or '')[:200] or None, 'documento_folio': (a.get('folio') or '')[:60] or None,
             'equipo_id': a.get('equipo_id', type=int), 'material_id': a.get('material_id', type=int),
             'origen_url': volver or None, 'fecha_detecto': sv.hoy()}
    return datos, _lotes_de_parametro(conn, a.get('lotes')), volver


# ---------------------------------------------------------------------------
# Lista
# ---------------------------------------------------------------------------

def _filtros():
    a = request.args
    return {'pestana': a.get('pestana') if a.get('pestana') in sv.PESTANAS else 'todas',
            'tipo': a.get('tipo') if a.get('tipo') in sv.TIPOS else None,
            'clasificacion': a.get('clasificacion') if a.get('clasificacion') in sv.CLASIFICACIONES else None,
            'area': a.get('area') or None,
            'categoria': a.get('categoria') if a.get('categoria') in sv.CATEGORIAS else None,
            'desde': a.get('desde') or None, 'hasta': a.get('hasta') or None, 'texto': (a.get('q') or '').strip() or None}


@bp.route('')
@login_required
@requiere_modulo('desviaciones')
def lista():
    filtros = _filtros()
    pagina = max(1, request.args.get('pagina', 1, type=int))
    conn = get_db_connection()
    renglones, total = sv.buscar(conn, **filtros, pagina=pagina, por_pagina=POR_PAGINA)
    indicadores = sv.indicadores(conn)
    conteo = sv.conteo_pestanas(conn)
    mis = len(sv.mis_acciones(conn, session.get('numero_empleado')))
    conn.close()
    paginas = max(1, (total + POR_PAGINA - 1) // POR_PAGINA)
    return render_page('desviaciones/lista.html', renglones=renglones, total=total, pagina=pagina, paginas=paginas,
                       filtros=filtros, indicadores=indicadores, conteo=conteo, mis=mis, sv=sv, hoy=sv.hoy())


@bp.route('/excel')
@login_required
@requiere_modulo('desviaciones')
def lista_excel():
    conn = get_db_connection()
    salida = reportes.lista(conn, _filtros())
    conn.close()
    return send_file(salida, as_attachment=True, download_name=f'desviaciones_{sv.hoy()}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ---------------------------------------------------------------------------
# Reportar (oficina y tablet)
# ---------------------------------------------------------------------------

def _datos_formulario():
    f = request.form
    datos = {c: f.get(c) for c in sv.CAMPOS_REPORTE if c in f}
    datos['posible_reaccion_adversa'] = 1 if f.get('posible_reaccion_adversa') else 0
    auditoria_5s = f.get('auditoria_5s', type=int)
    if auditoria_5s and f.get('tipo') == 'HALLAZGO':
        datos.update({'origen_modulo': 'cinco_s', 'origen_tipo': 'cinco_s', 'origen_id': auditoria_5s,
                      'origen_url': '/cinco_s'})
    return datos


def _reportar(conn, tipo, datos, lotes, adjuntos, descripcion_adjunto):
    did = sv.siguiente_id_desviacion(conn)
    fid = _firmar(conn, 'Realizó', 'dev_desviaciones', did, f"Reportó: {(datos.get('titulo') or datos.get('descripcion') or '')[:120]}")
    if not datos.get('detecto'):
        datos['detecto'] = conn.execute('SELECT nombre FROM firmas WHERE id = ?', (fid,)).fetchone()[0]
    nuevo = sv.reportar(conn, tipo=tipo, datos=datos, firma_id=fid, usuario=_usuario(), lotes=lotes, desviacion_id=did)
    _guardar_archivos(conn, adjuntos, 'dev_desviaciones', nuevo, descripcion_adjunto)
    return conn.execute('SELECT id, folio FROM dev_desviaciones WHERE id = ?', (nuevo,)).fetchone()


@bp.route('/nueva', methods=['GET', 'POST'])
@login_required
@requiere_modulo('desviaciones')
def nueva():
    conn = get_db_connection()
    if request.method == 'POST':
        tipo = request.form.get('tipo') or 'DESVIACION'
        datos = _datos_formulario()
        if tipo == 'HALLAZGO' and request.form.get('auditoria_5s', type=int) and not datos.get('auditoria'):
            a = conn.execute('SELECT area, fecha FROM cinco_s_auditorias WHERE id = ?',
                             (request.form.get('auditoria_5s', type=int),)).fetchone()
            if a:
                datos['auditoria'] = f"5S {a['area']} {a['fecha']}"
        try:
            ids = [int(x) for x in request.form.getlist('lote_id') if x.isdigit()]
            ids += [i for i in sv.resolver_lotes(conn, request.form.get('lotes_texto')) if i not in ids]
            fila = _reportar(conn, tipo, datos, ids, request.files.getlist('archivos'), 'Evidencia del reporte')
            conn.commit()
            flash(f"Quedó reportada con el folio {fila['folio']}.", 'success')
            conn.close()
            return hx_redirect(_detalle_url(fila['id']))
        except ERRORES as e:
            conn.rollback()
            flash(str(e), 'error')
        except Exception:
            conn.rollback()
            current_app.logger.exception('Desviaciones: falló reportar')
            flash('No se pudo guardar el reporte. Avisa a sistemas.', 'error')
        datos = {**request.form.to_dict(), **datos}
        prellenados = _lotes_de_parametro(conn, ','.join(request.form.getlist('lote_id')))
        volver = _volver_seguro(request.form.get('origen_url'), '')
    else:
        tipo = request.args.get('tipo') if request.args.get('tipo') in sv.TIPOS else 'DESVIACION'
        datos, prellenados, volver = _prellenado(conn)
    contexto = _catalogos(conn)
    auditorias = conn.execute('SELECT id, area, fecha, calificacion FROM cinco_s_auditorias '
                              'ORDER BY fecha DESC, id DESC LIMIT 50').fetchall()
    conn.close()
    return render_page('desviaciones/nueva.html', tipo=tipo, datos=datos, prellenados=prellenados, volver=volver,
                       auditorias=auditorias, sv=sv, hoy=sv.hoy(), **contexto)


@bp.route('/planta/nueva', methods=['GET', 'POST'])
@login_required
@requiere_modulo('desviaciones_planta')
def planta_nueva():
    conn = get_db_connection()
    if request.method == 'POST':
        volver = _volver_seguro(request.form.get('volver'), url_for('nucleo.planta'))
        datos = {c: request.form.get(c) for c in ('descripcion', 'area', 'origen_tipo', 'origen_modulo', 'origen_id',
                                                   'origen_texto', 'origen_url', 'documento_folio', 'equipo_id',
                                                   'material_id')}
        datos['fecha_detecto'] = sv.hoy()
        try:
            ids = [int(x) for x in request.form.getlist('lote_id') if x.isdigit()]
            ids += [i for i in sv.resolver_lotes(conn, request.form.get('lotes_texto')) if i not in ids]
            fila = _reportar(conn, 'DESVIACION', datos, ids, [request.files.get('foto')], 'Foto del reporte (tablet)')
            conn.commit()
            flash(f"Listo: quedó reportada con el folio {fila['folio']}. Calidad la va a revisar.", 'success')
            conn.close()
            return hx_redirect(volver)
        except ERRORES as e:
            conn.rollback()
            flash(str(e), 'error')
        except Exception:
            conn.rollback()
            current_app.logger.exception('Desviaciones: falló reportar en planta')
            flash('No se pudo guardar el reporte. Avisa a tu supervisor.', 'error')
        datos = {**request.form.to_dict(), **datos}
        prellenados = _lotes_de_parametro(conn, ','.join(request.form.getlist('lote_id')))
    else:
        datos, prellenados, volver = _prellenado(conn)
        volver = volver or url_for('nucleo.planta')
    conn.close()
    return full_page('desviaciones/planta_nueva.html', datos=datos, prellenados=prellenados, volver=volver,
                     areas=sv.AREAS)


# ---------------------------------------------------------------------------
# Detalle
# ---------------------------------------------------------------------------

def _contexto_detalle(conn, d):
    numero = session.get('numero_empleado')
    facultades = session.get('facultades') or []
    acciones = sv.acciones(conn, d['id'])
    for a in acciones:
        a['capacitacion'] = sv.avance_capacitacion(conn, a['id'])
        a['personas'] = a['capacitacion']['personas'] if a['capacitacion'] else []
        a['evidencias'] = archivos.archivos_de(conn, 'dev_acciones', a['id'])
    sin_agregar = [inventario.lote(conn, i) for i in sv.lotes_sin_agregar(conn, d)]
    gestor = sv.FACULTAD in facultades
    return {
        'd': d, 'lotes': sv.lotes(conn, d['id']), 'sin_agregar': [l for l in sin_agregar if l],
        'alcance': sv.alcance(conn, d['id']), 'acciones': acciones, 'parecidas': sv.parecidas(conn, d),
        'relacionadas': sv.relacionadas(conn, d['id']), 'historial': sv.historial(conn, d['id']),
        'firmas': sv.firmas_de(conn, d), 'adjuntos': archivos.archivos_de(conn, 'dev_desviaciones', d['id']),
        'faltan_envio': sv.faltantes_envio(conn, d['id']) if d['estatus'] == 'EN_INVESTIGACION' else [],
        'faltan_cierre': sv.faltantes_cierre(conn, d['id']) if d['estatus'] == 'EN_ACCIONES' else [],
        'usuarios': sv.usuarios_con_acceso(conn),
        'empleados': conn.execute('SELECT numero_empleado, nombre_completo FROM usuarios WHERE estatus = 1 '
                                  'ORDER BY nombre_completo').fetchall(),
        'cursos': conn.execute('SELECT id, nombre FROM cursos WHERE activo = 1 ORDER BY orden, nombre').fetchall(),
        'opciones_lotes': _opciones_lotes(conn),
        'gestor': gestor, 'es_responsable': str(d['responsable'] or '') == str(numero or ''),
        'puede_investigar': sv.puede_investigar(d, numero, facultades),
        'es_calidad': 'calidad' in (session.get('roles') or []),
        'numero': numero, 'hoy': sv.hoy(), 'sv': sv,
        'vence_sugerido': {c: sv.sumar_dias(sv.hoy(), sv.dias_investigacion(conn, c)) for c in sv.CLASIFICACIONES},
        'origen_url': d['origen_url'] if d['origen_url'] and d['origen_url'].startswith('/') else None,
    }


@bp.route('/<int:desviacion_id>')
@login_required
@requiere_modulo('desviaciones')
def detalle(desviacion_id):
    conn = get_db_connection()
    d = _desviacion_o_404(conn, desviacion_id)
    contexto = _contexto_detalle(conn, d)
    conn.close()
    return render_page('desviaciones/detalle.html', **contexto)


@bp.route('/<int:desviacion_id>/imprimir')
@login_required
@requiere_modulo('desviaciones')
def imprimir(desviacion_id):
    conn = get_db_connection()
    d = _desviacion_o_404(conn, desviacion_id)
    contexto = _contexto_detalle(conn, d)
    codigo = parametro('desviaciones.codigo_formato', conn)
    conn.close()
    return full_page('desviaciones/imprimir.html', codigo_formato=codigo,
                     impreso=datetime.now().strftime('%Y-%m-%d %H:%M'), **contexto)


@bp.route('/<int:desviacion_id>/alcance.xlsx')
@login_required
@requiere_modulo('desviaciones')
def alcance_excel(desviacion_id):
    conn = get_db_connection()
    d = _desviacion_o_404(conn, desviacion_id)
    salida = reportes.alcance(conn, desviacion_id)
    conn.close()
    return send_file(salida, as_attachment=True, download_name=f"alcance_{d['folio']}.xlsx",
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# --- Clasificar --------------------------------------------------------------

@bp.route('/<int:desviacion_id>/clasificar', methods=['POST'])
@login_required
@requiere_modulo('desviaciones')
@requiere_facultad('gestionar_desviaciones')
def clasificar(desviacion_id):
    f = request.form

    def accion(conn):
        ids = [int(x) for x in f.getlist('lote_id') if x.isdigit()]
        ids += [i for i in sv.resolver_lotes(conn, f.get('lotes_texto')) if i not in ids]
        fid = _firmar(conn, 'Aprobó', 'dev_desviaciones', desviacion_id,
                      f"Clasificó {f.get('clasificacion')}", facultad=sv.FACULTAD, critica=True)
        sv.clasificar(conn, desviacion_id, clasificacion=f.get('clasificacion'), responsable=f.get('responsable'),
                      vence=f.get('vence'), firma_id=fid, usuario=_usuario(), lotes=ids)
    return _ejecutar('clasificar', _detalle_url(desviacion_id), accion, 'Desviación clasificada; ya está en investigación.')


@bp.route('/<int:desviacion_id>/cerrar-menor', methods=['POST'])
@login_required
@requiere_modulo('desviaciones')
@requiere_facultad('gestionar_desviaciones')
def cerrar_menor(desviacion_id):
    def accion(conn):
        fid = _firmar(conn, 'Cerró', 'dev_desviaciones', desviacion_id, 'Cerró al clasificar (menor)',
                      facultad=sv.FACULTAD, critica=True)
        sv.cerrar_al_clasificar(conn, desviacion_id, justificacion=request.form.get('justificacion'),
                                firma_id=fid, usuario=_usuario())
    return _ejecutar('cerrar la desviación', _detalle_url(desviacion_id), accion, 'Desviación menor cerrada con la corrección.')


# --- Lotes -------------------------------------------------------------------

@bp.route('/<int:desviacion_id>/lotes', methods=['POST'])
@login_required
@requiere_modulo('desviaciones')
def agregar_lotes(desviacion_id):
    def accion(conn):
        d = sv.desviacion(conn, desviacion_id)
        if d and not (tiene_facultad(sv.FACULTAD) or str(d['responsable'] or '') == str(session.get('numero_empleado'))):
            raise sv.ErrorDesviacion('Solo el responsable o quien gestiona desviaciones agrega lotes.')
        ids = [int(x) for x in request.form.getlist('lote_id') if x.isdigit()]
        ids += [i for i in sv.resolver_lotes(conn, request.form.get('lotes_texto')) if i not in ids]
        if not ids:
            raise sv.ErrorDesviacion('Escribe o elige al menos un lote.')
        return sum(1 for i in ids if sv.ligar_lote(conn, desviacion_id, i, _usuario()))
    return _ejecutar('agregar los lotes', _detalle_url(desviacion_id, 'lotes'), accion,
                     lambda n: f'{n} lote{"" if n == 1 else "s"} agregado{"" if n == 1 else "s"}.')


@bp.route('/<int:desviacion_id>/lotes/alcance', methods=['POST'])
@login_required
@requiere_modulo('desviaciones')
def agregar_alcance(desviacion_id):
    def accion(conn):
        d = sv.desviacion(conn, desviacion_id)
        if d and not (tiene_facultad(sv.FACULTAD) or str(d['responsable'] or '') == str(session.get('numero_empleado'))):
            raise sv.ErrorDesviacion('Solo el responsable o quien gestiona desviaciones agrega lotes.')
        return sv.agregar_alcance(conn, desviacion_id, _usuario())
    return _ejecutar('agregar el alcance', _detalle_url(desviacion_id, 'lotes'), accion,
                     lambda n: f'{n} lote{"" if n == 1 else "s"} del alcance agregado{"" if n == 1 else "s"}.')


@bp.route('/<int:desviacion_id>/lotes/<int:lote_id>/retener', methods=['POST'])
@login_required
@requiere_modulo('desviaciones')
@requiere_facultad('gestionar_desviaciones')
def retener(desviacion_id, lote_id):
    def accion(conn):
        fid = _firmar(conn, 'Retuvo', 'lotes', lote_id, f'Retuvo el lote por desviación {desviacion_id}',
                      facultad=sv.FACULTAD, critica=True)
        return sv.retener(conn, desviacion_id, lote_id, firma_id=fid, usuario=_usuario())
    return _ejecutar('retener el lote', _detalle_url(desviacion_id, 'lotes'), accion,
                     lambda ids: 'Lote retenido.' if len(ids) == 1 else f'Lote retenido con sus {len(ids) - 1} presentaciones.')


@bp.route('/<int:desviacion_id>/lotes/<int:lote_id>/evaluar', methods=['POST'])
@login_required
@requiere_modulo('desviaciones')
def evaluar_lote(desviacion_id, lote_id):
    def accion(conn):
        d = sv.desviacion(conn, desviacion_id)
        if d and not (tiene_facultad(sv.FACULTAD) or str(d['responsable'] or '') == str(session.get('numero_empleado'))):
            raise sv.ErrorDesviacion('El impacto lo evalúa el responsable de investigar.')
        sv.evaluar_lote(conn, desviacion_id, lote_id, impacto=request.form.get('impacto'),
                        justificacion=request.form.get('justificacion'), usuario=_usuario())
    return _ejecutar('evaluar el lote', _detalle_url(desviacion_id, 'lotes'), accion, 'Evaluación guardada.')


# --- Investigación -----------------------------------------------------------

def _exigir_investigador(conn, desviacion_id):
    d = sv.desviacion(conn, desviacion_id)
    if d and not sv.puede_investigar(d, session.get('numero_empleado'), session.get('facultades')):
        raise sv.ErrorDesviacion('La investigación la llena el responsable asignado mientras está en investigación.')
    return d


@bp.route('/<int:desviacion_id>/investigacion', methods=['POST'])
@login_required
@requiere_modulo('desviaciones')
def guardar_investigacion(desviacion_id):
    def accion(conn):
        _exigir_investigador(conn, desviacion_id)
        datos = {c: request.form.get(c) for c in sv.CAMPOS_INVESTIGACION if c in request.form}
        datos['descripcion_ampliada'] = request.form.get('descripcion_ampliada')
        sv.guardar_investigacion(conn, desviacion_id, datos, _usuario())
    return _ejecutar('guardar la investigación', _detalle_url(desviacion_id, 'investigacion'), accion,
                     'Investigación guardada.')


@bp.route('/<int:desviacion_id>/enviar', methods=['POST'])
@login_required
@requiere_modulo('desviaciones')
def enviar(desviacion_id):
    def accion(conn):
        _exigir_investigador(conn, desviacion_id)
        fid = _firmar(conn, 'Realizó', 'dev_desviaciones', desviacion_id, 'Investigación enviada a aprobación',
                      critica=True)
        sv.enviar(conn, desviacion_id, firma_id=fid, usuario=_usuario())
    return _ejecutar('enviar la investigación', _detalle_url(desviacion_id), accion, 'Investigación enviada a aprobación.')


@bp.route('/<int:desviacion_id>/aprobar', methods=['POST'])
@login_required
@requiere_modulo('desviaciones')
@requiere_facultad('gestionar_desviaciones')
def aprobar(desviacion_id):
    def accion(conn):
        d = sv.desviacion(conn, desviacion_id)
        investigo = conn.execute('SELECT numero_empleado FROM firmas WHERE id = ?',
                                 (d['investigo_firma_id'],)).fetchone() if d and d['investigo_firma_id'] else None
        fid = _firmar(conn, 'Aprobó', 'dev_desviaciones', desviacion_id, 'Aprobó la investigación y las acciones',
                      facultad=sv.FACULTAD, critica=True, distinto_de=investigo[0] if investigo else None)
        sv.aprobar(conn, desviacion_id, firma_id=fid, usuario=_usuario())
    return _ejecutar('aprobar', _detalle_url(desviacion_id), accion, 'Investigación aprobada; las acciones están en curso.')


@bp.route('/<int:desviacion_id>/regresar', methods=['POST'])
@login_required
@requiere_modulo('desviaciones')
@requiere_facultad('gestionar_desviaciones')
def regresar(desviacion_id):
    def accion(conn):
        sv.regresar(conn, desviacion_id, comentarios=request.form.get('comentarios'), usuario=_usuario())
    return _ejecutar('regresar la investigación', _detalle_url(desviacion_id), accion,
                     'Investigación regresada con comentarios.')


@bp.route('/<int:desviacion_id>/relacionar', methods=['POST'])
@login_required
@requiere_modulo('desviaciones')
def relacionar(desviacion_id):
    def accion(conn):
        sv.relacionar(conn, desviacion_id, request.form.get('relacionada_id', type=int),
                      request.form.get('motivo') or 'Parecida', _usuario())
    return _ejecutar('ligar la desviación', _detalle_url(desviacion_id, 'investigacion'), accion, 'Desviaciones ligadas.')


# --- Acciones ----------------------------------------------------------------

@bp.route('/<int:desviacion_id>/acciones', methods=['POST'])
@login_required
@requiere_modulo('desviaciones')
def agregar_accion(desviacion_id):
    f = request.form

    def accion(conn):
        d = sv.desviacion(conn, desviacion_id)
        if d and not (sv.puede_investigar(d, session.get('numero_empleado'), session.get('facultades'))
                      or (d['estatus'] == 'EN_ACCIONES' and tiene_facultad(sv.FACULTAD))):
            raise sv.ErrorDesviacion('Las acciones las propone el responsable al investigar; después, quien gestiona desviaciones.')
        return sv.agregar_accion(conn, desviacion_id, tipo=f.get('tipo'), descripcion=f.get('descripcion'),
                                 responsable=f.get('responsable'), fecha_compromiso=f.get('fecha_compromiso'),
                                 curso_id=f.get('curso_id') if f.get('es_capacitacion') else None,
                                 personas=f.getlist('personas') if f.get('es_capacitacion') else None,
                                 eficacia_fecha=f.get('eficacia_fecha'), eficacia_criterio=f.get('eficacia_criterio'),
                                 usuario=_usuario())
    return _ejecutar('agregar la acción', _detalle_url(desviacion_id, 'acciones'), accion, 'Acción agregada.')


def _destino_accion(accion_id):
    destino = _volver_seguro(request.form.get('volver'), '')
    if destino:
        return destino
    conn = get_db_connection()
    fila = conn.execute('SELECT desviacion_id FROM dev_acciones WHERE id = ?', (accion_id,)).fetchone()
    conn.close()
    return _detalle_url(fila[0], 'acciones') if fila else url_for('desviaciones.mis_acciones')


@bp.route('/acciones/<int:accion_id>/reprogramar', methods=['POST'])
@login_required
@requiere_modulo('desviaciones')
@requiere_facultad('gestionar_desviaciones')
def reprogramar(accion_id):
    def accion(conn):
        fid = _firmar(conn, 'Autorizó', 'dev_acciones', accion_id, 'Cambió fecha o responsable de la acción',
                      facultad=sv.FACULTAD, critica=True)
        sv.reprogramar(conn, accion_id, motivo=request.form.get('motivo'), firma_id=fid, usuario=_usuario(),
                       fecha_compromiso=request.form.get('fecha_compromiso'), responsable=request.form.get('responsable'))
    return _ejecutar('cambiar la acción', _destino_accion(accion_id), accion, 'Acción actualizada.')


@bp.route('/acciones/<int:accion_id>/hecha', methods=['POST'])
@login_required
@requiere_modulo('desviaciones')
def marcar_hecha(accion_id):
    def accion(conn):
        fid = _firmar(conn, 'Realizó', 'dev_acciones', accion_id, 'Marcó la acción como hecha')
        sv.marcar_hecha(conn, accion_id, como_se_hizo=request.form.get('como_se_hizo'), firma_id=fid, usuario=_usuario())
        _guardar_archivos(conn, request.files.getlist('evidencia'), 'dev_acciones', accion_id, 'Evidencia de la acción')
    return _ejecutar('marcar la acción', _destino_accion(accion_id), accion, 'Acción marcada como hecha; falta que calidad la verifique.')


@bp.route('/acciones/<int:accion_id>/verificar', methods=['POST'])
@login_required
@requiere_modulo('desviaciones')
def verificar(accion_id):
    def accion(conn):
        a = sv.accion(conn, accion_id)
        hizo = conn.execute('SELECT numero_empleado FROM firmas WHERE id = ?',
                            (a['hecha_firma_id'],)).fetchone() if a and a['hecha_firma_id'] else None
        prohibidos = [x for x in (a['responsable'] if a else None, hizo[0] if hizo else None) if x]
        fid = _firmar(conn, 'Verificó', 'dev_acciones', accion_id, 'Verificó la acción', roles=['calidad'],
                      distinto_de=prohibidos or None)
        sv.verificar(conn, accion_id, firma_id=fid, usuario=_usuario(), comentario=request.form.get('comentario'))
    return _ejecutar('verificar la acción', _destino_accion(accion_id), accion, 'Acción verificada.')


@bp.route('/acciones/<int:accion_id>/cancelar', methods=['POST'])
@login_required
@requiere_modulo('desviaciones')
@requiere_facultad('gestionar_desviaciones')
def cancelar_accion(accion_id):
    def accion(conn):
        fid = _firmar(conn, 'Anuló', 'dev_acciones', accion_id, 'Canceló la acción', facultad=sv.FACULTAD, critica=True)
        sv.cancelar_accion(conn, accion_id, motivo=request.form.get('motivo'), firma_id=fid, usuario=_usuario())
    return _ejecutar('cancelar la acción', _destino_accion(accion_id), accion, 'Acción cancelada.')


@bp.route('/acciones/<int:accion_id>/eficacia', methods=['POST'])
@login_required
@requiere_modulo('desviaciones')
def eficacia(accion_id):
    def accion(conn):
        nueva_id = sv.siguiente_id_desviacion(conn)
        fid = _firmar(conn, 'Verificó', 'dev_acciones', accion_id, 'Revisó la eficacia de la acción', roles=['calidad'])
        nueva = sv.revisar_eficacia(conn, accion_id, eficaz=request.form.get('eficaz'),
                                    comentario=request.form.get('comentario'), firma_id=fid, usuario=_usuario(),
                                    nueva_id=nueva_id)
        return conn.execute('SELECT folio FROM dev_desviaciones WHERE id = ?', (nueva,)).fetchone()[0] if nueva else None
    return _ejecutar('revisar la eficacia', _destino_accion(accion_id), accion,
                     lambda folio: f'La acción no funcionó: se abrió {folio}, ligada a esta.' if folio
                     else 'Eficacia registrada: la acción funcionó.')


# --- Queja, cierre, anulación y comentarios ----------------------------------

@bp.route('/<int:desviacion_id>/respuesta', methods=['POST'])
@login_required
@requiere_modulo('desviaciones')
def respuesta(desviacion_id):
    def accion(conn):
        d = sv.desviacion(conn, desviacion_id)
        if d and not (tiene_facultad(sv.FACULTAD) or str(d['responsable'] or '') == str(session.get('numero_empleado'))):
            raise sv.ErrorDesviacion('La respuesta al cliente la registra el responsable o quien gestiona desviaciones.')
        sv.registrar_respuesta(conn, desviacion_id, respuesta=request.form.get('respuesta_cliente'),
                               fecha=request.form.get('fecha_respuesta'), usuario=_usuario())
    return _ejecutar('guardar la respuesta', _detalle_url(desviacion_id, 'cierre'), accion, 'Respuesta al cliente registrada.')


@bp.route('/<int:desviacion_id>/cerrar', methods=['POST'])
@login_required
@requiere_modulo('desviaciones')
@requiere_facultad('gestionar_desviaciones')
def cerrar(desviacion_id):
    def accion(conn):
        fid = _firmar(conn, 'Cerró', 'dev_desviaciones', desviacion_id, 'Cerró la desviación', facultad=sv.FACULTAD,
                      critica=True)
        sv.cerrar(conn, desviacion_id, conclusion=request.form.get('conclusion'), firma_id=fid, usuario=_usuario())
    return _ejecutar('cerrar', _detalle_url(desviacion_id), accion, 'Desviación cerrada.')


@bp.route('/<int:desviacion_id>/anular', methods=['POST'])
@login_required
@requiere_modulo('desviaciones')
@requiere_facultad('gestionar_desviaciones')
def anular(desviacion_id):
    def accion(conn):
        fid = _firmar(conn, 'Anuló', 'dev_desviaciones', desviacion_id, 'Anuló la desviación', facultad=sv.FACULTAD,
                      critica=True)
        sv.anular(conn, desviacion_id, motivo=request.form.get('motivo'), firma_id=fid, usuario=_usuario())
    return _ejecutar('anular', _detalle_url(desviacion_id), accion, 'Desviación anulada.')


@bp.route('/<int:desviacion_id>/comentario', methods=['POST'])
@login_required
@requiere_modulo('desviaciones')
def comentario(desviacion_id):
    def accion(conn):
        sv.comentar(conn, desviacion_id, request.form.get('comentario'), _usuario())
        _guardar_archivos(conn, request.files.getlist('archivos'), 'dev_desviaciones', desviacion_id,
                          'Complemento del reporte')
    return _ejecutar('guardar el comentario', _detalle_url(desviacion_id, 'historial'), accion, 'Comentario agregado.')


# ---------------------------------------------------------------------------
# Mis acciones y tendencias
# ---------------------------------------------------------------------------

@bp.route('/acciones')
@login_required
@requiere_modulo('desviaciones')
def mis_acciones():
    numero = session.get('numero_empleado')
    conn = get_db_connection()
    mias = sv.mis_acciones(conn, numero)
    es_calidad = 'calidad' in (session.get('roles') or [])
    por_verificar = sv.acciones_por_verificar(conn, excepto=numero) if es_calidad else []
    eficacias = sv.eficacias_por_revisar(conn) if es_calidad else []
    conn.close()
    return render_page('desviaciones/acciones.html', mias=mias, por_verificar=por_verificar, eficacias=eficacias,
                       es_calidad=es_calidad, numero=numero, hoy=sv.hoy(), volver=url_for('desviaciones.mis_acciones'))


@bp.route('/tendencias')
@login_required
@requiere_modulo('desviaciones')
def tendencias():
    conn = get_db_connection()
    t = sv.tendencias(conn)
    conn.close()
    return render_page('desviaciones/tendencias.html', t=t, sv=sv)


@bp.route('/tendencias/excel')
@login_required
@requiere_modulo('desviaciones')
def tendencias_excel():
    conn = get_db_connection()
    salida = reportes.tendencias(conn)
    conn.close()
    return send_file(salida, as_attachment=True, download_name=f'tendencias_desviaciones_{sv.hoy()}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
