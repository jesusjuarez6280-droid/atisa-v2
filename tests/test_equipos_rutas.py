"""Pantallas del módulo Equipos: alta, calibración con certificado, dictamen con firma crítica,
verificación del día y falla en la tablet."""
import io
import itertools
from datetime import date

import pytest

from fabricas import PASSWORD, conexion, crear_usuario, login
from modulos.equipos.servicios import calibraciones, catalogo

PIN = '2580'
_n = itertools.count(1)


@pytest.fixture
def gente(db):
    n = next(_n)
    return {
        'tecnico': (f'eqr_tec{n}', crear_usuario(db, f'eqr_tec{n}', rol='mantenimiento', pin=PIN)),
        'jefa': (f'eqr_jefa{n}', crear_usuario(db, f'eqr_jefa{n}', rol='calidad', facultades='dictaminar_equipos',
                                               pin=PIN)),
        'operador': (f'eqr_op{n}', crear_usuario(db, f'eqr_op{n}', rol='operador', pin=PIN)),
    }


def _entrar(client, persona):
    client.get('/logout')
    login(client, persona[0], PASSWORD)


def _firma(persona):
    return {'firma_numero': persona[1], 'firma_pin': PIN, 'firma_significado': 'Realizó'}


def _equipo(codigo):
    c = conexion()
    fila = c.execute('SELECT * FROM equipos WHERE codigo = ?', (codigo,)).fetchone()
    c.close()
    return fila


def _alta(client, codigo):
    datos = {'codigo': codigo, 'nombre': 'Báscula de piso', 'tipo': 'Báscula', 'area': 'Producción',
             'es_instrumento': '1', 'verificacion_diaria': '1', 'criticidad': 'Alta',
             'cal_frecuencia': '12', 'cal_modalidad': 'Externa', 'cal_dias_aviso': '30',
             'man_frecuencia': '6', 'man_tareas': 'Limpiar\nRevisar cable',
             'pc_nombre': ['Carga alta', ''], 'pc_nominal': ['20', ''], 'pc_tolerancia': ['0.05', ''],
             'pc_unidad': ['kg', ''],
             'pv_nombre': ['Carga alta'], 'pv_nominal': ['20'], 'pv_tolerancia': ['0.05'], 'pv_minimo': [''],
             'pv_maximo': [''], 'pv_unidad': ['kg'], 'pv_requiere_pesa': ['0']}
    return client.post('/equipos/nuevo', data=datos)


def test_mantenimiento_arranca_en_equipos_y_rh_no_entra(client, gente, db):
    _entrar(client, gente['tecnico'])
    assert client.get('/').headers['Location'].endswith('/equipos')
    rh = crear_usuario(db, f'eqr_rh{next(_n)}', rol='rh')
    client.get('/logout')
    login(client, db.execute('SELECT username FROM usuarios WHERE numero_empleado = ?', (rh,)).fetchone()[0], PASSWORD)
    assert client.get('/equipos').status_code == 302


def test_alta_calibracion_con_certificado_y_dictamen_con_firma_critica(client, gente):
    codigo = f'BAS-R{next(_n):03d}'
    _entrar(client, gente['tecnico'])
    r = _alta(client, codigo)
    e = _equipo(codigo)
    assert r.status_code == 302 and r.headers['Location'].endswith(f'/equipos/{e["id"]}')
    assert e['estatus'] == 'FUERA_DE_SERVICIO' and e['vence_mantenimiento']
    html = client.get(f'/equipos/{e["id"]}').get_data(as_text=True)
    assert codigo in html and 'Por calibrar' in html

    c = conexion()
    lab = calibraciones.crear_laboratorio(c, nombre=f'Lab rutas {codigo}', usuario='p')
    c.execute("UPDATE proveedores SET estatus = 'Aprobado' WHERE id = ?", (lab,))
    c.commit()
    c.close()
    datos = {'fecha': date.today().isoformat(), 'modalidad': 'Externa', 'proveedor_id': str(lab), 'certificado': 'CERT-77',
             'p_nominal': ['20'], 'p_tolerancia': ['0.05'], 'p_unidad': ['kg'], 'p_encontrado': ['20.01'],
             'p_dejado': [''], 'p_incertidumbre': [''], **_firma(gente['tecnico']),
             'certificado_pdf': (io.BytesIO(b'%PDF-1.4 certificado'), 'certificado.pdf')}
    r = client.post(f'/equipos/{e["id"]}/calibracion/nueva', data=datos, content_type='multipart/form-data')
    c = conexion()
    cal = c.execute('SELECT * FROM eq_calibraciones WHERE equipo_id = ?', (e['id'],)).fetchone()
    pdf = c.execute("SELECT * FROM archivos WHERE entidad = 'eq_calibraciones' AND entidad_id = ?", (cal['id'],)).fetchone()
    c.close()
    assert r.headers['Location'].endswith(f'/equipos/calibracion/{cal["id"]}')
    assert cal['estatus'] == 'POR_DICTAMINAR' and pdf is not None

    # El técnico no tiene la facultad: ni entra al dictamen
    r = client.post(f'/equipos/calibracion/{cal["id"]}/dictaminar', data={'dictamen': 'APTO', **_firma(gente['tecnico'])})
    assert r.status_code == 302
    # La jefa con su sesión no puede firmar con el número de otra persona (firma crítica)
    _entrar(client, gente['jefa'])
    client.post(f'/equipos/calibracion/{cal["id"]}/dictaminar', data={'dictamen': 'APTO', **_firma(gente['tecnico'])})
    assert _equipo(codigo)['estatus'] == 'FUERA_DE_SERVICIO'
    client.post(f'/equipos/calibracion/{cal["id"]}/dictaminar', data={'dictamen': 'APTO', **_firma(gente['jefa'])})
    e = _equipo(codigo)
    assert e['estatus'] == 'EN_SERVICIO' and e['vence_calibracion']
    html = client.get(f'/equipos/calibracion/{cal["id"]}').get_data(as_text=True)
    assert 'Apto' in html and 'CERT-77' in html


