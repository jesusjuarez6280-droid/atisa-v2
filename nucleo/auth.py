"""Permisos: roles, áreas adicionales y facultades de firma (movido y ampliado de erp.py).

- PERMISOS: qué roles ven cada módulo. Fuente única para decoradores y menú.
- Una persona tiene un rol principal + áreas adicionales (usuarios.roles_extra);
  basta con que CUALQUIERA de sus roles esté permitido.
- Facultades de firma (usuarios.facultades): solo el admin las asigna y nadie las
  tiene por defecto.
"""
from functools import wraps

from flask import session, flash, url_for, request

from nucleo.db import get_db_connection
from nucleo.vistas import hx_redirect

# Roles del sistema, en el orden en que se muestran.
ROLES = ['admin', 'rh', 'operador', 'almacen', 'produccion', 'calidad', 'mantenimiento']

ROLES_INFO = {
    'admin':         {'nombre': 'Administrador del sistema', 'icono': 'ri-shield-star-line',
                      'desc': 'Configuración total: usuarios, catálogos, auditoría y todos los módulos.'},
    'rh':            {'nombre': 'Recursos Humanos',          'icono': 'ri-team-line',
                      'desc': 'Expedientes, incidencias, capacitación, desempeño, reclutamiento y reportes.'},
    'operador':      {'nombre': 'Operador de Producción',    'icono': 'ri-user-line',
                      'desc': 'Pantalla de planta: procesos, órdenes en proceso y verificación de básculas.'},
    'almacen':       {'nombre': 'Almacén',                   'icono': 'ri-archive-2-line',
                      'desc': 'Recepción, existencias por lote, surtido y conteos.'},
    'produccion':    {'nombre': 'Producción',                'icono': 'ri-flask-line',
                      'desc': 'Órdenes de producción, fórmulas, llenado y procesos.'},
    'calidad':       {'nombre': 'Calidad',                   'icono': 'ri-shield-check-line',
                      'desc': 'Análisis y liberación, desviaciones/CAPA, equipos, 5S, limpiezas y documentos BPM.'},
    'mantenimiento': {'nombre': 'Mantenimiento',             'icono': 'ri-tools-line',
                      'desc': 'Equipos: mantenimientos y verificaciones.'},
}

FACULTADES = {
    'emitir_ordenes':         'Autorizar una orden de producción para que se pueda surtir',
    'aprobar_maestros':       'Aprobar fórmulas maestras y especificaciones de calidad',
    'liberar_lotes':          'Aprobar, rechazar o retener lotes',
    'autorizar_ajustes':      'Ajustes de inventario, destrucciones y devoluciones a proveedor',
    'gestionar_desviaciones': 'Clasificar y cerrar desviaciones, aprobar CAPA',
    'dictaminar_equipos':     'Revisar certificados de calibración y declarar un equipo apto o fuera de servicio',
}

# === RBAC — FUENTE ÚNICA DE VERDAD ===
# Qué roles pueden ver/usar cada módulo. Alimenta los decoradores del backend
# Y el render del menú lateral (botones que no aplican, desaparecen).
# Los módulos de modulos/ suman los suyos al registrarse (nucleo.registro).
PERMISOS = {
    'dashboard':     ['admin', 'rh'],
    'tablero':       ['admin', 'rh'],
    'recetas':       ['admin', 'operador', 'produccion'],
    'empleados':     ['admin', 'rh'],
    'documentos':    ['admin', 'rh', 'calidad'],
    'reportes':      ['admin', 'rh'],
    'asistencias':   ['admin', 'rh'],
    'expertise':     ['admin', 'rh'],
    'perfiles':      ['admin', 'rh'],
    'cardex':        ['admin', 'rh'],
    'bpd':           ['admin', 'rh'],
    'incidencias':   ['admin', 'rh'],
    'capacitacion':  ['admin', 'rh'],
    'cinco_s':       ['admin', 'rh', 'calidad'],
    'limpiezas':     ['admin', 'rh', 'calidad'],
    'produccion':    ['admin', 'rh', 'produccion'],
    'avisos':        ['admin', 'rh'],
    'organigrama':   ['admin', 'rh'],
    'reclutamiento': ['admin', 'rh'],
    'normativa':     ['admin', 'rh', 'calidad'],
    'activos':       ['admin', 'rh'],
    'gasolina':      ['admin', 'rh'],
    'auditoria':     ['admin'],
    'configuracion': ['admin'],
    # Pantallas de la base (/planta, /mi-pin, /pendientes, /archivo/<id>): todos.
    'nucleo':        list(ROLES),
}


