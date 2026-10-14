"""Pantallas de la tablet (pantalla completa, botones grandes) bajo /ordenes/planta."""
from flask import abort, request, url_for

from nucleo import registro
from nucleo.auth import login_required, requiere_modulo
from nucleo.db import get_db_connection
from nucleo.vistas import full_page

from .blueprint import ROLES_PLANTA, ROLES_SUPERVISOR, accion, bp, firmar, numero_firmante, usuario
from .servicios import ErrorOrdenes
from .servicios import ordenes as srv_ordenes
from .servicios import planta as srv_planta


def _url_orden(orden_id):
    return url_for('ordenes.planta_orden', orden_id=orden_id)


def _cargar(orden_id):
    """(conn, estado) o abort(404)."""
    conn = get_db_connection()
    if not srv_ordenes.orden_o_none(conn, orden_id):
        conn.close()
        abort(404)
    return conn, srv_ordenes.estado(conn, orden_id)


def _desviacion(origen, origen_id, o, texto, equipo_id=None, lotes=None):
    """URL del formulario de desviación ya lleno (None si el módulo no está instalado)."""
    lotes = lotes if lotes is not None else [x for x in [o['granel_lote_id']] if x]
    return registro.enlace('reportar_desviacion', origen=origen, origen_id=origen_id,
                           texto=f'{o["folio"]} · {texto}', folio=o['folio'], lotes=lotes, equipo_id=equipo_id,
                           volver=_url_orden(o['id']), planta=True)


def _verifica(prefijo, realizo_prefijo='realizo'):
    """Si el formulario trae la firma de quien verifica."""
    return bool(numero_firmante(prefijo)) or bool(request.form.get(f'{prefijo}_pin'))


@bp.route('/planta')
@login_required
@requiere_modulo('ordenes_planta')
def planta_lista():
    conn = get_db_connection()
    ordenes = []
    for o in conn.execute("SELECT id FROM op_ordenes WHERE estatus IN ('EMITIDA','EN_PROCESO') "
                          'ORDER BY COALESCE(fecha_programada, fecha_emision), id LIMIT 100').fetchall():
        e = srv_ordenes.estado(conn, o['id'])
        ordenes.append({'o': e['orden'], 'avance': e['avance'], 'bloques': e['bloques']})
    conn.close()
    return full_page('ordenes/planta_lista.html', ordenes=ordenes)


@bp.route('/planta/<int:orden_id>')
@login_required
@requiere_modulo('ordenes_planta')
def planta_orden(orden_id):
    conn, e = _cargar(orden_id)
    o = e['orden']
    reportar = _desviacion('orden', o['id'], o, 'Desviación en la orden')
    conn.close()
    return full_page('ordenes/planta_orden.html', e=e, o=o, reportar=reportar)


# --- Despeje -----------------------------------------------------------------------
@bp.route('/planta/<int:orden_id>/despeje', methods=['GET', 'POST'])
@login_required
@requiere_modulo('ordenes_planta')
def planta_despeje(orden_id):
    momento = request.values.get('momento') or 'Fabricación'
    if momento not in ('Fabricación', 'Envasado'):
        momento = 'Fabricación'
    if request.method == 'POST':
        def hacer(conn):
            o = srv_ordenes.orden(conn, orden_id)
            r = firmar(conn, 'realizo', 'Realizó', 'op_ordenes', orden_id, f'Despeje de {momento.lower()} {o["folio"]}',
                       roles=ROLES_PLANTA)
            v = firmar(conn, 'verifico', 'Verificó', 'op_ordenes', orden_id,
                       f'Verificó despeje de {momento.lower()} {o["folio"]}', roles=ROLES_SUPERVISOR,
                       distinto_de=numero_firmante('realizo'))
            srv_planta.despeje(conn, orden_id, momento=momento, checks=request.form, realizo_firma_id=r,
                               verifico_firma_id=v, observaciones=request.form.get('observaciones'))
        return accion(hacer, f'Despeje de {momento.lower()} registrado.', _url_orden(orden_id),
                      url_for('ordenes.planta_despeje', orden_id=orden_id, momento=momento))
    conn, e = _cargar(orden_id)
    conn.close()
    return full_page('ordenes/planta_despeje.html', e=e, o=e['orden'], momento=momento,
                     checks=srv_planta.CHECKS_DESPEJE, hecho=e['despejes'].get(momento))


