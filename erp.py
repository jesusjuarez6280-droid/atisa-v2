from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, send_file, make_response, g, has_app_context
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_caching import Cache
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from functools import wraps
from datetime import datetime, timedelta
import sqlite3
import pandas as pd
import io
import os
import json
import math
import calendar
import time

app = Flask(__name__)

# Ruta de la base de datos (configurable para pruebas o para moverla de carpeta).
DB_PATH = os.environ.get('ATISA_DB', 'base_planta.db')

def _cargar_secret_key():
    """Llave de sesión. Prioridad: variable de entorno ATISA_SECRET_KEY; si no existe,
    se genera UNA vez una llave aleatoria y se guarda en '.secret_key' junto a la base
    (archivo ignorado por git). Nunca hay una llave fija en el código: el repo es
    público y con ella cualquiera podría fabricar una cookie de administrador."""
    llave = os.environ.get('ATISA_SECRET_KEY')
    if llave:
        return llave
    ruta = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), '.secret_key')
    try:
        with open(ruta, 'r', encoding='utf-8') as fh:
            llave = fh.read().strip()
    except OSError:
        llave = ''
    if len(llave) < 32:
        import secrets
        llave = secrets.token_hex(32)
        with open(ruta, 'w', encoding='utf-8') as fh:
            fh.write(llave)
    return llave

app.secret_key = _cargar_secret_key()

# === BLINDAJE DE LA SESIÓN (cookies + expiración) ===
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,     # el cookie NO es legible por JavaScript (mitiga robo por XSS)
    SESSION_COOKIE_SAMESITE='Lax',    # el navegador no manda el cookie en sitios cruzados (mitiga CSRF)
    # Secure solo cuando se sirve por HTTPS (en local es HTTP, si lo forzara rompería el login).
    SESSION_COOKIE_SECURE=os.environ.get('ATISA_HTTPS', '').lower() in ('1', 'true', 'yes'),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),   # la sesión expira a las 8 horas
)

# === Política de contraseñas y anti-fuerza-bruta en el login ===
MIN_PASSWORD = 8
# Contraseña con la que nace el admin en una base nueva. Es pública (está en el repo),
# así que quien entre con ella queda OBLIGADO a cambiarla antes de usar el sistema.
PASSWORD_INICIAL_ADMIN = 'admin123'
def _password_debil(passw):
    """Devuelve un mensaje si la contraseña es débil, o None si es aceptable."""
    if not passw or len(passw) < MIN_PASSWORD:
        return f'La contraseña debe tener al menos {MIN_PASSWORD} caracteres.'
    return None

# Rate-limit de login EN MEMORIA (suficiente para un proceso; documentado). Bloquea
# una combinación (IP, usuario) tras varios intentos fallidos seguidos.
_LOGIN_INTENTOS = {}
_LOGIN_MAX = 5
_LOGIN_VENTANA = 600      # segundos que se consideran los intentos (10 min)
def _login_bloqueado(clave):
    ahora = time.time()
    intentos = [t for t in _LOGIN_INTENTOS.get(clave, []) if ahora - t < _LOGIN_VENTANA]
    _LOGIN_INTENTOS[clave] = intentos
    return len(intentos) >= _LOGIN_MAX
def _login_registrar_fallo(clave):
    _LOGIN_INTENTOS.setdefault(clave, []).append(time.time())
def _login_limpiar(clave):
    _LOGIN_INTENTOS.pop(clave, None)

def _int_seguro(valor, default=0):
    """Convierte a entero sin reventar: texto/None/vacío -> default. Evita que un
    dato mal escrito (ej. 'abc' en 'minutos') tumbe toda la operación."""
    try:
        return int(float(str(valor).strip()))
    except (TypeError, ValueError):
        return default

def _destino_interno(destino, default):
    """Devuelve 'destino' SOLO si es una ruta interna de la propia app; si no, 'default'.
    Bloquea open-redirect: URLs externas (http://…), protocolo-relativas (//host) y el
    truco de backslash (/\\host, que algunos navegadores tratan como //host)."""
    d = (destino or '').strip()
    if not d.startswith('/') or d.replace('\\', '/').startswith('//'):
        return default
    return d

# === CACHÉ EN MEMORIA (catálogos que casi no cambian) ===
cache = Cache(config={'CACHE_TYPE': 'SimpleCache', 'CACHE_DEFAULT_TIMEOUT': 300})
cache.init_app(app)

# === ARCHIVOS PRIVADOS ===
# Expedientes, contratos y responsivas firmadas se guardan en static/contratos/, pero
# Flask sirve /static/ sin pedir sesión. Se bloquea el acceso directo: esos archivos
# solo salen por las rutas que validan login y permiso (ver_documento, ver_contrato,
# normativa_archivo).
_PREFIJOS_PRIVADOS = ('/static/contratos/', '/static/documentos_legales/')

@app.before_request
def _bloquear_archivos_privados():
    # normpath resuelve trucos como /static/css/../contratos/x.pdf o //static//contratos
    import posixpath
    ruta = posixpath.normpath('/' + request.path.replace('\\', '/').lstrip('/')).lower() + '/'
    if ruta.startswith(_PREFIJOS_PRIVADOS):
        abort(404)

# === CSRF — protege TODAS las peticiones POST contra falsificación de solicitudes ===
app.config['WTF_CSRF_TIME_LIMIT'] = None   # el token vive toda la sesión (intranet)
csrf = CSRFProtect(app)

@app.errorhandler(CSRFError)
def _manejar_csrf(e):
    """Token CSRF ausente/expirado: aviso claro en vez de un 400 crudo."""
    flash('Tu sesión expiró o la solicitud no es válida. Vuelve a intentarlo.', 'error')
    return hx_redirect(url_for('login'))

# === HELPERS HTMX (sensación SPA sin reescribir en React/Vue) ===
def render_page(template, **context):
    """Renderiza la página completa, o solo el #content si la petición viene de HTMX."""
    layout = '_partial.html' if request.headers.get('HX-Request') else 'base.html'
    return render_template(template, _layout=layout, **context)

def hx_redirect(url):
    """Redirección compatible con HTMX (HX-Redirect) y con carga normal (302)."""
    if request.headers.get('HX-Request'):
        resp = make_response('')
        resp.headers['HX-Redirect'] = url
        return resp
    return redirect(url)

def full_page(template, **context):
    """Páginas STANDALONE (documento HTML completo: login, responsiva, impresión,
    ejecutar receta). Si llegan vía HTMX (un enlace boosteado por error), se fuerza
    una navegación real para NO inyectar un <html> completo dentro de #content."""
    if request.headers.get('HX-Request'):
        destino = request.full_path
        if destino.endswith('?'):
            destino = destino[:-1]
        resp = make_response('')
        resp.headers['HX-Redirect'] = destino
        return resp
    return render_template(template, **context)

@cache.cached(timeout=600, key_prefix='empleados_activos')
def get_empleados_activos():
    """Catálogo de empleados activos para dropdowns. Cacheado 10 min."""
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT numero_empleado, nombre_completo FROM usuarios WHERE estatus = 1 ORDER BY nombre_completo"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# === DATOS DE LA EMPRESA (edita aquí la razón social si cambia) ===
EMPRESA_NOMBRE = 'ATISA, S.A. de C.V.'
EMPRESA_CIUDAD = 'Tlaquepaque, Jalisco'

# === RBAC — FUENTE ÚNICA DE VERDAD ===
# Qué roles pueden ver/usar cada módulo. Alimenta los decoradores del backend
# Y el render del menú lateral (botones que no aplican, desaparecen).
PERMISOS = {
    'dashboard':     ['admin', 'rh'],
    'tablero':       ['admin', 'rh'],
    'recetas':       ['admin', 'operador'],
    'empleados':     ['admin', 'rh'],
    'documentos':    ['admin', 'rh'],
    'reportes':      ['admin', 'rh'],
    'asistencias':   ['admin', 'rh'],
    'expertise':     ['admin', 'rh'],
    'perfiles':      ['admin', 'rh'],
    'cardex':        ['admin', 'rh'],
    'bpd':           ['admin', 'rh'],
    'incidencias':   ['admin', 'rh'],
    'capacitacion':  ['admin', 'rh'],
    'cinco_s':       ['admin', 'rh'],
    'limpiezas':     ['admin', 'rh'],
    'produccion':    ['admin', 'rh'],
    'avisos':        ['admin', 'rh'],
    'organigrama':   ['admin', 'rh'],
    'reclutamiento': ['admin', 'rh'],
    'normativa':     ['admin', 'rh'],
    'activos':       ['admin', 'rh'],
    'gasolina':      ['admin', 'rh'],
    'auditoria':     ['admin'],
    'configuracion': ['admin'],
}

# --- Navegación por departamentos (organigrama ATISA) ---
# El menú se agrupa en los 8 departamentos del organigrama. Cada módulo apunta a
# una ruta que YA existe; 'match' resalta el módulo activo también en sus subrutas
# (ej. /asistencias sigue resaltado dentro de /reporte_individual).
# Los departamentos con 'modulos' vacío son el plan a futuro (hoja de ruta): solo
# el admin los ve, marcados como "Próximamente", hasta que se construyan.
# Para mover un módulo de departamento, basta con cambiarlo de lista aquí.
NAV_DEPARTAMENTOS = [
    {'key': 'general', 'nombre': 'General', 'icono': 'ri-home-5-line', 'modulos': [
        ('/dashboard',   'Dashboard',        'ri-dashboard-3-line',    ''),
        ('/tablero',     'Tablero Gerencial','ri-line-chart-line',     ''),
        ('/organigrama', 'Organigrama',      'ri-organization-chart',  ''),
        ('/avisos',      'Avisos',           'ri-megaphone-line',      ''),
    ]},
    {'key': 'rh', 'nombre': 'Recursos Humanos', 'icono': 'ri-group-line', 'modulos': [
        ('/empleados',    'Empleados',        'ri-id-card-line',        ''),
        ('/incidencias',  'Incidencias',      'ri-calendar-close-line', ''),
        ('/asistencias',  'Asistencias',      'ri-time-line',           '/reporte_individual'),
        ('/capacitacion', 'Capacitación',     'ri-graduation-cap-line', ''),
        ('/expertise',    'Matriz Expertise', 'ri-medal-line',          ''),
        ('/perfiles',     'Perfiles de Puesto','ri-profile-line',       ''),
        ('/reclutamiento','Reclutamiento',    'ri-user-search-line',    ''),
        ('/cardex',       'Cardex & Madurez', 'ri-profile-line',        ''),
        ('/bpd',          'Desempeño BPD',    'ri-speed-up-line',       ''),
        ('/reportes',     'Reportes',         'ri-bar-chart-2-line',    ''),
    ]},
    {'key': 'produccion', 'nombre': 'Producción', 'icono': 'ri-flask-line', 'modulos': [
        ('/produccion', 'Producción', 'ri-medicine-bottle-line', ''),
        ('/recetas',    'Procesos',   'ri-list-check-3',         ''),
    ]},
    {'key': 'calidad', 'nombre': 'Calidad', 'icono': 'ri-shield-check-line', 'modulos': [
        ('/cinco_s',    'Auditorías 5S',      'ri-sparkling-2-line',  ''),
        ('/limpiezas',  'Rol Limpiezas',      'ri-brush-line',        ''),
        ('/documentos', 'Documentos BPM',     'ri-folder-open-line',  ''),
        ('/normativa',  'Documentos Legales', 'ri-scales-3-line',     '/normativa/bpm'),
        ('/activos',    'Activos y EPP',      'ri-shield-check-line', ''),
    ]},
    {'key': 'logistica', 'nombre': 'Logística', 'icono': 'ri-truck-line', 'modulos': [
        ('/gasolina', 'Tarjeta Gasolina', 'ri-gas-station-fill', ''),
    ]},
    {'key': 'administracion', 'nombre': 'Administración', 'icono': 'ri-building-line', 'modulos': [
        ('/auditoria',     'Auditoría y Catálogos', 'ri-file-shield-2-line', ''),
        ('/configuracion', 'Configuración',         'ri-settings-4-line',    ''),
    ]},
    {'key': 'comercial',         'nombre': 'Comercial',                  'icono': 'ri-line-chart-line',   'modulos': []},
    {'key': 'id',                'nombre': 'Investigación y Desarrollo', 'icono': 'ri-microscope-line',   'modulos': []},
    {'key': 'farmacovigilancia', 'nombre': 'Farmacovigilancia',         'icono': 'ri-heart-pulse-line',  'modulos': []},
]

def construir_nav(rol):
    """Arma el árbol de navegación visible para el rol en sesión.
    Filtra cada módulo por PERMISOS y oculta departamentos sin módulos
    accesibles (los departamentos 'a futuro' solo los ve el admin)."""
    arbol = []
    for d in NAV_DEPARTAMENTOS:
        mods = [m for m in d['modulos'] if rol in PERMISOS.get(m[0][1:], [])]
        es_roadmap = len(d['modulos']) == 0
        if mods or (es_roadmap and rol == 'admin'):
            arbol.append({**d, 'modulos_visibles': mods, 'es_roadmap': es_roadmap})
    return arbol

def landing_endpoint():
    """Primer módulo al que el rol en sesión SÍ tiene acceso.
    Evita el bucle de redirección: nunca devuelve un destino que luego deniegue."""
    rol = session.get('rol')
    prioridad = ['dashboard', 'recetas', 'empleados', 'documentos', 'reportes',
                 'asistencias', 'activos', 'gasolina', 'configuracion']
    for modulo in prioridad:
        if rol in PERMISOS.get(modulo, []):
            return modulo
    return 'sin_acceso'   # rol sin ningún módulo: pantalla estática (no se atora)

@app.context_processor
def inject_globals():
    return {
        'EMPRESA_NOMBRE': EMPRESA_NOMBRE,
        'EMPRESA_CIUDAD': EMPRESA_CIUDAD,
        # True si ya existe el CSS de Tailwind precompilado (evita el runtime en el navegador)
        'tailwind_estatico': os.path.exists(os.path.join(app.static_folder, 'css', 'tailwind.css')),
        # Matriz de permisos para ocultar botones del menú según el rol
        'permisos': PERMISOS,
        # Árbol de navegación por departamentos, ya filtrado por el rol en sesión
        'nav_visible': construir_nav(session.get('rol')),
    }

# Carpetas para guardar archivos físicos
app.config['UPLOAD_FOLDER'] = 'static/img_recetas'
app.config['UPLOAD_CONTRATOS'] = 'static/contratos'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['UPLOAD_CONTRATOS'], exist_ok=True)

# Extensiones permitidas para archivos subidos. Excluye .html/.svg/.js para
# evitar Stored XSS al servirlos con send_file (que adivina el Content-Type).
ALLOWED_EXTENSIONS = {'pdf', 'jpg', 'jpeg', 'png'}

def extension_permitida(filename):
    return '.' in (filename or '') and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# === RESPALDO AUTOMÁTICO DIARIO DE LA BASE ===
# La primera petición de cada día copia la base a respaldos/ con la API de respaldo
# de SQLite (copia consistente aunque haya usuarios escribiendo, incluye el WAL).
# Se conservan los últimos RESPALDOS_A_CONSERVAR días. Corre en un hilo aparte para
# no frenar a quien hizo la petición. Para copias fuera del sitio, sincroniza la
# carpeta respaldos/ (USB, NAS, OneDrive...).
import threading
RESPALDOS_DIR = os.environ.get('ATISA_RESPALDOS', os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), 'respaldos'))
RESPALDOS_A_CONSERVAR = 30
_respaldo_lock = threading.Lock()
_respaldo_ultimo_dia = None

def respaldar_bd(destino=None):
    """Copia la base a 'destino' (o a respaldos/base_planta_AAAA-MM-DD.db) y depura
    los respaldos viejos. Devuelve la ruta del respaldo."""
    os.makedirs(RESPALDOS_DIR, exist_ok=True)
    destino = destino or os.path.join(RESPALDOS_DIR, f"base_planta_{datetime.now():%Y-%m-%d}.db")
    temporal = destino + '.tmp'
    origen = sqlite3.connect(DB_PATH, timeout=30.0)
    try:
        copia = sqlite3.connect(temporal)
        try:
            origen.backup(copia)
        finally:
            copia.close()
    finally:
        origen.close()
    os.replace(temporal, destino)   # nunca deja un respaldo a medias con el nombre final
    viejos = sorted(f for f in os.listdir(RESPALDOS_DIR)
                    if f.startswith('base_planta_') and f.endswith('.db'))
    for f in viejos[:-RESPALDOS_A_CONSERVAR]:
        try:
            os.remove(os.path.join(RESPALDOS_DIR, f))
        except OSError:
            pass
    return destino

def _respaldo_en_hilo():
    try:
        respaldar_bd()
    except Exception:
        app.logger.exception('Falló el respaldo automático de la base')

@app.before_request
def _respaldo_diario():
    global _respaldo_ultimo_dia
    hoy = datetime.now().strftime('%Y-%m-%d')
    if _respaldo_ultimo_dia == hoy or os.environ.get('ATISA_SIN_RESPALDO'):
        return
    with _respaldo_lock:
        if _respaldo_ultimo_dia == hoy:
            return
        _respaldo_ultimo_dia = hoy
        if not os.path.exists(os.path.join(RESPALDOS_DIR, f'base_planta_{hoy}.db')) and os.path.exists(DB_PATH):
            threading.Thread(target=_respaldo_en_hilo, daemon=True).start()

# --- 1. CONFIGURACIÓN DE LA BASE DE DATOS ---
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # PRAGMAs de rendimiento (por conexión, costo ~microsegundos)
    conn.execute('PRAGMA synchronous = NORMAL;')   # rápido y seguro junto a WAL
    conn.execute('PRAGMA temp_store = MEMORY;')     # tablas temporales en RAM
    conn.execute('PRAGMA cache_size = -16000;')     # ~16 MB de page cache
    conn.execute('PRAGMA foreign_keys = ON;')
    conn.execute('PRAGMA busy_timeout = 5000;')     # espera en vez de "database is locked"
    # Red de seguridad: registra la conexión para cerrarla al final del request
    # aunque la ruta truene o el dev olvide conn.close(). Evita bloqueos en red (WAL).
    if has_app_context():
        g.setdefault('_db_conns', []).append(conn)
    return conn

@app.teardown_appcontext
def _cerrar_conexiones_db(exc):
    """Cierra TODA conexión abierta durante el request (idempotente con conn.close())."""
    conns = g.pop('_db_conns', None)
    if conns:
        for c in conns:
            try:
                c.close()
            except Exception:
                pass

