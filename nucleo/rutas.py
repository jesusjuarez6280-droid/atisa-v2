"""Pantallas de la base (blueprint 'nucleo'):
/planta, /planta/crear-pin, /mi-pin, /pendientes, /archivo/<id>,
/configuracion/parametros, /configuracion/roles-firmas y /pin/borrar.
"""
import sqlite3

from flask import Blueprint, request, flash, url_for, session, send_file, abort, current_app
from werkzeug.security import check_password_hash

from nucleo.auth import (login_required, requiere_modulo, usuario_puede, usuario_actual,
                         ROLES, ROLES_INFO, FACULTADES, PERMISOS)
from nucleo.auditoria import _auditar
from nucleo.db import get_db_connection
from nucleo.vistas import render_page, full_page, hx_redirect
from nucleo import archivos, firmas, parametros, registro

bp = Blueprint('nucleo', __name__, template_folder='templates')


# --- Inicio de la tablet de planta -------------------------------------------
@bp.route('/planta')
@login_required
@requiere_modulo('nucleo')
def planta():
    conn = get_db_connection()
    usuario = usuario_actual()
    accesos = []
    if usuario_puede('recetas'):
        n = conn.execute('SELECT COUNT(*) FROM recetas').fetchone()[0]
        accesos.append({'titulo': 'Procesos', 'icono': 'ri-list-check-3', 'url': '/recetas', 'contador': n})
    accesos += registro.recolectar('accesos_planta', conn, usuario)
    conn.close()
    return full_page('nucleo/planta.html', accesos=accesos)


@bp.route('/planta/crear-pin', methods=['POST'])
@login_required
@requiere_modulo('nucleo')
def planta_crear_pin():
    """En la tablet: número de empleado + contraseña de esa persona + PIN dos veces."""
    numero = (request.form.get('numero_empleado') or '').strip()
    pin = (request.form.get('pin') or '').strip()
    conn = get_db_connection()
    u = conn.execute('SELECT * FROM usuarios WHERE numero_empleado = ? AND estatus = 1', (numero,)).fetchone()
    if not u or not check_password_hash(u['password_hash'], request.form.get('password') or ''):
        conn.close()
        _auditar('acceso', 'Crear PIN rechazado', request.path, detalle=f'empleado {numero}')
        flash('Número de empleado o contraseña incorrectos.', 'error')
        return hx_redirect(url_for('nucleo.planta'))
    if pin != (request.form.get('pin_confirma') or '').strip():
        conn.close()
        flash('Los dos PIN no coinciden.', 'error')
        return hx_redirect(url_for('nucleo.planta'))
    try:
        firmas.fijar_pin(conn, numero, pin, session.get('nombre'))
        conn.commit()
        _auditar('cambio', 'Creó su PIN de firma', request.path, detalle=f"empleado {numero}")
        flash(f"Listo, {u['nombre_completo']}: tu PIN quedó guardado.", 'success')
    except firmas.FirmaInvalida as e:
        conn.rollback()
        flash(str(e), 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('nucleo.planta'))


# --- Mi PIN --------------------------------------------------------------------
@bp.route('/mi-pin', methods=['GET', 'POST'])
@login_required
@requiere_modulo('nucleo')
def mi_pin():
    conn = get_db_connection()
    numero = session.get('numero_empleado')
    if request.method == 'POST':
        u = conn.execute('SELECT password_hash FROM usuarios WHERE id = ?', (session['user_id'],)).fetchone()
        pin = (request.form.get('pin') or '').strip()
        if not u or not check_password_hash(u['password_hash'], request.form.get('password_actual') or ''):
            flash('La contraseña no es correcta.', 'error')
        elif pin != (request.form.get('pin_confirma') or '').strip():
            flash('Los dos PIN no coinciden.', 'error')
        else:
            try:
                firmas.fijar_pin(conn, numero, pin, session.get('nombre'))
                conn.commit()
                flash('Tu PIN de firma quedó guardado.', 'success')
                conn.close()
                return hx_redirect(url_for('nucleo.mi_pin'))
            except firmas.FirmaInvalida as e:
                conn.rollback()
                flash(str(e), 'error')
    tiene = firmas.tiene_pin(conn, numero)
    conn.close()
    return render_page('nucleo/mi_pin.html', tiene_pin=tiene)


# --- Mis pendientes ------------------------------------------------------------
@bp.route('/pendientes')
@login_required
@requiere_modulo('nucleo')
def pendientes():
    conn = get_db_connection()
    grupos = registro.recolectar_por_modulo('pendientes', conn, usuario_actual())
    conn.close()
    return render_page('nucleo/pendientes.html', grupos=grupos)


