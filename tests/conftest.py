import os
import shutil
import sys
import tempfile

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

# Base temporal: las pruebas NUNCA tocan base_planta.db
_TMP = tempfile.mkdtemp(prefix='atisa_test_')
os.environ['ATISA_DB'] = os.path.join(_TMP, 'prueba.db')
os.environ['ATISA_RESPALDOS'] = os.path.join(_TMP, 'respaldos')
os.environ['ATISA_SIN_RESPALDO'] = '1'
os.environ['ATISA_PRIVADO'] = os.path.join(_TMP, 'privado')
# Módulo de prueba (tests/modulo_prueba): ejercita el registro de módulos de la base.
os.environ['ATISA_MODULOS_EXTRA'] = 'modulo_prueba'
os.environ.pop('ATISA_SECRET_KEY', None)

import erp  # noqa: E402

erp.inicializar_bd()


@pytest.fixture
def app():
    erp.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    erp._LOGIN_INTENTOS.clear()
    yield erp.app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db():
    conn = erp.sqlite3.connect(erp.DB_PATH)
    conn.row_factory = erp.sqlite3.Row
    yield conn
    conn.close()


def crear_usuario(db, username, password, rol='rh', numero='9000', debe_cambiar=0, **extra):
    cols = {'numero_empleado': numero, 'nombre_completo': f'Prueba {username}', 'username': username,
            'password_hash': erp.generate_password_hash(password), 'rol': rol,
            'debe_cambiar_password': debe_cambiar, **extra}
    db.execute('DELETE FROM usuarios WHERE username = ?', (username,))
    db.execute(f"INSERT INTO usuarios ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
               tuple(cols.values()))
    db.commit()


def login(client, username, password):
    return client.post('/login', data={'username': username, 'password': password})


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP, ignore_errors=True)
