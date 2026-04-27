from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps
from datetime import datetime, timedelta
import sqlite3
import pandas as pd
import io
import os
import json

app = Flask(__name__)
app.secret_key = 'super_secreta_llave_atisa_2026'

# Carpetas para guardar archivos físicos
app.config['UPLOAD_FOLDER'] = 'static/img_recetas'
app.config['UPLOAD_CONTRATOS'] = 'static/contratos'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['UPLOAD_CONTRATOS'], exist_ok=True)

# --- 1. CONFIGURACIÓN DE LA BASE DE DATOS ---
def get_db_connection():
    conn = sqlite3.connect('base_planta.db')
    conn.row_factory = sqlite3.Row 
    return conn

def inicializar_bd():
    conn = get_db_connection()
    
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
            FOREIGN KEY(numero_empleado) REFERENCES usuarios(numero_empleado),
            FOREIGN KEY(activo_id) REFERENCES inventario_activos(id)
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
    
    conn.commit()
    conn.close()

# --- 2. LOS CADENEROS ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def requiere_rol(rol_necesario):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('login'))
            if session.get('rol') != rol_necesario and session.get('rol') != 'admin':
                abort(403) 
            return f(*args, **kwargs)
        return decorated_function  
    return decorator 

# --- 3. RUTAS ---

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

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
            return redirect(url_for('dashboard'))
        else:
            flash('Datos incorrectos', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')

@app.route('/empleados')
@login_required
def empleados():
    conn = get_db_connection()
    lista_empleados = conn.execute('SELECT * FROM usuarios ORDER BY numero_empleado ASC').fetchall()
    conn.close()
    return render_template('empleados.html', empleados=lista_empleados)

@app.route('/crear_empleado', methods=['POST'])
@login_required
@requiere_rol('admin')
def crear_empleado():
    num_emp = request.form.get('numero_empleado')
    nombre = request.form.get('nombre_completo')
    user = request.form.get('username')
    passw = request.form.get('password')
    rol = request.form.get('rol')
    hash_pass = generate_password_hash(passw)
    conn = get_db_connection()
    try:
        conn.execute('INSERT INTO usuarios (numero_empleado, nombre_completo, username, password_hash, rol) VALUES (?, ?, ?, ?, ?)', (num_emp, nombre, user, hash_pass, rol))
        conn.commit()
    except: flash('Error al crear usuario', 'error')
    finally: conn.close()
    return redirect(url_for('empleados'))

@app.route('/recetas')
@login_required
def recetas():
    conn = get_db_connection()
    recetas = conn.execute('SELECT * FROM recetas ORDER BY id DESC').fetchall()
    conn.close()
    return render_template('recetas.html', recetas=recetas)

@app.route('/crear_receta', methods=['POST'])
@login_required
@requiere_rol('admin')
def crear_receta():
    nombre = request.form.get('nombre')
    descripcion = request.form.get('descripcion')
    creado_por = session.get('nombre')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT INTO recetas (nombre, descripcion, creado_por) VALUES (?, ?, ?)', (nombre, descripcion, creado_por))
    receta_id = cursor.lastrowid 
    paso_num = 1
    while True:
        instruccion = request.form.get(f'paso_{paso_num}_instruccion')
        if not instruccion: break 
        minutos = request.form.get(f'paso_{paso_num}_minutos') or 0
        tiene_tiempo = 1 if int(minutos) > 0 else 0
        imagen = request.files.get(f'paso_{paso_num}_imagen')
        ruta_imagen = ""
        if imagen and imagen.filename:
            filename = secure_filename(imagen.filename)
            nuevo_nombre = f"receta_{receta_id}_paso_{paso_num}_{filename}"
            imagen.save(os.path.join(app.config['UPLOAD_FOLDER'], nuevo_nombre))
            ruta_imagen = f"img_recetas/{nuevo_nombre}" 
        cursor.execute('INSERT INTO recetas_pasos (receta_id, numero_paso, instruccion, tiene_tiempo, minutos, imagen_ruta) VALUES (?, ?, ?, ?, ?, ?)', (receta_id, paso_num, instruccion, tiene_tiempo, int(minutos), ruta_imagen))
        paso_num += 1
    conn.commit()
    conn.close()
    return redirect(url_for('recetas'))

@app.route('/reportes')
@login_required
@requiere_rol('rh')
def reportes():
    conn = get_db_connection()
    usuarios = conn.execute("SELECT * FROM usuarios WHERE rol != 'admin'").fetchall()
    reportes_asistencia = []
    for u in usuarios:
        num_emp = u['numero_empleado']
        registros = conn.execute('SELECT date(timestamp) as fecha, min(timestamp) as entrada, max(timestamp) as salida FROM asistencias WHERE numero_empleado = ? GROUP BY date(timestamp) ORDER BY fecha DESC LIMIT 7', (num_emp,)).fetchall()
        dias = len(registros)
        hrs_reg = 0; hrs_ext = 0
        for r in registros:
            if r['entrada'] != r['salida']:
                try:
                    ent = datetime.strptime(r['entrada'], '%Y-%m-%d %H:%M:%S')
                    sal = datetime.strptime(r['salida'], '%Y-%m-%d %H:%M:%S')
                    diff = (sal - ent).total_seconds() / 3600.0
                    if diff > 9: hrs_reg += 9; hrs_ext += (diff - 9)
                    else: hrs_reg += diff
                except: pass
        if dias > 0:
            nombre = u['nombre_completo'].split()
            ini = (nombre[0][0] + nombre[1][0]).upper() if len(nombre) > 1 else nombre[0][0:2].upper()
            reportes_asistencia.append({'numero_empleado': num_emp, 'nombre_completo': u['nombre_completo'], 'iniciales': ini, 'dias_trabajados': dias, 'horas_regulares': round(hrs_reg, 1), 'horas_extras': round(hrs_ext, 1)})
    conn.close()
    return render_template('reportes.html', reportes_asistencia=reportes_asistencia)

@app.route('/gasolina')
@login_required
def gasolina():
    conn = get_db_connection()
    registros = conn.execute('SELECT * FROM bitacora_gasolina ORDER BY id DESC').fetchall()
    conn.close()
    return render_template('gasolina.html', registros=registros)

@app.route('/activos')
@login_required
@requiere_rol('rh')
def activos():
    conn = get_db_connection()
    inventario = conn.execute('SELECT * FROM inventario_activos').fetchall()
    asignaciones = conn.execute('''
        SELECT a.id, u.nombre_completo, i.categoria, i.descripcion, a.fecha_prestamo, a.ruta_contrato
        FROM asignaciones_activos a
        JOIN usuarios u ON a.numero_empleado = u.numero_empleado
        JOIN inventario_activos i ON a.activo_id = i.id
        WHERE a.estatus = 'Activo'
    ''').fetchall()
    empleados = conn.execute('SELECT * FROM usuarios').fetchall()
    conn.close()
    return render_template('activos.html', inventario=inventario, asignaciones=asignaciones, empleados=empleados)

@app.route('/documentos')
@login_required
def documentos():
    return render_template('documentos.html')

@app.route('/configuracion')
@login_required
@requiere_rol('admin')
def configuracion():
    return render_template('configuracion.html')

if __name__ == '__main__':
    inicializar_bd()
    app.run(debug=True, host='0.0.0.0', port=5000)