# --- Pesar / surtir -------------------------------------------------------------------
@bp.route('/planta/<int:orden_id>/pesar/<int:componente_id>', methods=['GET', 'POST'])
@login_required
@requiere_modulo('ordenes_planta')
def planta_pesar(orden_id, componente_id):
    url = url_for('ordenes.planta_pesar', orden_id=orden_id, componente_id=componente_id)
    if request.method == 'POST':
        def hacer(conn):
            o = srv_ordenes.orden(conn, orden_id)
            c = srv_planta.componente(conn, orden_id, componente_id)
            que = 'Pesada' if c['tipo'] == 'MP' else 'Surtido'
            r = firmar(conn, 'realizo', 'Realizó', 'op_ordenes', orden_id, f'{que} {c["codigo"]} {o["folio"]}',
                       roles=ROLES_PLANTA)
            v = None
            if _verifica('verifico'):
                v = firmar(conn, 'verifico', 'Verificó', 'op_ordenes', orden_id,
                           f'Verificó {que.lower()} {c["codigo"]} {o["folio"]}', roles=ROLES_PLANTA,
                           distinto_de=numero_firmante('realizo'))
            f = request.form
            srv_planta.pesar(conn, orden_id, componente_id, lote_id=f.get('lote_id', type=int), cantidad=f.get('cantidad'),
                             equipo_id=f.get('equipo_id', type=int), motivo_otro_lote=f.get('motivo_otro_lote'),
                             usuario=usuario(), realizo_firma_id=r, verifico_firma_id=v,
                             registro_posterior=f.get('registro_posterior'),
                             motivo_registro_posterior=f.get('motivo_registro_posterior'), hora_real=f.get('hora_real'))
        return accion(hacer, 'Pesada registrada.', url, url)
    conn, e = _cargar(orden_id)
    c = next((x for x in e['componentes'] if x['id'] == componente_id), None)
    if not c:
        conn.close()
        abort(404)
    lotes = srv_planta.lotes_para(conn, c)
    equipos = srv_planta.equipos_para(conn, basculas=True)
    for q in equipos:
        q['verificar_url'] = registro.enlace('verificar_equipo', equipo_id=q['id']) if not q['apto'] else None
    pesadas = conn.execute(
        'SELECT p.*, l.numero_lote, eq.codigo AS equipo_codigo, fr.nombre AS realizo, fv.nombre AS verifico '
        'FROM op_pesadas p JOIN lotes l ON l.id = p.lote_id LEFT JOIN equipos eq ON eq.id = p.equipo_id '
        'LEFT JOIN firmas fr ON fr.id = p.realizo_firma_id LEFT JOIN firmas fv ON fv.id = p.verifico_firma_id '
        'WHERE p.componente_id = ? ORDER BY p.id', (componente_id,)).fetchall()
    verificar = srv_planta.verificacion_obligatoria(conn, c)
    conn.close()
    momento = 'Fabricación' if c['tipo'] == 'MP' else 'Envasado'
    return full_page('ordenes/planta_pesar.html', e=e, o=e['orden'], c=c, lotes=lotes, equipos=equipos,
                     pesadas=pesadas, verificar=verificar, falta_despeje=momento not in e['despejes'], momento=momento)


@bp.route('/planta/<int:orden_id>/devolver/<int:componente_id>', methods=['POST'])
@login_required
@requiere_modulo('ordenes_planta')
def planta_devolver(orden_id, componente_id):
    url = url_for('ordenes.planta_pesar', orden_id=orden_id, componente_id=componente_id)

    def hacer(conn):
        o = srv_ordenes.orden(conn, orden_id)
        fid = firmar(conn, 'realizo', 'Realizó', 'op_ordenes', orden_id, f'Devolución a almacén {o["folio"]}',
                     roles=ROLES_PLANTA)
        srv_planta.devolver(conn, orden_id, componente_id, lote_id=request.form.get('lote_id', type=int),
                            cantidad=request.form.get('cantidad'), usuario=usuario(), firma_id=fid,
                            motivo=request.form.get('motivo'))
    return accion(hacer, 'Devolución registrada.', url, url)


# --- Pasos ---------------------------------------------------------------------------
@bp.route('/planta/<int:orden_id>/paso/<int:paso_id>')
@login_required
@requiere_modulo('ordenes_planta')
def planta_paso(orden_id, paso_id):
    conn, e = _cargar(orden_id)
    p = next((x for x in e['pasos'] if x['id'] == paso_id), None)
    if not p:
        conn.close()
        abort(404)
    bloqueo = srv_planta.bloqueo_paso(conn, orden_id, p, e) if p['estatus'] != 'Hecho' else None
    equipos = srv_planta.equipos_para(conn, tipo=p['equipo_tipo']) if p['equipo_tipo'] else []
    firmas = {f['id']: dict(f) for f in conn.execute('SELECT * FROM firmas WHERE id IN (?, ?, ?)', (
        p['realizo_firma_id'] or 0, p['verifico_firma_id'] or 0, p['justificacion_firma_id'] or 0)).fetchall()}
    o = e['orden']
    reportar = _desviacion('orden_paso', p['id'], o, f'Paso {p["numero"]}: valor fuera de rango',
                           equipo_id=p['equipo_id']) if p['fuera_de_rango'] else None
    tiene_desv = bool(srv_ordenes.desviaciones(conn, 'orden_paso', p['id'])) if p['fuera_de_rango'] else False
    siguiente = next((x for x in e['pasos'] if x['estatus'] != 'Hecho' and x['id'] != p['id']), None)
    conn.close()
    return full_page('ordenes/planta_paso.html', e=e, o=o, p=p, bloqueo=bloqueo, equipos=equipos, firmas=firmas,
                     reportar=reportar, tiene_desv=tiene_desv, siguiente=siguiente,
                     desviaciones_instalado=srv_ordenes.desviaciones_instalado())


