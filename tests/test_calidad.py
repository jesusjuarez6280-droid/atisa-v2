import erp
from conftest import crear_usuario, login


def test_bitacora_registra_que_se_cambio_sin_contrasenas(client, db):
    crear_usuario(db, 'admin_bit', 'clave-bit-1', rol='admin', numero='6001')
    login(client, 'admin_bit', 'clave-bit-1')
    client.post('/avisos/crear', data={'aviso': 'Revisar báscula 3', 'responsable': 'Calidad',
                                      'password': 'no-debe-verse', 'csrf_token': 'x'})
    fila = db.execute("SELECT * FROM auditoria WHERE ruta='/avisos/crear' ORDER BY id DESC LIMIT 1").fetchone()
    assert fila is not None
    assert 'aviso=Revisar báscula 3' in fila['detalle']
    assert 'no-debe-verse' not in fila['detalle'] and 'csrf' not in fila['detalle']


def test_detalle_se_recorta(app):
    with app.test_request_context('/x', method='POST', data={'nota': 'a' * 500, 'otro': 'b'}):
        d = erp._detalle_formulario()
    assert len(d) <= 400 and 'otro=b' in d


def test_error_inesperado_muestra_pagina_amable_y_queda_en_log(app, client, db, caplog):
    crear_usuario(db, 'admin_err', 'clave-err-1', rol='admin', numero='6002')
    login(client, 'admin_err', 'clave-err-1')
    original = erp.app.view_functions['avisos']

    def rota():
        raise RuntimeError('falla de prueba')
    erp.app.view_functions['avisos'] = rota
    app.config['TESTING'] = False
    app.config['PROPAGATE_EXCEPTIONS'] = False
    try:
        r = client.get('/avisos')
    finally:
        erp.app.view_functions['avisos'] = original
        app.config['TESTING'] = True
        app.config['PROPAGATE_EXCEPTIONS'] = None
    assert r.status_code == 500
    assert 'Algo salió mal' in r.get_data(as_text=True)
    assert 'falla de prueba' in caplog.text
