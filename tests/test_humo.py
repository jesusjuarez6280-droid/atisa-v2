"""Pruebas de humo: abren TODAS las pantallas con cada rol.

Sirven de red de seguridad cuando se agregan módulos nuevos:
- ninguna pantalla truena (error 500) con ningún rol;
- cada rol entra solo a lo que PERMISOS le permite y lo demás le es negado;
- sin sesión, nada (salvo login) responde;
- toda ruta nueva declara su permiso con @requiere_modulo (o @requiere_rol);
- todas las plantillas compilan.
"""
import os

import pytest

import erp
from conftest import crear_usuario, login

ROLES = ['admin', 'rh', 'operador']
# Rutas que no llevan permiso de módulo, a propósito.
PUBLICAS = {'/', '/login', '/logout'}
SOLO_SESION = {'/sin_acceso', '/cambiar_password'}
# GET que no se recorren: cierran la sesión de la prueba.
NO_RECORRER = {'/logout'}


def _guardas(view):
    """Lee de los decoradores qué permiso exige la vista: ('modulo', x) o ('rol', x)."""
    vistos, f = set(), view
    while f is not None and id(f) not in vistos:
        vistos.add(id(f))
        for nombre, celda in zip(f.__code__.co_freevars, f.__closure__ or ()):
            try:
                valor = celda.cell_contents
            except ValueError:
                continue
            if nombre == 'modulo':
                return ('modulo', valor)
            if nombre == 'rol_necesario':
                return ('rol', valor)
        f = getattr(f, '__wrapped__', None)
    return None


def _permitido(guarda, rol):
    if guarda is None:
        return True
    tipo, valor = guarda
    if tipo == 'modulo':
        return rol in erp.PERMISOS.get(valor, [])
    return rol == valor or rol == 'admin'


def _reglas(metodo):
    for r in erp.app.url_map.iter_rules():
        if r.endpoint != 'static' and metodo in r.methods:
            yield r


@pytest.fixture(scope='module')
def datos():
    """Un registro de cada cosa que las pantallas con parámetro necesitan."""
    conn = erp.sqlite3.connect(erp.DB_PATH)
    conn.row_factory = erp.sqlite3.Row
    crear_usuario(conn, 'humo_emp', 'clave-humo-1', rol='operador', numero='8001',
                  nombre_completo='Empleado Humo', puesto='Operador', area='Producción',
                  fecha_ingreso='2024-01-15')
    for rol in ROLES:
        crear_usuario(conn, f'humo_{rol}', 'clave-humo-1', rol=rol, numero=f'80{ROLES.index(rol) + 10}')

    os.makedirs(erp.app.config['UPLOAD_CONTRATOS'], exist_ok=True)
    os.makedirs(erp.UPLOAD_LEGALES, exist_ok=True)
    pdf_contrato = os.path.join(erp.app.config['UPLOAD_CONTRATOS'], 'zz_humo_contrato.pdf')
    pdf_legal = os.path.join(erp.UPLOAD_LEGALES, 'zz_humo_legal.pdf')
    for ruta in (pdf_contrato, pdf_legal):
        with open(ruta, 'wb') as fh:
            fh.write(b'%PDF-1.4 humo')

    cur = conn.cursor()
    cur.execute("INSERT INTO recetas (nombre, descripcion, creado_por) VALUES ('Receta humo', 'desc', 'admin')")
    receta_id = cur.lastrowid
    cur.execute("INSERT INTO recetas_pasos (receta_id, numero_paso, instruccion, tiene_tiempo, minutos) "
                "VALUES (?, 1, 'Mezclar', 1, 5)", (receta_id,))
    cur.execute("INSERT INTO inventario_activos (categoria, descripcion, estatus) VALUES ('Laptop', 'Equipo humo', 'Asignado')")
    activo_id = cur.lastrowid
    cur.execute("INSERT INTO asignaciones_activos (numero_empleado, activo_id, fecha_prestamo, ruta_contrato, estatus) "
                "VALUES ('8001', ?, '2026-01-01', ?, 'Activo')", (activo_id, pdf_contrato))
    asignacion_id = cur.lastrowid
    cur.execute("INSERT INTO documentos_empleados (numero_empleado, tipo_documento, nombre_archivo, ruta_archivo) "
                "VALUES ('8001', 'INE', 'humo.pdf', ?)", (pdf_contrato,))
    doc_id = cur.lastrowid
    cur.execute("INSERT INTO vacantes (puesto, area, estatus) VALUES ('Vacante humo', 'Calidad', 'Abierta')")
    vacante_id = cur.lastrowid
    cur.execute("INSERT INTO documentos_legales (codigo, nombre, categoria, archivo, estatus) "
                "VALUES ('HUMO-1', 'Doc humo', 'Interna', 'zz_humo_legal.pdf', 'Vigente')")
    legal_id = cur.lastrowid
    puesto_id = conn.execute('SELECT MIN(id) FROM puestos').fetchone()[0]
    curso_id = conn.execute('SELECT MIN(id) FROM cursos').fetchone()[0]
    cur.execute("INSERT INTO capacitacion_registro (numero_empleado, curso_id, estatus, fecha) "
                "VALUES ('8001', ?, 'Tomado', '2026-02-01')", (curso_id,))
    conn.commit()
    conn.close()
    yield {
        'receta_id': receta_id, 'asignacion_id': asignacion_id, 'doc_id': doc_id,
        'vid': vacante_id, 'did': legal_id, 'pid': puesto_id, 'curso_id': curso_id,
        'numero_empleado': '8001', 'nombre_empleado': 'Empleado Humo',
    }
    for ruta in (pdf_contrato, pdf_legal):
        os.remove(ruta)