@bp.route('/planta/<int:orden_id>/paso/<int:paso_id>/iniciar', methods=['POST'])
@login_required
@requiere_modulo('ordenes_planta')
def planta_paso_iniciar(orden_id, paso_id):
    url = url_for('ordenes.planta_paso', orden_id=orden_id, paso_id=paso_id)
    return accion(lambda conn: srv_planta.iniciar_paso(conn, orden_id, paso_id), None, url, url)


@bp.route('/planta/<int:orden_id>/paso/<int:paso_id>/firmar', methods=['POST'])
@login_required
@requiere_modulo('ordenes_planta')
def planta_paso_firmar(orden_id, paso_id):
    url = url_for('ordenes.planta_paso', orden_id=orden_id, paso_id=paso_id)

    def hacer(conn):
        o = srv_ordenes.orden(conn, orden_id)
        p = srv_planta.paso(conn, orden_id, paso_id)
        r = firmar(conn, 'realizo', 'Realizó', 'op_orden_pasos', paso_id, f'Paso {p["numero"]} {o["folio"]}',
                   roles=ROLES_PLANTA)
        v = None
        if _verifica('verifico'):
            v = firmar(conn, 'verifico', 'Verificó', 'op_orden_pasos', paso_id, f'Verificó paso {p["numero"]} {o["folio"]}',
                       roles=ROLES_PLANTA, distinto_de=numero_firmante('realizo'))
        f = request.form
        valores = {int(k[6:]): v for k, v in f.items() if k.startswith('valor_') and k[6:].isdigit()}
        res = srv_planta.firmar_paso(conn, orden_id, paso_id, valores=valores, realizo_firma_id=r, verifico_firma_id=v,
                                     equipo_id=f.get('equipo_id', type=int), comentario=f.get('comentario'),
                                     registro_posterior=f.get('registro_posterior'),
                                     motivo_registro_posterior=f.get('motivo_registro_posterior'),
                                     inicio_real=f.get('inicio_real'), fin_real=f.get('fin_real'))
        if res['fuera_de_rango']:
            res['avisos'].insert(0, 'Un valor salió fuera de rango: el paso quedó marcado y pide desviación.')
        return res
    return accion(hacer, 'Paso firmado.', url, url)


# --- Granel ---------------------------------------------------------------------------
@bp.route('/planta/<int:orden_id>/granel', methods=['GET', 'POST'])
@login_required
@requiere_modulo('ordenes_planta')
def planta_granel(orden_id):
    url = url_for('ordenes.planta_granel', orden_id=orden_id)
    if request.method == 'POST':
        def hacer(conn):
            o = srv_ordenes.orden(conn, orden_id)
            fid = firmar(conn, 'realizo', 'Realizó', 'op_ordenes', orden_id, f'Granel obtenido {o["folio"]}',
                         roles=ROLES_SUPERVISOR)
            r = srv_planta.registrar_granel(conn, orden_id, cantidad=request.form.get('cantidad'), usuario=usuario(),
                                            firma_id=fid, equipo_id=request.form.get('equipo_id', type=int))
            if r['fuera']:
                r['avisos'] = [f'Rendimiento {r["rendimiento"]} % fuera de los límites: pide desviación.']
            return r
        return accion(hacer, lambda r: f'Granel registrado. Rendimiento: {r["rendimiento"]} %.', url, url)
    conn, e = _cargar(orden_id)
    o = e['orden']
    equipos = srv_planta.equipos_para(conn, basculas=True)
    reportar = _desviacion('orden_granel', o['id'], o, 'Rendimiento fuera de límites') if o['rendimiento_fuera'] else None
    conn.close()
    return full_page('ordenes/planta_granel.html', e=e, o=o, equipos=equipos, reportar=reportar,
                     desviaciones_instalado=srv_ordenes.desviaciones_instalado())


