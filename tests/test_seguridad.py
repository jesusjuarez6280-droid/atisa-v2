import io
import os

import openpyxl
from flask.sessions import SecureCookieSessionInterface
from itsdangerous import URLSafeTimedSerializer

import erp
from conftest import crear_usuario, login


def test_no_hay_llave_de_sesion_fija():
    assert erp.app.secret_key != 'atisa-dev-key-CAMBIAR-en-produccion'
    assert len(erp.app.secret_key) >= 32


def test_cookie_firmada_con_llave_vieja_no_entra(client):
    # Antes, cualquiera que leyera el repo podía firmar {'user_id': 1} y entrar como admin.
    iface = SecureCookieSessionInterface()
    s = URLSafeTimedSerializer('atisa-dev-key-CAMBIAR-en-produccion', salt=iface.salt,
                               serializer=iface.serializer,
                               signer_kwargs={'key_derivation': iface.key_derivation,
                                              'digest_method': iface.digest_method})
    client.set_cookie('session', s.dumps({'user_id': 1, 'rol': 'admin'}))
    r = client.get('/configuracion')
    assert r.status_code == 302 and '/login' in r.headers['Location']


def test_debug_apagado_por_defecto():
    fuente = open(os.path.join(os.path.dirname(erp.__file__), 'erp.py'), encoding='utf-8').read()
    assert "os.environ.get('ATISA_DEBUG', 'false')" in fuente


def test_excel_de_reportes_sin_datos_sensibles(client, db):
    crear_usuario(db, 'rh_excel', 'clave-segura-1', rol='rh', numero='9101')
    crear_usuario(db, 'op_excel', 'clave-segura-2', rol='operador', numero='9102',
                  curp='CURPSECRETA', nss='NSS123', banco_clabe='CLABE999', salario_diario='999')
    login(client, 'rh_excel', 'clave-segura-1')
    r = client.get('/exportar_excel')
    assert r.status_code == 200
    hoja = openpyxl.load_workbook(io.BytesIO(r.data))['Usuarios']
    encabezados = [c.value for c in hoja[1]]
    for prohibida in ('password_hash', 'curp', 'rfc', 'nss', 'banco_clabe', 'salario_diario', 'domicilio'):
        assert prohibida not in encabezados
    contenido = ' '.join(str(c.value) for fila in hoja.iter_rows() for c in fila)
    assert 'CURPSECRETA' not in contenido and 'CLABE999' not in contenido
    assert 'Prueba op_excel' in contenido


def test_archivos_privados_no_se_sirven_por_static(client):
    os.makedirs('static/contratos', exist_ok=True)
    ruta = os.path.join('static', 'contratos', 'zz_prueba_privada.pdf')
    with open(ruta, 'wb') as fh:
        fh.write(b'%PDF-privado')
    try:
        for url in ('/static/contratos/zz_prueba_privada.pdf',
                    '/static/css/../contratos/zz_prueba_privada.pdf',
                    '/static//contratos/zz_prueba_privada.pdf',
                    '/static/CONTRATOS/zz_prueba_privada.pdf'):
            assert client.get(url).status_code == 404, url
        assert client.get('/static/css/estilos.css').status_code == 200
    finally:
        os.remove(ruta)


def test_admin_con_password_inicial_debe_cambiarla(client, db):
    crear_usuario(db, 'admin_ini', erp.PASSWORD_INICIAL_ADMIN, rol='admin', numero='9201')
    r = login(client, 'admin_ini', erp.PASSWORD_INICIAL_ADMIN)
    assert r.status_code == 302
    r = client.get('/configuracion')
    assert r.status_code == 302 and '/cambiar_password' in r.headers['Location']

    # Rechaza contraseña corta o igual a la inicial
    r = client.post('/cambiar_password', data={'password_actual': erp.PASSWORD_INICIAL_ADMIN,
                                               'password_nueva': 'corta', 'password_confirma': 'corta'})
    assert client.get('/configuracion').status_code == 302

    r = client.post('/cambiar_password', data={'password_actual': erp.PASSWORD_INICIAL_ADMIN,
                                               'password_nueva': 'NuevaClave2026', 'password_confirma': 'NuevaClave2026'})
    assert r.status_code == 302
    assert client.get('/configuracion').status_code == 200
    fila = db.execute("SELECT debe_cambiar_password FROM usuarios WHERE username='admin_ini'").fetchone()
    assert fila['debe_cambiar_password'] == 0


def test_usuario_normal_no_es_forzado(client, db):
    crear_usuario(db, 'rh_normal', 'clave-segura-3', rol='rh', numero='9301')
    login(client, 'rh_normal', 'clave-segura-3')
    assert client.get('/empleados').status_code == 200


def test_respaldo_diario(tmp_path):
    destino = erp.respaldar_bd(str(tmp_path / 'copia.db'))
    copia = erp.sqlite3.connect(destino)
    assert copia.execute('SELECT COUNT(*) FROM usuarios').fetchone()[0] >= 1
    copia.close()