# === M10 · AUDITORÍA: registro de trazabilidad (nunca debe romper el request) ===
def _auditar(tipo, accion, ruta='', detalle='', usuario=None, rol=None):
    """Inserta una línea en el registro de auditoría. A prueba de fallos:
    si algo sale mal (BD ocupada, tabla vieja), se ignora en silencio."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=3.0)
        try:
            ip = request.remote_addr if request else None
        except Exception:
            ip = None
        conn.execute(
            "INSERT INTO auditoria (usuario, rol, tipo, accion, ruta, detalle, ip) VALUES (?,?,?,?,?,?,?)",
            (usuario or session.get('nombre') or session.get('username'),
             rol or session.get('rol'), tipo, accion, ruta, detalle, ip))
        conn.commit()
        conn.close()
    except Exception:
        pass

# Rutas cuyo POST NO se registra como 'cambio' de datos:
#  - /login ya se registra aparte como 'acceso' (evita el doble registro).
_AUDIT_POST_IGNORAR = {'/login'}

@app.after_request
def _auditar_cambios(response):
    """Registra automáticamente cada operación de escritura (POST) exitosa como
    un 'cambio', sin tener que tocar ruta por ruta. Los accesos (login/logout) se
    registran explícitamente en sus rutas."""
    try:
        if (request.method == 'POST' and 'user_id' in session
                and response.status_code < 400
                and request.path not in _AUDIT_POST_IGNORAR
                and not request.path.startswith('/static')):
            _auditar('cambio', 'Modificación de datos', request.path)
    except Exception:
        pass
    return response

def _festivos_mexico(anio):
    """Días festivos oficiales de México (LFT Art. 74): fijos + lunes conmemorativos.
    Devuelve lista de (fecha 'YYYY-MM-DD', nombre)."""
    def nth_lunes(mes, n):
        primero = datetime(anio, mes, 1)
        offset = (7 - primero.weekday()) % 7          # días hasta el primer lunes
        dia = 1 + offset + (n - 1) * 7
        return datetime(anio, mes, dia)
    return [
        (f'{anio}-01-01', 'Año Nuevo'),
        (nth_lunes(2, 1).strftime('%Y-%m-%d'),  'Día de la Constitución'),
        (nth_lunes(3, 3).strftime('%Y-%m-%d'),  'Natalicio de Benito Juárez'),
        (f'{anio}-05-01', 'Día del Trabajo'),
        (f'{anio}-09-16', 'Día de la Independencia'),
        (nth_lunes(11, 3).strftime('%Y-%m-%d'), 'Revolución Mexicana'),
        (f'{anio}-12-25', 'Navidad'),
    ]

def antiguedad_anios(fecha_ingreso):
    """Años completos de antigüedad desde la fecha de ingreso. None si no hay fecha."""
    if not fecha_ingreso:
        return None
    try:
        ing = datetime.strptime(str(fecha_ingreso)[:10], '%Y-%m-%d')
    except (ValueError, TypeError):
        return None
    hoy = datetime.now()
    anios = hoy.year - ing.year - (1 if (hoy.month, hoy.day) < (ing.month, ing.day) else 0)
    return max(0, anios)

def dias_vacaciones_lft(anios):
    """Días de vacaciones que le tocan según la LFT (reforma 2023):
    año 1=12, 2=14, 3=16, 4=18, 5=20; luego 22 y +2 por cada bloque de 5 años."""
    if anios is None or anios < 1:
        return 0
    base = {1: 12, 2: 14, 3: 16, 4: 18, 5: 20}
    if anios <= 5:
        return base[anios]
    return 22 + ((anios - 6) // 5) * 2

# === M4 · Módulos de inducción ATISA (matriz M4: IND-01 a IND-11) ===
MODULOS_INDUCCION = [
    'Inducción 1 · Historia y Misión ATISA',
    'Inducción 2 · Productos ATISA',
    'Inducción 3 · Reglamento Interior de Trabajo',
    'Inducción 4 · Descripción de Puesto',
    'Inducción 5 · Política de Calidad',
    'Inducción 6 · Código de Conducta',
    'Inducción 7 · Reglamento de Casilleros',
    'Inducción 8 · Higiene BPM',
    'Inducción 9 · Lavado de Manos (9 pasos)',
    'Inducción 10 · Conducta en Producción',
    'Inducción 11 · Equipo de Protección Personal',
]

# === M8 · Catálogo semilla de documentos legales y normatividad (matriz oficial ATISA) ===
DOC_CATEGORIAS = ['Formatos RH', 'Logística / Almacén', 'Normatividad Laboral', 'BPM SENASICA / SADER']
DOC_ESTATUS = ['Vigente', 'En revisión', 'Obsoleto']
DOCUMENTOS_LEGALES_SEED = [
    # (código, nombre, categoría, versión)
    ('F-RHC-SE-01',   'Solicitud de empleo',                'Formatos RH', ''),
    ('F-RHC-3.1-3',   'Acta de conformidad de inducción',   'Formatos RH', 'v02'),
    ('F-RHC-3.1-4',   'Acta de recepción de puesto',        'Formatos RH', ''),
    ('F-RHC-EXP-01',  'Checklist de expediente personal',   'Formatos RH', ''),
    ('F-RHC-MEM-01',  'Memorándum (sanciones / amonestaciones)', 'Formatos RH', ''),
    ('F-RHC-CONF-01', 'Acuerdo de confidencialidad',        'Formatos RH', ''),
    ('P-ALM-LOG-01',  'Procedimiento logístico',            'Logística / Almacén', ''),
    ('F-ALM-LOG-01/08','Formatos operativos de almacén',    'Logística / Almacén', ''),
    ('RIT-2025',      'Reglamento Interior de Trabajo ATISA','Normatividad Laboral', '2025'),
    ('COD-CONDUCTA',  'Código de Conducta ATISA',           'Normatividad Laboral', ''),
    ('POL-LFPDPPP',   'Política de Privacidad (LFPDPPP)',    'Normatividad Laboral', ''),
    ('POL-SGCI',      'Política de Calidad e Inocuidad',     'Normatividad Laboral', ''),
    ('NOM-035-STPS',  'NOM-035-STPS · Riesgo psicosocial',  'Normatividad Laboral', ''),
    ('NOM-017-STPS',  'NOM-017-STPS · Equipo de protección personal', 'Normatividad Laboral', ''),
    ('MAN-BPM-01',    'Manual BPM SENASICA vigente',         'BPM SENASICA / SADER', ''),
    ('F-BPM-MIN-01',  'Minutas de reunión BPM',              'BPM SENASICA / SADER', ''),
    ('F-BPM-LIMP-01', 'Registros de limpieza y exclusa',     'BPM SENASICA / SADER', ''),
    ('NOM-ZOO',       'NOMs veterinarias (012, 022, 024, 025, 026, 064-ZOO)', 'BPM SENASICA / SADER', ''),
]
BPM_AREAS = ['Instalaciones', 'Personal', 'Producción', 'Documentación', 'Almacén', 'Limpieza y sanidad']
BPM_ESTATUS = ['Pendiente', 'En proceso', 'Cumplida', 'No aplica']
# Muestra representativa del maestro de 113 actividades BPM (el maestro completo se carga después).
BPM_ACTIVIDADES_SEED = [
    ('Rótulos e identificación de áreas por función', 'Instalaciones'),
    ('Control de plagas con programa documentado', 'Instalaciones'),
    ('Superficies lavables en zona de producción', 'Instalaciones'),
    ('Exámenes médicos de ingreso al personal', 'Personal'),
    ('Uniforme y EPP completo por área', 'Personal'),
    ('Capacitación BPM anual documentada', 'Personal'),
    ('Registro de lotes y trazabilidad de producto', 'Producción'),
    ('Control de temperatura y condiciones de proceso', 'Producción'),
    ('Procedimientos operativos vigentes y firmados', 'Documentación'),
    ('Bitácoras de producción completas y legibles', 'Documentación'),
    ('Control de entradas y salidas de almacén', 'Almacén'),
    ('Materia prima con orden PEPS y estatus de calidad', 'Almacén'),
    ('Programa de limpieza y sanitización por área', 'Limpieza y sanidad'),
    ('Registro de acceso por exclusa a producción', 'Limpieza y sanidad'),
]

def inicializar_bd():
    conn = get_db_connection()

    # WAL: lecturas concurrentes sin bloquear al escritor (clave en red con varios usuarios)
    conn.execute('PRAGMA journal_mode = WAL;')

    # Tabla de Usuarios
    conn.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_empleado TEXT UNIQUE NOT NULL,
            nombre_completo TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            rol TEXT NOT NULL,
            estatus INTEGER DEFAULT 1
        )
    ''')

    # Migración idempotente: enriquecer el EXPEDIENTE del empleado (fuente única).
    # Antes el puesto/área solo vivían pegados a una asignación de activo (traspapelado);
    # ahora son parte del empleado y todos los módulos los leen de aquí.
    cols_usuarios = {row[1] for row in conn.execute('PRAGMA table_info(usuarios)').fetchall()}
    for col, tipo in [('puesto', 'TEXT'), ('area', 'TEXT'), ('cumpleanos', 'TEXT'),
                      ('talla_calzado', 'TEXT'), ('fecha_ingreso', 'TEXT'),
                      # M1 · Expediente digital completo del colaborador
                      ('fecha_nacimiento', 'TEXT'), ('curp', 'TEXT'), ('rfc', 'TEXT'), ('nss', 'TEXT'),
                      ('domicilio', 'TEXT'), ('telefono', 'TEXT'), ('correo', 'TEXT'),
                      ('estado_civil', 'TEXT'), ('num_dependientes', 'TEXT'),
                      ('emergencia_nombre', 'TEXT'), ('emergencia_parentesco', 'TEXT'),
                      ('emergencia_telefono', 'TEXT'), ('escolaridad', 'TEXT'), ('cedula_profesional', 'TEXT'),
                      ('jefe_inmediato', 'TEXT'), ('tipo_contrato', 'TEXT'), ('fecha_fin_contrato', 'TEXT'),
                      ('salario_diario', 'TEXT'), ('tipo_jornada', 'TEXT'), ('turno', 'TEXT'),
                      ('infonavit', 'TEXT'), ('fonacot', 'TEXT'), ('banco_clabe', 'TEXT'),
                      ('fecha_baja', 'TEXT'), ('motivo_baja', 'TEXT'),
                      # Seguridad · obliga a cambiar la contraseña inicial en el siguiente acceso
                      ('debe_cambiar_password', 'INTEGER DEFAULT 0')]:
        if col not in cols_usuarios:
            try:
                conn.execute(f'ALTER TABLE usuarios ADD COLUMN {col} {tipo}')
            except sqlite3.OperationalError:
                pass

    # Tabla de Asistencias
    conn.execute('''
        CREATE TABLE IF NOT EXISTS asistencias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_empleado TEXT NOT NULL,
            timestamp DATETIME NOT NULL,
            puerta TEXT NOT NULL,
            tipo_registro TEXT NOT NULL,
            FOREIGN KEY(numero_empleado) REFERENCES usuarios(numero_empleado)
        )
    ''')
    
    # Tabla de Recetas
    conn.execute('''
        CREATE TABLE IF NOT EXISTS recetas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            descripcion TEXT,
            creado_por TEXT NOT NULL
        )
    ''')
    
    conn.execute('''
        CREATE TABLE IF NOT EXISTS recetas_pasos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receta_id INTEGER NOT NULL,
            numero_paso INTEGER NOT NULL,
            instruccion TEXT NOT NULL,
            tiene_tiempo INTEGER DEFAULT 0,
            minutos INTEGER DEFAULT 0,
            imagen_ruta TEXT,
            FOREIGN KEY(receta_id) REFERENCES recetas(id) ON DELETE CASCADE
        )
    ''')
    
    # Bitácora de Gasolina
    conn.execute('''
        CREATE TABLE IF NOT EXISTS bitacora_gasolina (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha DATETIME DEFAULT (datetime('now', 'localtime')),
            nombre_empleado TEXT NOT NULL,
            no_estacion TEXT NOT NULL,
            monto REAL NOT NULL
        )
    ''')

    # Inventario Físico
    conn.execute('''
        CREATE TABLE IF NOT EXISTS inventario_activos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            categoria TEXT NOT NULL,
            descripcion TEXT NOT NULL,
            estatus TEXT DEFAULT 'Disponible'
        )
    ''')

    # Asignaciones y Contratos
    conn.execute('''
        CREATE TABLE IF NOT EXISTS asignaciones_activos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_empleado TEXT NOT NULL,
            activo_id INTEGER NOT NULL,
            fecha_prestamo DATETIME DEFAULT (datetime('now', 'localtime')),
            ruta_contrato TEXT,
            estatus TEXT DEFAULT 'Activo',
            puesto TEXT,
            area TEXT,
            marca TEXT,
            modelo TEXT,
            no_serie TEXT,
            observaciones TEXT,
            tipo_responsiva TEXT,
            firmada INTEGER DEFAULT 0,
            FOREIGN KEY(numero_empleado) REFERENCES usuarios(numero_empleado),
            FOREIGN KEY(activo_id) REFERENCES inventario_activos(id)
        )
    ''')

    # Migración idempotente: agregar columnas si la tabla ya existía sin ellas
    existing_cols = {row[1] for row in conn.execute('PRAGMA table_info(asignaciones_activos)').fetchall()}
    for col, tipo in [('puesto','TEXT'), ('area','TEXT'), ('marca','TEXT'), ('modelo','TEXT'),
                       ('no_serie','TEXT'), ('observaciones','TEXT'), ('tipo_responsiva','TEXT'),
                       ('firmada','INTEGER DEFAULT 0'), ('imei','TEXT'),
                       ('placa','TEXT'), ('color','TEXT'), ('talla','TEXT')]:
        if col not in existing_cols:
            try:
                conn.execute(f'ALTER TABLE asignaciones_activos ADD COLUMN {col} {tipo}')
            except sqlite3.OperationalError:
                pass
    
    # Tarjetas de Gasolina
    conn.execute('''
        CREATE TABLE IF NOT EXISTS tarjetas_gasolina (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_tarjeta TEXT UNIQUE NOT NULL,
            saldo REAL NOT NULL DEFAULT 0,
            estatus TEXT DEFAULT 'Disponible',
            numero_empleado TEXT,
            FOREIGN KEY(numero_empleado) REFERENCES usuarios(numero_empleado)
        )
    ''')

    # Asignaciones de Gasolina
    conn.execute('''
        CREATE TABLE IF NOT EXISTS asignacion_gasolina (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_empleado TEXT NOT NULL,
            tarjeta_id INTEGER NOT NULL,
            fecha_asignacion DATETIME DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY(numero_empleado) REFERENCES usuarios(numero_empleado),
            FOREIGN KEY(tarjeta_id) REFERENCES tarjetas_gasolina(id)
        )
    ''')

    # Documentos de Empleados
    conn.execute('''
        CREATE TABLE IF NOT EXISTS documentos_empleados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_empleado TEXT NOT NULL,
            tipo_documento TEXT NOT NULL,
            nombre_archivo TEXT NOT NULL,
            ruta_archivo TEXT NOT NULL,
            fecha_subida DATETIME DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY(numero_empleado) REFERENCES usuarios(numero_empleado)
        )
    ''')

    # Horarios laborales por empleado
    conn.execute('''
        CREATE TABLE IF NOT EXISTS horarios_empleados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_empleado TEXT UNIQUE NOT NULL,
            hora_entrada TEXT NOT NULL DEFAULT '08:30',
            hora_salida TEXT NOT NULL DEFAULT '18:00',
            FOREIGN KEY(numero_empleado) REFERENCES usuarios(numero_empleado)
        )
    ''')

    # Registros crudos importados del checador ZKteco
    conn.execute('''
        CREATE TABLE IF NOT EXISTS registros_zkteco (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zk_id INTEGER,
            user_id TEXT NOT NULL,
            nombre TEXT NOT NULL,
            fecha TEXT NOT NULL,
            hora TEXT NOT NULL,
            tipo TEXT DEFAULT 'ZK-Auto',
            estatus TEXT DEFAULT 'Sincronizado',
            fecha_importacion DATETIME DEFAULT (datetime('now','localtime'))
        )
    ''')

    # Resumen procesado de asistencias por empleado por día
    conn.execute('''
        CREATE TABLE IF NOT EXISTS resumen_asistencias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_empleado TEXT NOT NULL,
            nombre TEXT NOT NULL,
            fecha TEXT NOT NULL,
            hora_entrada_real TEXT,
            hora_salida_real TEXT,
            minutos_retardo INTEGER DEFAULT 0,
            horas_extras REAL DEFAULT 0,
            estatus TEXT DEFAULT 'Presente',
            UNIQUE(numero_empleado, fecha)
        )
    ''')

    # === MÓDULO EXPERTISE (Equipo Multifuncional / Producción) ===
    # Catálogo de tareas/competencias evaluables
    conn.execute('''
        CREATE TABLE IF NOT EXISTS competencias_tareas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            area TEXT DEFAULT 'Producción',
            orden INTEGER DEFAULT 0,
            activo INTEGER DEFAULT 1
        )
    ''')
    # Evaluación vigente de cada colaborador en cada tarea (0-100). Una fila por par.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS expertise_evaluaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_empleado TEXT NOT NULL,
            tarea_id INTEGER NOT NULL,
            nivel_pct INTEGER NOT NULL DEFAULT 0,
            actualizado_por TEXT,
            fecha_actualizacion DATETIME DEFAULT (datetime('now','localtime')),
            UNIQUE(numero_empleado, tarea_id),
            FOREIGN KEY(numero_empleado) REFERENCES usuarios(numero_empleado),
            FOREIGN KEY(tarea_id) REFERENCES competencias_tareas(id) ON DELETE CASCADE
        )
    ''')
    # Semilla del catálogo (solo la primera vez): las 14 tareas del Excel de Producción
    if conn.execute("SELECT COUNT(*) AS n FROM competencias_tareas").fetchone()['n'] == 0:
        tareas_base = [
            'Mezcladora pre mezclas', 'Mezcladora polvos solubles', 'Pesado MP',
            'Tamizado', 'Trasvase', 'Tanque 100', 'Tanque 500', 'Bomba Diafragma',
            'Bomba Pistón', 'Llenado de líquidos', 'Uso de básculas',
            'Llenado de documentación', 'Lotificado de etiqueta y caja', 'Empacado',
        ]
        for i, nom in enumerate(tareas_base, start=1):
            conn.execute(
                "INSERT INTO competencias_tareas (nombre, area, orden, activo) VALUES (?, 'Producción', ?, 1)",
                (nom, i)
            )

    # === MÓDULO DESEMPEÑO BPD (evaluaciones periódicas con semáforo y tendencia) ===
    conn.execute('''
        CREATE TABLE IF NOT EXISTS bpd_evaluaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_empleado TEXT NOT NULL,
            fecha TEXT NOT NULL,
            calificacion REAL NOT NULL,
            observaciones TEXT,
            evaluado_por TEXT,
            creado_en DATETIME DEFAULT (datetime('now','localtime')),
            FOREIGN KEY(numero_empleado) REFERENCES usuarios(numero_empleado)
        )
    ''')

    # === MÓDULO INCIDENCIAS (calendario RH: faltas, permisos, vacaciones) ===
    conn.execute('''
        CREATE TABLE IF NOT EXISTS incidencias_dia (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_empleado TEXT NOT NULL,
            fecha TEXT NOT NULL,
            codigo TEXT NOT NULL,
            nota TEXT,
            registrado_por TEXT,
            UNIQUE(numero_empleado, fecha),
            FOREIGN KEY(numero_empleado) REFERENCES usuarios(numero_empleado)
        )
    ''')

    # === CALENDARIO DE DÍAS FESTIVOS (para detectar faltas automáticas) ===
    conn.execute('''
        CREATE TABLE IF NOT EXISTS dias_festivos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT UNIQUE NOT NULL,
            nombre TEXT
        )
    ''')
    # Semilla: días festivos oficiales (LFT Art. 74) del año pasado al próximo.
    _anio_now = datetime.now().year
    for _y in range(_anio_now - 1, _anio_now + 2):
        for _f, _nom in _festivos_mexico(_y):
            try:
                conn.execute("INSERT OR IGNORE INTO dias_festivos (fecha, nombre) VALUES (?, ?)", (_f, _nom))
            except sqlite3.Error:
                pass

    # === MÓDULO CAPACITACIÓN (programa de cursos + estatus por colaborador) ===
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cursos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            tipo TEXT DEFAULT 'General',
            orden INTEGER DEFAULT 0,
            activo INTEGER DEFAULT 1
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS capacitacion_registro (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_empleado TEXT NOT NULL,
            curso_id INTEGER NOT NULL,
            estatus TEXT DEFAULT 'Pendiente',
            fecha TEXT,
            UNIQUE(numero_empleado, curso_id),
            FOREIGN KEY(numero_empleado) REFERENCES usuarios(numero_empleado),
            FOREIGN KEY(curso_id) REFERENCES cursos(id) ON DELETE CASCADE
        )
    ''')
    if conn.execute("SELECT COUNT(*) AS n FROM cursos").fetchone()['n'] == 0:
        cursos_base = ["5'S", 'Seguridad EPP', 'BPM', 'Trabajo en equipo', 'Inducción',
                       'BPD', 'Operación y limpieza de equipos', 'POKAYOKE / Kanban',
                       'Inteligencia emocional', 'Trabajo Estandarizado']
        for i, nom in enumerate(cursos_base, start=1):
            conn.execute("INSERT INTO cursos (nombre, tipo, orden, activo) VALUES (?, 'BPM', ?, 1)", (nom, i))

    # M4 · Enriquecer el catálogo de cursos (Plan Anual: metadatos) y el registro (calificación)
    cols_cursos = {row[1] for row in conn.execute('PRAGMA table_info(cursos)').fetchall()}
    for col, tipo in [('categoria', "TEXT DEFAULT 'Plan Anual'"), ('modalidad', 'TEXT'),
                      ('instructor', 'TEXT'), ('fecha_programada', 'TEXT'),
                      ('duracion_horas', 'TEXT'), ('destinatarios', 'TEXT'), ('costo', 'TEXT')]:
        if col not in cols_cursos:
            try:
                conn.execute(f'ALTER TABLE cursos ADD COLUMN {col} {tipo}')
            except sqlite3.OperationalError:
                pass
    cols_capreg = {row[1] for row in conn.execute('PRAGMA table_info(capacitacion_registro)').fetchall()}
    if 'calificacion' not in cols_capreg:
        try:
            conn.execute('ALTER TABLE capacitacion_registro ADD COLUMN calificacion INTEGER')
        except sqlite3.OperationalError:
            pass
    # Sembrar los 11 módulos de INDUCCIÓN ATISA (una sola vez)
    if conn.execute("SELECT COUNT(*) AS n FROM cursos WHERE categoria='Inducción'").fetchone()['n'] == 0:
        base_ord = conn.execute("SELECT COALESCE(MAX(orden),0) AS o FROM cursos").fetchone()['o']
        for i, nom in enumerate(MODULOS_INDUCCION, start=1):
            conn.execute(
                "INSERT INTO cursos (nombre, tipo, categoria, orden, activo) VALUES (?, 'Inducción', 'Inducción', ?, 1)",
                (nom, base_ord + i))

    # === MÓDULO 5S (auditorías de orden y limpieza por área) ===
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cinco_s_auditorias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            area TEXT NOT NULL,
            lider TEXT,
            fecha TEXT NOT NULL,
            hallazgos INTEGER DEFAULT 0,
            calificacion REAL NOT NULL,
            nota TEXT,
            registrado_por TEXT,
            creado_en DATETIME DEFAULT (datetime('now','localtime'))
        )
    ''')

    # === MÓDULO ROL DE LIMPIEZAS (rotación semanal de áreas auxiliares) ===
    conn.execute('''
        CREATE TABLE IF NOT EXISTS limpieza_rol (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            semana TEXT NOT NULL,
            area TEXT NOT NULL,
            responsable TEXT NOT NULL,
            nota TEXT,
            orden INTEGER DEFAULT 0,
            creado_en DATETIME DEFAULT (datetime('now','localtime'))
        )
    ''')

    # === MÓDULO PRODUCCIÓN (llenado por día/lote) ===
    conn.execute('''
        CREATE TABLE IF NOT EXISTS produccion_llenado (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            producto TEXT NOT NULL,
            lote TEXT,
            turno_manana INTEGER DEFAULT 0,
            turno_tarde INTEGER DEFAULT 0,
            nota TEXT,
            registrado_por TEXT,
            creado_en DATETIME DEFAULT (datetime('now','localtime'))
        )
    ''')

    # === MÓDULO AVISOS (tablón con compromisos y responsables) ===
    conn.execute('''
        CREATE TABLE IF NOT EXISTS avisos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            aviso TEXT NOT NULL,
            fecha_aviso TEXT,
            compromiso TEXT,
            responsable TEXT,
            estatus TEXT DEFAULT 'Abierto',
            creado_en DATETIME DEFAULT (datetime('now','localtime'))
        )
    ''')

    # Crear Admin por defecto
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM usuarios WHERE username = 'admin'")
    if not cursor.fetchone():
        hash_pass = generate_password_hash(PASSWORD_INICIAL_ADMIN)
        conn.execute('''
            INSERT INTO usuarios (numero_empleado, nombre_completo, username, password_hash, rol, debe_cambiar_password)
            VALUES (?, ?, ?, ?, ?, 1)
        ''', ('0001', 'Ing. Jesús Armando Juárez Cabrera', 'admin', hash_pass, 'admin'))

    # === MÓDULO M3 · PERFIL DE PUESTO & KPIs ===
    conn.execute('''
        CREATE TABLE IF NOT EXISTS puestos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo TEXT, nombre TEXT NOT NULL, area TEXT, nivel TEXT,
            objetivo TEXT, escolaridad TEXT, experiencia TEXT, conocimientos TEXT,
            habilidades TEXT, responsabilidades_bpm TEXT, epp TEXT, riesgos TEXT,
            activo INTEGER DEFAULT 1
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS puesto_kpis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            puesto_id INTEGER NOT NULL,
            nombre TEXT NOT NULL, meta TEXT, frecuencia TEXT DEFAULT 'Mensual',
            peso INTEGER DEFAULT 0, orden INTEGER DEFAULT 0,
            FOREIGN KEY(puesto_id) REFERENCES puestos(id) ON DELETE CASCADE
        )
    ''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kpi_puesto ON puesto_kpis(puesto_id)")
    # Semilla (primera vez): puestos ATISA + KPIs medibles (de la matriz de evaluación por puesto)
    if conn.execute("SELECT COUNT(*) AS n FROM puestos").fetchone()['n'] == 0:
        seed_puestos = [
            ('PROD-OPE', 'Operador', 'Producción', 'Operativo', 'Pesa, mezcla y envasa producto conforme a la fórmula.',
             [('Volumen de producción (unidades/lotes vs meta)', 'Cumple meta', 'Mensual'),
              ('Calidad — mermas y reprocesos', 'Merma mínima', 'Mensual'),
              ('Apego a la fórmula (desviaciones)', 'Sin desviaciones', 'Por lote')]),
            ('PROD-PES', 'Pesador', 'Producción', 'Operativo', 'Pesa e identifica las materias primas para producción.',
             [('Exactitud del pesaje (errores detectados)', 'Cero errores', 'Por lote'),
              ('Identificación correcta de materiales', 'Sin confusiones', 'Diario'),
              ('Oportunidad (entrega al ritmo de producción)', 'No detiene la planta', 'Diario')]),
            ('PROD-SUP', 'Supervisor', 'Producción', 'Coordinador', 'Coordina el turno y al personal operativo.',
             [('Cumplimiento del programa del turno', 'Plan cumplido', 'Diario'),
              ('Calidad del turno (rechazos/no conformidades)', 'Índices bajos', 'Mensual'),
              ('Arranque del turno a tiempo', 'Sin retrasos', 'Diario')]),
            ('PROD-JEF', 'Jefe de Producción', 'Producción', 'Jefe', 'Planea y responde por la producción de la planta.',
             [('Cumplimiento del plan (lotes vs programa)', 'Programa cumplido', 'Mensual'),
              ('Eficiencia del área (producción vs capacidad, merma)', 'Alta, baja merma', 'Mensual'),
              ('Continuidad operativa (paros evitables)', 'Mínimos paros', 'Mensual')]),
            ('MANT-FOG', 'Fogonero / Mantenimiento', 'Mantenimiento', 'Técnico', 'Opera la caldera y atiende el mantenimiento de equipos.',
             [('Operación estable de caldera (paros no programados)', 'Sin paros imprevistos', 'Mensual'),
              ('Parámetros en norma (presión/temperatura/agua)', 'Siempre en rango', 'Diario'),
              ('Mantenimiento preventivo (hechos vs programados)', 'Programa al corriente', 'Mensual')]),
            ('CAL-ANA', 'Analista', 'Calidad / Laboratorio', 'Técnico', 'Realiza los análisis de laboratorio a materias y producto.',
             [('Oportunidad de análisis (dentro del tiempo)', 'No retrasa liberación', 'Semanal'),
              ('Confiabilidad (reanálisis o errores)', 'Sin errores', 'Mensual'),
              ('Registro oportuno de bitácoras', 'Registros completos', 'Diario')]),
            ('CAL-INS', 'Inspector de Calidad', 'Calidad / Laboratorio', 'Técnico', 'Verifica en piso el cumplimiento de las buenas prácticas.',
             [('Cobertura de recepciones inspeccionadas', 'Nada entra sin revisión', 'Diario'),
              ('Oportunidad de liberación (acepta/rechaza)', 'Ágil y correcto', 'Diario'),
              ('Detección en piso (desviaciones BPM)', 'Detecta y reporta', 'Mensual')]),
            ('CAL-JEF', 'Jefe de Calidad', 'Calidad / Laboratorio', 'Jefe', 'Responde por el control de calidad y el laboratorio.',
             [('Liberaciones a tiempo (certificados en plazo)', 'Sin cuello de botella', 'Semanal'),
              ('Cierre de desviaciones en tiempo', 'Sin acumulación', 'Mensual'),
              ('Capacidad del laboratorio vs demanda', 'No limita a producción', 'Mensual')]),
            ('VEN-VEN', 'Vendedor', 'Ventas', 'Técnico', 'Vende y coloca producto en su zona o cartera.',
             [('Ventas contra meta ($ o unidades)', 'Alcanza o supera meta', 'Mensual'),
              ('Cobranza / cartera vencida', 'Cobranza oportuna', 'Mensual'),
              ('Cobertura (visitas vs planeadas)', 'Cumple su ruta', 'Semanal'),
              ('Desarrollo de cartera (clientes nuevos)', 'Cartera en crecimiento', 'Mensual')]),
            ('VEN-PRO', 'Promotor de Venta', 'Ventas', 'Operativo', 'Promueve e impulsa la venta en puntos de venta.',
             [('Ventas contra meta', 'Alcanza meta', 'Mensual'),
              ('Cobertura (visitas vs plan)', 'Cumple su ruta', 'Semanal'),
              ('Material publicitario colocado', 'Puntos activados', 'Mensual')]),
            ('VEN-DIS', 'Distribuidor', 'Ventas', 'Técnico', 'Distribuye y coloca producto en su zona asignada.',
             [('Ventas contra meta', 'Alcanza meta', 'Mensual'),
              ('Cobranza / cartera vencida', 'Cartera baja', 'Mensual'),
              ('Rotación y caducidades en punto de venta', 'Sin caducados', 'Mensual')]),
            ('VEN-AST', 'Asesor Técnico', 'Ventas', 'Técnico', 'Brinda soporte técnico de los productos a clientes y equipo.',
             [('Casos técnicos resueltos', 'Resuelve oportunamente', 'Mensual'),
              ('Capacitación técnica impartida', 'Capacita con regularidad', 'Mensual'),
              ('Apoyo a la venta (cierres asistidos)', 'Se traduce en ventas', 'Mensual')]),
            ('VEN-JEF', 'Jefe de Ventas y MKT', 'Ventas', 'Jefe', 'Dirige al equipo comercial y la estrategia.',
             [('Meta global del equipo', 'El equipo cumple', 'Mensual'),
              ('Cobranza global del equipo', 'Cartera sana', 'Mensual'),
              ('Crecimiento (clientes nuevos, cobertura)', 'La marca crece', 'Trimestral')]),
            ('IYD-DES', 'Desarrollo de Producto', 'Investigación y Desarrollo', 'Técnico', 'Formula y mejora productos nuevos y existentes.',
             [('Cumplimiento de proyectos (vs plan)', 'Cumple fechas', 'Mensual'),
              ('Calidad del desarrollo (lotes piloto)', 'Pocos rechazos en piloto', 'Mensual')]),
            ('IYD-JEF', 'Jefe de I&D', 'Investigación y Desarrollo', 'Jefe', 'Dirige los proyectos de desarrollo y regulatorios.',
             [('Avance del portafolio (proyectos a tiempo)', 'En tiempo', 'Mensual'),
              ('Registros regulatorios avanzados', 'Registros avanzando', 'Trimestral'),
              ('Validación de métodos analíticos', 'Métodos validados', 'Mensual')]),
            ('ALM-ENC', 'Encargado de Almacén', 'Almacén / Logística', 'Coordinador', 'Recibe, resguarda y entrega materiales y producto.',
             [('Exactitud de inventario (sistema vs físico)', 'Siempre cuadra', 'Mensual'),
              ('Control de caducidades', 'Sin caducados', 'Mensual'),
              ('Oportunidad de entrega a producción', 'No frena la planta', 'Diario')]),
            ('ALM-CHO', 'Chofer Auxiliar', 'Almacén / Logística', 'Operativo', 'Transporta y entrega mercancía y materiales.',
             [('Entregas a tiempo y completas', 'Cliente recibe bien y a tiempo', 'Diario'),
              ('Incidencias (siniestros, multas, faltantes)', 'Sin incidentes', 'Mensual'),
              ('Cuidado del vehículo y consumo de combustible', 'Buen estado, consumo razonable', 'Mensual')]),
            ('ADM-CON', 'Asistente Contable', 'Administración', 'Técnico', 'Registra y controla las operaciones contables diarias.',
             [('Registro oportuno y exacto (pólizas)', 'Sin atrasos ni errores', 'Diario'),
              ('Conciliaciones a tiempo', 'Puntuales y cuadradas', 'Mensual'),
              ('Documentación completa (CFDI, soportes)', 'Documentación íntegra', 'Mensual')]),
            ('ADM-JEF', 'Jefe de Administración', 'Administración', 'Jefe', 'Responde por las finanzas y la administración.',
             [('Cierres en fecha (contables y fiscales)', 'Siempre en fecha', 'Mensual'),
              ('Salud financiera (cuentas por cobrar/pagar)', 'Flujo sano', 'Mensual'),
              ('Cumplimiento normativo (sin multas)', 'Cumplimiento pleno', 'Trimestral')]),
            ('ADM-ASC', 'Asistente de Compras', 'Administración', 'Técnico', 'Apoya las compras: requisiciones, cotizaciones y órdenes.',
             [('Oportunidad (requisiciones en plazo)', 'Sin acumulación', 'Semanal'),
              ('Exactitud (cotizaciones/órdenes sin error)', 'Sin errores costosos', 'Mensual')]),
            ('ADM-AUC', 'Auxiliar de Compras', 'Administración', 'Operativo', 'Ejecuta el seguimiento operativo de compras.',
             [('Oportunidad (requisiciones en plazo)', 'Sin acumulación', 'Semanal'),
              ('Cotizaciones gestionadas', 'A tiempo', 'Semanal')]),
            ('ADM-JCO', 'Jefe de Compras', 'Administración', 'Jefe', 'Asegura el abasto de materiales e insumos a la planta.',
             [('Abasto oportuno (sin desabasto)', 'Sin desabasto que frene', 'Mensual'),
              ('Negociación (precios y condiciones)', 'Genera ahorro', 'Trimestral'),
              ('Proveedores confiables (entregas y calidad)', 'Proveedores que cumplen', 'Mensual')]),
            ('RH-EXT', 'Externo RH', 'Administración', 'Técnico', 'Recluta, selecciona y contrata al personal.',
             [('Tiempo de cobertura de vacantes', 'Cobertura ágil', 'Mensual'),
              ('Permanencia del personal contratado', 'El personal permanece', 'Trimestral'),
              ('Expediente de ingreso completo', 'Expediente íntegro', 'Mensual')]),
            ('DIR-GER', 'Gerente General', 'Dirección', 'Gerente', 'Dirige la operación integral de la empresa.',
             [('Índice global de la empresa', 'La empresa en verde', 'Mensual')]),
            ('DIR-DIR', 'Director General', 'Dirección', 'Dirección', 'Dirige estratégicamente a la empresa.',
             [('Resultado de la empresa (ventas, utilidad, producción, calidad)', 'Metas globales cumplidas', 'Trimestral')]),
        ]
        for cod, nom, area, niv, obj, kpis in seed_puestos:
            cur = conn.execute(
                'INSERT INTO puestos (codigo, nombre, area, nivel, objetivo) VALUES (?, ?, ?, ?, ?)',
                (cod, nom, area, niv, obj))
            pid = cur.lastrowid
            for i, (kn, km, kf) in enumerate(kpis, start=1):
                conn.execute(
                    'INSERT INTO puesto_kpis (puesto_id, nombre, meta, frecuencia, orden) VALUES (?, ?, ?, ?, ?)',
                    (pid, kn, km, kf, i))

    # === MÓDULO M7 · RECLUTAMIENTO & ONBOARDING ===
    conn.execute('''
        CREATE TABLE IF NOT EXISTS vacantes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            puesto TEXT NOT NULL, area TEXT, justificacion TEXT, canal TEXT,
            fecha_apertura TEXT DEFAULT (date('now','localtime')),
            fecha_cierre TEXT, estatus TEXT DEFAULT 'Abierta', creado_por TEXT
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS candidatos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vacante_id INTEGER NOT NULL,
            nombre TEXT NOT NULL, telefono TEXT, correo TEXT, notas TEXT,
            puntuacion INTEGER DEFAULT 0, recomendacion TEXT DEFAULT 'En espera',
            fecha TEXT DEFAULT (date('now','localtime')),
            FOREIGN KEY(vacante_id) REFERENCES vacantes(id) ON DELETE CASCADE
        )
    ''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cand_vacante ON candidatos(vacante_id)")

    # === MÓDULO M8 · DOCUMENTOS LEGALES Y COMPLIANCE ===
    # Repositorio central de formatos oficiales y normatividad (metadatos + archivo opcional).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS documentos_legales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo TEXT, nombre TEXT NOT NULL, categoria TEXT NOT NULL,
            version TEXT, vigencia TEXT, archivo TEXT, notas TEXT,
            estatus TEXT DEFAULT 'Vigente',
            actualizado TEXT DEFAULT (date('now','localtime'))
        )
    ''')
    # Maestro BPM SENASICA: 113 actividades con semáforo de avance (BPM-02 del Excel).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS bpm_actividades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero INTEGER, actividad TEXT NOT NULL, area TEXT,
            responsable TEXT, estatus TEXT DEFAULT 'Pendiente',
            fecha_compromiso TEXT, evidencia TEXT, notas TEXT
        )
    ''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_doclegal_cat ON documentos_legales(categoria)")
    # Semilla del repositorio de documentos legales (catálogo oficial ATISA de la matriz M8)
    if conn.execute("SELECT COUNT(*) AS n FROM documentos_legales").fetchone()['n'] == 0:
        for cod, nom, cat, ver in DOCUMENTOS_LEGALES_SEED:
            conn.execute(
                "INSERT INTO documentos_legales (codigo, nombre, categoria, version, estatus) VALUES (?, ?, ?, ?, 'Vigente')",
                (cod, nom, cat, ver))
    # Semilla del tracker BPM (actividades de muestra por área; el maestro real se carga después)
    if conn.execute("SELECT COUNT(*) AS n FROM bpm_actividades").fetchone()['n'] == 0:
        for i, (act, area) in enumerate(BPM_ACTIVIDADES_SEED, start=1):
            conn.execute(
                "INSERT INTO bpm_actividades (numero, actividad, area, estatus) VALUES (?, ?, ?, 'Pendiente')",
                (i, act, area))

    # === MÓDULO M10 · AUDITORÍA (trazabilidad de accesos y cambios) ===
    conn.execute('''
        CREATE TABLE IF NOT EXISTS auditoria (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT DEFAULT (datetime('now','localtime')),
            usuario TEXT, rol TEXT, tipo TEXT,
            accion TEXT, ruta TEXT, detalle TEXT, ip TEXT
        )
    ''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_auditoria_ts ON auditoria(ts)")

    # === MÓDULO M6 · CARDEX: eventos de trayectoria (promociones, reconocimientos, amonestaciones) ===
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cardex_eventos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_empleado TEXT NOT NULL,
            tipo TEXT NOT NULL,
            fecha TEXT NOT NULL,
            titulo TEXT NOT NULL,
            detalle TEXT,
            registrado_por TEXT,
            creado_en DATETIME DEFAULT (datetime('now','localtime')),
            FOREIGN KEY(numero_empleado) REFERENCES usuarios(numero_empleado)
        )
    ''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cardex_emp ON cardex_eventos(numero_empleado)")

    # --- Índices en columnas calientes (acelera SELECT/JOIN/WHERE) ---
    indices = [
        "CREATE INDEX IF NOT EXISTS idx_usuarios_estatus     ON usuarios(estatus)",
        "CREATE INDEX IF NOT EXISTS idx_resumen_fecha        ON resumen_asistencias(fecha)",
        "CREATE INDEX IF NOT EXISTS idx_resumen_nombre_fecha ON resumen_asistencias(nombre, fecha)",
        "CREATE INDEX IF NOT EXISTS idx_asistencias_emp_ts   ON asistencias(numero_empleado, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_zk_user_fecha        ON registros_zkteco(user_id, fecha)",
        "CREATE INDEX IF NOT EXISTS idx_asig_estatus         ON asignaciones_activos(estatus)",
        "CREATE INDEX IF NOT EXISTS idx_asig_emp             ON asignaciones_activos(numero_empleado)",
        "CREATE INDEX IF NOT EXISTS idx_inv_estatus          ON inventario_activos(estatus)",
        "CREATE INDEX IF NOT EXISTS idx_docs_emp             ON documentos_empleados(numero_empleado)",
        "CREATE INDEX IF NOT EXISTS idx_exp_emp              ON expertise_evaluaciones(numero_empleado)",
        "CREATE INDEX IF NOT EXISTS idx_exp_tarea            ON expertise_evaluaciones(tarea_id)",
        "CREATE INDEX IF NOT EXISTS idx_bpd_emp_fecha        ON bpd_evaluaciones(numero_empleado, fecha)",
        "CREATE INDEX IF NOT EXISTS idx_incid_fecha          ON incidencias_dia(fecha)",
        "CREATE INDEX IF NOT EXISTS idx_incid_emp            ON incidencias_dia(numero_empleado)",
        "CREATE INDEX IF NOT EXISTS idx_cap_emp              ON capacitacion_registro(numero_empleado)",
        "CREATE INDEX IF NOT EXISTS idx_5s_area              ON cinco_s_auditorias(area)",
        "CREATE INDEX IF NOT EXISTS idx_prod_fecha           ON produccion_llenado(fecha)",
    ]
    for sql in indices:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # por si alguna tabla aún no existe en una BD antigua

    # Índice ÚNICO: re-importar el mismo Excel del checador ya NO duplica checadas
    # (hace efectivo el INSERT OR IGNORE de importar_zkteco). Si una BD vieja ya tuviera
    # duplicados, simplemente se omite y no rompe el arranque.
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_zk_unico ON registros_zkteco(user_id, fecha, hora)")
    except sqlite3.Error:
        pass

    conn.commit()
    conn.close()

# --- 2. LOS CADENEROS ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return hx_redirect(url_for('login'))
        # Re-validar contra la BD en cada request: si al usuario lo dieron de baja
        # o le cambiaron el rol mientras estaba dentro, se aplica al instante (no se
        # queda con un rol "congelado" del login ni sigue navegando ya dado de baja).
        conn = get_db_connection()
        u = conn.execute(
            'SELECT nombre_completo, rol, estatus, debe_cambiar_password FROM usuarios WHERE id = ?',
            (session['user_id'],)
        ).fetchone()
        conn.close()
        if not u or u['estatus'] != 1:
            session.clear()
            flash('Tu sesión ya no es válida. Inicia sesión de nuevo.', 'error')
            return hx_redirect(url_for('login'))
        session['rol'] = u['rol']
        session['nombre'] = u['nombre_completo']
        # Contraseña inicial / temporal: no se usa el sistema hasta cambiarla.
        if u['debe_cambiar_password'] and request.endpoint != 'cambiar_password':
            return hx_redirect(url_for('cambiar_password'))
        return f(*args, **kwargs)
    return decorated_function

def _denegar():
    """Acceso no autorizado: avisa con toast y manda a una pantalla válida (sin trabarse)."""
    flash('No tienes permiso para acceder a esa sección.', 'error')
    return hx_redirect(url_for(landing_endpoint()))

def requiere_rol(rol_necesario):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session:
                return hx_redirect(url_for('login'))
            if session.get('rol') != rol_necesario and session.get('rol') != 'admin':
                return _denegar()
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def requiere_modulo(modulo):
    """Permite el acceso solo a los roles listados en PERMISOS[modulo]."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session:
                return hx_redirect(url_for('login'))
            if session.get('rol') not in PERMISOS.get(modulo, []):
                return _denegar()
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def _bloquea_quitar_admin(user_id, nuevo_rol=None, nuevo_estatus=0):
    """Devuelve un mensaje de error si la operación dejaría al sistema SIN ningún
    administrador activo, o si el usuario intenta darse de baja a sí mismo.
    Devuelve None si la operación es segura."""
    if str(user_id) == str(session.get('user_id')) and nuevo_estatus == 0:
        return 'No puedes darte de baja a ti mismo.'
    conn = get_db_connection()
    objetivo = conn.execute('SELECT rol, estatus FROM usuarios WHERE id = ?', (user_id,)).fetchone()
    mensaje = None
    if objetivo and objetivo['rol'] == 'admin' and objetivo['estatus'] == 1:
        deja_de_admin = (nuevo_rol is not None and nuevo_rol != 'admin') or nuevo_estatus == 0
        if deja_de_admin:
            n = conn.execute("SELECT COUNT(*) AS n FROM usuarios WHERE rol = 'admin' AND estatus = 1").fetchone()['n']
            if n <= 1:
                mensaje = 'No puedes quitar al único administrador activo del sistema.'
    conn.close()
    return mensaje