# --- Envasado -------------------------------------------------------------------------
@bp.route('/planta/<int:orden_id>/envasado')
@login_required
@requiere_modulo('ordenes_planta')
def planta_envasado(orden_id):
    conn, e = _cargar(orden_id)
    o = e['orden']
    reportes = {}
    for c in e['conciliaciones'].values():
        if abs(c['diferencia']) > 1e-6:
            reportes[c['material_id']] = _desviacion('orden_conciliacion', c['id'], o, 'Conciliación no cuadra')
    conn.close()
    return full_page('ordenes/planta_envasado.html', e=e, o=o, reportes=reportes,
                     desviaciones_instalado=srv_ordenes.desviaciones_instalado())


@bp.route('/planta/<int:orden_id>/envasar/<int:presentacion_id>', methods=['POST'])
@login_required
@requiere_modulo('ordenes_planta')
def planta_envasar(orden_id, presentacion_id):
    url = url_for('ordenes.planta_envasado', orden_id=orden_id)

    def hacer(conn):
        o = srv_ordenes.orden(conn, orden_id)
        fid = firmar(conn, 'realizo', 'Realizó', 'op_ordenes', orden_id, f'Envasado {o["folio"]}', roles=ROLES_PLANTA)
        srv_planta.envasar(conn, orden_id, presentacion_id, buenas=request.form.get('buenas'),
                           rechazadas=request.form.get('rechazadas'), usuario=usuario(), firma_id=fid)
    return accion(hacer, 'Envasado registrado.', url, url)


@bp.route('/planta/<int:orden_id>/conciliar/<int:material_id>', methods=['POST'])
@login_required
@requiere_modulo('ordenes_planta')
def planta_conciliar(orden_id, material_id):
    url = url_for('ordenes.planta_envasado', orden_id=orden_id)

    def hacer(conn):
        o = srv_ordenes.orden(conn, orden_id)
        fid = firmar(conn, 'realizo', 'Realizó', 'op_ordenes', orden_id, f'Conciliación {o["folio"]}', roles=ROLES_PLANTA)
        dif = srv_planta.conciliar(conn, orden_id, material_id, usadas=request.form.get('usadas'),
                                   danadas=request.form.get('danadas'), firma_id=fid)
        return {'avisos': [] if abs(dif) < 1e-6 else [f'La conciliación no cuadra (diferencia {dif:g}): pide desviación.']}
    return accion(hacer, 'Conciliación registrada.', url, url)


# --- Terminar y justificar -----------------------------------------------------------
@bp.route('/planta/<int:orden_id>/terminar', methods=['GET', 'POST'])
@login_required
@requiere_modulo('ordenes_planta')
def planta_terminar(orden_id):
    url = url_for('ordenes.planta_terminar', orden_id=orden_id)
    if request.method == 'POST':
        def hacer(conn):
            o = srv_ordenes.orden(conn, orden_id)
            fid = firmar(conn, 'realizo', 'Realizó', 'op_ordenes', orden_id, f'Terminó {o["folio"]}', roles=ROLES_SUPERVISOR)
            srv_ordenes.terminar(conn, orden_id, firma_id=fid, usuario=usuario(),
                                 motivo_merma=request.form.get('motivo_merma'))
        return accion(hacer, 'Orden terminada. Falta que producción revise el expediente.', _url_orden(orden_id), url)
    conn, e = _cargar(orden_id)
    o = e['orden']
    problemas = []
    for p in e['problemas']:
        problemas.append({**p, 'reportar': _desviacion(p['origen'], p['origen_id'], o, p['texto'])})
    conn.close()
    return full_page('ordenes/planta_terminar.html', e=e, o=o, problemas=problemas,
                     desviaciones_instalado=srv_ordenes.desviaciones_instalado())


@bp.route('/planta/<int:orden_id>/justificar', methods=['POST'])
@login_required
@requiere_modulo('ordenes_planta')
def planta_justificar(orden_id):
    volver = request.form.get('volver') or ''
    if not volver.startswith('/ordenes/') or volver.startswith('//'):
        volver = url_for('ordenes.planta_terminar', orden_id=orden_id)

    def hacer(conn):
        o = srv_ordenes.orden(conn, orden_id)
        origen = request.form.get('origen')
        if origen not in ('orden_paso', 'orden_granel', 'orden_conciliacion'):
            raise ErrorOrdenes('No hay nada que justificar ahí.')
        fid = firmar(conn, 'realizo', 'Aprobó', 'op_ordenes', orden_id, f'Justificación {origen} {o["folio"]}',
                     roles=ROLES_SUPERVISOR)
        srv_planta.justificar(conn, orden_id, origen=origen, origen_id=request.form.get('origen_id', type=int),
                              justificacion=request.form.get('justificacion'), firma_id=fid)
    return accion(hacer, 'Justificación registrada.', volver, volver)