# --- Archivos privados ---------------------------------------------------------
@bp.route('/archivo/<int:archivo_id>')
@login_required
@requiere_modulo('nucleo')
def archivo(archivo_id):
    conn = get_db_connection()
    a = archivos.archivo(conn, archivo_id)
    conn.close()
    if not a:
        abort(404)
    # Solo quien tiene el permiso del módulo dueño (o 'nucleo' para los de la base).
    if not (a['modulo'] == 'nucleo' or usuario_puede(a['modulo']) or 'admin' in usuario_actual()['roles']):
        abort(404)
    ruta = archivos.ruta_absoluta(a)
    if not ruta:
        abort(404)
    try:
        return send_file(ruta, download_name=a['nombre_original'])
    except FileNotFoundError:
        current_app.logger.warning('Falta en disco el archivo %s (%s)', archivo_id, a['ruta'])
        abort(404)


# --- Configuración → Parámetros -----------------------------------------------
@bp.route('/configuracion/parametros', methods=['GET', 'POST'])
@login_required
@requiere_modulo('configuracion')
def configuracion_parametros():
    conn = get_db_connection()
    if request.method == 'POST':
        clave = request.form.get('clave') or ''
        valor = (request.form.get('valor') or '').strip()
        if clave not in parametros.DEFAULTS or not valor:
            flash('Parámetro o valor no válido.', 'error')
        else:
            parametros.guardar_parametro(conn, clave, valor, session.get('nombre'))
            conn.commit()
            flash('Parámetro guardado.', 'success')
        conn.close()
        return hx_redirect(url_for('nucleo.configuracion_parametros'))
    filas = parametros.todos(conn)
    conn.close()
    nombres = {m.clave: m.nombre for m in registro.modulos_activos()}
    nombres['nucleo'] = 'Base común'
    return render_page('nucleo/parametros.html', filas=filas, nombres=nombres)


# --- Configuración → Roles y firmas -------------------------------------------
@bp.route('/configuracion/roles-firmas', methods=['POST'])
@login_required
@requiere_modulo('configuracion')
def roles_firmas_guardar():
    """Áreas adicionales y facultades de firma de una persona (solo admin)."""
    user_id = request.form.get('user_id')
    extras = [r for r in request.form.getlist('roles_extra') if r in ROLES and r != 'admin']
    facs = [f for f in request.form.getlist('facultades') if f in FACULTADES]
    conn = get_db_connection()
    u = conn.execute('SELECT id, numero_empleado, nombre_completo, rol, roles_extra, facultades '
                     'FROM usuarios WHERE id = ?', (user_id,)).fetchone()
    if not u:
        conn.close()
        flash('No se encontró a esa persona.', 'error')
        return hx_redirect(url_for('configuracion'))
    extras = [r for r in extras if r != u['rol']]
    conn.execute('UPDATE usuarios SET roles_extra = ?, facultades = ? WHERE id = ?',
                 (','.join(extras), ','.join(facs), u['id']))
    conn.commit()
    conn.close()
    propio = ' (a sí mismo)' if str(u['id']) == str(session.get('user_id')) else ''
    _auditar('cambio', 'Roles y facultades de firma', request.path,
             detalle=f"{u['numero_empleado']} {u['nombre_completo']}{propio}: "
                     f"áreas [{u['roles_extra'] or ''}]→[{','.join(extras)}]; "
                     f"facultades [{u['facultades'] or ''}]→[{','.join(facs)}]")
    flash(f"Roles y firmas de {u['nombre_completo']} actualizados.", 'success')
    return hx_redirect(url_for('configuracion') + '#roles-firmas')


@bp.route('/pin/borrar', methods=['POST'])
@login_required
@requiere_modulo('empleados')
def pin_borrar():
    """RH o el admin borran el PIN de alguien (nunca lo ven); la persona crea otro."""
    numero = (request.form.get('numero_empleado') or '').strip()
    conn = get_db_connection()
    try:
        firmas.borrar_pin(conn, numero, session.get('nombre'))
        conn.commit()
        flash('PIN borrado. La persona debe crear uno nuevo.', 'success')
    except firmas.FirmaInvalida as e:
        conn.rollback()
        flash(str(e), 'error')
    finally:
        conn.close()
    destino = request.form.get('volver') or ''
    if not destino.startswith('/') or destino.replace('\\', '/').startswith('//'):
        destino = url_for('configuracion') if usuario_puede('configuracion') else url_for('empleados')
    return hx_redirect(destino)


# --- Ayudas para plantillas (todas las pantallas) ------------------------------
def dias_hasta(fecha):
    """Días de hoy a 'AAAA-MM-DD' (negativo si ya pasó); None si la fecha no es válida."""
    from datetime import date
    try:
        return (date.fromisoformat(str(fecha)[:10]) - date.today()).days
    except (TypeError, ValueError):
        return None


@bp.app_context_processor
def _ayudas_plantillas():
    return {'dias_hasta': dias_hasta, 'ROLES_INFO': ROLES_INFO, 'FACULTADES': FACULTADES}