# --- 3. RUTAS ---

@app.route('/')
def index():
    if 'user_id' in session:
        return hx_redirect(url_for(landing_endpoint()))
    return hx_redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        clave = (request.remote_addr or '?', (username or '').strip().lower())
        # Anti-fuerza-bruta: si esta (IP, usuario) acumuló demasiados fallos, no se
        # revisa la contraseña (evita adivinar a base de miles de intentos).
        if _login_bloqueado(clave):
            _auditar('acceso', 'Bloqueo por intentos fallidos', '/login',
                     detalle=f'usuario: {username}', usuario=username or '—', rol='—')
            flash('Demasiados intentos fallidos. Espera unos minutos e intenta de nuevo.', 'error')
            return full_page('login.html')
        conn = get_db_connection()
        usuario = conn.execute('SELECT * FROM usuarios WHERE username = ? AND estatus = 1', (username,)).fetchone()
        conn.close()
        if usuario and check_password_hash(usuario['password_hash'], password):
            _login_limpiar(clave)
            if password == PASSWORD_INICIAL_ADMIN and not usuario['debe_cambiar_password']:
                # Bases creadas antes de este cambio: el admin sigue con la contraseña pública.
                conn = get_db_connection()
                conn.execute('UPDATE usuarios SET debe_cambiar_password = 1 WHERE id = ?', (usuario['id'],))
                conn.commit()
                conn.close()
            session.permanent = True          # activa la expiración de 8 h
            session['user_id'] = usuario['id']
            session['username'] = usuario['username']
            session['nombre'] = usuario['nombre_completo']
            session['rol'] = usuario['rol']
            _auditar('acceso', 'Inicio de sesión', '/login',
                     usuario=usuario['nombre_completo'], rol=usuario['rol'])
            return hx_redirect(url_for(landing_endpoint()))
        else:
            _login_registrar_fallo(clave)
            _auditar('acceso', 'Intento de acceso fallido', '/login',
                     detalle=f'usuario: {username}', usuario=username or '—', rol='—')
            flash('Datos incorrectos', 'error')
    return full_page('login.html')

@app.route('/logout')
def logout():
    _auditar('acceso', 'Cierre de sesión', '/logout')
    session.clear()
    return hx_redirect(url_for('login'))

@app.route('/cambiar_password', methods=['GET', 'POST'])
@login_required
def cambiar_password():
    """Cambio de contraseña del usuario en sesión (obligatorio si tiene la inicial)."""
    if request.method == 'POST':
        actual = request.form.get('password_actual') or ''
        nueva = request.form.get('password_nueva') or ''
        confirma = request.form.get('password_confirma') or ''
        conn = get_db_connection()
        u = conn.execute('SELECT password_hash FROM usuarios WHERE id = ?', (session['user_id'],)).fetchone()
        error = None
        if not u or not check_password_hash(u['password_hash'], actual):
            error = 'La contraseña actual no es correcta.'
        elif nueva != confirma:
            error = 'La nueva contraseña y su confirmación no coinciden.'
        elif nueva == actual or nueva == PASSWORD_INICIAL_ADMIN:
            error = 'La nueva contraseña debe ser distinta a la anterior.'
        else:
            error = _password_debil(nueva)
        if error:
            conn.close()
            flash(error, 'error')
            return full_page('cambiar_password.html')
        conn.execute('UPDATE usuarios SET password_hash = ?, debe_cambiar_password = 0 WHERE id = ?',
                     (generate_password_hash(nueva), session['user_id']))
        conn.commit()
        conn.close()
        _auditar('acceso', 'Cambio de contraseña', '/cambiar_password')
        flash('Contraseña actualizada.', 'success')
        return hx_redirect(url_for(landing_endpoint()))
    return full_page('cambiar_password.html')

@app.route('/sin_acceso')
@login_required
def sin_acceso():
    """Pantalla estática para usuarios cuyo rol no tiene módulos asignados."""
    return render_page('sin_acceso.html')

@app.route('/dashboard')
@login_required
@requiere_modulo('dashboard')
def dashboard():
    conn = get_db_connection()
    empleados = conn.execute(
        "SELECT * FROM usuarios WHERE estatus = 1 ORDER BY numero_empleado ASC"
    ).fetchall()
    metricas = {
        'empleados_activos': conn.execute("SELECT COUNT(*) AS n FROM usuarios WHERE estatus = 1").fetchone()['n'],
        'asistencias_hoy': conn.execute("SELECT COUNT(*) AS n FROM resumen_asistencias WHERE fecha = date('now', 'localtime')").fetchone()['n'],
        'retardos_hoy': conn.execute("SELECT COUNT(*) AS n FROM resumen_asistencias WHERE fecha = date('now', 'localtime') AND minutos_retardo > 0").fetchone()['n'],
        'recetas_total': conn.execute("SELECT COUNT(*) AS n FROM recetas").fetchone()['n'],
        'activos_asignados': conn.execute("SELECT COUNT(*) AS n FROM asignaciones_activos WHERE estatus = 'Activo'").fetchone()['n'],
    }
    conn.close()
    return render_page('dashboard.html', empleados=empleados, metricas=metricas)

@app.route('/empleados')
@login_required
@requiere_modulo('empleados')
def empleados():
    conn = get_db_connection()
    rows = conn.execute('''
        SELECT u.*, COALESCE(h.hora_entrada, '08:30') AS hora_entrada,
               COALESCE(h.hora_salida, '18:00') AS hora_salida
        FROM usuarios u
        LEFT JOIN horarios_empleados h ON u.numero_empleado = h.numero_empleado
        ORDER BY u.numero_empleado ASC
    ''').fetchall()
    conn.close()
    lista_empleados = []
    for r in rows:
        emp = dict(r)
        ant = antiguedad_anios(emp.get('fecha_ingreso'))
        emp['antiguedad'] = ant
        emp['vacaciones'] = dias_vacaciones_lft(ant) if ant is not None else None
        lista_empleados.append(emp)
    return render_page('empleados.html', empleados=lista_empleados)

@app.route('/crear_empleado', methods=['POST'])
@login_required
@requiere_modulo('empleados')
def crear_empleado():
    num_emp = (request.form.get('numero_empleado') or '').strip()
    nombre = (request.form.get('nombre_completo') or '').strip()
    user = (request.form.get('username') or '').strip()
    passw = request.form.get('password')
    rol = request.form.get('rol')
    hora_entrada = request.form.get('hora_entrada') or '08:30'
    hora_salida = request.form.get('hora_salida') or '18:00'
    puesto = (request.form.get('puesto') or '').strip() or None
    area = (request.form.get('area') or '').strip() or None
    cumpleanos = (request.form.get('cumpleanos') or '').strip() or None
    talla_calzado = (request.form.get('talla_calzado') or '').strip() or None
    fecha_ingreso = (request.form.get('fecha_ingreso') or '').strip() or None
    if not (num_emp and nombre and user and passw and rol):
        flash('Faltan datos obligatorios para crear el empleado.', 'error')
        return hx_redirect(url_for('empleados'))
    # Contraseña mínima (evita cuentas con claves triviales tipo "123").
    err_pass = _password_debil(passw)
    if err_pass:
        flash(err_pass, 'error')
        return hx_redirect(url_for('empleados'))
    # El rol debe ser uno válido; si no, el usuario quedaría sin acceso a nada.
    if rol not in ('admin', 'rh', 'operador'):
        flash('El rol seleccionado no es válido.', 'error')
        return hx_redirect(url_for('empleados'))
    # Blindaje anti-escalada: solo un admin puede crear cuentas admin
    if rol == 'admin' and session.get('rol') != 'admin':
        flash('Solo un administrador puede crear cuentas de administrador.', 'error')
        return hx_redirect(url_for('empleados'))
    conn = get_db_connection()
    try:
        hash_pass = generate_password_hash(passw)
        conn.execute('INSERT INTO usuarios (numero_empleado, nombre_completo, username, password_hash, rol) VALUES (?, ?, ?, ?, ?)', (num_emp, nombre, user, hash_pass, rol))
        conn.execute(
            'UPDATE usuarios SET puesto = ?, area = ?, cumpleanos = ?, talla_calzado = ?, fecha_ingreso = ? WHERE numero_empleado = ?',
            (puesto, area, cumpleanos, talla_calzado, fecha_ingreso, num_emp)
        )
        conn.execute(
            'INSERT OR REPLACE INTO horarios_empleados (numero_empleado, hora_entrada, hora_salida) VALUES (?, ?, ?)',
            (num_emp, hora_entrada, hora_salida)
        )
        conn.commit()
        cache.delete('empleados_activos')
        flash('Empleado creado correctamente', 'success')
    except sqlite3.IntegrityError as e:
        msg = str(e)
        if 'username' in msg:
            flash('Este nombre de usuario ya está en uso, por favor elige otro.', 'error')
        elif 'numero_empleado' in msg:
            flash('Ese número de empleado ya está registrado, usa uno distinto.', 'error')
        else:
            flash('Ya existe un registro con esos datos.', 'error')
    except Exception:
        flash('Error al crear usuario', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('empleados'))

@app.route('/editar_empleado', methods=['POST'])
@login_required
@requiere_modulo('empleados')
def editar_empleado():
    user_id = request.form.get('user_id')
    num_emp = (request.form.get('numero_empleado') or '').strip()
    nombre = (request.form.get('nombre_completo') or '').strip()
    rol = request.form.get('rol')
    estatus = request.form.get('estatus', 1)
    if not (nombre and rol):
        flash('Faltan datos obligatorios (nombre o rol).', 'error')
        return hx_redirect(url_for('empleados'))
    if rol not in ('admin', 'rh', 'operador'):
        flash('El rol seleccionado no es válido.', 'error')
        return hx_redirect(url_for('empleados'))
    passw = request.form.get('password')
    # Si se captura una nueva contraseña, debe cumplir el mínimo (vacío = no se cambia).
    if passw:
        err_pass = _password_debil(passw)
        if err_pass:
            flash(err_pass, 'error')
            return hx_redirect(url_for('empleados'))
    hora_entrada = request.form.get('hora_entrada') or '08:30'
    hora_salida = request.form.get('hora_salida') or '18:00'
    puesto = (request.form.get('puesto') or '').strip() or None
    area = (request.form.get('area') or '').strip() or None
    cumpleanos = (request.form.get('cumpleanos') or '').strip() or None
    talla_calzado = (request.form.get('talla_calzado') or '').strip() or None
    fecha_ingreso = (request.form.get('fecha_ingreso') or '').strip() or None
    # Blindaje anti-escalada: un no-admin no puede modificar cuentas admin ni promover a admin
    if session.get('rol') != 'admin':
        conn = get_db_connection()
        objetivo = conn.execute('SELECT rol FROM usuarios WHERE id = ?', (user_id,)).fetchone()
        conn.close()
        if objetivo and objetivo['rol'] == 'admin':
            flash('No tienes permiso para modificar una cuenta de administrador.', 'error')
            return hx_redirect(url_for('empleados'))
        if rol == 'admin':
            flash('No tienes permiso para asignar el rol de administrador.', 'error')
            return hx_redirect(url_for('empleados'))
    # Candado de integridad: no auto-bajarse ni dejar al sistema sin admin activo
    try:
        _estatus_int = int(estatus)
    except (TypeError, ValueError):
        _estatus_int = 1
    err = _bloquea_quitar_admin(user_id, nuevo_rol=rol, nuevo_estatus=_estatus_int)
    if err:
        flash(err, 'error')
        return hx_redirect(url_for('empleados'))
    conn = get_db_connection()
    try:
        # El número de empleado es la LLAVE del negocio (referenciada por horarios,
        # asignaciones, documentos, expertise, etc.). Es INMUTABLE al editar: cambiarlo
        # rompería todas esas referencias (y SQLite lo rechazaría). Se conserva el actual.
        actual = conn.execute('SELECT numero_empleado FROM usuarios WHERE id = ?', (user_id,)).fetchone()
        num_real = actual['numero_empleado'] if actual else num_emp
        conn.execute(
            '''UPDATE usuarios SET nombre_completo = ?, rol = ?, estatus = ?,
               puesto = ?, area = ?, cumpleanos = ?, talla_calzado = ?, fecha_ingreso = ? WHERE id = ?''',
            (nombre, rol, int(estatus), puesto, area, cumpleanos, talla_calzado, fecha_ingreso, user_id)
        )
        if passw:
            conn.execute(
                'UPDATE usuarios SET password_hash = ? WHERE id = ?',
                (generate_password_hash(passw), user_id)
            )
        conn.execute(
            'INSERT OR REPLACE INTO horarios_empleados (numero_empleado, hora_entrada, hora_salida) VALUES (?, ?, ?)',
            (num_real, hora_entrada, hora_salida)
        )
        conn.commit()
        cache.delete('empleados_activos')
        flash('Empleado actualizado correctamente', 'success')
    except sqlite3.IntegrityError as e:
        msg = str(e)
        if 'username' in msg:
            flash('Este nombre de usuario ya está en uso, por favor elige otro.', 'error')
        elif 'numero_empleado' in msg:
            flash('Ese número de empleado ya está registrado, usa uno distinto.', 'error')
        else:
            flash('Ya existe un registro con esos datos.', 'error')
    except Exception:
        flash('Error al actualizar empleado', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('empleados'))

@app.route('/eliminar_empleado', methods=['POST'])
@login_required
@requiere_modulo('empleados')
def eliminar_empleado():
    user_id = request.form.get('user_id')
    # Blindaje anti-escalada: un no-admin no puede dar de baja a un admin
    if session.get('rol') != 'admin':
        conn = get_db_connection()
        objetivo = conn.execute('SELECT rol FROM usuarios WHERE id = ?', (user_id,)).fetchone()
        conn.close()
        if objetivo and objetivo['rol'] == 'admin':
            flash('No tienes permiso para dar de baja a un administrador.', 'error')
            return hx_redirect(url_for('empleados'))
    # Candado de integridad: no auto-bajarse ni dejar al sistema sin admin activo
    err = _bloquea_quitar_admin(user_id, nuevo_estatus=0)
    if err:
        flash(err, 'error')
        return hx_redirect(url_for('empleados'))
    conn = get_db_connection()
    try:
        conn.execute('UPDATE usuarios SET estatus = 0 WHERE id = ?', (user_id,))
        conn.commit()
        cache.delete('empleados_activos')
        flash('Empleado dado de baja', 'success')
    except Exception:
        flash('Error al dar de baja al empleado', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('empleados'))

@app.route('/recetas')
@login_required
@requiere_modulo('recetas')
def recetas():
    conn = get_db_connection()
    recetas = conn.execute('''
        SELECT r.*, (SELECT COUNT(*) FROM recetas_pasos WHERE receta_id = r.id) AS total_pasos
        FROM recetas r
        ORDER BY r.id DESC
    ''').fetchall()
    conn.close()
    return render_page('recetas.html', recetas=recetas)

@app.route('/eliminar_receta', methods=['POST'])
@login_required
@requiere_rol('admin')
def eliminar_receta():
    receta_id = request.form.get('receta_id')
    confirmacion = request.form.get('confirmacion')
    if confirmacion != 'CONFIRMAR':
        flash('Eliminación cancelada: confirmación inválida', 'error')
        return hx_redirect(url_for('recetas'))
    conn = get_db_connection()
    try:
        pasos = conn.execute(
            'SELECT imagen_ruta FROM recetas_pasos WHERE receta_id = ?',
            (receta_id,)
        ).fetchall()
        for p in pasos:
            if p['imagen_ruta']:
                ruta = os.path.join('static', p['imagen_ruta'])
                if os.path.exists(ruta):
                    try:
                        os.remove(ruta)
                    except OSError:
                        pass
        conn.execute('DELETE FROM recetas_pasos WHERE receta_id = ?', (receta_id,))
        conn.execute('DELETE FROM recetas WHERE id = ?', (receta_id,))
        conn.commit()
        flash('Receta eliminada correctamente', 'success')
    except Exception:
        flash('Error al eliminar la receta', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('recetas'))

@app.route('/editar_receta_form/<int:receta_id>')
@login_required
@requiere_rol('admin')
def editar_receta_form(receta_id):
    conn = get_db_connection()
    receta = conn.execute('SELECT * FROM recetas WHERE id = ?', (receta_id,)).fetchone()
    pasos = conn.execute(
        'SELECT * FROM recetas_pasos WHERE receta_id = ? ORDER BY numero_paso ASC',
        (receta_id,)
    ).fetchall()
    conn.close()
    if not receta:
        flash('Receta no encontrada', 'error')
        return hx_redirect(url_for('recetas'))
    return render_page('crear_receta.html', receta=receta, pasos=pasos, modo='editar')

