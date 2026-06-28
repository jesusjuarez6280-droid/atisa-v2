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

app = Flask(__name__)
app.secret_key = 'super_secreta_llave_atisa_2026'

# === CACHÉ EN MEMORIA (catálogos que casi no cambian) ===
cache = Cache(config={'CACHE_TYPE': 'SimpleCache', 'CACHE_DEFAULT_TIMEOUT': 300})
cache.init_app(app)

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
    'recetas':       ['admin', 'operador'],
    'empleados':     ['admin', 'rh'],
    'documentos':    ['admin', 'rh'],
    'reportes':      ['admin', 'rh'],
    'asistencias':   ['admin', 'rh'],
    'expertise':     ['admin', 'rh'],
    'bpd':           ['admin', 'rh'],
    'incidencias':   ['admin', 'rh'],
    'capacitacion':  ['admin', 'rh'],
    'cinco_s':       ['admin', 'rh'],
    'limpiezas':     ['admin', 'rh'],
    'produccion':    ['admin', 'rh'],
    'avisos':        ['admin', 'rh'],
    'activos':       ['admin', 'rh'],
    'gasolina':      ['admin', 'rh'],
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
        ('/dashboard', 'Dashboard', 'ri-dashboard-3-line', ''),
        ('/avisos',    'Avisos',    'ri-megaphone-line',   ''),
    ]},
    {'key': 'rh', 'nombre': 'Recursos Humanos', 'icono': 'ri-group-line', 'modulos': [
        ('/empleados',    'Empleados',        'ri-id-card-line',        ''),
        ('/incidencias',  'Incidencias',      'ri-calendar-close-line', ''),
        ('/asistencias',  'Asistencias',      'ri-time-line',           '/reporte_individual'),
        ('/capacitacion', 'Capacitación',     'ri-graduation-cap-line', ''),
        ('/expertise',    'Matriz Expertise', 'ri-medal-line',          ''),
        ('/bpd',          'Desempeño BPD',    'ri-speed-up-line',       ''),
        ('/reportes',     'Reportes',         'ri-bar-chart-2-line',    ''),
    ]},
    {'key': 'produccion', 'nombre': 'Producción', 'icono': 'ri-flask-line', 'modulos': [
        ('/produccion', 'Producción', 'ri-medicine-bottle-line', ''),
        ('/recetas',    'Procesos',   'ri-list-check-3',         ''),
    ]},
    {'key': 'calidad', 'nombre': 'Calidad', 'icono': 'ri-shield-check-line', 'modulos': [
        ('/cinco_s',    'Auditorías 5S',  'ri-sparkling-2-line',  ''),
        ('/limpiezas',  'Rol Limpiezas',  'ri-brush-line',        ''),
        ('/documentos', 'Documentos BPM', 'ri-folder-open-line',  ''),
        ('/activos',    'Activos y EPP',  'ri-shield-check-line', ''),
    ]},
    {'key': 'logistica', 'nombre': 'Logística', 'icono': 'ri-truck-line', 'modulos': [
        ('/gasolina', 'Tarjeta Gasolina', 'ri-gas-station-fill', ''),
    ]},
    {'key': 'administracion', 'nombre': 'Administración', 'icono': 'ri-building-line', 'modulos': [
        ('/configuracion', 'Configuración', 'ri-settings-4-line', ''),
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

# --- 1. CONFIGURACIÓN DE LA BASE DE DATOS ---
def get_db_connection():
    conn = sqlite3.connect('base_planta.db', timeout=5.0)
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
                      ('talla_calzado', 'TEXT'), ('fecha_ingreso', 'TEXT')]:
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
        hash_pass = generate_password_hash("admin123")
        conn.execute('''
            INSERT INTO usuarios (numero_empleado, nombre_completo, username, password_hash, rol) 
            VALUES (?, ?, ?, ?, ?)
        ''', ('0001', 'Ing. Jesús Armando Juárez Cabrera', 'admin', hash_pass, 'admin'))

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
            'SELECT nombre_completo, rol, estatus FROM usuarios WHERE id = ?', (session['user_id'],)
        ).fetchone()
        conn.close()
        if not u or u['estatus'] != 1:
            session.clear()
            flash('Tu sesión ya no es válida. Inicia sesión de nuevo.', 'error')
            return hx_redirect(url_for('login'))
        session['rol'] = u['rol']
        session['nombre'] = u['nombre_completo']
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
        conn = get_db_connection()
        usuario = conn.execute('SELECT * FROM usuarios WHERE username = ? AND estatus = 1', (username,)).fetchone()
        conn.close()
        if usuario and check_password_hash(usuario['password_hash'], password):
            session['user_id'] = usuario['id']
            session['username'] = usuario['username']
            session['nombre'] = usuario['nombre_completo']
            session['rol'] = usuario['rol']
            return hx_redirect(url_for(landing_endpoint()))
        else:
            flash('Datos incorrectos', 'error')
    return full_page('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return hx_redirect(url_for('login'))

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
            minutos = int(request.form.get(f'paso_{paso_num}_minutos') or 0)
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
            minutos = int(request.form.get(f'paso_{paso_num}_minutos') or 0)
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

@app.route('/exportar_excel')
@login_required
@requiere_modulo('reportes')
def exportar_excel():
    conn = get_db_connection()
    try:
        df_usuarios = pd.read_sql_query("SELECT * FROM usuarios WHERE rol != 'admin'", conn)
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

@app.route('/editar_tarjeta', methods=['POST'])
@login_required
@requiere_modulo('gasolina')
def editar_tarjeta():
    tarjeta_id = request.form.get('tarjeta_id')
    saldo = request.form.get('saldo')
    estatus = request.form.get('estatus')
    conn = get_db_connection()
    try:
        conn.execute(
            'UPDATE tarjetas_gasolina SET saldo = ?, estatus = ? WHERE id = ?',
            (float(saldo), estatus, tarjeta_id)
        )
        conn.commit()
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
            'SELECT activo_id FROM asignaciones_activos WHERE id = ?',
            (asignacion_id,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE asignaciones_activos SET estatus = 'Devuelto' WHERE id = ?",
                (asignacion_id,)
            )
            conn.execute(
                "UPDATE inventario_activos SET estatus = 'Disponible' WHERE id = ?",
                (row['activo_id'],)
            )
            conn.commit()
            flash('Activo devuelto correctamente', 'success')
        else:
            flash('Asignación no encontrada', 'error')
    except Exception:
        flash('Error al devolver activo', 'error')
    finally:
        conn.close()
    return hx_redirect(url_for('activos'))

@app.route('/editar_activo', methods=['POST'])
@login_required
@requiere_modulo('activos')
def editar_activo():
    activo_id = request.form.get('activo_id')
    categoria = request.form.get('categoria')
    descripcion = request.form.get('descripcion')
    estatus = request.form.get('estatus')
    conn = get_db_connection()
    try:
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
    if not archivo or not archivo.filename:
        flash('Debe seleccionar un archivo', 'error')
        return hx_redirect(url_for('documentos'))
    if not extension_permitida(archivo.filename):
        flash('Formato no permitido. Solo se aceptan archivos PDF, JPG o PNG.', 'error')
        return hx_redirect(url_for('documentos'))
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
    return hx_redirect(url_for('documentos'))

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
    app.run(debug=True, host='0.0.0.0', port=5001)