def test_verificacion_del_dia_y_falla_en_la_tablet(client, gente, db):
    codigo = f'BAS-R{next(_n):03d}'
    _entrar(client, gente['tecnico'])
    _alta(client, codigo)
    e = _equipo(codigo)
    c = conexion()
    # Arranque con la calibración ya vigente (como se captura la última de cada instrumento)
    from fabricas import firma
    lab = calibraciones.crear_laboratorio(c, nombre=f'Lab planta {codigo}', usuario='p')
    cal = calibraciones.registrar(c, e['id'], fecha=date.today().isoformat(), modalidad='Externa', proveedor_id=lab,
                                  certificado='X', con_certificado_pdf=True, usuario='p',
                                  firma_id=firma(c, gente['tecnico'][1], PIN, 'Realizó'),
                                  puntos=[{'nominal': 20, 'tolerancia': 0.05, 'unidad': 'kg', 'encontrado': 20}])
    calibraciones.dictaminar(c, cal['id'], es_apto=True, usuario='p',
                             firma_id=firma(c, gente['jefa'][1], PIN, 'Aprobó', facultad='dictaminar_equipos'))
    c.commit()
    punto = catalogo.puntos(c, e['id'], 'VERIFICACION')[0]['id']
    c.close()

    _entrar(client, gente['operador'])
    assert 'Verificaciones del día' in client.get('/planta').get_data(as_text=True)
    assert codigo in client.get('/equipos/planta/verificar').get_data(as_text=True)
    html = client.get(f'/equipos/planta/verificar/{e["id"]}').get_data(as_text=True)
    assert 'Carga alta' in html and 'csrf_token' in html
    r = client.post(f'/equipos/planta/verificar/{e["id"]}',
                    data={'nivelada_limpia': '1', f'lectura_{punto}': '20.02', **_firma(gente['operador'])})
    assert r.headers['Location'].endswith('/equipos/planta/verificar')
    assert _equipo(codigo)['ultima_verificacion'] == date.today().isoformat()
    assert f'>{codigo}<' not in client.get('/equipos/planta/verificar').get_data(as_text=True)

    r = client.post('/equipos/planta/falla', data={'equipo_id': str(e['id']), 'descripcion': 'Pantalla apagada',
                                                   'se_puede_usar': '0', **_firma(gente['operador'])})
    assert r.headers['Location'].endswith('/planta')
    assert _equipo(codigo)['estatus'] == 'FUERA_DE_SERVICIO'
    ot = db.execute("SELECT * FROM eq_ordenes_trabajo WHERE equipo_id = ? AND tipo = 'CORRECTIVO'", (e['id'],)).fetchone()
    assert ot['estatus'] == 'ABIERTA' and ot['descripcion'] == 'Pantalla apagada'
    # El operador no entra a la oficina de equipos
    assert client.get('/equipos').status_code == 302


def test_pantallas_y_exportaciones(client, gente, db):
    codigo = f'BAS-R{next(_n):03d}'
    _entrar(client, gente['tecnico'])
    _alta(client, codigo)
    e = _equipo(codigo)
    client.post('/equipos/falla', data={'equipo_id': str(e['id']), 'descripcion': 'Cable roto', 'se_puede_usar': '1',
                                        **_firma(gente['tecnico'])})
    ot = db.execute('SELECT id FROM eq_ordenes_trabajo WHERE equipo_id = ?', (e['id'],)).fetchone()['id']
    for url in ('/equipos', '/equipos/calibraciones', '/equipos/mantenimiento', '/equipos/mantenimiento?tab=abiertas',
                '/equipos/falla', f'/equipos/programa?anio={date.today().year}', '/equipos/nuevo',
                f'/equipos/{e["id"]}/editar', f'/equipos/{e["id"]}/calibracion/nueva', f'/equipos/{e["id"]}/etiqueta',
                f'/equipos/ot/{ot}', f'/equipos/ot/{ot}?imprimir=1', '/equipos?filtro=fuera') + tuple(
                f'/equipos/{e["id"]}?tab={t}' for t in ('calibracion', 'verificaciones', 'mantenimiento', 'uso', 'historial')):
        r = client.get(url)
        assert r.status_code == 200, url
    assert codigo in client.get('/equipos?filtro=fuera').get_data(as_text=True)
    assert 'Cable roto' in client.get(f'/equipos/ot/{ot}').get_data(as_text=True)
    for url in ('/equipos/exportar', f'/equipos/programa/excel?anio={date.today().year}'):
        r = client.get(url)
        assert r.status_code == 200 and r.data[:2] == b'PK', url
    assert client.get('/equipos/999999').status_code == 404
    assert client.get('/equipos/calibracion/999999').status_code == 404
    assert client.get('/equipos/impacto/999999').status_code == 404