@app.route('/actualizar_receta/<int:receta_id>', methods=['POST'])
@login_required
@requiere_rol('admin')
def actualizar_receta(receta_id):
    nombre = request.form.get('nombre')
    descripcion = request.form.get('descripcion')
    conn = get_db_connection()
    try:
        conn.execute(
            'UPDATE recetas SET nombre = ?, descripcion = ? WHERE id = ?',
            (nombre, descripcion, receta_id)
        )
        pasos_existentes = conn.execute(
            'SELECT id, imagen_ruta FROM recetas_pasos WHERE receta_id = ?',
            (receta_id,)
        ).fetchall()
        mapa_existentes = {p['id']: p['imagen_ruta'] for p in pasos_existentes}
        ids_recibidos = set()
        paso_num = 1
        while True:
            instruccion = request.form.get(f'paso_{paso_num}_instruccion')
            if not instruccion:
                break
            minutos = max(0, _int_seguro(request.form.get(f'paso_{paso_num}_minutos'), 0))
            tiene_tiempo = 1 if minutos > 0 else 0
            paso_existente_id = request.form.get(f'paso_{paso_num}_id')
            imagen = request.files.get(f'paso_{paso_num}_imagen')
            ruta_imagen_actual = mapa_existentes.get(int(paso_existente_id)) if paso_existente_id else None
            ruta_imagen = ruta_imagen_actual or ''
            if imagen and imagen.filename and extension_permitida(imagen.filename):
                filename = secure_filename(imagen.filename)
                nuevo_nombre = f"receta_{receta_id}_paso_{paso_num}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{filename}"
                imagen.save(os.path.join(app.config['UPLOAD_FOLDER'], nuevo_nombre))
                ruta_imagen = f"img_recetas/{nuevo_nombre}"
            if paso_existente_id:
                ids_recibidos.add(int(paso_existente_id))
                conn.execute(
                    '''UPDATE recetas_pasos SET numero_paso = ?, instruccion = ?,
                       tiene_tiempo = ?, minutos = ?, imagen_ruta = ?
                       WHERE id = ? AND receta_id = ?''',
                    (paso_num, instruccion, tiene_tiempo, minutos, ruta_imagen,
                     int(paso_existente_id), receta_id)
                )
            else:
                conn.execute(
                    '''INSERT INTO recetas_pasos (receta_id, numero_paso, instruccion,
                       tiene_tiempo, minutos, imagen_ruta) VALUES (?, ?, ?, ?, ?, ?)''',
                    (receta_id, paso_num, instruccion, tiene_tiempo, minutos, ruta_imagen)
                )
            paso_num += 1
        for pid in mapa_existentes.keys():
            if pid not in ids_recibidos:
                conn.execute('DELETE FROM recetas_pasos WHERE id = ?', (pid,))
        conn.commit()
        flash('Receta actualizada correctamente', 'success')
    except Exception:
        flash('Error al actualizar la receta', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('recetas'))

@app.route('/crear_receta', methods=['POST'])
@login_required
@requiere_rol('admin')
def crear_receta():
    nombre = request.form.get('nombre')
    descripcion = request.form.get('descripcion')
    creado_por = session.get('nombre')
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('INSERT INTO recetas (nombre, descripcion, creado_por) VALUES (?, ?, ?)', (nombre, descripcion, creado_por))
        receta_id = cursor.lastrowid
        paso_num = 1
        while True:
            instruccion = request.form.get(f'paso_{paso_num}_instruccion')
            if not instruccion: break
            minutos = max(0, _int_seguro(request.form.get(f'paso_{paso_num}_minutos'), 0))
            tiene_tiempo = 1 if minutos > 0 else 0
            imagen = request.files.get(f'paso_{paso_num}_imagen')
            ruta_imagen = ""
            if imagen and imagen.filename and extension_permitida(imagen.filename):
                filename = secure_filename(imagen.filename)
                nuevo_nombre = f"receta_{receta_id}_paso_{paso_num}_{filename}"
                imagen.save(os.path.join(app.config['UPLOAD_FOLDER'], nuevo_nombre))
                ruta_imagen = f"img_recetas/{nuevo_nombre}"
            cursor.execute('INSERT INTO recetas_pasos (receta_id, numero_paso, instruccion, tiene_tiempo, minutos, imagen_ruta) VALUES (?, ?, ?, ?, ?, ?)', (receta_id, paso_num, instruccion, tiene_tiempo, minutos, ruta_imagen))
            paso_num += 1
        conn.commit()
        flash('Receta creada correctamente', 'success')
    except Exception:
        flash('Error al crear la receta', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('recetas'))

@app.route('/reportes')
@login_required
@requiere_modulo('reportes')
def reportes():
    periodo = request.args.get('periodo', 'semana')
    # Fuente de verdad: resumen_asistencias (lo que llena el import del checador).
    # Antes leía de la tabla 'asistencias', que NUNCA se poblaba -> el reporte salía vacío.
    if periodo == 'hoy':
        filtro_sql = "AND fecha = date('now', 'localtime')"
    elif periodo == 'mes':
        filtro_sql = "AND fecha >= date('now', 'localtime', 'start of month')"
    else:
        periodo = 'semana'
        filtro_sql = "AND fecha >= date('now', 'localtime', '-6 days')"
    conn = get_db_connection()
    usuarios = conn.execute("SELECT numero_empleado, nombre_completo FROM usuarios WHERE rol != 'admin'").fetchall()
    reportes_asistencia = []
    for u in usuarios:
        num_emp = u['numero_empleado']
        registros = conn.execute(
            f"SELECT hora_entrada_real, hora_salida_real, horas_extras FROM resumen_asistencias WHERE numero_empleado = ? {filtro_sql}",
            (num_emp,)
        ).fetchall()
        dias = len(registros)
        if dias == 0:
            continue
        hrs_reg = 0.0
        hrs_ext = 0.0
        for r in registros:
            ext = r['horas_extras'] or 0.0
            hrs_ext += ext
            ent = _parse_hora(r['hora_entrada_real'])
            sal = _parse_hora(r['hora_salida_real'])
            if ent and sal and sal > ent:
                trabajadas = (sal - ent).total_seconds() / 3600.0
                hrs_reg += max(0.0, trabajadas - ext)   # regulares = jornada - extras (sin doble conteo)
        nombre = (u['nombre_completo'] or '').split()
        if len(nombre) > 1:
            ini = (nombre[0][0] + nombre[1][0]).upper()
        elif len(nombre) == 1:
            ini = nombre[0][0:2].upper()
        else:
            ini = '?'
        reportes_asistencia.append({'numero_empleado': num_emp, 'nombre_completo': u['nombre_completo'],
                                    'iniciales': ini, 'dias_trabajados': dias,
                                    'horas_regulares': round(hrs_reg, 1), 'horas_extras': round(hrs_ext, 1)})
    conn.close()
    return render_page('reportes.html', reportes_asistencia=reportes_asistencia, periodo=periodo)

# Datos de la plantilla que SÍ pueden salir en el Excel general de Reportes.
# Lista fija en código (no inyectable). Los datos sensibles se consultan solo en el
# expediente individual, dentro del sistema.
COLUMNAS_EXPORTABLES_USUARIOS = [
    'numero_empleado', 'nombre_completo', 'puesto', 'area', 'rol', 'estatus',
    'fecha_ingreso', 'tipo_contrato', 'fecha_fin_contrato', 'tipo_jornada', 'turno',
    'jefe_inmediato', 'fecha_baja',
]

@app.route('/exportar_excel')
@login_required
@requiere_modulo('reportes')
def exportar_excel():
    conn = get_db_connection()
    try:
        # Columnas EXPLÍCITAS: nunca contraseñas ni datos personales sensibles
        # (CURP, RFC, NSS, domicilio, salario, CLABE...). Ver COLUMNAS_EXPORTABLES_USUARIOS.
        df_usuarios = pd.read_sql_query(
            f"SELECT {', '.join(COLUMNAS_EXPORTABLES_USUARIOS)} FROM usuarios WHERE rol != 'admin'", conn)
        df_inventario = pd.read_sql_query('SELECT * FROM inventario_activos', conn)
        df_asignaciones = pd.read_sql_query('''
            SELECT a.id, u.nombre_completo, i.categoria, i.descripcion,
                   a.fecha_prestamo, a.estatus
            FROM asignaciones_activos a
            JOIN usuarios u ON a.numero_empleado = u.numero_empleado
            JOIN inventario_activos i ON a.activo_id = i.id
            WHERE a.estatus = 'Activo'
        ''', conn)
        df_tarjetas = pd.read_sql_query('''
            SELECT t.id, t.numero_tarjeta, t.saldo, t.estatus,
                   u.nombre_completo as responsable
            FROM tarjetas_gasolina t
            LEFT JOIN usuarios u ON t.numero_empleado = u.numero_empleado
        ''', conn)
        df_bitacora = pd.read_sql_query('SELECT * FROM bitacora_gasolina ORDER BY id DESC', conn)
    finally:
        conn.close()

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df_usuarios.to_excel(writer, sheet_name='Usuarios', index=False)
        df_inventario.to_excel(writer, sheet_name='Inventario', index=False)
        df_asignaciones.to_excel(writer, sheet_name='Asignaciones', index=False)
        df_tarjetas.to_excel(writer, sheet_name='Tarjetas Gasolina', index=False)
        df_bitacora.to_excel(writer, sheet_name='Bitacora', index=False)
    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        download_name='reporte_atisa.xlsx',
        as_attachment=True
    )

@app.route('/gasolina')
@login_required
@requiere_modulo('gasolina')
def gasolina():
    conn = get_db_connection()
    empleados = conn.execute('SELECT * FROM usuarios ORDER BY nombre_completo').fetchall()
    tarjetas = conn.execute('''
        SELECT t.id, t.numero_tarjeta, t.saldo, t.estatus, t.numero_empleado,
               u.nombre_completo as nombre_empleado
        FROM tarjetas_gasolina t
        LEFT JOIN usuarios u ON t.numero_empleado = u.numero_empleado
        ORDER BY t.id DESC
    ''').fetchall()
    registros = conn.execute('SELECT * FROM bitacora_gasolina ORDER BY id DESC').fetchall()
    conn.close()
    return render_page('gasolina.html', tarjetas=tarjetas, empleados=empleados, registros=registros)

@app.route('/registrar_tarjeta', methods=['POST'])
@login_required
@requiere_modulo('gasolina')
def registrar_tarjeta():
    numero_tarjeta = (request.form.get('numero_tarjeta') or '').strip()
    if not numero_tarjeta:
        flash('Escribe el número de la tarjeta.', 'error')
        return hx_redirect(url_for('gasolina'))
    try:
        saldo_f = float(request.form.get('saldo_inicial') or 0)
    except (TypeError, ValueError):
        flash('El saldo inicial debe ser un número válido.', 'error')
        return hx_redirect(url_for('gasolina'))
    if saldo_f < 0:
        flash('El saldo inicial no puede ser negativo.', 'error')
        return hx_redirect(url_for('gasolina'))
    conn = get_db_connection()
    try:
        conn.execute(
            'INSERT INTO tarjetas_gasolina (numero_tarjeta, saldo) VALUES (?, ?)',
            (numero_tarjeta, saldo_f)
        )
        conn.commit()
        flash('Tarjeta registrada correctamente', 'success')
    except Exception:
        flash('Error al registrar tarjeta (¿número duplicado?)', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('gasolina'))

@app.route('/asignar_gasolina', methods=['POST'])
@login_required
@requiere_modulo('gasolina')
def asignar_gasolina():
    empleado_id = request.form.get('empleado_id')
    tarjeta_id = request.form.get('tarjeta_id')
    conn = get_db_connection()
    try:
        conn.execute(
            'INSERT INTO asignacion_gasolina (numero_empleado, tarjeta_id) VALUES (?, ?)',
            (empleado_id, tarjeta_id)
        )
        conn.execute(
            "UPDATE tarjetas_gasolina SET estatus = 'Asignada', numero_empleado = ? WHERE id = ?",
            (empleado_id, tarjeta_id)
        )
        conn.commit()
        flash('Tarjeta asignada correctamente', 'success')
    except Exception:
        flash('Error al asignar tarjeta', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('gasolina'))

ESTATUS_TARJETA_VALIDOS = {'Disponible', 'Asignada', 'Bloqueada'}

@app.route('/editar_tarjeta', methods=['POST'])
@login_required
@requiere_modulo('gasolina')
def editar_tarjeta():
    tarjeta_id = request.form.get('tarjeta_id')
    estatus = (request.form.get('estatus') or '').strip()
    try:
        saldo_f = float(request.form.get('saldo'))
    except (TypeError, ValueError):
        flash('El saldo debe ser un número válido.', 'error')
        return hx_redirect(url_for('gasolina'))
    if saldo_f < 0:
        flash('El saldo no puede ser negativo.', 'error')
        return hx_redirect(url_for('gasolina'))
    if estatus not in ESTATUS_TARJETA_VALIDOS:
        flash('Estatus de tarjeta no válido.', 'error')
        return hx_redirect(url_for('gasolina'))
    conn = get_db_connection()
    try:
        cur = conn.execute(
            'UPDATE tarjetas_gasolina SET saldo = ?, estatus = ? WHERE id = ?',
            (saldo_f, estatus, tarjeta_id)
        )
        conn.commit()
        if cur.rowcount == 0:
            flash('Tarjeta no encontrada.', 'error')
        else:
            flash('Tarjeta actualizada correctamente', 'success')
    except Exception:
        flash('Error al actualizar tarjeta', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('gasolina'))

@app.route('/registrar_carga', methods=['POST'])
@login_required
@requiere_modulo('gasolina')
def registrar_carga():
    tarjeta_id = request.form.get('tarjeta_id')
    no_estacion = request.form.get('no_estacion')
    monto = request.form.get('monto')
    nombre_empleado = request.form.get('nombre_empleado')
    # Validación del monto ANTES de tocar la tarjeta: debe ser un número > 0.
    # (Un monto negativo haría saldo - (-x) = saldo + x, inflando la tarjeta.)
    try:
        monto_f = float(monto)
    except (TypeError, ValueError):
        flash('El monto debe ser un número válido.', 'error')
        return hx_redirect(url_for('gasolina'))
    if monto_f <= 0:
        flash('El monto de la carga debe ser mayor a $0.', 'error')
        return hx_redirect(url_for('gasolina'))
    conn = get_db_connection()
    try:
        # La tarjeta debe existir ANTES de registrar la carga (si no, quedaría una
        # bitácora huérfana y un "registrado correctamente" engañoso sin descontar nada).
        if not conn.execute('SELECT 1 FROM tarjetas_gasolina WHERE id = ?', (tarjeta_id,)).fetchone():
            flash('La tarjeta seleccionada no existe.', 'error')
            return hx_redirect(url_for('gasolina'))
        conn.execute(
            'INSERT INTO bitacora_gasolina (nombre_empleado, no_estacion, monto) VALUES (?, ?, ?)',
            (nombre_empleado, no_estacion, monto_f)
        )
        conn.execute(
            'UPDATE tarjetas_gasolina SET saldo = saldo - ? WHERE id = ?',
            (monto_f, tarjeta_id)
        )
        fila = conn.execute('SELECT saldo FROM tarjetas_gasolina WHERE id = ?', (tarjeta_id,)).fetchone()
        conn.commit()
        # Se permite el sobregiro, pero se avisa si la tarjeta quedó en saldo negativo
        if fila and fila['saldo'] < 0:
            flash(f'Carga registrada. ⚠️ La tarjeta quedó con saldo NEGATIVO de ${fila["saldo"]:.2f}.', 'error')
        else:
            flash('Carga registrada correctamente', 'success')
    except Exception:
        flash('Error al registrar carga', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('gasolina'))

@app.route('/activos')
@login_required
@requiere_modulo('activos')
def activos():
    conn = get_db_connection()
    inventario = conn.execute('SELECT * FROM inventario_activos').fetchall()
    asignaciones = conn.execute('''
        SELECT a.id, u.nombre_completo, i.categoria, i.descripcion,
               a.fecha_prestamo, a.ruta_contrato, a.firmada, a.tipo_responsiva,
               a.puesto, a.area
        FROM asignaciones_activos a
        JOIN usuarios u ON a.numero_empleado = u.numero_empleado
        JOIN inventario_activos i ON a.activo_id = i.id
        WHERE a.estatus = 'Activo'
        ORDER BY a.id DESC
    ''').fetchall()
    empleados = conn.execute("SELECT * FROM usuarios WHERE estatus = 1 ORDER BY nombre_completo").fetchall()
    conn.close()
    return render_page('activos.html', inventario=inventario, asignaciones=asignaciones, empleados=empleados)

@app.route('/crear_activo', methods=['POST'])
@login_required
@requiere_modulo('activos')
def crear_activo():
    categoria = (request.form.get('categoria') or '').strip()
    descripcion = (request.form.get('descripcion') or '').strip()
    if not (categoria and descripcion):
        flash('Selecciona la categoría y escribe la descripción del activo.', 'error')
        return hx_redirect(url_for('activos'))
    conn = get_db_connection()
    try:
        conn.execute(
            'INSERT INTO inventario_activos (categoria, descripcion) VALUES (?, ?)',
            (categoria, descripcion)
        )
        conn.commit()
        flash('Activo dado de alta correctamente', 'success')
    except Exception:
        flash('Error al crear activo', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('activos'))

