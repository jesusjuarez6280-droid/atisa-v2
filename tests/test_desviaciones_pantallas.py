"""Desviaciones y CAPA: pantallas, firmas desde formulario y permisos."""
import io
import itertools
from datetime import date, timedelta

import pytest

from fabricas import PASSWORD, crear_lote, crear_usuario, login
from modulos.desviaciones import servicios as sv

PIN = '2580'
_n = itertools.count(1)


def _hoy(dias=0):
    return (date.today() + timedelta(days=dias)).isoformat()


@pytest.fixture
def gente(db):
    n = next(_n)
    return {
        'jefe': (f'dvp_jefe{n}', crear_usuario(db, f'dvp_jefe{n}', rol='calidad', facultades='gestionar_desviaciones',
                                              pin=PIN)),
        'analista': (f'dvp_ana{n}', crear_usuario(db, f'dvp_ana{n}', rol='calidad', pin=PIN)),
        'prod': (f'dvp_prod{n}', crear_usuario(db, f'dvp_prod{n}', rol='produccion', pin=PIN)),
        'oper': (f'dvp_oper{n}', crear_usuario(db, f'dvp_oper{n}', rol='operador', pin=PIN)),
        'rh': (f'dvp_rh{n}', crear_usuario(db, f'dvp_rh{n}', rol='rh', pin=PIN)),
        'area': f'Área pantallas {n}',
    }


def _entrar(client, gente, quien):
    login(client, gente[quien][0], PASSWORD)
    return gente[quien][1]


def _firma(numero):
    return {'firma_numero': numero, 'firma_pin': PIN}


def _ultima(db):
    return dict(db.execute('SELECT * FROM dev_desviaciones ORDER BY id DESC LIMIT 1').fetchone())


def test_reportar_en_oficina_con_firma_y_archivo(client, db, gente):
    numero = _entrar(client, gente, 'prod')
    lid = crear_lote(db, cantidad=3, folio='OP-PANT-1')
    html = client.get(f'/desviaciones/nueva?origen=orden&origen_id=9&folio=OP-PANT-1&lotes={lid}&texto=OP-PANT-1&volver=/recetas'
                      ).get_data(as_text=True)
    assert 'OP-PANT-1' in html and f'value="{lid}" checked' in html
    datos = {'tipo': 'DESVIACION', 'descripcion': 'Se derramó el granel', 'area': gente['area'], 'fecha_detecto': _hoy(),
             'origen_tipo': 'orden', 'origen_id': '9', 'origen_texto': 'OP-PANT-1', 'origen_url': '/recetas',
             'documento_folio': 'OP-PANT-1', 'lote_id': str(lid), **_firma(numero),
             'archivos': (io.BytesIO(b'\x89PNG foto'), 'foto.png')}
    r = client.post('/desviaciones/nueva', data=datos, content_type='multipart/form-data')
    d = _ultima(db)
    assert r.status_code == 302 and r.headers['Location'].endswith(f"/desviaciones/{d['id']}")
    assert d['folio'].startswith('DES-') and d['origen_modulo'] == 'ordenes' and d['documento_folio'] == 'OP-PANT-1'
    assert db.execute("SELECT COUNT(*) FROM archivos WHERE entidad = 'dev_desviaciones' AND entidad_id = ?",
                      (d['id'],)).fetchone()[0] == 1
    f = db.execute('SELECT * FROM firmas WHERE id = ?', (d['reporto_firma_id'],)).fetchone()
    assert f['numero_empleado'] == numero and f['entidad_id'] == d['id']
    assert client.get(f"/desviaciones/{d['id']}").status_code == 200


def test_pin_equivocado_no_guarda_y_conserva_lo_capturado(client, db, gente):
    numero = _entrar(client, gente, 'prod')
    antes = db.execute('SELECT COUNT(*) FROM dev_desviaciones').fetchone()[0]
    r = client.post('/desviaciones/nueva', data={'tipo': 'DESVIACION', 'descripcion': 'Texto que no se pierde',
                                                 'area': gente['area'], 'fecha_detecto': _hoy(),
                                                 'firma_numero': numero, 'firma_pin': '9999'})
    assert r.status_code == 200 and 'Texto que no se pierde' in r.get_data(as_text=True)
    assert db.execute('SELECT COUNT(*) FROM dev_desviaciones').fetchone()[0] == antes


def test_tablet_firma_con_numero_y_pin_y_regresa(client, db, gente):
    _entrar(client, gente, 'oper')
    assert client.get('/desviaciones').status_code == 302          # el operador solo reporta
    assert 'Reportar desviación' in client.get('/planta').get_data(as_text=True)
    assert client.get('/desviaciones/planta/nueva?volver=/planta').status_code == 200
    r = client.post('/desviaciones/planta/nueva', data={'descripcion': 'Fuga en la marmita', 'area': gente['area'],
                                                        'volver': '/planta', **_firma(gente['oper'][1]),
                                                        'foto': (io.BytesIO(b'\xff\xd8 jpg'), 'f.jpg')},
                    content_type='multipart/form-data')
    assert r.status_code == 302 and r.headers['Location'].endswith('/planta')
    d = _ultima(db)
    assert d['descripcion'] == 'Fuga en la marmita' and d['fecha_detecto'] == _hoy()
    assert db.execute('SELECT numero_empleado FROM firmas WHERE id = ?', (d['reporto_firma_id'],)).fetchone()[0] == gente['oper'][1]