def _lista(texto):
    return [x.strip() for x in (texto or '').split(',') if x.strip()]


def roles_sesion():
    """Roles de quien tiene la sesión: principal + áreas adicionales."""
    roles = session.get('roles')
    if roles:
        return list(roles)
    rol = session.get('rol')
    return [rol] if rol else []


def facultades_sesion():
    return list(session.get('facultades') or [])


def usuario_puede(permiso):
    """True si alguno de los roles de la sesión está en PERMISOS[permiso]."""
    return bool(set(roles_sesion()) & set(PERMISOS.get(permiso, [])))


def tiene_facultad(facultad):
    return facultad in facultades_sesion()


def usuario_actual():
    """{numero_empleado, nombre, roles, facultades} para los ganchos de los módulos."""
    return {'numero_empleado': session.get('numero_empleado'), 'nombre': session.get('nombre'),
            'username': session.get('username'), 'roles': roles_sesion(),
            'facultades': facultades_sesion()}


def guardar_en_sesion(u):
    """Copia a la sesión los datos de permisos de una fila de usuarios."""
    extras = [r for r in _lista(u['roles_extra'] if 'roles_extra' in u.keys() else '')
              if r != 'admin' and r != u['rol'] and r in ROLES]
    session['rol'] = u['rol']
    session['roles'] = [u['rol']] + list(dict.fromkeys(extras))
    session['facultades'] = [f for f in _lista(u['facultades'] if 'facultades' in u.keys() else '')
                             if f in FACULTADES]
    session['nombre'] = u['nombre_completo']
    session['numero_empleado'] = u['numero_empleado']


# --- LOS CADENEROS ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return hx_redirect(url_for('login'))
        # Re-validar contra la BD en cada request: si al usuario lo dieron de baja
        # o le cambiaron el rol, áreas o facultades mientras estaba dentro, se aplica
        # al instante (no se queda con permisos "congelados" del login).
        conn = get_db_connection()
        u = conn.execute(
            'SELECT numero_empleado, nombre_completo, rol, estatus, debe_cambiar_password, '
            'roles_extra, facultades FROM usuarios WHERE id = ?',
            (session['user_id'],)
        ).fetchone()
        conn.close()
        if not u or u['estatus'] != 1:
            session.clear()
            flash('Tu sesión ya no es válida. Inicia sesión de nuevo.', 'error')
            return hx_redirect(url_for('login'))
        guardar_en_sesion(u)
        # Contraseña inicial / temporal: no se usa el sistema hasta cambiarla.
        if u['debe_cambiar_password'] and request.endpoint != 'cambiar_password':
            return hx_redirect(url_for('cambiar_password'))
        return f(*args, **kwargs)
    return decorated_function


def _denegar():
    """Acceso no autorizado: avisa con toast y manda a una pantalla válida (sin trabarse)."""
    from nucleo.menu import landing_endpoint
    flash('No tienes permiso para acceder a esa sección.', 'error')
    return hx_redirect(url_for(landing_endpoint()))


def requiere_rol(rol_necesario):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session:
                return hx_redirect(url_for('login'))
            roles = roles_sesion()
            if rol_necesario not in roles and 'admin' not in roles:
                return _denegar()
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def requiere_modulo(modulo):
    """Permite el acceso solo a los roles listados en PERMISOS[modulo] (basta uno)."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session:
                return hx_redirect(url_for('login'))
            if not usuario_puede(modulo):
                return _denegar()
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def requiere_facultad(facultad):
    """Además del módulo: exige la facultad de firma (liberar_lotes, aprobar_maestros…).
    Va debajo de @requiere_modulo."""
    if facultad not in FACULTADES:
        raise ValueError(f'Facultad desconocida: {facultad}')

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session:
                return hx_redirect(url_for('login'))
            if not tiene_facultad(facultad):
                flash(f'Necesitas la facultad «{FACULTADES[facultad]}» para esto.', 'error')
                from nucleo.menu import landing_endpoint
                return hx_redirect(request.referrer if request.referrer and request.referrer.startswith(request.host_url)
                                   else url_for(landing_endpoint()))
            return f(*args, **kwargs)
        return decorated_function
    return decorator