@app.route('/asignar_activo', methods=['POST'])
@login_required
@requiere_modulo('activos')
def asignar_activo():
    numero_empleado = request.form.get('numero_empleado')
    activo_id = request.form.get('activo_id')
    puesto = request.form.get('puesto') or None
    area = request.form.get('area') or None
    marca = request.form.get('marca') or None
    modelo = request.form.get('modelo') or None
    no_serie = request.form.get('no_serie') or None
    imei = request.form.get('imei') or None
    observaciones = request.form.get('observaciones') or None
    tipo_responsiva = request.form.get('tipo_responsiva') or 'Comodato de Celular'
    placa = request.form.get('placa') or None
    color = request.form.get('color') or None
    talla = request.form.get('talla') or None
    conn = get_db_connection()
    nueva_id = None
    try:
        # Candado: no asignar un activo inexistente ni uno que ya está asignado (evita doble asignación)
        inv = conn.execute("SELECT estatus FROM inventario_activos WHERE id = ?", (activo_id,)).fetchone()
        if not inv:
            flash('El activo seleccionado no existe.', 'error')
            return hx_redirect(url_for('activos'))
        if inv['estatus'] != 'Disponible':
            flash('Ese activo no está disponible (ya está asignado). Devuélvelo antes de reasignarlo.', 'error')
            return hx_redirect(url_for('activos'))
        # Si no se capturó puesto/área, tomarlos del EXPEDIENTE del empleado (fuente única).
        if not puesto or not area:
            emp = conn.execute(
                'SELECT puesto, area FROM usuarios WHERE numero_empleado = ?', (numero_empleado,)
            ).fetchone()
            if emp:
                puesto = puesto or emp['puesto']
                area = area or emp['area']
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO asignaciones_activos
               (numero_empleado, activo_id, puesto, area, marca, modelo, no_serie,
                observaciones, tipo_responsiva, imei, firmada, placa, color, talla)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)''',
            (numero_empleado, activo_id, puesto, area, marca, modelo, no_serie,
             observaciones, tipo_responsiva, imei, placa, color, talla)
        )
        nueva_id = cursor.lastrowid
        conn.execute(
            "UPDATE inventario_activos SET estatus = 'Asignado' WHERE id = ?",
            (activo_id,)
        )
        conn.commit()
        flash('Activo asignado. Genera la responsiva, imprímela y súbela firmada.', 'success')
    except Exception:
        flash('Error al asignar activo', 'error')
    finally:
        conn.close()
    if nueva_id:
        return hx_redirect(url_for('responsiva', asignacion_id=nueva_id))
    return hx_redirect(url_for('activos'))

@app.route('/responsiva/<int:asignacion_id>')
@login_required
@requiere_modulo('activos')
def responsiva(asignacion_id):
    conn = get_db_connection()
    row = conn.execute('''
        SELECT a.*, u.nombre_completo, u.username, i.categoria, i.descripcion
        FROM asignaciones_activos a
        JOIN usuarios u ON a.numero_empleado = u.numero_empleado
        JOIN inventario_activos i ON a.activo_id = i.id
        WHERE a.id = ?
    ''', (asignacion_id,)).fetchone()
    conn.close()
    if not row:
        flash('Asignación no encontrada', 'error')
        return hx_redirect(url_for('activos'))
    return full_page('responsiva.html', a=row, hoy=datetime.now())

@app.route('/subir_contrato_firmado/<int:asignacion_id>', methods=['POST'])
@login_required
@requiere_modulo('activos')
def subir_contrato_firmado(asignacion_id):
    archivo = request.files.get('contrato_firmado')
    if not archivo or not archivo.filename:
        flash('Debe seleccionar el archivo escaneado', 'error')
        return hx_redirect(url_for('activos'))
    if not extension_permitida(archivo.filename):
        flash('Formato no permitido. Solo se aceptan archivos PDF, JPG o PNG.', 'error')
        return hx_redirect(url_for('activos'))
    filename = secure_filename(archivo.filename)
    nombre_unico = f"responsiva_firmada_{asignacion_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{filename}"
    ruta_fisica = os.path.join(app.config['UPLOAD_CONTRATOS'], nombre_unico)
    conn = get_db_connection()
    try:
        archivo.save(ruta_fisica)
        conn.execute(
            'UPDATE asignaciones_activos SET ruta_contrato = ?, firmada = 1 WHERE id = ?',
            (ruta_fisica, asignacion_id)
        )
        conn.commit()
        flash('Responsiva firmada cargada correctamente', 'success')
    except Exception:
        flash('Error al subir la responsiva firmada', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('activos'))

@app.route('/devolver_activo', methods=['POST'])
@login_required
@requiere_modulo('activos')
def devolver_activo():
    asignacion_id = request.form.get('asignacion_id')
    conn = get_db_connection()
    try:
        row = conn.execute(
            'SELECT activo_id, estatus FROM asignaciones_activos WHERE id = ?',
            (asignacion_id,)
        ).fetchone()
        if row and row['estatus'] == 'Activo':
            conn.execute(
                "UPDATE asignaciones_activos SET estatus = 'Devuelto' WHERE id = ?",
                (asignacion_id,)
            )
            # Solo liberar el activo si NO tiene otra asignación vigente (evita desincronizar
            # el inventario si el activo ya fue re-asignado a otra persona).
            otra = conn.execute(
                "SELECT 1 FROM asignaciones_activos WHERE activo_id = ? AND estatus = 'Activo' AND id != ? LIMIT 1",
                (row['activo_id'], asignacion_id)
            ).fetchone()
            if not otra:
                conn.execute(
                    "UPDATE inventario_activos SET estatus = 'Disponible' WHERE id = ?",
                    (row['activo_id'],)
                )
            conn.commit()
            flash('Activo devuelto correctamente', 'success')
        elif row:
            flash('Esta asignación ya había sido devuelta.', 'error')
        else:
            flash('Asignación no encontrada', 'error')
    except Exception:
        flash('Error al devolver activo', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('activos'))

ESTATUS_ACTIVO_VALIDOS = {'Disponible', 'Asignado', 'Baja', 'Mantenimiento'}

@app.route('/editar_activo', methods=['POST'])
@login_required
@requiere_modulo('activos')
def editar_activo():
    activo_id = request.form.get('activo_id')
    categoria = (request.form.get('categoria') or '').strip()
    descripcion = (request.form.get('descripcion') or '').strip()
    estatus = (request.form.get('estatus') or '').strip()
    if not (categoria and descripcion):
        flash('La categoría y la descripción del activo no pueden quedar vacías.', 'error')
        return hx_redirect(url_for('activos'))
    if estatus not in ESTATUS_ACTIVO_VALIDOS:
        flash('Estatus de activo no válido.', 'error')
        return hx_redirect(url_for('activos'))
    conn = get_db_connection()
    try:
        # Candado anti-desync: no marcar 'Disponible' un activo que sigue asignado
        # (si no, se podría volver a asignar y quedar prestado a dos personas a la vez).
        if estatus == 'Disponible':
            asignado = conn.execute(
                "SELECT 1 FROM asignaciones_activos WHERE activo_id = ? AND estatus = 'Activo' LIMIT 1",
                (activo_id,)
            ).fetchone()
            if asignado:
                flash('No puedes marcar como Disponible un activo que sigue asignado. Devuélvelo primero.', 'error')
                return hx_redirect(url_for('activos'))
        conn.execute(
            'UPDATE inventario_activos SET categoria = ?, descripcion = ?, estatus = ? WHERE id = ?',
            (categoria, descripcion, estatus, activo_id)
        )
        conn.commit()
        flash('Activo actualizado correctamente', 'success')
    except Exception:
        flash('Error al actualizar activo', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('activos'))

@app.route('/ver_contrato/<int:asignacion_id>')
@login_required
@requiere_modulo('activos')
def ver_contrato(asignacion_id):
    conn = get_db_connection()
    row = conn.execute(
        'SELECT ruta_contrato FROM asignaciones_activos WHERE id = ?',
        (asignacion_id,)
    ).fetchone()
    conn.close()
    if not row or not row['ruta_contrato'] or not os.path.exists(row['ruta_contrato']):
        flash('Contrato no disponible', 'error')
        return hx_redirect(url_for('activos'))
    resp = make_response(send_file(row['ruta_contrato']))
    resp.headers['X-Content-Type-Options'] = 'nosniff'   # evita sniffing -> XSS
    return resp

@app.route('/documentos')
@login_required
@requiere_modulo('documentos')
def documentos():
    conn = get_db_connection()
    empleados = conn.execute('SELECT * FROM usuarios ORDER BY nombre_completo').fetchall()
    documentos = conn.execute('''
        SELECT d.id, d.numero_empleado, d.tipo_documento, d.nombre_archivo,
               d.ruta_archivo, d.fecha_subida, u.nombre_completo
        FROM documentos_empleados d
        LEFT JOIN usuarios u ON d.numero_empleado = u.numero_empleado
        ORDER BY d.id DESC
    ''').fetchall()
    conn.close()
    return render_page('documentos.html', empleados=empleados, documentos=documentos)

@app.route('/subir_documento', methods=['POST'])
@login_required
@requiere_modulo('documentos')
def subir_documento():
    numero_empleado = request.form.get('numero_empleado')
    tipo_documento = request.form.get('tipo_documento')
    archivo = request.files.get('archivo')
    # Regreso: si la subida viene de la ficha de expediente, vuelve ahí.
    # Solo se permiten rutas internas (evita open-redirect a sitios externos).
    destino = _destino_interno(request.form.get('volver'), url_for('documentos'))
    if not archivo or not archivo.filename:
        flash('Debe seleccionar un archivo', 'error')
        return hx_redirect(destino)
    if not extension_permitida(archivo.filename):
        flash('Formato no permitido. Solo se aceptan archivos PDF, JPG o PNG.', 'error')
        return hx_redirect(destino)
    filename = secure_filename(archivo.filename)
    nombre_unico = f"doc_{numero_empleado}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{filename}"
    ruta_fisica = os.path.join(app.config['UPLOAD_CONTRATOS'], nombre_unico)
    conn = get_db_connection()
    try:
        archivo.save(ruta_fisica)
        conn.execute(
            'INSERT INTO documentos_empleados (numero_empleado, tipo_documento, nombre_archivo, ruta_archivo) VALUES (?, ?, ?, ?)',
            (numero_empleado, tipo_documento, filename, ruta_fisica)
        )
        conn.commit()
        flash('Documento subido correctamente', 'success')
    except Exception:
        flash('Error al subir documento', 'error')
    finally:
        conn.close()
    return hx_redirect(destino)


# --- M1 · EXPEDIENTE DIGITAL DEL COLABORADOR ---
# Documentos requeridos del expediente (checklist BPM/SENASICA). El nombre se usa
# como 'tipo_documento' al subir el archivo, para casar cada archivo con su casilla.
DOCS_EXPEDIENTE = [
    'Solicitud de empleo', 'Contrato de trabajo', 'INE / IFE', 'CURP oficial',
    'Comprobante de domicilio', 'Acta de nacimiento', 'RFC (constancia fiscal)', 'Alta IMSS',
    'Cartilla IMSS', 'Comprobante escolar', 'Cédula profesional',
    'Carta de no antecedentes penales', 'Licencia de manejo', 'Acuerdo de confidencialidad',
    'Acta de inducción', 'Acta de recepción de puesto', 'Examen médico de ingreso',
    'Fotos para credencial', 'Constancias de capacitación',
]
# Campos del expediente que edita RH (columnas REALES de 'usuarios'; lista FIJA en código).
EXPEDIENTE_CAMPOS = [
    'fecha_nacimiento', 'curp', 'rfc', 'nss', 'domicilio', 'telefono', 'correo', 'estado_civil',
    'num_dependientes', 'emergencia_nombre', 'emergencia_parentesco', 'emergencia_telefono',
    'escolaridad', 'cedula_profesional', 'puesto', 'area', 'fecha_ingreso', 'jefe_inmediato',
    'tipo_contrato', 'fecha_fin_contrato', 'salario_diario', 'tipo_jornada', 'turno',
    'infonavit', 'fonacot', 'banco_clabe',
]
# Campos que cuentan para el % de completitud del expediente.
EXPEDIENTE_CLAVE = [
    'fecha_nacimiento', 'curp', 'rfc', 'nss', 'domicilio', 'telefono', 'correo',
    'contacto_emergencia', 'escolaridad', 'puesto', 'area', 'fecha_ingreso', 'jefe_inmediato',
    'tipo_contrato', 'salario_diario', 'tipo_jornada', 'banco_clabe',
]
EXP_CATALOGOS = {
    'estado_civil': ['Soltero(a)', 'Casado(a)', 'Divorciado(a)', 'Unión libre', 'Viudo(a)'],
    'tipo_contrato': ['Indefinido', 'Temporal', 'Por obra determinada', 'Prueba', 'Honorarios'],
    'tipo_jornada': ['Diurna', 'Nocturna', 'Mixta', 'Por obra'],
    'turno': ['Matutino', 'Vespertino', 'Nocturno', 'Rotativo'],
    'area': ['Producción', 'Calidad / Laboratorio', 'Ventas', 'Almacén / Logística',
             'Administración', 'Mantenimiento'],
}


@app.route('/expediente/<numero_empleado>')
@login_required
@requiere_modulo('empleados')
def expediente(numero_empleado):
    conn = get_db_connection()
    row = conn.execute('''
        SELECT u.*, h.hora_entrada, h.hora_salida
        FROM usuarios u
        LEFT JOIN horarios_empleados h ON u.numero_empleado = h.numero_empleado
        WHERE u.numero_empleado = ?
    ''', (numero_empleado,)).fetchone()
    if not row:
        conn.close()
        flash('Empleado no encontrado.', 'error')
        return hx_redirect(url_for('empleados'))
    emp = dict(row)
    docs = conn.execute(
        'SELECT id, tipo_documento, nombre_archivo, fecha_subida FROM documentos_empleados '
        'WHERE numero_empleado = ? ORDER BY fecha_subida DESC',
        (numero_empleado,)
    ).fetchall()
    conn.close()

    # Casar cada documento requerido con el archivo más reciente de ese tipo
    por_tipo = {}
    for d in docs:
        por_tipo.setdefault(d['tipo_documento'], d)
    checklist = [{'nombre': t, 'doc': por_tipo.get(t)} for t in DOCS_EXPEDIENTE]
    docs_presentes = sum(1 for c in checklist if c['doc'])
    pct_docs = round(docs_presentes / len(DOCS_EXPEDIENTE) * 100)

    def lleno(campo):
        if campo == 'contacto_emergencia':
            return bool((emp.get('emergencia_nombre') or '').strip()
                        and (emp.get('emergencia_telefono') or '').strip())
        return bool(str(emp.get(campo) or '').strip())
    campos_llenos = sum(1 for c in EXPEDIENTE_CLAVE if lleno(c))
    pct_campos = round(campos_llenos / len(EXPEDIENTE_CLAVE) * 100)
    pct_total = round((pct_campos + pct_docs) / 2)

    ant = antiguedad_anios(emp.get('fecha_ingreso'))
    emp['antiguedad'] = ant
    emp['vacaciones'] = dias_vacaciones_lft(ant) if ant is not None else None
    return render_page('expediente.html', emp=emp, checklist=checklist,
                       docs_presentes=docs_presentes, total_docs=len(DOCS_EXPEDIENTE),
                       pct_docs=pct_docs, pct_campos=pct_campos, pct_total=pct_total,
                       catalogos=EXP_CATALOGOS)


@app.route('/expediente/guardar', methods=['POST'])
@login_required
@requiere_modulo('empleados')
def expediente_guardar():
    num = (request.form.get('numero_empleado') or '').strip()
    if not num:
        flash('Falta el empleado.', 'error')
        return hx_redirect(url_for('empleados'))
    # SET dinámico, pero con nombres de columna de una lista FIJA en código (no inyectable).
    sets = ', '.join(f'{c} = ?' for c in EXPEDIENTE_CAMPOS)
    valores = [(request.form.get(c) or '').strip() or None for c in EXPEDIENTE_CAMPOS]
    conn = get_db_connection()
    try:
        conn.execute(f'UPDATE usuarios SET {sets} WHERE numero_empleado = ?', (*valores, num))
        conn.commit()
        cache.delete('empleados_activos')
        flash('Expediente guardado correctamente.', 'success')
    except Exception:
        flash('No se pudo guardar el expediente.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('expediente', numero_empleado=num))


# --- M3 · PERFIL DE PUESTO & KPIs ---
PUESTO_CAMPOS = ['codigo', 'nombre', 'area', 'nivel', 'objetivo', 'escolaridad',
                 'experiencia', 'conocimientos', 'habilidades', 'responsabilidades_bpm',
                 'epp', 'riesgos']
PUESTO_NIVELES = ['Operativo', 'Técnico', 'Coordinador', 'Jefe', 'Gerente', 'Dirección']
PUESTO_AREAS = ['Producción', 'Calidad / Laboratorio', 'Ventas', 'Almacén / Logística',
                'Administración', 'Mantenimiento', 'Investigación y Desarrollo', 'Dirección']
KPI_FRECUENCIAS = ['Diario', 'Semanal', 'Quincenal', 'Mensual', 'Trimestral', 'Anual', 'Por lote']


@app.route('/perfiles')
@login_required
@requiere_modulo('perfiles')
def perfiles():
    conn = get_db_connection()
    rows = conn.execute('''
        SELECT p.*,
               (SELECT COUNT(*) FROM puesto_kpis k WHERE k.puesto_id = p.id) AS n_kpis,
               (SELECT COUNT(*) FROM usuarios u WHERE u.estatus = 1 AND u.puesto = p.nombre) AS n_personas
        FROM puestos p WHERE p.activo = 1 ORDER BY p.area ASC, p.nombre ASC
    ''').fetchall()
    conn.close()
    por_area = {}
    for r in rows:
        por_area.setdefault(r['area'] or 'Sin área', []).append(dict(r))
    total_kpis = sum(r['n_kpis'] for r in rows)
    return render_page('perfiles.html', por_area=por_area, total_puestos=len(rows),
                       total_kpis=total_kpis, niveles=PUESTO_NIVELES, areas=PUESTO_AREAS)


@app.route('/perfiles/<int:pid>')
@login_required
@requiere_modulo('perfiles')
def perfil_detalle(pid):
    conn = get_db_connection()
    p = conn.execute('SELECT * FROM puestos WHERE id = ?', (pid,)).fetchone()
    if not p:
        conn.close()
        flash('Puesto no encontrado.', 'error')
        return hx_redirect(url_for('perfiles'))
    kpis = conn.execute('SELECT * FROM puesto_kpis WHERE puesto_id = ? ORDER BY orden, id', (pid,)).fetchall()
    personas = conn.execute(
        'SELECT numero_empleado, nombre_completo FROM usuarios WHERE estatus = 1 AND puesto = ? ORDER BY nombre_completo',
        (p['nombre'],)).fetchall()
    conn.close()
    return render_page('perfil_detalle.html', p=dict(p), kpis=kpis, personas=personas,
                       niveles=PUESTO_NIVELES, areas=PUESTO_AREAS, frecuencias=KPI_FRECUENCIAS)


@app.route('/perfiles/guardar', methods=['POST'])
@login_required
@requiere_modulo('perfiles')
def perfil_guardar():
    pid = (request.form.get('id') or '').strip()
    if not (request.form.get('nombre') or '').strip():
        flash('El nombre del puesto es obligatorio.', 'error')
        return hx_redirect(url_for('perfiles'))
    # Nombres de columna de una lista FIJA en código (no inyectable).
    valores = [(request.form.get(c) or '').strip() or None for c in PUESTO_CAMPOS]
    conn = get_db_connection()
    try:
        if pid:
            sets = ', '.join(f'{c} = ?' for c in PUESTO_CAMPOS)
            conn.execute(f'UPDATE puestos SET {sets} WHERE id = ?', (*valores, pid))
            destino = url_for('perfil_detalle', pid=pid)
        else:
            cols = ', '.join(PUESTO_CAMPOS)
            ph = ', '.join('?' for _ in PUESTO_CAMPOS)
            cur = conn.execute(f'INSERT INTO puestos ({cols}) VALUES ({ph})', valores)
            destino = url_for('perfil_detalle', pid=cur.lastrowid)
        conn.commit()
        flash('Puesto guardado correctamente.', 'success')
    except Exception:
        flash('No se pudo guardar el puesto.', 'error')
        destino = url_for('perfiles')
    finally:
        conn.close()
    return hx_redirect(destino)


@app.route('/perfiles/eliminar', methods=['POST'])
@login_required
@requiere_modulo('perfiles')
def perfil_eliminar():
    pid = (request.form.get('id') or '').strip()
    conn = get_db_connection()
    try:
        conn.execute('UPDATE puestos SET activo = 0 WHERE id = ?', (pid,))
        conn.commit()
        flash('Puesto archivado.', 'success')
    except Exception:
        flash('No se pudo archivar el puesto.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('perfiles'))


@app.route('/perfiles/kpi/agregar', methods=['POST'])
@login_required
@requiere_modulo('perfiles')
def perfil_kpi_agregar():
    pid = (request.form.get('puesto_id') or '').strip()
    nombre = (request.form.get('nombre') or '').strip()
    if not (pid and nombre):
        flash('Falta el nombre del KPI.', 'error')
        return hx_redirect(url_for('perfiles'))
    meta = (request.form.get('meta') or '').strip() or None
    frecuencia = (request.form.get('frecuencia') or 'Mensual').strip()
    conn = get_db_connection()
    try:
        orden = conn.execute('SELECT COALESCE(MAX(orden), 0) + 1 AS o FROM puesto_kpis WHERE puesto_id = ?', (pid,)).fetchone()['o']
        conn.execute('INSERT INTO puesto_kpis (puesto_id, nombre, meta, frecuencia, orden) VALUES (?, ?, ?, ?, ?)',
                     (pid, nombre, meta, frecuencia, orden))
        conn.commit()
        flash('KPI agregado.', 'success')
    except Exception:
        flash('No se pudo agregar el KPI.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('perfil_detalle', pid=pid))


@app.route('/perfiles/kpi/eliminar', methods=['POST'])
@login_required
@requiere_modulo('perfiles')
def perfil_kpi_eliminar():
    kid = (request.form.get('id') or '').strip()
    pid = (request.form.get('puesto_id') or '').strip()
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM puesto_kpis WHERE id = ?', (kid,))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
    return hx_redirect(url_for('perfil_detalle', pid=pid))


# --- M5 · ORGANIGRAMA INTERACTIVO ---
NIVEL_RANK = {'Dirección': 0, 'Gerente': 1, 'Jefe': 2, 'Coordinador': 3, 'Técnico': 4, 'Operativo': 5}


@app.route('/organigrama')
@login_required
@requiere_modulo('organigrama')
def organigrama():
    conn = get_db_connection()
    filas = conn.execute('''
        SELECT u.numero_empleado, u.nombre_completo, u.puesto, u.area,
               COALESCE(p.nivel, '') AS nivel
        FROM usuarios u
        LEFT JOIN puestos p ON p.nombre = u.puesto AND p.activo = 1
        WHERE u.estatus = 1
        ORDER BY u.nombre_completo
    ''').fetchall()
    vacantes = conn.execute('''
        SELECT nombre, area FROM puestos
        WHERE activo = 1 AND nombre NOT IN (
            SELECT puesto FROM usuarios WHERE estatus = 1 AND puesto IS NOT NULL AND puesto != ''
        ) ORDER BY area, nombre
    ''').fetchall()
    conn.close()

    filas = [dict(r) for r in filas]
    # Liderazgo (Dirección / Gerente) va arriba, el resto agrupado por área
    lideres = sorted([r for r in filas if r['nivel'] in ('Dirección', 'Gerente')],
                     key=lambda x: NIVEL_RANK.get(x['nivel'], 9))
    lideres_ids = {l['numero_empleado'] for l in lideres}
    por_area = {}
    for r in filas:
        if r['numero_empleado'] in lideres_ids:
            continue
        por_area.setdefault(r['area'] or 'Sin área', []).append(r)
    for a in por_area:
        por_area[a].sort(key=lambda x: (NIVEL_RANK.get(x['nivel'], 9), x['nombre_completo']))
    vac_por_area = {}
    for v in vacantes:
        vac_por_area.setdefault(v['area'] or 'Sin área', []).append(v['nombre'])
    areas_orden = sorted(set(por_area) | set(vac_por_area))
    return render_page('organigrama.html', lideres=lideres, por_area=por_area,
                       vac_por_area=vac_por_area, areas_orden=areas_orden,
                       total=len(filas), total_vac=len(vacantes))


# --- M7 · RECLUTAMIENTO & ONBOARDING ---
VACANTE_ESTATUS = ['Abierta', 'En proceso', 'Cubierta', 'Cancelada']
RECOMENDACIONES = ['Contratar', 'Citar', 'En espera', 'Descartar']


@app.route('/reclutamiento')
@login_required
@requiere_modulo('reclutamiento')
def reclutamiento():
    conn = get_db_connection()
    rows = conn.execute('''
        SELECT v.*, (SELECT COUNT(*) FROM candidatos c WHERE c.vacante_id = v.id) AS n_cand
        FROM vacantes v ORDER BY
          CASE v.estatus WHEN 'Abierta' THEN 0 WHEN 'En proceso' THEN 1 WHEN 'Cubierta' THEN 2 ELSE 3 END,
          v.fecha_apertura DESC, v.id DESC
    ''').fetchall()
    puestos = conn.execute('SELECT nombre, area FROM puestos WHERE activo = 1 ORDER BY nombre').fetchall()
    conn.close()
    vacantes = [dict(r) for r in rows]
    kpis = {
        'abiertas': sum(1 for v in vacantes if v['estatus'] == 'Abierta'),
        'proceso': sum(1 for v in vacantes if v['estatus'] == 'En proceso'),
        'cubiertas': sum(1 for v in vacantes if v['estatus'] == 'Cubierta'),
    }
    return render_page('reclutamiento.html', vacantes=vacantes, puestos=puestos, kpis=kpis,
                       areas=PUESTO_AREAS)


@app.route('/reclutamiento/<int:vid>')
@login_required
@requiere_modulo('reclutamiento')
def vacante_detalle(vid):
    conn = get_db_connection()
    v = conn.execute('SELECT * FROM vacantes WHERE id = ?', (vid,)).fetchone()
    if not v:
        conn.close()
        flash('Vacante no encontrada.', 'error')
        return hx_redirect(url_for('reclutamiento'))
    cands = conn.execute('SELECT * FROM candidatos WHERE vacante_id = ? ORDER BY puntuacion DESC, id DESC', (vid,)).fetchall()
    conn.close()
    return render_page('vacante_detalle.html', v=dict(v), candidatos=cands,
                       estatus_lista=VACANTE_ESTATUS, recomendaciones=RECOMENDACIONES)


@app.route('/reclutamiento/vacante/guardar', methods=['POST'])
@login_required
@requiere_modulo('reclutamiento')
def vacante_guardar():
    vid = (request.form.get('id') or '').strip()
    puesto = (request.form.get('puesto') or '').strip()
    if not puesto:
        flash('Selecciona el puesto de la vacante.', 'error')
        return hx_redirect(url_for('reclutamiento'))
    area = (request.form.get('area') or '').strip() or None
    justif = (request.form.get('justificacion') or '').strip() or None
    canal = (request.form.get('canal') or '').strip() or None
    conn = get_db_connection()
    try:
        if vid:
            conn.execute('UPDATE vacantes SET puesto=?, area=?, justificacion=?, canal=? WHERE id=?',
                         (puesto, area, justif, canal, vid))
            destino = url_for('vacante_detalle', vid=vid)
        else:
            cur = conn.execute('INSERT INTO vacantes (puesto, area, justificacion, canal, creado_por) VALUES (?,?,?,?,?)',
                               (puesto, area, justif, canal, session.get('nombre')))
            destino = url_for('vacante_detalle', vid=cur.lastrowid)
        conn.commit()
        flash('Vacante guardada.', 'success')
    except Exception:
        flash('No se pudo guardar la vacante.', 'error')
        destino = url_for('reclutamiento')
    finally:
        conn.close()
    return hx_redirect(destino)


@app.route('/reclutamiento/vacante/estatus', methods=['POST'])
@login_required
@requiere_modulo('reclutamiento')
def vacante_estatus():
    vid = (request.form.get('id') or '').strip()
    est = (request.form.get('estatus') or '').strip()
    if est not in VACANTE_ESTATUS:
        return hx_redirect(url_for('vacante_detalle', vid=vid))
    cierre = datetime.now().strftime('%Y-%m-%d') if est in ('Cubierta', 'Cancelada') else None
    conn = get_db_connection()
    try:
        conn.execute('UPDATE vacantes SET estatus=?, fecha_cierre=? WHERE id=?', (est, cierre, vid))
        conn.commit()
        flash(f'Vacante marcada como {est}.', 'success')
    except Exception:
        flash('No se pudo actualizar.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('vacante_detalle', vid=vid))


@app.route('/reclutamiento/candidato/agregar', methods=['POST'])
@login_required
@requiere_modulo('reclutamiento')
def candidato_agregar():
    vid = (request.form.get('vacante_id') or '').strip()
    nombre = (request.form.get('nombre') or '').strip()
    if not (vid and nombre):
        flash('Falta el nombre del candidato.', 'error')
        return hx_redirect(url_for('reclutamiento'))
    try:
        punt = max(0, min(5, int(request.form.get('puntuacion') or 0)))
    except ValueError:
        punt = 0
    rec = (request.form.get('recomendacion') or 'En espera').strip()
    if rec not in RECOMENDACIONES:
        rec = 'En espera'
    conn = get_db_connection()
    try:
        conn.execute('INSERT INTO candidatos (vacante_id, nombre, telefono, correo, notas, puntuacion, recomendacion) VALUES (?,?,?,?,?,?,?)',
                     (vid, nombre, (request.form.get('telefono') or '').strip() or None,
                      (request.form.get('correo') or '').strip() or None,
                      (request.form.get('notas') or '').strip() or None, punt, rec))
        conn.commit()
        flash('Candidato agregado.', 'success')
    except Exception:
        flash('No se pudo agregar el candidato.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('vacante_detalle', vid=vid))


@app.route('/reclutamiento/candidato/recomendacion', methods=['POST'])
@login_required
@requiere_modulo('reclutamiento')
def candidato_recomendacion():
    cid = (request.form.get('id') or '').strip()
    vid = (request.form.get('vacante_id') or '').strip()
    rec = (request.form.get('recomendacion') or '').strip()
    if rec in RECOMENDACIONES:
        conn = get_db_connection()
        try:
            conn.execute('UPDATE candidatos SET recomendacion=? WHERE id=?', (rec, cid))
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()
    return hx_redirect(url_for('vacante_detalle', vid=vid))


@app.route('/reclutamiento/candidato/eliminar', methods=['POST'])
@login_required
@requiere_modulo('reclutamiento')
def candidato_eliminar():
    cid = (request.form.get('id') or '').strip()
    vid = (request.form.get('vacante_id') or '').strip()
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM candidatos WHERE id=?', (cid,))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
    return hx_redirect(url_for('vacante_detalle', vid=vid))


# ============================================================
# === M9 · TABLERO GERENCIAL (dashboards + alertas) ==========
# ============================================================
def _semaforo(pct, bueno=90, regular=70):
    """Verde/Amarillo/Rojo según un porcentaje de cumplimiento."""
    if pct >= bueno:
        return 'verde'
    if pct >= regular:
        return 'amarillo'
    return 'rojo'

@app.route('/tablero')
@login_required
@requiere_modulo('tablero')
def tablero():
    conn = get_db_connection()
    hoy = datetime.now()
    anio = hoy.year
    mes_pref = hoy.strftime('%Y-%m')

    # --- Headcount por área (colaboradores activos) ---
    filas_area = conn.execute('''
        SELECT COALESCE(NULLIF(TRIM(area), ''), 'Sin área') AS area, COUNT(*) AS n
        FROM usuarios WHERE estatus = 1 AND rol != 'admin'
        GROUP BY area ORDER BY n DESC, area
    ''').fetchall()
    por_area = [dict(r) for r in filas_area]
    headcount = sum(r['n'] for r in por_area)
    max_area = max((r['n'] for r in por_area), default=0)

    # --- Rotación del año (altas / bajas) ---
    altas = conn.execute("SELECT COUNT(*) n FROM usuarios WHERE fecha_ingreso LIKE ?", (f'{anio}%',)).fetchone()['n']
    bajas = conn.execute("SELECT COUNT(*) n FROM usuarios WHERE fecha_baja LIKE ?", (f'{anio}%',)).fetchone()['n']
    rotacion = round(bajas / headcount * 100, 1) if headcount else 0.0

    # --- Ausentismo del mes en curso (faltas 'F') ---
    faltas_mes = conn.execute(
        "SELECT COUNT(*) n FROM incidencias_dia WHERE codigo = 'F' AND fecha LIKE ?", (f'{mes_pref}%',)
    ).fetchone()['n']
    dias_registrados = conn.execute(
        "SELECT COUNT(DISTINCT fecha) n FROM resumen_asistencias WHERE fecha LIKE ?", (f'{mes_pref}%',)
    ).fetchone()['n']
    base_ausentismo = headcount * dias_registrados
    ausentismo = round(faltas_mes / base_ausentismo * 100, 1) if base_ausentismo else 0.0

    # --- Avance de capacitación (celdas 'Tomado' sobre matriz completa) ---
    n_cursos = conn.execute("SELECT COUNT(*) n FROM cursos WHERE activo = 1").fetchone()['n']
    tomados = conn.execute("SELECT COUNT(*) n FROM capacitacion_registro WHERE estatus = 'Tomado'").fetchone()['n']
    total_celdas = headcount * n_cursos
    avance_cap = round(tomados / total_celdas * 100) if total_celdas else 0

    # --- Cumplimiento documental (expedientes con todos sus documentos) ---
    total_docs_req = len(DOCS_EXPEDIENTE)
    docs_por_emp = dict(conn.execute('''
        SELECT numero_empleado, COUNT(DISTINCT tipo_documento) n
        FROM documentos_empleados GROUP BY numero_empleado
    ''').fetchall())
    activos = conn.execute(
        "SELECT numero_empleado FROM usuarios WHERE estatus = 1 AND rol != 'admin'"
    ).fetchall()
    completos = sum(1 for a in activos if docs_por_emp.get(a['numero_empleado'], 0) >= total_docs_req)
    cumpl_doc = round(completos / len(activos) * 100) if activos else 0

    kpis = {
        'headcount': headcount, 'altas': altas, 'bajas': bajas, 'rotacion': rotacion,
        'ausentismo': ausentismo, 'faltas_mes': faltas_mes,
        'avance_cap': avance_cap, 'cumpl_doc': cumpl_doc,
        'sem_cap': _semaforo(avance_cap), 'sem_doc': _semaforo(cumpl_doc),
    }

    # ---------- ALERTAS AUTOMÁTICAS ----------
    alertas = []
    # RPT-16 · Contratos temporales por vencer (≤ 30 días)
    contratos = conn.execute('''
        SELECT numero_empleado, nombre_completo, puesto, area, fecha_fin_contrato
        FROM usuarios
        WHERE estatus = 1 AND fecha_fin_contrato IS NOT NULL AND TRIM(fecha_fin_contrato) != ''
          AND date(fecha_fin_contrato) BETWEEN date('now') AND date('now', '+30 day')
        ORDER BY fecha_fin_contrato
    ''').fetchall()
    for c in contratos:
        alertas.append({'tipo': 'Contrato por vencer', 'icono': 'ri-file-warning-line', 'nivel': 'rojo',
                        'nombre': c['nombre_completo'], 'area': c['area'],
                        'detalle': f"Vence el {c['fecha_fin_contrato']}", 'ref': c['numero_empleado']})
    # RPT-18 · 3+ faltas acumuladas (últimos 90 días)
    faltones = conn.execute('''
        SELECT i.numero_empleado, u.nombre_completo, u.area, COUNT(*) n
        FROM incidencias_dia i JOIN usuarios u ON u.numero_empleado = i.numero_empleado
        WHERE i.codigo = 'F' AND i.fecha >= date('now', '-90 day') AND u.estatus = 1
        GROUP BY i.numero_empleado HAVING n >= 3 ORDER BY n DESC
    ''').fetchall()
    for f in faltones:
        alertas.append({'tipo': 'Faltas acumuladas', 'icono': 'ri-calendar-close-line', 'nivel': 'rojo',
                        'nombre': f['nombre_completo'], 'area': f['area'],
                        'detalle': f"{f['n']} faltas en los últimos 90 días", 'ref': f['numero_empleado']})
    # RPT-11 · Expedientes documentales incompletos
    for a in activos:
        num = a['numero_empleado']
        tiene = docs_por_emp.get(num, 0)
        if tiene < total_docs_req:
            u = conn.execute("SELECT nombre_completo, area FROM usuarios WHERE numero_empleado = ?", (num,)).fetchone()
            faltan = total_docs_req - tiene
            alertas.append({'tipo': 'Expediente incompleto', 'icono': 'ri-folder-warning-line', 'nivel': 'amarillo',
                            'nombre': u['nombre_completo'] if u else num, 'area': u['area'] if u else '',
                            'detalle': f"Faltan {faltan} de {total_docs_req} documentos", 'ref': num})

    # Ordena: rojos primero, luego amarillos
    orden_nivel = {'rojo': 0, 'amarillo': 1, 'verde': 2}
    alertas.sort(key=lambda x: orden_nivel.get(x['nivel'], 3))
    resumen_alertas = {
        'rojas': sum(1 for a in alertas if a['nivel'] == 'rojo'),
        'amarillas': sum(1 for a in alertas if a['nivel'] == 'amarillo'),
        'total': len(alertas),
    }
    conn.close()
    return render_page('tablero.html', kpis=kpis, por_area=por_area, max_area=max_area,
                       alertas=alertas, resumen_alertas=resumen_alertas,
                       mes_actual=hoy.strftime('%B %Y').capitalize(), anio=anio)


# ============================================================
# === M8 · DOCUMENTOS LEGALES Y COMPLIANCE ===================
# ============================================================
UPLOAD_LEGALES = os.path.join('static', 'documentos_legales')

@app.route('/normativa')
@login_required
@requiere_modulo('normativa')
def normativa():
    conn = get_db_connection()
    docs = [dict(r) for r in conn.execute(
        "SELECT * FROM documentos_legales ORDER BY categoria, codigo, nombre").fetchall()]
    # Agrupa por categoría respetando el orden del catálogo
    por_categoria = {cat: [] for cat in DOC_CATEGORIAS}
    for d in docs:
        por_categoria.setdefault(d['categoria'], []).append(d)
    # Resumen del tracker BPM (para la tarjeta de acceso)
    bpm = conn.execute("SELECT estatus, COUNT(*) n FROM bpm_actividades GROUP BY estatus").fetchall()
    bpm_conteo = {r['estatus']: r['n'] for r in bpm}
    bpm_total = sum(bpm_conteo.values())
    bpm_cumplidas = bpm_conteo.get('Cumplida', 0)
    bpm_pct = round(bpm_cumplidas / bpm_total * 100) if bpm_total else 0
    conn.close()
    kpis = {
        'total': len(docs),
        'vigentes': sum(1 for d in docs if d['estatus'] == 'Vigente'),
        'con_archivo': sum(1 for d in docs if d['archivo']),
        'bpm_pct': bpm_pct, 'bpm_total': bpm_total, 'bpm_cumplidas': bpm_cumplidas,
    }
    return render_page('normativa.html', por_categoria=por_categoria, categorias=DOC_CATEGORIAS,
                       estatus_lista=DOC_ESTATUS, kpis=kpis)

@app.route('/normativa/guardar', methods=['POST'])
@login_required
@requiere_modulo('normativa')
def normativa_guardar():
    did = (request.form.get('id') or '').strip()
    nombre = (request.form.get('nombre') or '').strip()
    categoria = (request.form.get('categoria') or '').strip()
    if not nombre or categoria not in DOC_CATEGORIAS:
        flash('Falta el nombre o la categoría del documento.', 'error')
        return hx_redirect(url_for('normativa'))
    codigo = (request.form.get('codigo') or '').strip()
    version = (request.form.get('version') or '').strip()
    vigencia = (request.form.get('vigencia') or '').strip()
    notas = (request.form.get('notas') or '').strip()
    estatus = request.form.get('estatus')
    if estatus not in DOC_ESTATUS:
        estatus = 'Vigente'
    conn = get_db_connection()
    try:
        if did:
            conn.execute('''UPDATE documentos_legales SET codigo=?, nombre=?, categoria=?, version=?,
                            vigencia=?, notas=?, estatus=?, actualizado=date('now','localtime') WHERE id=?''',
                         (codigo, nombre, categoria, version, vigencia, notas, estatus, did))
        else:
            conn.execute('''INSERT INTO documentos_legales (codigo, nombre, categoria, version, vigencia, notas, estatus)
                            VALUES (?,?,?,?,?,?,?)''',
                         (codigo, nombre, categoria, version, vigencia, notas, estatus))
        conn.commit()
        flash('Documento guardado.', 'success')
    except Exception:
        flash('No se pudo guardar el documento.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('normativa'))

@app.route('/normativa/subir/<int:did>', methods=['POST'])
@login_required
@requiere_modulo('normativa')
def normativa_subir(did):
    archivo = request.files.get('archivo')
    if not archivo or not archivo.filename:
        flash('Selecciona un archivo.', 'error')
        return hx_redirect(url_for('normativa'))
    if not extension_permitida(archivo.filename):
        flash('Solo se permiten archivos PDF, JPG o PNG.', 'error')
        return hx_redirect(url_for('normativa'))
    conn = get_db_connection()
    doc = conn.execute("SELECT id FROM documentos_legales WHERE id=?", (did,)).fetchone()
    if not doc:
        conn.close()
        flash('Documento no encontrado.', 'error')
        return hx_redirect(url_for('normativa'))
    os.makedirs(UPLOAD_LEGALES, exist_ok=True)
    ext = archivo.filename.rsplit('.', 1)[1].lower()
    nombre_fs = f'legal_{did}_{secure_filename(archivo.filename)}'
    ruta = os.path.join(UPLOAD_LEGALES, nombre_fs)
    try:
        archivo.save(ruta)
        conn.execute("UPDATE documentos_legales SET archivo=?, actualizado=date('now','localtime') WHERE id=?",
                     (nombre_fs, did))
        conn.commit()
        flash('Archivo adjuntado.', 'success')
    except Exception:
        flash('No se pudo guardar el archivo.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('normativa'))

@app.route('/normativa/archivo/<int:did>')
@login_required
@requiere_modulo('normativa')
def normativa_archivo(did):
    conn = get_db_connection()
    doc = conn.execute("SELECT archivo FROM documentos_legales WHERE id=?", (did,)).fetchone()
    conn.close()
    if not doc or not doc['archivo']:
        flash('Ese documento no tiene archivo adjunto.', 'error')
        return hx_redirect(url_for('normativa'))
    ruta = os.path.join(UPLOAD_LEGALES, doc['archivo'])
    if not os.path.exists(ruta):
        flash('Archivo no disponible.', 'error')
        return hx_redirect(url_for('normativa'))
    resp = make_response(send_file(ruta))
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp

@app.route('/normativa/eliminar', methods=['POST'])
@login_required
@requiere_modulo('normativa')
def normativa_eliminar():
    did = (request.form.get('id') or '').strip()
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT archivo FROM documentos_legales WHERE id=?", (did,)).fetchone()
        conn.execute("DELETE FROM documentos_legales WHERE id=?", (did,))
        conn.commit()
        if row and row['archivo']:
            try:
                os.remove(os.path.join(UPLOAD_LEGALES, row['archivo']))
            except OSError:
                pass
        flash('Documento eliminado.', 'success')
    except Exception:
        flash('No se pudo eliminar.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('normativa'))

@app.route('/normativa/bpm')
@login_required
@requiere_modulo('normativa')
def normativa_bpm():
    area_f = (request.args.get('area') or '').strip()
    conn = get_db_connection()
    if area_f and area_f in BPM_AREAS:
        acts = conn.execute("SELECT * FROM bpm_actividades WHERE area=? ORDER BY numero", (area_f,)).fetchall()
    else:
        area_f = ''
        acts = conn.execute("SELECT * FROM bpm_actividades ORDER BY numero").fetchall()
    acts = [dict(a) for a in acts]
    conteo = conn.execute("SELECT estatus, COUNT(*) n FROM bpm_actividades GROUP BY estatus").fetchall()
    conn.close()
    c = {r['estatus']: r['n'] for r in conteo}
    total = sum(c.values())
    cumplidas = c.get('Cumplida', 0)
    kpis = {
        'total': total, 'cumplidas': cumplidas,
        'proceso': c.get('En proceso', 0), 'pendientes': c.get('Pendiente', 0),
        'pct': round(cumplidas / total * 100) if total else 0,
    }
    return render_page('normativa_bpm.html', acts=acts, areas=BPM_AREAS, estatus_lista=BPM_ESTATUS,
                       area_actual=area_f, kpis=kpis)

@app.route('/normativa/bpm/actualizar', methods=['POST'])
@login_required
@requiere_modulo('normativa')
def normativa_bpm_actualizar():
    aid = (request.form.get('id') or '').strip()
    estatus = request.form.get('estatus')
    responsable = (request.form.get('responsable') or '').strip()
    fecha = (request.form.get('fecha_compromiso') or '').strip()
    notas = (request.form.get('notas') or '').strip()
    if estatus not in BPM_ESTATUS:
        flash('Estatus no válido.', 'error')
        return hx_redirect(url_for('normativa_bpm'))
    conn = get_db_connection()
    try:
        conn.execute("UPDATE bpm_actividades SET estatus=?, responsable=?, fecha_compromiso=?, notas=? WHERE id=?",
                     (estatus, responsable, fecha, notas, aid))
        conn.commit()
        flash('Actividad BPM actualizada.', 'success')
    except Exception:
        flash('No se pudo actualizar.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('normativa_bpm'))

@app.route('/normativa/bpm/agregar', methods=['POST'])
@login_required
@requiere_modulo('normativa')
def normativa_bpm_agregar():
    actividad = (request.form.get('actividad') or '').strip()
    area = (request.form.get('area') or '').strip()
    if not actividad or area not in BPM_AREAS:
        flash('Falta la actividad o el área.', 'error')
        return hx_redirect(url_for('normativa_bpm'))
    conn = get_db_connection()
    try:
        maxn = conn.execute("SELECT COALESCE(MAX(numero),0)+1 AS n FROM bpm_actividades").fetchone()['n']
        conn.execute("INSERT INTO bpm_actividades (numero, actividad, area, estatus) VALUES (?,?,?,'Pendiente')",
                     (maxn, actividad, area))
        conn.commit()
        flash('Actividad agregada al tracker BPM.', 'success')
    except Exception:
        flash('No se pudo agregar.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('normativa_bpm'))


# ============================================================
# === M10 · AUDITORÍA Y CATÁLOGOS MAESTROS ===================
# ============================================================
ROLES_INFO = {
    'admin':    {'nombre': 'Administrador del sistema', 'icono': 'ri-shield-star-line',
                 'desc': 'Configuración total: usuarios, catálogos, auditoría y todos los módulos.'},
    'rh':       {'nombre': 'Recursos Humanos',          'icono': 'ri-team-line',
                 'desc': 'Expedientes, incidencias, capacitación, desempeño, reclutamiento y reportes.'},
    'operador': {'nombre': 'Operador de Producción',    'icono': 'ri-user-line',
                 'desc': 'Acceso a procesos de producción y recetas asignadas.'},
}

@app.route('/auditoria')
@login_required
@requiere_modulo('auditoria')
def auditoria():
    tipo_f = (request.args.get('tipo') or '').strip()
    conn = get_db_connection()

    # --- Roles del sistema (con nº de módulos accesibles, calculado de PERMISOS) ---
    roles = []
    for rol, info in ROLES_INFO.items():
        n_mod = sum(1 for m, permitidos in PERMISOS.items() if rol in permitidos)
        n_users = conn.execute("SELECT COUNT(*) n FROM usuarios WHERE rol=? AND estatus=1", (rol,)).fetchone()['n']
        roles.append({'clave': rol, **info, 'modulos': n_mod, 'usuarios': n_users})

    # --- Catálogos maestros (conteo en vivo + acceso a su módulo de gestión) ---
    def _cnt(sql):
        try:
            return conn.execute(sql).fetchone()['n']
        except sqlite3.Error:
            return 0
    catalogos = [
        {'nombre': 'Puestos',              'icono': 'ri-profile-line',        'conteo': _cnt("SELECT COUNT(*) n FROM puestos WHERE activo=1"),       'ruta': '/perfiles'},
        {'nombre': 'Departamentos / áreas','icono': 'ri-building-4-line',     'conteo': len(PUESTO_AREAS),                                          'ruta': '/organigrama'},
        {'nombre': 'Tipos de contrato',    'icono': 'ri-file-text-line',      'conteo': len(EXP_CATALOGOS.get('tipo_contrato', [])),                'ruta': None},
        {'nombre': 'Tipos de incidencia',  'icono': 'ri-calendar-event-line', 'conteo': len(CODIGOS_INCIDENCIA),                                    'ruta': '/incidencias'},
        {'nombre': 'Cursos de capacitación','icono': 'ri-graduation-cap-line','conteo': _cnt("SELECT COUNT(*) n FROM cursos WHERE activo=1"),        'ruta': '/capacitacion'},
        {'nombre': 'Competencias / tareas','icono': 'ri-medal-line',          'conteo': _cnt("SELECT COUNT(*) n FROM competencias_tareas WHERE activo=1"), 'ruta': '/expertise'},
        {'nombre': 'Documentos legales',   'icono': 'ri-scales-3-line',       'conteo': _cnt("SELECT COUNT(*) n FROM documentos_legales"),          'ruta': '/normativa'},
        {'nombre': 'Actividades BPM',      'icono': 'ri-shield-check-line',   'conteo': _cnt("SELECT COUNT(*) n FROM bpm_actividades"),             'ruta': '/normativa/bpm'},
        {'nombre': 'Días festivos',        'icono': 'ri-calendar-2-line',     'conteo': _cnt("SELECT COUNT(*) n FROM dias_festivos"),               'ruta': '/incidencias'},
    ]

    # --- Registro de auditoría (últimos 200, con filtro por tipo) ---
    if tipo_f in ('acceso', 'cambio'):
        registros = conn.execute("SELECT * FROM auditoria WHERE tipo=? ORDER BY id DESC LIMIT 200", (tipo_f,)).fetchall()
    else:
        tipo_f = ''
        registros = conn.execute("SELECT * FROM auditoria ORDER BY id DESC LIMIT 200").fetchall()
    registros = [dict(r) for r in registros]
    conteo = conn.execute("SELECT tipo, COUNT(*) n FROM auditoria GROUP BY tipo").fetchall()
    conn.close()
    ct = {r['tipo']: r['n'] for r in conteo}
    resumen = {'accesos': ct.get('acceso', 0), 'cambios': ct.get('cambio', 0), 'total': sum(ct.values())}
    return render_page('auditoria.html', roles=roles, catalogos=catalogos,
                       registros=registros, resumen=resumen, tipo_actual=tipo_f)


@app.route('/configuracion')
@login_required
@requiere_modulo('configuracion')
def configuracion():
    conn = get_db_connection()
    usuarios = conn.execute('''
        SELECT u.*, COALESCE(h.hora_entrada, '08:30') AS hora_entrada,
               COALESCE(h.hora_salida, '18:00') AS hora_salida
        FROM usuarios u
        LEFT JOIN horarios_empleados h ON u.numero_empleado = h.numero_empleado
        ORDER BY u.estatus DESC, u.numero_empleado ASC
    ''').fetchall()
    stats = {
        'total': len(usuarios),
        'activos': sum(1 for u in usuarios if u['estatus'] == 1),
        'inactivos': sum(1 for u in usuarios if u['estatus'] == 0),
        'recetas': conn.execute("SELECT COUNT(*) AS n FROM recetas").fetchone()['n'],
        'documentos': conn.execute("SELECT COUNT(*) AS n FROM documentos_empleados").fetchone()['n'],
        'activos_inv': conn.execute("SELECT COUNT(*) AS n FROM inventario_activos").fetchone()['n'],
    }
    conn.close()
    return render_page('configuracion.html', usuarios=usuarios, stats=stats)

@app.route('/crear_receta_form')
@login_required
@requiere_rol('admin')
def crear_receta_form():
    return render_page('crear_receta.html')

@app.route('/ejecutar_receta/<int:receta_id>')
@login_required
@requiere_modulo('recetas')
def ejecutar_receta(receta_id):
    conn = get_db_connection()
    receta = conn.execute('SELECT * FROM recetas WHERE id = ?', (receta_id,)).fetchone()
    pasos = conn.execute(
        'SELECT * FROM recetas_pasos WHERE receta_id = ? ORDER BY numero_paso ASC',
        (receta_id,)
    ).fetchall()
    conn.close()
    if not receta:
        flash('Receta no encontrada', 'error')
        return hx_redirect(url_for('recetas'))
    total_pasos = len(pasos)
    pasos_json = json.dumps([dict(p) for p in pasos])
    return full_page('ejecutar_receta.html', receta=receta, pasos=pasos, total_pasos=total_pasos, pasos_json=pasos_json)

@app.route('/ver_documento/<int:doc_id>')
@login_required
@requiere_modulo('documentos')
def ver_documento(doc_id):
    conn = get_db_connection()
    doc = conn.execute('SELECT * FROM documentos_empleados WHERE id = ?', (doc_id,)).fetchone()
    conn.close()
    if not doc or not os.path.exists(doc['ruta_archivo']):
        flash('Documento no disponible', 'error')
        return hx_redirect(url_for('documentos'))
    resp = make_response(send_file(doc['ruta_archivo']))
    resp.headers['X-Content-Type-Options'] = 'nosniff'   # evita sniffing -> XSS
    return resp

# --- 4. MÓDULO DE ASISTENCIAS ---

def _parse_hora(s):
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    for fmt in ('%H:%M:%S', '%H:%M', '%I:%M:%S %p', '%I:%M %p'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def procesar_asistencias():
    conn = get_db_connection()
    combos = conn.execute('''
        SELECT DISTINCT user_id, nombre, fecha
        FROM registros_zkteco
        ORDER BY fecha ASC, nombre ASC
    ''').fetchall()

    for combo in combos:
        nombre = combo['nombre']
        fecha = combo['fecha']
        checks = conn.execute(
            'SELECT hora FROM registros_zkteco WHERE nombre = ? AND fecha = ? ORDER BY hora ASC',
            (nombre, fecha)
        ).fetchall()
        if not checks:
            continue

        hora_entrada_real = checks[0]['hora']
        hora_salida_real = checks[-1]['hora'] if len(checks) > 1 else None

        usuario = conn.execute(
            'SELECT numero_empleado FROM usuarios WHERE nombre_completo = ?',
            (nombre,)
        ).fetchone()
        if usuario:
            numero_empleado = usuario['numero_empleado']
            horario = conn.execute(
                'SELECT hora_entrada, hora_salida FROM horarios_empleados WHERE numero_empleado = ?',
                (numero_empleado,)
            ).fetchone()
            hora_entrada_cfg = horario['hora_entrada'] if horario else '08:30'
            hora_salida_cfg = horario['hora_salida'] if horario else '18:00'
        else:
            numero_empleado = combo['user_id']
            hora_entrada_cfg = '08:30'
            hora_salida_cfg = '18:00'

        minutos_retardo = 0
        ent_real = _parse_hora(hora_entrada_real)
        ent_cfg = _parse_hora(hora_entrada_cfg)
        if ent_real and ent_cfg and ent_real > ent_cfg:
            minutos_retardo = max(0, int((ent_real - ent_cfg).total_seconds() / 60))

        horas_extras = 0.0
        if hora_salida_real:
            sal_real = _parse_hora(hora_salida_real)
            sal_cfg = _parse_hora(hora_salida_cfg)
            if sal_real and sal_cfg and sal_real > sal_cfg:
                horas_extras = max(0.0, round((sal_real - sal_cfg).total_seconds() / 3600.0, 2))

        estatus = 'Retardo' if minutos_retardo > 0 else 'Presente'

        conn.execute('''
            INSERT OR REPLACE INTO resumen_asistencias
            (numero_empleado, nombre, fecha, hora_entrada_real, hora_salida_real,
             minutos_retardo, horas_extras, estatus)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (numero_empleado, nombre, fecha, hora_entrada_real, hora_salida_real,
              minutos_retardo, horas_extras, estatus))

    conn.commit()
    conn.close()