def test_rh_no_reporta_en_tablet(client, gente):
    _entrar(client, gente, 'rh')
    assert client.get('/desviaciones').status_code == 200
    assert client.get('/desviaciones/planta/nueva').status_code == 302


def test_sin_facultad_no_clasifica(client, db, gente):
    numero = _entrar(client, gente, 'prod')
    client.post('/desviaciones/nueva', data={'tipo': 'DESVIACION', 'descripcion': 'x', 'area': gente['area'],
                                             'fecha_detecto': _hoy(), **_firma(numero)})
    did = _ultima(db)['id']
    _entrar(client, gente, 'analista')
    client.post(f'/desviaciones/{did}/clasificar', data={'clasificacion': 'MAYOR', 'responsable': numero,
                                                         **_firma(gente['analista'][1])})
    assert sv.desviacion(db, did)['estatus'] == 'REPORTADA'


def test_flujo_completo_por_pantallas(client, db, gente):
    prod = _entrar(client, gente, 'prod')
    client.post('/desviaciones/nueva', data={'tipo': 'QUEJA', 'descripcion': 'Frascos con sedimento', 'cliente': 'Granja',
                                             'area': gente['area'], 'fecha_detecto': _hoy(), **_firma(prod)})
    did = _ultima(db)['id']
    url = f'/desviaciones/{did}'

    jefe = _entrar(client, gente, 'jefe')
    assert 'Clasificar y asignar' in client.get(url).get_data(as_text=True)
    client.post(f'{url}/clasificar', data={'clasificacion': 'MAYOR', 'responsable': prod, **_firma(jefe)})
    assert sv.desviacion(db, did)['estatus'] == 'EN_INVESTIGACION'

    _entrar(client, gente, 'prod')
    client.post(f'{url}/investigacion', data={'causa_raiz': 'Filtro roto', 'categoria_causa': 'Máquina',
                                              'porques': 'uno\ndos', 'm_maquina': 'Se revisó el filtro'})
    client.post(f'{url}/acciones', data={'tipo': 'CORRECTIVA', 'descripcion': 'Cambiar el filtro', 'responsable': prod,
                                         'fecha_compromiso': _hoy(5), 'eficacia_criterio': 'Sin sedimento en 3 lotes'})
    html = client.get(url).get_data(as_text=True)
    assert 'Cambiar el filtro' in html and 'Enviar a aprobación' in html
    client.post(f'{url}/enviar', data=_firma(prod))
    assert sv.desviacion(db, did)['estatus'] == 'POR_APROBAR'

    _entrar(client, gente, 'jefe')
    client.post(f'{url}/aprobar', data=_firma(jefe))
    assert sv.desviacion(db, did)['estatus'] == 'EN_ACCIONES'
    aid = db.execute('SELECT id FROM dev_acciones WHERE desviacion_id = ?', (did,)).fetchone()[0]
    assert sv.accion(db, aid)['eficacia_fecha'] == sv.sumar_dias(_hoy(5), 90)

    _entrar(client, gente, 'prod')
    assert 'Cambiar el filtro' in client.get('/desviaciones/acciones').get_data(as_text=True)
    client.post(f'/desviaciones/acciones/{aid}/hecha', data={'como_se_hizo': 'Filtro nuevo', **_firma(prod),
                                                             'evidencia': (io.BytesIO(b'%PDF-1.4 x'), 'ev.pdf')},
                content_type='multipart/form-data')
    assert sv.accion(db, aid)['estatus'] == 'HECHA'

    ana = _entrar(client, gente, 'analista')
    assert 'Filtro nuevo' in client.get('/desviaciones/acciones').get_data(as_text=True)
    client.post(f'/desviaciones/acciones/{aid}/verificar', data=_firma(ana))
    assert sv.accion(db, aid)['estatus'] == 'VERIFICADA'

    _entrar(client, gente, 'jefe')
    assert 'respuesta al cliente' in client.get(url).get_data(as_text=True)
    client.post(f'{url}/respuesta', data={'respuesta_cliente': 'Se repuso', 'fecha_respuesta': _hoy()})
    client.post(f'{url}/cerrar', data={'conclusion': 'Filtro cambiado', **_firma(jefe)})
    assert sv.desviacion(db, did)['estatus'] == 'CERRADA'
    for ruta in (url, f'{url}/imprimir', '/desviaciones?pestana=cerradas', '/desviaciones/tendencias'):
        assert client.get(ruta).status_code == 200, ruta
    assert 'Filtro cambiado' in client.get(f'{url}/imprimir').get_data(as_text=True)


def test_excel_de_lista_tendencias_y_alcance(client, db, gente):
    numero = _entrar(client, gente, 'prod')
    client.post('/desviaciones/nueva', data={'tipo': 'DESVIACION', 'descripcion': 'x', 'area': gente['area'],
                                             'fecha_detecto': _hoy(), **_firma(numero)})
    did = _ultima(db)['id']
    for ruta in ('/desviaciones/excel', '/desviaciones/tendencias/excel', f'/desviaciones/{did}/alcance.xlsx'):
        r = client.get(ruta)
        assert r.status_code == 200 and r.data[:2] == b'PK', ruta


def test_id_inexistente_da_404(client, gente):
    _entrar(client, gente, 'jefe')
    assert client.get('/desviaciones/999999').status_code == 404
    r = client.post('/desviaciones/999999/comentario', data={'comentario': 'x'})
    assert r.status_code == 302