def _url(regla, datos):
    valores = {a: datos.get(a, 1) for a in regla.arguments}
    return erp.app.url_map.bind('localhost').build(regla.endpoint, valores)


def test_toda_ruta_declara_su_permiso():
    """Si esto falla en un módulo nuevo: agrega @login_required y @requiere_modulo('...')
    a la ruta, y el módulo a PERMISOS."""
    sin_permiso = [r.rule for r in erp.app.url_map.iter_rules()
                   if r.endpoint != 'static' and r.rule not in PUBLICAS | SOLO_SESION
                   and _guardas(erp.app.view_functions[r.endpoint]) is None]
    assert sin_permiso == []


@pytest.mark.parametrize('metodo', ['GET', 'POST'])
def test_sin_sesion_todo_manda_al_login(client, datos, metodo):
    fallas = []
    for regla in _reglas(metodo):
        if regla.rule in PUBLICAS:
            continue
        r = client.open(_url(regla, datos), method=metodo)
        if r.status_code != 302 or '/login' not in r.headers.get('Location', ''):
            fallas.append((regla.rule, r.status_code))
    assert fallas == []


@pytest.mark.parametrize('rol', ROLES)
def test_todas_las_pantallas_abren_con_cada_rol(client, datos, rol):
    login(client, f'humo_{rol}', 'clave-humo-1')
    errores, sin_bloquear, no_abren = [], [], []
    for regla in _reglas('GET'):
        if regla.rule in PUBLICAS | NO_RECORRER:
            continue
        url = _url(regla, datos)
        try:
            r = client.get(url)
        except Exception as e:  # en TESTING las excepciones suben: es un 500
            errores.append((url, repr(e)))
            continue
        if r.status_code >= 500:
            errores.append((url, r.status_code))
            continue
        guarda = _guardas(erp.app.view_functions[regla.endpoint])
        if not _permitido(guarda, rol):
            if r.status_code == 200:
                sin_bloquear.append(url)
        elif not regla.arguments and r.status_code != 200 and regla.rule != '/':
            no_abren.append((url, r.status_code, r.headers.get('Location')))
    assert errores == [], 'Pantallas que truenan'
    assert sin_bloquear == [], f'El rol {rol} entra donde no debe'
    assert no_abren == [], f'El rol {rol} no puede abrir pantallas que sí le tocan'


def test_pantallas_con_datos_muestran_el_registro(client, datos):
    login(client, 'humo_admin', 'clave-humo-1')
    for url, texto in [('/expediente/8001', 'Empleado Humo'), ('/cardex/8001', 'Empleado Humo'),
                       (f"/reclutamiento/{datos['vid']}", 'Vacante humo'),
                       (f"/ejecutar_receta/{datos['receta_id']}", 'Mezclar')]:
        html = client.get(url).get_data(as_text=True)
        assert texto.upper() in html.upper(), url
    for url in (f"/ver_contrato/{datos['asignacion_id']}", f"/ver_documento/{datos['doc_id']}",
                f"/normativa/archivo/{datos['did']}"):
        r = client.get(url)
        assert r.status_code == 200 and r.data.startswith(b'%PDF'), url


def test_todas_las_plantillas_compilan():
    for nombre in erp.app.jinja_env.list_templates():
        if nombre.endswith('.html'):
            erp.app.jinja_env.get_template(nombre)


def test_auditoria_y_configuracion_siguen_siendo_solo_de_admin():
    # Guardián contra abrir por accidente la bitácora o la administración de usuarios.
    assert erp.PERMISOS['auditoria'] == ['admin']
    assert erp.PERMISOS['configuracion'] == ['admin']