@app.route('/asistencias')
@login_required
@requiere_modulo('asistencias')
def asistencias():
    conn = get_db_connection()
    empleados = conn.execute('''
        SELECT u.*, COALESCE(h.hora_entrada, '08:30') as hora_entrada,
               COALESCE(h.hora_salida, '18:00') as hora_salida
        FROM usuarios u
        LEFT JOIN horarios_empleados h ON u.numero_empleado = h.numero_empleado
        WHERE u.rol != 'admin'
        ORDER BY u.nombre_completo
    ''').fetchall()
    resumen = conn.execute(
        'SELECT * FROM resumen_asistencias ORDER BY fecha DESC, nombre ASC'
    ).fetchall()
    conn.close()
    return render_page('asistencias.html', empleados=empleados, resumen=resumen)

@app.route('/importar_zkteco', methods=['POST'])
@login_required
@requiere_modulo('asistencias')
def importar_zkteco():
    archivo = request.files.get('archivo_zkteco')
    if not archivo or not archivo.filename:
        flash('Debe seleccionar un archivo Excel', 'error')
        return hx_redirect(url_for('asistencias'))
    try:
        df = pd.read_excel(archivo, engine='openpyxl')
    except Exception:
        flash('No se pudo leer el archivo. Verifica que sea un Excel válido.', 'error')
        return hx_redirect(url_for('asistencias'))

    df.columns = [str(c).strip().lower() for c in df.columns]
    columnas_requeridas = {'user_id', 'fecha', 'hora', 'nombre'}
    if not columnas_requeridas.issubset(set(df.columns)):
        flash(f'Faltan columnas en el Excel. Requeridas: {", ".join(sorted(columnas_requeridas))}', 'error')
        return hx_redirect(url_for('asistencias'))

    conn = get_db_connection()
    insertados = 0
    repetidos = 0
    try:
        for _, fila in df.iterrows():
            user_id = str(fila.get('user_id', '')).strip()
            nombre = str(fila.get('nombre', '')).strip()
            fecha_raw = fila.get('fecha')
            hora_raw = fila.get('hora')

            if not user_id or not nombre or pd.isna(fecha_raw) or pd.isna(hora_raw):
                continue

            if hasattr(fecha_raw, 'strftime'):
                fecha = fecha_raw.strftime('%Y-%m-%d')
            else:
                fecha = str(fecha_raw).strip()[:10]

            if hasattr(hora_raw, 'strftime'):
                hora = hora_raw.strftime('%H:%M:%S')
            else:
                hora = str(hora_raw).strip()

            zk_id = fila.get('id')
            try:
                zk_id = int(zk_id) if not pd.isna(zk_id) else None
            except (ValueError, TypeError):
                zk_id = None

            tipo = str(fila.get('tipo', 'ZK-Auto')).strip() or 'ZK-Auto'
            estatus_zk = str(fila.get('estatus', 'Sincronizado')).strip() or 'Sincronizado'

            cur = conn.execute('''
                INSERT OR IGNORE INTO registros_zkteco
                (zk_id, user_id, nombre, fecha, hora, tipo, estatus)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (zk_id, user_id, nombre, fecha, hora, tipo, estatus_zk))
            if cur.rowcount == 1:
                insertados += 1
            else:
                repetidos += 1            # ya existía (INSERT OR IGNORE lo omitió)
        conn.commit()
    except Exception:
        flash('Error al guardar los registros del checador. Verifica el archivo e intenta de nuevo.', 'error')
        return hx_redirect(url_for('asistencias'))
    finally:
        conn.close()

    try:
        procesar_asistencias()
    except Exception:
        flash('Los registros se importaron, pero falló el cálculo del resumen de asistencias.', 'error')
        return hx_redirect(url_for('asistencias'))

    extra = f' ({repetidos} ya existían y se omitieron)' if repetidos else ''
    flash(f'Se importaron {insertados} registros nuevos{extra} y se procesó el resumen de asistencias.', 'success')
    return hx_redirect(url_for('asistencias'))

@app.route('/exportar_asistencias')
@login_required
@requiere_modulo('asistencias')
def exportar_asistencias():
    fecha_inicio = request.args.get('fecha_inicio', '').strip()
    fecha_fin = request.args.get('fecha_fin', '').strip()
    empleado_id = request.args.get('empleado_id', '').strip()

    conn = get_db_connection()
    try:
        filtros_resumen = []
        params_resumen = []
        if fecha_inicio:
            filtros_resumen.append('fecha >= ?')
            params_resumen.append(fecha_inicio)
        if fecha_fin:
            filtros_resumen.append('fecha <= ?')
            params_resumen.append(fecha_fin)
        if empleado_id:
            filtros_resumen.append('numero_empleado = ?')
            params_resumen.append(empleado_id)
        where_resumen = ('WHERE ' + ' AND '.join(filtros_resumen)) if filtros_resumen else ''

        df_resumen = pd.read_sql_query(
            f'''SELECT nombre AS Nombre, fecha AS Fecha,
                       hora_entrada_real AS "Entrada Real",
                       hora_salida_real AS "Salida Real",
                       minutos_retardo AS "Minutos Retardo",
                       horas_extras AS "Horas Extras",
                       estatus AS Estatus
                FROM resumen_asistencias {where_resumen}
                ORDER BY fecha DESC, nombre ASC''',
            conn, params=params_resumen
        )

        filtros_zk = []
        params_zk = []
        if fecha_inicio:
            filtros_zk.append('fecha >= ?')
            params_zk.append(fecha_inicio)
        if fecha_fin:
            filtros_zk.append('fecha <= ?')
            params_zk.append(fecha_fin)
        if empleado_id:
            filtros_zk.append('user_id = ?')
            params_zk.append(empleado_id)
        where_zk = ('WHERE ' + ' AND '.join(filtros_zk)) if filtros_zk else ''
        df_detalle = pd.read_sql_query(
            f'''SELECT nombre AS Nombre, fecha AS Fecha, hora AS Hora, tipo AS Tipo
                FROM registros_zkteco {where_zk}
                ORDER BY fecha DESC, hora ASC''',
            conn, params=params_zk
        )
    finally:
        conn.close()

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df_resumen.to_excel(writer, sheet_name='Resumen General', index=False)
        df_detalle.to_excel(writer, sheet_name='Detalle Checadas', index=False)
    output.seek(0)
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        download_name='asistencias.xlsx',
        as_attachment=True
    )

@app.route('/reporte_individual/<string:nombre_empleado>')
@login_required
@requiere_modulo('asistencias')
def reporte_individual(nombre_empleado):
    fecha_inicio = request.args.get('fecha_inicio', '').strip() or '0000-01-01'
    fecha_fin = request.args.get('fecha_fin', '').strip() or '9999-12-31'
    conn = get_db_connection()
    registros = conn.execute('''
        SELECT * FROM resumen_asistencias
        WHERE nombre = ? AND fecha BETWEEN ? AND ?
        ORDER BY fecha ASC
    ''', (nombre_empleado, fecha_inicio, fecha_fin)).fetchall()
    totales = conn.execute('''
        SELECT COALESCE(SUM(minutos_retardo), 0) AS total_retardos,
               COALESCE(SUM(horas_extras), 0) AS total_horas_extras
        FROM resumen_asistencias
        WHERE nombre = ? AND fecha BETWEEN ? AND ?
    ''', (nombre_empleado, fecha_inicio, fecha_fin)).fetchone()
    conn.close()
    total_retardos = totales['total_retardos'] if totales else 0
    total_horas_extras = totales['total_horas_extras'] if totales else 0
    return full_page(
        'reporte_individual.html',
        nombre=nombre_empleado,
        registros=registros,
        total_retardos=total_retardos,
        total_horas_extras=total_horas_extras,
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin
    )

# ============================================================
# === 5. MÓDULO: MATRIZ DE EXPERTISE (Equipo Multifuncional) ==
# ============================================================
# Filosofía: el usuario captura SOLO el % de dominio (0-100) de cada
# colaborador por tarea. El sistema calcula solo: el nivel N1-N4, el promedio
# por persona, cuántos expertos hay por tarea y dibuja el radar de competencias
# (SVG puro, sin librerías externas — funciona sin internet e imprime bien).

# Regla de niveles del Excel: N1 0-25 · N2 25-50 · N3 50-75 · N4 75-100
NIVELES_EXPERTISE = [
    (25,  'N1', 'Principiante', 'bg-rose-50 text-rose-600 border-rose-100',          '#e11d48'),
    (50,  'N2', 'Básico',       'bg-amber-50 text-amber-600 border-amber-100',       '#d97706'),
    (75,  'N3', 'Competente',   'bg-sky-50 text-[#0e6ba8] border-sky-100',           '#0e6ba8'),
    (101, 'N4', 'Experto',      'bg-emerald-50 text-emerald-600 border-emerald-100', '#059669'),
]

def nivel_expertise(pct):
    """Convierte un % (0-100) en su nivel: {pct, n, etiqueta, css, hex}."""
    try:
        p = max(0, min(100, int(round(float(pct)))))
    except (TypeError, ValueError):
        p = 0
    for limite, n, etiqueta, css, hexc in NIVELES_EXPERTISE:
        if p < limite:
            return {'pct': p, 'n': n, 'etiqueta': etiqueta, 'css': css, 'hex': hexc}
    ultimo = NIVELES_EXPERTISE[-1]
    return {'pct': p, 'n': ultimo[1], 'etiqueta': ultimo[2], 'css': ultimo[3], 'hex': ultimo[4]}

def _radar_expertise(tareas, valores, size=320):
    """Calcula la geometría SVG del radar de una persona.
    valores: dict {tarea_id: pct}. Devuelve polígono, vértices, ejes, anillos y etiquetas."""
    n = len(tareas)
    cx = cy = size / 2.0
    R = size / 2.0 - 48          # margen para las etiquetas
    pts, vertices, ejes, etiquetas = [], [], [], []
    for i, t in enumerate(tareas):
        ang = -math.pi / 2 + i * 2 * math.pi / n
        cos, sin = math.cos(ang), math.sin(ang)
        niv = nivel_expertise(valores.get(t['id'], 0))
        pct = niv['pct']
        r = R * pct / 100.0
        px, py = cx + r * cos, cy + r * sin
        pts.append(f"{px:.1f},{py:.1f}")
        vertices.append({'x': px, 'y': py, 'hex': niv['hex'], 'pct': pct})
        ejes.append({'x2': cx + R * cos, 'y2': cy + R * sin})
        lx, ly = cx + (R + 15) * cos, cy + (R + 15) * sin
        anchor = 'middle'
        if cos > 0.25:
            anchor = 'start'
        elif cos < -0.25:
            anchor = 'end'
        corto = t['nombre'] if len(t['nombre']) <= 16 else t['nombre'][:15] + '…'
        etiquetas.append({'x': lx, 'y': ly, 'anchor': anchor, 'texto': corto})
    anillos = []
    for frac in (0.25, 0.5, 0.75, 1.0):
        ring = []
        for i in range(n):
            ang = -math.pi / 2 + i * 2 * math.pi / n
            ring.append(f"{cx + R * frac * math.cos(ang):.1f},{cy + R * frac * math.sin(ang):.1f}")
        anillos.append(' '.join(ring))
    return {
        'size': size, 'cx': cx, 'cy': cy, 'R': R,
        'poligono': ' '.join(pts), 'vertices': vertices,
        'ejes': ejes, 'etiquetas': etiquetas, 'anillos': anillos,
    }

@app.route('/expertise')
@login_required
@requiere_modulo('expertise')
def expertise():
    conn = get_db_connection()
    tareas = [dict(r) for r in conn.execute(
        "SELECT id, nombre, area FROM competencias_tareas WHERE activo = 1 ORDER BY orden, id"
    ).fetchall()]
    empleados = _empleados_activos_no_admin(conn)
    evals = conn.execute(
        "SELECT numero_empleado, tarea_id, nivel_pct FROM expertise_evaluaciones"
    ).fetchall()
    conn.close()

    # matriz[numero_empleado][tarea_id] = pct
    matriz = {}
    for e in evals:
        matriz.setdefault(e['numero_empleado'], {})[e['tarea_id']] = e['nivel_pct']

    # Filas precalculadas (celdas con nivel, promedio, nivel del promedio)
    filas = []
    expertos_por_tarea = {t['id']: 0 for t in tareas}
    for emp in empleados:
        num = emp['numero_empleado']
        celdas, suma = [], 0
        for t in tareas:
            niv = nivel_expertise(matriz.get(num, {}).get(t['id'], 0))
            celdas.append({'tarea_id': t['id'], **niv})
            suma += niv['pct']
            if niv['n'] == 'N4':
                expertos_por_tarea[t['id']] += 1
        prom = round(suma / len(tareas)) if tareas else 0
        partes = emp['nombre_completo'].split()
        filas.append({
            'numero_empleado': num,
            'nombre_completo': emp['nombre_completo'],
            'corto': ' '.join(partes[:2]) if partes else emp['nombre_completo'],
            'puesto': emp['puesto'],
            'celdas': celdas,
            'promedio': prom,
            'nivel_prom': nivel_expertise(prom),
        })

    prom_equipo = round(sum(f['promedio'] for f in filas) / len(filas)) if filas else 0
    total_expertos = sum(expertos_por_tarea.values())

    # Radar de la persona seleccionada (por defecto, la primera)
    ver = request.args.get('ver', '').strip()
    if not ver and filas:
        ver = filas[0]['numero_empleado']
    radar = _radar_expertise(tareas, matriz.get(ver, {})) if (ver and tareas) else None
    radar_emp = next((f for f in filas if f['numero_empleado'] == ver), None)

    return render_page(
        'expertise.html',
        tareas=tareas, filas=filas, expertos_por_tarea=expertos_por_tarea,
        total_empleados=len(filas), prom_equipo=prom_equipo, total_expertos=total_expertos,
        radar=radar, radar_emp=radar_emp, ver=ver,
    )

@app.route('/expertise/guardar', methods=['POST'])
@login_required
@requiere_modulo('expertise')
def expertise_guardar():
    num = request.form.get('numero_empleado')
    if not num:
        flash('Falta seleccionar al colaborador.', 'error')
        return hx_redirect(url_for('expertise'))
    conn = get_db_connection()
    try:
        tareas = conn.execute("SELECT id FROM competencias_tareas WHERE activo = 1").fetchall()
        for t in tareas:
            raw = request.form.get(f'tarea_{t["id"]}')
            if raw is None or raw == '':
                continue
            try:
                pct = max(0, min(100, int(round(float(raw)))))
            except ValueError:
                continue
            conn.execute('''
                INSERT INTO expertise_evaluaciones
                    (numero_empleado, tarea_id, nivel_pct, actualizado_por, fecha_actualizacion)
                VALUES (?, ?, ?, ?, datetime('now','localtime'))
                ON CONFLICT(numero_empleado, tarea_id) DO UPDATE SET
                    nivel_pct = excluded.nivel_pct,
                    actualizado_por = excluded.actualizado_por,
                    fecha_actualizacion = excluded.fecha_actualizacion
            ''', (num, t['id'], pct, session.get('nombre')))
        conn.commit()
        flash('Matriz de expertise actualizada.', 'success')
    except Exception:
        flash('No se pudo guardar la evaluación.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('expertise', ver=num))

@app.route('/expertise/nueva_tarea', methods=['POST'])
@login_required
@requiere_modulo('expertise')
def expertise_nueva_tarea():
    nombre = (request.form.get('nombre') or '').strip()
    area = (request.form.get('area') or 'Producción').strip()
    if not nombre:
        flash('Escribe el nombre de la tarea.', 'error')
        return hx_redirect(url_for('expertise'))
    conn = get_db_connection()
    try:
        maxord = conn.execute(
            "SELECT COALESCE(MAX(orden), 0) + 1 AS o FROM competencias_tareas"
        ).fetchone()['o']
        conn.execute(
            "INSERT INTO competencias_tareas (nombre, area, orden, activo) VALUES (?, ?, ?, 1)",
            (nombre, area, maxord)
        )
        conn.commit()
        flash('Tarea agregada al catálogo.', 'success')
    except Exception:
        flash('No se pudo agregar la tarea.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('expertise'))

@app.route('/expertise/eliminar_tarea', methods=['POST'])
@login_required
@requiere_modulo('expertise')
def expertise_eliminar_tarea():
    tarea_id = request.form.get('tarea_id')
    conn = get_db_connection()
    try:
        # Baja lógica: la tarea desaparece de la matriz pero el histórico se conserva
        conn.execute("UPDATE competencias_tareas SET activo = 0 WHERE id = ?", (tarea_id,))
        conn.commit()
        flash('Tarea ocultada del catálogo.', 'success')
    except Exception:
        flash('No se pudo quitar la tarea.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('expertise'))

# ============================================================
# === 6. HELPERS COMPARTIDOS (semáforo y mini-gráfica) =======
# ============================================================
def semaforo_valor(v):
    """Verde >=80 · Amarillo 60-79 · Rojo <60 (regla del Excel BPD/5S)."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        x = 0.0
    if x >= 80:
        return {'valor': round(x, 1), 'nivel': 'Verde',    'css': 'bg-emerald-50 text-emerald-600 border-emerald-100', 'hex': '#059669'}
    if x >= 60:
        return {'valor': round(x, 1), 'nivel': 'Amarillo', 'css': 'bg-amber-50 text-amber-600 border-amber-100',       'hex': '#d97706'}
    return {'valor': round(x, 1), 'nivel': 'Rojo',     'css': 'bg-rose-50 text-rose-600 border-rose-100',           'hex': '#e11d48'}

def _sparkline(vals, w=132, h=36, pad=4):
    """Geometría de una mini-gráfica de línea (0-100) en SVG."""
    vals = [max(0.0, min(100.0, float(v))) for v in vals]
    n = len(vals)
    if n == 0:
        return {'puntos': '', 'w': w, 'h': h, 'n': 0}
    if n == 1:
        x = w / 2.0
        y = h - pad - (vals[0] / 100.0) * (h - 2 * pad)
        return {'puntos': f'{x:.1f},{y:.1f}', 'w': w, 'h': h, 'n': 1, 'cx': x, 'cy': y}
    pts = []
    for i, v in enumerate(vals):
        x = pad + i * (w - 2 * pad) / (n - 1)
        y = h - pad - (v / 100.0) * (h - 2 * pad)
        pts.append(f'{x:.1f},{y:.1f}')
    ult = pts[-1].split(',')
    return {'puntos': ' '.join(pts), 'w': w, 'h': h, 'n': n, 'cx': float(ult[0]), 'cy': float(ult[1])}

def _empleados_activos_no_admin(conn):
    return conn.execute(
        "SELECT numero_empleado, nombre_completo, puesto, area, fecha_ingreso FROM usuarios WHERE estatus = 1 AND rol != 'admin' ORDER BY nombre_completo"
    ).fetchall()

# ============================================================
# === 7. MÓDULO: DESEMPEÑO BPD ===============================
# ============================================================
@app.route('/bpd')
@login_required
@requiere_modulo('bpd')
def bpd():
    conn = get_db_connection()
    empleados = _empleados_activos_no_admin(conn)
    evals = conn.execute(
        "SELECT numero_empleado, fecha, calificacion FROM bpd_evaluaciones ORDER BY fecha ASC, id ASC"
    ).fetchall()
    conn.close()

    por_emp = {}
    for e in evals:
        por_emp.setdefault(e['numero_empleado'], []).append(e['calificacion'])

    filas = []
    for emp in empleados:
        num = emp['numero_empleado']
        serie = por_emp.get(num, [])
        partes = emp['nombre_completo'].split()
        if serie:
            promedio = round(sum(serie) / len(serie), 1)
            ultima = round(serie[-1], 1)
            if len(serie) >= 2:
                delta = serie[-1] - serie[-2]
                tendencia = 'sube' if delta > 0.5 else ('baja' if delta < -0.5 else 'igual')
            else:
                tendencia = 'igual'
        else:
            promedio = ultima = 0
            tendencia = 'sin'
        filas.append({
            'numero_empleado': num,
            'nombre_completo': emp['nombre_completo'],
            'corto': ' '.join(partes[:2]) if partes else emp['nombre_completo'],
            'puesto': emp['puesto'],
            'n_eval': len(serie),
            'promedio': promedio,
            'ultima': ultima,
            'tendencia': tendencia,
            'sem': semaforo_valor(ultima if serie else 0),
            'spark': _sparkline(serie) if serie else None,
        })

    evaluados = [f for f in filas if f['n_eval'] > 0]
    kpis = {
        'colaboradores': len(filas),
        'prom_equipo': round(sum(f['promedio'] for f in evaluados) / len(evaluados), 1) if evaluados else 0,
        'en_rojo': sum(1 for f in evaluados if f['sem']['nivel'] == 'Rojo'),
        'total_eval': len(evals),
    }
    return render_page('bpd.html', filas=filas, kpis=kpis, hoy=datetime.now().strftime('%Y-%m-%d'))

@app.route('/bpd/registrar', methods=['POST'])
@login_required
@requiere_modulo('bpd')
def bpd_registrar():
    num = request.form.get('numero_empleado')
    fecha = (request.form.get('fecha') or '').strip() or datetime.now().strftime('%Y-%m-%d')
    try:
        calif = max(0.0, min(100.0, float(request.form.get('calificacion'))))
    except (TypeError, ValueError):
        flash('La calificación debe ser un número de 0 a 100.', 'error')
        return hx_redirect(url_for('bpd'))
    if not num:
        flash('Selecciona al colaborador.', 'error')
        return hx_redirect(url_for('bpd'))
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO bpd_evaluaciones (numero_empleado, fecha, calificacion, observaciones, evaluado_por) VALUES (?, ?, ?, ?, ?)",
            (num, fecha, calif, (request.form.get('observaciones') or '').strip(), session.get('nombre'))
        )
        conn.commit()
        flash('Evaluación BPD registrada.', 'success')
    except Exception:
        flash('No se pudo registrar la evaluación.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('bpd'))

# ============================================================
# === 8. MÓDULO: INCIDENCIAS (calendario RH) =================
# ============================================================
CODIGOS_INCIDENCIA = [
    ('A',    'Asistencia',         'bg-emerald-50 text-emerald-600 border-emerald-100'),
    ('F',    'Falta',              'bg-rose-50 text-rose-600 border-rose-100'),
    ('FJ',   'Falta justificada',  'bg-amber-50 text-amber-600 border-amber-100'),
    ('PCG',  'Permiso c/ goce',    'bg-sky-50 text-[#0e6ba8] border-sky-100'),
    ('PSG',  'Permiso s/ goce',    'bg-violet-50 text-violet-600 border-violet-100'),
    ('INC',  'Incapacidad',        'bg-orange-50 text-orange-600 border-orange-100'),
    ('V',    'Vacaciones',         'bg-teal-50 text-teal-600 border-teal-100'),
    ('FEST', 'Festivo',            'bg-slate-100 text-slate-500 border-slate-200'),
    ('D',    'Descanso',           'bg-slate-50 text-slate-400 border-slate-100'),
    ('B',    'Baja',               'bg-zinc-200 text-zinc-600 border-zinc-300'),
]
MAP_INCID = {c[0]: {'etiqueta': c[1], 'css': c[2]} for c in CODIGOS_INCIDENCIA}

@app.route('/incidencias')
@login_required
@requiere_modulo('incidencias')
def incidencias():
    mes = (request.args.get('mes') or '').strip()
    try:
        anio, mnum = map(int, mes.split('-'))
        datetime(anio, mnum, 1)
    except (ValueError, AttributeError):
        ahora = datetime.now()
        anio, mnum = ahora.year, ahora.month
    dias_mes = calendar.monthrange(anio, mnum)[1]
    dias = list(range(1, dias_mes + 1))
    prefijo = f'{anio:04d}-{mnum:02d}'

    # navegación de meses
    mes_prev = f'{anio-1:04d}-12' if mnum == 1 else f'{anio:04d}-{mnum-1:02d}'
    mes_next = f'{anio+1:04d}-01' if mnum == 12 else f'{anio:04d}-{mnum+1:02d}'
    nombres_mes = ['', 'Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio',
                   'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre']

    conn = get_db_connection()
    empleados = _empleados_activos_no_admin(conn)
    regs = conn.execute(
        "SELECT numero_empleado, fecha, codigo FROM incidencias_dia WHERE fecha LIKE ?",
        (prefijo + '%',)
    ).fetchall()
    # Biométrico (checador): asistencia y retardos YA conocidos -> se auto-llenan.
    # El sistema no pide marcar la 'A' a mano; solo lo que el reloj no puede saber.
    bioregs = conn.execute(
        "SELECT numero_empleado, fecha, minutos_retardo FROM resumen_asistencias WHERE fecha LIKE ?",
        (prefijo + '%',)
    ).fetchall()
    festivos_mes = [dict(r) for r in conn.execute(
        "SELECT id, fecha, nombre FROM dias_festivos WHERE fecha LIKE ? ORDER BY fecha",
        (prefijo + '%',)
    ).fetchall()]
    conn.close()

    grid = {}
    for r in regs:
        try:
            d = int(r['fecha'][8:10])
        except (ValueError, IndexError):
            continue
        grid.setdefault(r['numero_empleado'], {})[d] = r['codigo']

    bio = {}
    dias_con_datos = set()   # días del mes en que ALGUIEN checó -> día laboral con registro
    for r in bioregs:
        try:
            d = int(r['fecha'][8:10])
        except (ValueError, IndexError):
            continue
        bio.setdefault(r['numero_empleado'], {})[d] = (r['minutos_retardo'] or 0)
        dias_con_datos.add(d)

    festivos_set = {int(f['fecha'][8:10]) for f in festivos_mes}
    hoy_date = datetime.now().date()

    filas = []
    auto_total = 0
    for emp in empleados:
        num = emp['numero_empleado']
        ingreso_date = None
        if emp['fecha_ingreso']:
            try:
                ingreso_date = datetime.strptime(str(emp['fecha_ingreso'])[:10], '%Y-%m-%d').date()
            except (ValueError, TypeError):
                ingreso_date = None
        celdas = []
        faltas = 0
        retardos = 0
        for d in dias:
            fecha_d = datetime(anio, mnum, d).date()
            manual = grid.get(num, {}).get(d)
            if manual:                                   # 1) lo manual SIEMPRE manda
                cod, auto, ret = manual, False, 0
            elif num in bio and d in bio[num]:           # 2) checada -> Asistencia
                cod, auto, ret = 'A', True, bio[num][d]
            elif ingreso_date and fecha_d < ingreso_date:   # 3) antes de ingresar: nada
                cod, auto, ret = None, False, 0
            elif d in festivos_set:                      # 4) festivo oficial
                cod, auto, ret = 'FEST', True, 0
            elif fecha_d.weekday() == 6:                 # 5) domingo -> descanso
                cod, auto, ret = 'D', True, 0
            elif fecha_d >= hoy_date:                    # 6) hoy/futuro: aún no se sabe
                cod, auto, ret = None, False, 0
            elif d in dias_con_datos:                    # 7) día laboral con registro y sin checada -> FALTA
                cod, auto, ret = 'F', True, 0
            else:                                        # 8) día sin datos importados: nada
                cod, auto, ret = None, False, 0
            celdas.append({'dia': d, 'codigo': cod,
                           'css': MAP_INCID.get(cod, {}).get('css', '') if cod else '',
                           'auto': auto, 'retardo': ret})
            if cod == 'F':
                faltas += 1
            if cod == 'A' and auto:
                auto_total += 1
            if cod == 'A' and ret > 0:
                retardos += 1
        partes = emp['nombre_completo'].split()
        filas.append({
            'numero_empleado': num,
            'nombre_completo': emp['nombre_completo'],
            'corto': ' '.join(partes[:2]) if partes else emp['nombre_completo'],
            'puesto': emp['puesto'],
            'celdas': celdas,
            'faltas': faltas,
            'retardos': retardos,
            'alerta': faltas >= 3,
        })

    return render_page('incidencias.html', filas=filas, dias=dias, codigos=CODIGOS_INCIDENCIA,
                       mes=prefijo, mes_label=f'{nombres_mes[mnum]} {anio}', mes_prev=mes_prev,
                       mes_next=mes_next, auto_total=auto_total, festivos_mes=festivos_mes,
                       hoy=datetime.now().strftime('%Y-%m-%d'))

@app.route('/incidencias/marcar', methods=['POST'])
@login_required
@requiere_modulo('incidencias')
def incidencias_marcar():
    num = request.form.get('numero_empleado')
    fecha = (request.form.get('fecha') or '').strip()
    codigo = (request.form.get('codigo') or '').strip()
    mes_volver = fecha[:7] if len(fecha) >= 7 else ''
    if not num or not fecha:
        flash('Falta el colaborador o la fecha.', 'error')
        return hx_redirect(url_for('incidencias', mes=mes_volver))
    if codigo and codigo not in MAP_INCID:
        flash('Código de incidencia no válido.', 'error')
        return hx_redirect(url_for('incidencias', mes=mes_volver))
    conn = get_db_connection()
    try:
        if not codigo:
            conn.execute("DELETE FROM incidencias_dia WHERE numero_empleado = ? AND fecha = ?", (num, fecha))
        else:
            conn.execute('''
                INSERT INTO incidencias_dia (numero_empleado, fecha, codigo, nota, registrado_por)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(numero_empleado, fecha) DO UPDATE SET
                    codigo = excluded.codigo, nota = excluded.nota, registrado_por = excluded.registrado_por
            ''', (num, fecha, codigo, (request.form.get('nota') or '').strip(), session.get('nombre')))
        conn.commit()
        flash('Incidencia registrada.', 'success')
    except Exception:
        flash('No se pudo registrar la incidencia.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('incidencias', mes=mes_volver))

@app.route('/festivos/agregar', methods=['POST'])
@login_required
@requiere_modulo('incidencias')
def festivo_agregar():
    fecha = (request.form.get('fecha') or '').strip()
    nombre = (request.form.get('nombre') or '').strip() or 'Día festivo'
    mes_volver = fecha[:7] if len(fecha) >= 7 else ''
    try:
        datetime.strptime(fecha, '%Y-%m-%d')
    except ValueError:
        flash('Fecha inválida.', 'error')
        return hx_redirect(url_for('incidencias', mes=mes_volver))
    conn = get_db_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO dias_festivos (fecha, nombre) VALUES (?, ?)", (fecha, nombre))
        conn.commit()
        flash('Día festivo agregado.', 'success')
    except Exception:
        flash('No se pudo agregar el día festivo.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('incidencias', mes=mes_volver))

@app.route('/festivos/eliminar', methods=['POST'])
@login_required
@requiere_modulo('incidencias')
def festivo_eliminar():
    mes_volver = (request.form.get('mes') or '').strip()
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM dias_festivos WHERE id = ?", (request.form.get('id'),))
        conn.commit()
        flash('Día festivo eliminado.', 'success')
    except Exception:
        flash('No se pudo eliminar.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('incidencias', mes=mes_volver))

# ============================================================
# === 9. MÓDULO: CAPACITACIÓN ================================
# ============================================================
ESTADOS_CAP = {
    'Tomado':     {'sigla': 'O', 'css': 'bg-emerald-50 text-emerald-600 border-emerald-100'},
    'Programado': {'sigla': 'P', 'css': 'bg-amber-50 text-amber-600 border-amber-100'},
    'Pendiente':  {'sigla': '·', 'css': 'bg-slate-50 text-slate-300 border-slate-100'},
}

@app.route('/capacitacion')
@login_required
@requiere_modulo('capacitacion')
def capacitacion():
    conn = get_db_connection()
    cursos = [dict(r) for r in conn.execute(
        "SELECT id, nombre FROM cursos WHERE activo = 1 ORDER BY orden, id").fetchall()]
    empleados = _empleados_activos_no_admin(conn)
    regs = conn.execute("SELECT numero_empleado, curso_id, estatus FROM capacitacion_registro").fetchall()
    conn.close()

    matriz = {}
    for r in regs:
        matriz.setdefault(r['numero_empleado'], {})[r['curso_id']] = r['estatus']

    filas = []
    tomados_por_curso = {c['id']: 0 for c in cursos}
    for emp in empleados:
        num = emp['numero_empleado']
        celdas, tomados = [], 0
        for c in cursos:
            est = matriz.get(num, {}).get(c['id'], 'Pendiente')
            info = ESTADOS_CAP.get(est, ESTADOS_CAP['Pendiente'])
            celdas.append({'curso_id': c['id'], 'estatus': est, **info})
            if est == 'Tomado':
                tomados += 1
                tomados_por_curso[c['id']] += 1
        partes = emp['nombre_completo'].split()
        filas.append({
            'numero_empleado': num,
            'nombre_completo': emp['nombre_completo'],
            'corto': ' '.join(partes[:2]) if partes else emp['nombre_completo'],
            'puesto': emp['puesto'],
            'celdas': celdas,
            'avance': round(tomados / len(cursos) * 100) if cursos else 0,
        })

    total_celdas = len(filas) * len(cursos)
    total_tomados = sum(tomados_por_curso.values())
    kpis = {
        'colaboradores': len(filas),
        'cursos': len(cursos),
        'avance': round(total_tomados / total_celdas * 100) if total_celdas else 0,
        'tomados': total_tomados,
    }
    return render_page('capacitacion.html', cursos=cursos, filas=filas,
                       tomados_por_curso=tomados_por_curso, kpis=kpis, estados=ESTADOS_CAP)

@app.route('/capacitacion/guardar', methods=['POST'])
@login_required
@requiere_modulo('capacitacion')
def capacitacion_guardar():
    num = request.form.get('numero_empleado')
    if not num:
        flash('Falta seleccionar al colaborador.', 'error')
        return hx_redirect(url_for('capacitacion'))
    conn = get_db_connection()
    try:
        cursos = conn.execute("SELECT id FROM cursos WHERE activo = 1").fetchall()
        for c in cursos:
            est = request.form.get(f'curso_{c["id"]}')
            if est not in ESTADOS_CAP:
                continue
            conn.execute('''
                INSERT INTO capacitacion_registro (numero_empleado, curso_id, estatus, fecha)
                VALUES (?, ?, ?, datetime('now','localtime'))
                ON CONFLICT(numero_empleado, curso_id) DO UPDATE SET estatus = excluded.estatus, fecha = excluded.fecha
            ''', (num, c['id'], est))
        conn.commit()
        flash('Capacitación actualizada.', 'success')
    except Exception:
        flash('No se pudo guardar la capacitación.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('capacitacion'))

@app.route('/capacitacion/nuevo_curso', methods=['POST'])
@login_required
@requiere_modulo('capacitacion')
def capacitacion_nuevo_curso():
    nombre = (request.form.get('nombre') or '').strip()
    if not nombre:
        flash('Escribe el nombre del curso.', 'error')
        return hx_redirect(url_for('capacitacion'))
    conn = get_db_connection()
    try:
        maxord = conn.execute("SELECT COALESCE(MAX(orden), 0) + 1 AS o FROM cursos").fetchone()['o']
        conn.execute("INSERT INTO cursos (nombre, tipo, orden, activo) VALUES (?, 'BPM', ?, 1)", (nombre, maxord))
        conn.commit()
        flash('Curso agregado.', 'success')
    except Exception:
        flash('No se pudo agregar el curso.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('capacitacion'))

@app.route('/capacitacion/eliminar_curso', methods=['POST'])
@login_required
@requiere_modulo('capacitacion')
def capacitacion_eliminar_curso():
    curso_id = request.form.get('curso_id')
    conn = get_db_connection()
    try:
        conn.execute("UPDATE cursos SET activo = 0 WHERE id = ?", (curso_id,))
        conn.commit()
        flash('Curso ocultado del catálogo.', 'success')
    except Exception:
        flash('No se pudo quitar el curso.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('capacitacion'))

# --- M4 · Detección de Necesidades de Capacitación (DNC) ---
DNC_TARGET = 80   # nivel de competencia objetivo (%) para considerar la brecha cubierta

@app.route('/capacitacion/dnc')
@login_required
@requiere_modulo('capacitacion')
def capacitacion_dnc():
    """Deriva la DNC de la Matriz de Expertise: por área y competencia calcula el nivel
    promedio real, la brecha contra el objetivo y una prioridad medible (sin criterio a dedo)."""
    conn = get_db_connection()
    tareas = conn.execute(
        "SELECT id, nombre, area FROM competencias_tareas WHERE activo = 1 ORDER BY area, orden, id").fetchall()
    evals = conn.execute("SELECT tarea_id, nivel_pct FROM expertise_evaluaciones").fetchall()
    conn.close()

    niveles = {}
    for e in evals:
        niveles.setdefault(e['tarea_id'], []).append(e['nivel_pct'] or 0)

    por_area = {}
    resumen = {'alta': 0, 'media': 0, 'baja': 0, 'sin': 0}
    for t in tareas:
        vals = niveles.get(t['id'], [])
        n = len(vals)
        prom = round(sum(vals) / n) if n else 0
        if n == 0:
            prio, css, brecha = 'Sin datos', 'bg-slate-100 text-slate-500', DNC_TARGET
            resumen['sin'] += 1
        elif prom < 60:
            prio, css, brecha = 'Alta', 'bg-rose-50 text-rose-600', DNC_TARGET - prom
            resumen['alta'] += 1
        elif prom < DNC_TARGET:
            prio, css, brecha = 'Media', 'bg-amber-50 text-amber-700', DNC_TARGET - prom
            resumen['media'] += 1
        else:
            prio, css, brecha = 'Baja', 'bg-emerald-50 text-emerald-700', 0
            resumen['baja'] += 1
        por_area.setdefault(t['area'] or 'General', []).append({
            'nombre': t['nombre'], 'evaluados': n, 'promedio': prom,
            'brecha': brecha, 'prioridad': prio, 'css': css})
    for a in por_area:
        por_area[a].sort(key=lambda x: -x['brecha'])
    return render_page('capacitacion_dnc.html', por_area=por_area, resumen=resumen, target=DNC_TARGET)

@app.route('/capacitacion/constancia/<numero_empleado>/<int:curso_id>')
@login_required
@requiere_modulo('capacitacion')
def capacitacion_constancia(numero_empleado, curso_id):
    """Constancia de participación imprimible (PDF vía navegador) de un curso ya tomado."""
    conn = get_db_connection()
    emp = conn.execute(
        "SELECT numero_empleado, nombre_completo, puesto, area FROM usuarios WHERE numero_empleado = ?",
        (numero_empleado,)).fetchone()
    curso = conn.execute(
        "SELECT nombre, tipo, duracion_horas, instructor FROM cursos WHERE id = ?", (curso_id,)).fetchone()
    reg = conn.execute(
        "SELECT estatus, fecha, calificacion FROM capacitacion_registro WHERE numero_empleado = ? AND curso_id = ?",
        (numero_empleado, curso_id)).fetchone()
    conn.close()
    if not emp or not curso:
        flash('No se encontró al colaborador o el curso.', 'error')
        return hx_redirect(url_for('capacitacion'))
    if not reg or reg['estatus'] != 'Tomado':
        flash('Solo se puede emitir constancia de un curso ya tomado.', 'error')
        return hx_redirect(url_for('capacitacion'))
    folio = f'ATISA-CAP-{emp["numero_empleado"]}-{curso_id}'
    return full_page('constancia.html', emp=emp, curso=curso, reg=reg, hoy=datetime.now(), folio=folio)

# ============================================================
# === M6 · CARDEX & NIVEL DE MADUREZ =========================
# ============================================================
NIVELES_MADUREZ = {
    1: {'label': 'Nuevo',         'css': 'bg-slate-100 text-slate-500'},
    2: {'label': 'En aprendizaje','css': 'bg-sky-50 text-[#0e6ba8]'},
    3: {'label': 'Competente',    'css': 'bg-blue-50 text-azul'},
    4: {'label': 'Avanzado',      'css': 'bg-emerald-50 text-emerald-600'},
    5: {'label': 'Experto',       'css': 'bg-emerald-100 text-emerald-700'},
}
CARDEX_TIPOS = {
    'promocion':      {'label': 'Promoción',        'icono': 'ri-arrow-up-circle-line', 'css': 'bg-emerald-50 text-emerald-600'},
    'reconocimiento': {'label': 'Reconocimiento',   'icono': 'ri-medal-line',           'css': 'bg-amber-50 text-amber-600'},
    'amonestacion':   {'label': 'Amonestación',     'icono': 'ri-error-warning-line',   'css': 'bg-rose-50 text-rose-600'},
    'puesto':         {'label': 'Cambio de puesto', 'icono': 'ri-briefcase-4-line',     'css': 'bg-sky-50 text-[#0e6ba8]'},
}

def _antiguedad(fecha_ingreso):
    """Antigüedad legible y años totales desde la fecha de ingreso (automático)."""
    if not fecha_ingreso:
        return {'texto': '—', 'anios': 0, 'dias': 0}
    try:
        ing = datetime.strptime(str(fecha_ingreso)[:10], '%Y-%m-%d')
    except ValueError:
        return {'texto': '—', 'anios': 0, 'dias': 0}
    dias = (datetime.now() - ing).days
    if dias < 0:
        return {'texto': '—', 'anios': 0, 'dias': 0}
    anios, meses = dias // 365, (dias % 365) // 30
    partes = []
    if anios:
        partes.append(f"{anios} año{'s' if anios != 1 else ''}")
    if meses:
        partes.append(f"{meses} mes{'es' if meses != 1 else ''}")
    return {'texto': ' y '.join(partes) or 'Menos de 1 mes', 'anios': anios, 'dias': dias}

def _nivel_madurez(pct_expertise, n_evals, anios_antiguedad=0):
    """Nivel 1-5 medible, sin criterio a dedo:
    - Si hay evaluación de competencias, manda el promedio real (más preciso):
      1 Nuevo (<20%), 2 Aprendizaje (<40), 3 Competente (<60), 4 Avanzado (<80), 5 Experto (>=80).
    - Si NO hay competencias evaluadas (áreas fuera de la Matriz de Expertise), se estima por
      antigüedad: 1 (<1 año), 2 (<2), 3 (<4), 4 (<7), 5 (>=7). Así un veterano no aparece como 'Nuevo'."""
    if n_evals:
        p = pct_expertise
        if p < 20: return 1
        if p < 40: return 2
        if p < 60: return 3
        if p < 80: return 4
        return 5
    a = anios_antiguedad or 0
    if a < 1: return 1
    if a < 2: return 2
    if a < 4: return 3
    if a < 7: return 4
    return 5

@app.route('/cardex')
@login_required
@requiere_modulo('cardex')
def cardex():
    conn = get_db_connection()
    empleados = _empleados_activos_no_admin(conn)
    exp_rows = conn.execute(
        "SELECT numero_empleado, AVG(nivel_pct) prom, COUNT(*) n FROM expertise_evaluaciones GROUP BY numero_empleado").fetchall()
    exp = {r['numero_empleado']: (r['prom'] or 0, r['n']) for r in exp_rows}
    bpd_last = {}
    for r in conn.execute("SELECT numero_empleado, calificacion FROM bpd_evaluaciones ORDER BY fecha ASC, id ASC").fetchall():
        bpd_last[r['numero_empleado']] = r['calificacion']
    conn.close()

    filas = []
    reparto = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for emp in empleados:
        num = emp['numero_empleado']
        prom_exp, n_exp = exp.get(num, (0, 0))
        nivel = _nivel_madurez(prom_exp, n_exp, _antiguedad(emp['fecha_ingreso'])['anios'])
        reparto[nivel] += 1
        scores = []
        if num in bpd_last:
            scores.append(bpd_last[num])
        if n_exp:
            scores.append(prom_exp)
        general = round(sum(scores) / len(scores)) if scores else 0
        partes = emp['nombre_completo'].split()
        filas.append({
            'numero_empleado': num,
            'nombre_completo': emp['nombre_completo'],
            'corto': ' '.join(partes[:2]) if partes else emp['nombre_completo'],
            'puesto': emp['puesto'], 'area': emp['area'],
            'nivel': nivel, 'nivel_info': NIVELES_MADUREZ[nivel],
            'expertise': round(prom_exp), 'n_exp': n_exp,
            'sem': semaforo_valor(general) if scores else None,
        })
    filas.sort(key=lambda f: (-f['nivel'], f['nombre_completo']))
    kpis = {'colaboradores': len(filas), 'reparto': reparto,
            'expertos': reparto[5] + reparto[4],
            'sin_eval': sum(1 for f in filas if f['sem'] is None)}
    return render_page('cardex.html', filas=filas, kpis=kpis, niveles=NIVELES_MADUREZ)

@app.route('/cardex/<numero_empleado>')
@login_required
@requiere_modulo('cardex')
def cardex_detalle(numero_empleado):
    conn = get_db_connection()
    emp = conn.execute("SELECT * FROM usuarios WHERE numero_empleado = ?", (numero_empleado,)).fetchone()
    if not emp:
        conn.close()
        flash('Colaborador no encontrado.', 'error')
        return hx_redirect(url_for('cardex'))
    emp = dict(emp)

    # Expertise + radar
    tareas = conn.execute("SELECT id, nombre, area FROM competencias_tareas WHERE activo = 1 ORDER BY orden, id").fetchall()
    ev = conn.execute("SELECT tarea_id, nivel_pct FROM expertise_evaluaciones WHERE numero_empleado = ?", (numero_empleado,)).fetchall()
    valores = {r['tarea_id']: r['nivel_pct'] for r in ev}
    prom_exp = round(sum(valores.values()) / len(valores)) if valores else 0
    n_exp = len(valores)
    radar = _radar_expertise(tareas, valores) if (tareas and valores) else None

    # Desempeño (BPD)
    serie = [r['calificacion'] for r in conn.execute(
        "SELECT calificacion FROM bpd_evaluaciones WHERE numero_empleado = ? ORDER BY fecha ASC, id ASC", (numero_empleado,)).fetchall()]
    desempeno = {'n': len(serie), 'ultima': round(serie[-1], 1) if serie else 0,
                 'promedio': round(sum(serie) / len(serie), 1) if serie else 0,
                 'spark': _sparkline(serie) if serie else None}

    # Asistencia (faltas / retardos últimos 90 días)
    faltas90 = conn.execute(
        "SELECT COUNT(*) n FROM incidencias_dia WHERE numero_empleado = ? AND codigo = 'F' AND fecha >= date('now','-90 day')",
        (numero_empleado,)).fetchone()['n']
    asistencia_score = max(0, 100 - faltas90 * 15)

    # Capacitación (cursos tomados / activos)
    n_cursos = conn.execute("SELECT COUNT(*) n FROM cursos WHERE activo = 1").fetchone()['n']
    tomados = conn.execute(
        "SELECT COUNT(*) n FROM capacitacion_registro WHERE numero_empleado = ? AND estatus = 'Tomado'",
        (numero_empleado,)).fetchone()['n']
    avance_cap = round(tomados / n_cursos * 100) if n_cursos else 0

    # Expediente (documentos cargados / requeridos)
    docs_cargados = conn.execute(
        "SELECT COUNT(DISTINCT tipo_documento) n FROM documentos_empleados WHERE numero_empleado = ?",
        (numero_empleado,)).fetchone()['n']
    total_docs = len(DOCS_EXPEDIENTE)
    pct_docs = round(docs_cargados / total_docs * 100) if total_docs else 0

    # Eventos de trayectoria
    eventos = [dict(r) for r in conn.execute(
        "SELECT * FROM cardex_eventos WHERE numero_empleado = ? ORDER BY fecha DESC, id DESC", (numero_empleado,)).fetchall()]
    for e in eventos:
        e['info'] = CARDEX_TIPOS.get(e['tipo'], {'label': e['tipo'], 'icono': 'ri-bookmark-line', 'css': 'bg-slate-100 text-slate-500'})
    conn.close()

    # Nivel de madurez y semáforo general (compuesto de lo disponible)
    antiguedad = _antiguedad(emp.get('fecha_ingreso'))
    nivel = _nivel_madurez(prom_exp, n_exp, antiguedad['anios'])
    scores = [asistencia_score, avance_cap, pct_docs]
    if serie:
        scores.append(desempeno['ultima'])
    if n_exp:
        scores.append(prom_exp)
    general = round(sum(scores) / len(scores)) if scores else 0

    # Alertas activas del colaborador
    alertas = []
    if faltas90 >= 3:
        alertas.append({'texto': f'{faltas90} faltas en los últimos 90 días', 'css': 'text-rose-600', 'icono': 'ri-calendar-close-line'})
    if pct_docs < 100:
        alertas.append({'texto': f'Expediente incompleto ({docs_cargados}/{total_docs} documentos)', 'css': 'text-amber-600', 'icono': 'ri-folder-warning-line'})
    ffin = emp.get('fecha_fin_contrato')
    if ffin:
        try:
            dias_fin = (datetime.strptime(str(ffin)[:10], '%Y-%m-%d') - datetime.now()).days
            if 0 <= dias_fin <= 30:
                alertas.append({'texto': f'Contrato vence en {dias_fin} días ({ffin})', 'css': 'text-rose-600', 'icono': 'ri-file-warning-line'})
        except ValueError:
            pass
    if avance_cap < 60:
        alertas.append({'texto': f'Capacitación por debajo del plan ({avance_cap}%)', 'css': 'text-amber-600', 'icono': 'ri-graduation-cap-line'})

    metricas = {
        'desempeno': desempeno, 'asistencia_score': asistencia_score, 'faltas90': faltas90,
        'avance_cap': avance_cap, 'tomados': tomados, 'n_cursos': n_cursos,
        'pct_docs': pct_docs, 'docs_cargados': docs_cargados, 'total_docs': total_docs,
        'expertise': prom_exp, 'n_exp': n_exp,
    }
    return render_page('cardex_detalle.html', emp=emp, antiguedad=antiguedad,
                       nivel=nivel, nivel_info=NIVELES_MADUREZ[nivel], niveles=NIVELES_MADUREZ,
                       general=general, sem_general=semaforo_valor(general), radar=radar,
                       metricas=metricas, eventos=eventos, alertas=alertas, tipos=CARDEX_TIPOS)

@app.route('/cardex/evento/agregar', methods=['POST'])
@login_required
@requiere_modulo('cardex')
def cardex_evento_agregar():
    num = (request.form.get('numero_empleado') or '').strip()
    tipo = request.form.get('tipo')
    titulo = (request.form.get('titulo') or '').strip()
    fecha = (request.form.get('fecha') or '').strip()
    detalle = (request.form.get('detalle') or '').strip()
    if not num or tipo not in CARDEX_TIPOS or not titulo:
        flash('Faltan datos del evento (tipo o título).', 'error')
        return hx_redirect(url_for('cardex_detalle', numero_empleado=num or '0'))
    if not fecha:
        fecha = datetime.now().strftime('%Y-%m-%d')
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO cardex_eventos (numero_empleado, tipo, fecha, titulo, detalle, registrado_por) VALUES (?,?,?,?,?,?)",
            (num, tipo, fecha, titulo, detalle, session.get('nombre')))
        conn.commit()
        flash('Evento agregado al cardex.', 'success')
    except Exception:
        flash('No se pudo agregar el evento.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('cardex_detalle', numero_empleado=num))

@app.route('/cardex/evento/eliminar', methods=['POST'])
@login_required
@requiere_modulo('cardex')
def cardex_evento_eliminar():
    eid = (request.form.get('id') or '').strip()
    num = (request.form.get('numero_empleado') or '').strip()
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM cardex_eventos WHERE id = ?", (eid,))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
    return hx_redirect(url_for('cardex_detalle', numero_empleado=num))

# ============================================================
# === 10. MÓDULO: AUDITORÍAS 5S ==============================
# ============================================================
@app.route('/cinco_s')
@login_required
@requiere_modulo('cinco_s')
def cinco_s():
    conn = get_db_connection()
    auditorias = conn.execute(
        "SELECT * FROM cinco_s_auditorias ORDER BY fecha DESC, id DESC"
    ).fetchall()
    conn.close()

    # Última calificación por área (para el resumen con semáforo)
    areas = {}
    for a in auditorias:
        if a['area'] not in areas:   # la primera es la más reciente por el ORDER BY
            areas[a['area']] = {'area': a['area'], 'lider': a['lider'], 'fecha': a['fecha'],
                                'sem': semaforo_valor(a['calificacion'])}
    areas_list = list(areas.values())
    prom = round(sum(x['sem']['valor'] for x in areas_list) / len(areas_list), 1) if areas_list else 0
    kpis = {
        'areas': len(areas_list),
        'prom': prom,
        'en_rojo': sum(1 for x in areas_list if x['sem']['nivel'] == 'Rojo'),
        'total': len(auditorias),
    }
    return render_page('cinco_s.html', auditorias=auditorias, areas=areas_list, kpis=kpis,
                       semaforo=semaforo_valor, hoy=datetime.now().strftime('%Y-%m-%d'))

@app.route('/cinco_s/registrar', methods=['POST'])
@login_required
@requiere_modulo('cinco_s')
def cinco_s_registrar():
    area = (request.form.get('area') or '').strip()
    if not area:
        flash('Escribe el área auditada.', 'error')
        return hx_redirect(url_for('cinco_s'))
    fecha = (request.form.get('fecha') or '').strip() or datetime.now().strftime('%Y-%m-%d')
    try:
        hallazgos = max(0, int(request.form.get('hallazgos') or 0))
    except ValueError:
        hallazgos = 0
    calificacion = max(0.0, 100.0 - 5.0 * hallazgos)   # regla del Excel: -5 pts por hallazgo
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO cinco_s_auditorias (area, lider, fecha, hallazgos, calificacion, nota, registrado_por) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (area, (request.form.get('lider') or '').strip(), fecha, hallazgos, calificacion,
             (request.form.get('nota') or '').strip(), session.get('nombre'))
        )
        conn.commit()
        flash(f'Auditoría 5S registrada ({calificacion:.0f} pts).', 'success')
    except Exception:
        flash('No se pudo registrar la auditoría.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('cinco_s'))

@app.route('/cinco_s/eliminar', methods=['POST'])
@login_required
@requiere_modulo('cinco_s')
def cinco_s_eliminar():
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM cinco_s_auditorias WHERE id = ?", (request.form.get('id'),))
        conn.commit()
        flash('Auditoría eliminada.', 'success')
    except Exception:
        flash('No se pudo eliminar.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('cinco_s'))

# ============================================================
# === 11. MÓDULO: ROL DE LIMPIEZAS ===========================
# ============================================================
@app.route('/limpiezas')
@login_required
@requiere_modulo('limpiezas')
def limpiezas():
    conn = get_db_connection()
    registros = conn.execute(
        "SELECT * FROM limpieza_rol ORDER BY orden DESC, id DESC"
    ).fetchall()
    empleados = _empleados_activos_no_admin(conn)
    conn.close()
    # Agrupar por semana conservando el orden de aparición
    semanas = []
    grupos = {}
    for r in registros:
        if r['semana'] not in grupos:
            grupos[r['semana']] = []
            semanas.append(r['semana'])
        grupos[r['semana']].append(r)
    return render_page('limpiezas.html', semanas=semanas, grupos=grupos, empleados=empleados,
                       total=len(registros))

@app.route('/limpiezas/registrar', methods=['POST'])
@login_required
@requiere_modulo('limpiezas')
def limpiezas_registrar():
    semana = (request.form.get('semana') or '').strip()
    area = (request.form.get('area') or '').strip()
    responsable = (request.form.get('responsable') or '').strip()
    if not (semana and area and responsable):
        flash('Completa semana, área y responsable.', 'error')
        return hx_redirect(url_for('limpiezas'))
    conn = get_db_connection()
    try:
        maxord = conn.execute("SELECT COALESCE(MAX(orden), 0) + 1 AS o FROM limpieza_rol").fetchone()['o']
        conn.execute(
            "INSERT INTO limpieza_rol (semana, area, responsable, nota, orden) VALUES (?, ?, ?, ?, ?)",
            (semana, area, responsable, (request.form.get('nota') or '').strip(), maxord)
        )
        conn.commit()
        flash('Asignación de limpieza registrada.', 'success')
    except Exception:
        flash('No se pudo registrar la asignación.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('limpiezas'))

@app.route('/limpiezas/eliminar', methods=['POST'])
@login_required
@requiere_modulo('limpiezas')
def limpiezas_eliminar():
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM limpieza_rol WHERE id = ?", (request.form.get('id'),))
        conn.commit()
        flash('Asignación eliminada.', 'success')
    except Exception:
        flash('No se pudo eliminar.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('limpiezas'))

# ============================================================
# === 12. MÓDULO: PRODUCCIÓN (llenado) =======================
# ============================================================
@app.route('/produccion')
@login_required
@requiere_modulo('produccion')
def produccion():
    conn = get_db_connection()
    registros = conn.execute(
        "SELECT * FROM produccion_llenado ORDER BY fecha DESC, id DESC LIMIT 200"
    ).fetchall()
    conn.close()
    filas = []
    for r in registros:
        total = (r['turno_manana'] or 0) + (r['turno_tarde'] or 0)
        filas.append({**dict(r), 'total': total})
    # KPIs del mes en curso
    mes_actual = datetime.now().strftime('%Y-%m')
    delmes = [f for f in filas if (f['fecha'] or '').startswith(mes_actual)]
    total_mes = sum(f['total'] for f in delmes)
    dias_reg = len(set(f['fecha'] for f in delmes))
    kpis = {
        'total_mes': total_mes,
        'dias': dias_reg,
        'prom_dia': round(total_mes / dias_reg) if dias_reg else 0,
        'registros': len(filas),
    }
    # mini-barras de los últimos días (orden cronológico)
    ultimos = list(reversed(filas[:14]))
    maxv = max((f['total'] for f in ultimos), default=0) or 1
    barras = [{'fecha': f['fecha'], 'total': f['total'], 'pct': round(f['total'] / maxv * 100)} for f in ultimos]
    return render_page('produccion.html', filas=filas, kpis=kpis, barras=barras,
                       hoy=datetime.now().strftime('%Y-%m-%d'))

@app.route('/produccion/registrar', methods=['POST'])
@login_required
@requiere_modulo('produccion')
def produccion_registrar():
    fecha = (request.form.get('fecha') or '').strip() or datetime.now().strftime('%Y-%m-%d')
    producto = (request.form.get('producto') or '').strip()
    if not producto:
        flash('Escribe el producto.', 'error')
        return hx_redirect(url_for('produccion'))
    def _int(v):
        try:
            return max(0, int(float(v)))
        except (TypeError, ValueError):
            return 0
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO produccion_llenado (fecha, producto, lote, turno_manana, turno_tarde, nota, registrado_por) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (fecha, producto, (request.form.get('lote') or '').strip(),
             _int(request.form.get('turno_manana')), _int(request.form.get('turno_tarde')),
             (request.form.get('nota') or '').strip(), session.get('nombre'))
        )
        conn.commit()
        flash('Producción registrada.', 'success')
    except Exception:
        flash('No se pudo registrar la producción.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('produccion'))

@app.route('/produccion/eliminar', methods=['POST'])
@login_required
@requiere_modulo('produccion')
def produccion_eliminar():
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM produccion_llenado WHERE id = ?", (request.form.get('id'),))
        conn.commit()
        flash('Registro eliminado.', 'success')
    except Exception:
        flash('No se pudo eliminar.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('produccion'))

# ============================================================
# === 13. MÓDULO: AVISOS =====================================
# ============================================================
@app.route('/avisos')
@login_required
@requiere_modulo('avisos')
def avisos():
    conn = get_db_connection()
    lista = conn.execute(
        "SELECT * FROM avisos ORDER BY CASE estatus WHEN 'Abierto' THEN 0 ELSE 1 END, id DESC"
    ).fetchall()
    conn.close()
    kpis = {
        'total': len(lista),
        'abiertos': sum(1 for a in lista if a['estatus'] == 'Abierto'),
        'cerrados': sum(1 for a in lista if a['estatus'] != 'Abierto'),
    }
    return render_page('avisos.html', lista=lista, kpis=kpis, hoy=datetime.now().strftime('%Y-%m-%d'))

@app.route('/avisos/crear', methods=['POST'])
@login_required
@requiere_modulo('avisos')
def avisos_crear():
    aviso = (request.form.get('aviso') or '').strip()
    if not aviso:
        flash('Escribe el aviso.', 'error')
        return hx_redirect(url_for('avisos'))
    fecha = (request.form.get('fecha_aviso') or '').strip() or datetime.now().strftime('%Y-%m-%d')
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO avisos (aviso, fecha_aviso, compromiso, responsable) VALUES (?, ?, ?, ?)",
            (aviso, fecha, (request.form.get('compromiso') or '').strip(), (request.form.get('responsable') or '').strip())
        )
        conn.commit()
        flash('Aviso publicado.', 'success')
    except Exception:
        flash('No se pudo publicar el aviso.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('avisos'))

@app.route('/avisos/cerrar', methods=['POST'])
@login_required
@requiere_modulo('avisos')
def avisos_cerrar():
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT estatus FROM avisos WHERE id = ?", (request.form.get('id'),)).fetchone()
        nuevo = 'Abierto' if (row and row['estatus'] != 'Abierto') else 'Cerrado'
        conn.execute("UPDATE avisos SET estatus = ? WHERE id = ?", (nuevo, request.form.get('id')))
        conn.commit()
        flash(f'Aviso marcado como {nuevo.lower()}.', 'success')
    except Exception:
        flash('No se pudo actualizar el aviso.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('avisos'))

@app.route('/avisos/eliminar', methods=['POST'])
@login_required
@requiere_modulo('avisos')
def avisos_eliminar():
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM avisos WHERE id = ?", (request.form.get('id'),))
        conn.commit()
        flash('Aviso eliminado.', 'success')
    except Exception:
        flash('No se pudo eliminar.', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('avisos'))

if __name__ == '__main__':
    inicializar_bd()
    # El modo debug expone el depurador de Werkzeug (ejecución de código) y trazas de
    # error: como el ERP escucha en toda la red (0.0.0.0), queda APAGADO por defecto.
    # Para desarrollar en tu PC: define ATISA_DEBUG=true.
    debug = os.environ.get('ATISA_DEBUG', 'false').lower() in ('1', 'true', 'yes')
    app.run(debug=debug, host='0.0.0.0', port=5001)