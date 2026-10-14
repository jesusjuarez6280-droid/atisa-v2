"""Almacén: pantallas, permisos y firmas desde la app."""
import io
import itertools
from datetime import date, timedelta

import pytest

from fabricas import PASSWORD, conexion, crear_lote, crear_material, crear_usuario, login
from nucleo import inventario
from modulos.almacen import reportes

_n = itertools.count(1)


def _dias(n):
    return (date.today() + timedelta(days=n)).isoformat()


@pytest.fixture(scope='module')
def gente():
    c = conexion()
    g = {
        'almacen': crear_usuario(c, 'alm_pan_almacen', rol='almacen', pin='2580'),
        'jefe': crear_usuario(c, 'alm_pan_jefe', rol='almacen', facultades='autorizar_ajustes', pin='2580'),
        'calidad': crear_usuario(c, 'alm_pan_calidad', rol='calidad', facultades='liberar_lotes', pin='2580'),
        'produccion': crear_usuario(c, 'alm_pan_produccion', rol='produccion'),
        'operador': crear_usuario(c, 'alm_pan_operador', rol='operador'),
    }
    c.close()
    return g


@pytest.fixture
def db_alm():
    c = conexion()
    yield c
    c.close()


def _proveedor(conn):
    pid = inventario.crear_proveedor(conn, nombre=f'Prov pantallas {next(_n)}', usuario='pruebas', estatus='Aprobado')
    conn.commit()
    return pid


def test_recibir_desde_la_pantalla_con_certificado(client, gente, db_alm):
    login(client, 'alm_pan_almacen', PASSWORD)
    mp = crear_material(db_alm, tipo='MP')
    # test_nucleo_base supone que el lote 1 no tiene archivos: que el certificado no caiga en él.
    crear_lote(db_alm, mp, fecha_caducidad=_dias(300))
    datos = {
        'proveedor_id': _proveedor(db_alm), 'documento': 'F-123', 'fecha': date.today().isoformat(),
        'p-0-material_id': mp, 'p-0-lote_proveedor': 'PROV-77', 'p-0-cantidad': '12.5', 'p-0-contenedores': '3',
        'p-0-fecha_caducidad': _dias(500), 'p-0-empaque_integro': '1', 'p-0-identificacion_ok': '1',
        'p-0-trae_certificado': '1', 'p-0-limpio': '1',
        'p-0-certificado': (io.BytesIO(b'%PDF-1.4 certificado'), 'coa.pdf'),
        'firma_numero': gente['almacen'], 'firma_pin': '2580', 'firma_significado': 'Realizó',
    }
    r = client.post('/almacen/recepciones/nueva', data=datos, content_type='multipart/form-data')
    assert r.status_code == 302 and '/almacen/recepciones/' in r.headers['Location']
    lote = db_alm.execute('SELECT * FROM lotes WHERE material_id = ? ORDER BY id DESC', (mp,)).fetchone()
    assert lote['estatus'] == 'CUARENTENA' and lote['existencia'] == 12.5 and lote['lote_proveedor'] == 'PROV-77'
    archivo = db_alm.execute("SELECT * FROM archivos WHERE entidad = 'lotes' AND entidad_id = ?",
                             (lote['id'],)).fetchone()
    assert archivo and archivo['modulo'] == 'almacen'
    detalle = client.get(r.headers['Location']).get_data(as_text=True)
    assert 'PROV-77' in detalle and lote['numero_lote'] in detalle
    etiqueta = client.get(f'/almacen/lote/{lote["id"]}/etiqueta').get_data(as_text=True)
    assert 'CUARENTENA' in etiqueta and '3 de 3' in etiqueta


def test_pin_equivocado_no_registra_y_regresa_al_formulario(client, gente, db_alm):
    login(client, 'alm_pan_almacen', PASSWORD)
    mp = crear_material(db_alm, tipo='MP')
    r = client.post('/almacen/recepciones/nueva', data={
        'proveedor_id': _proveedor(db_alm), 'p-0-material_id': mp, 'p-0-lote_proveedor': 'X', 'p-0-cantidad': '1',
        'p-0-fecha_caducidad': _dias(500), 'firma_numero': gente['almacen'], 'firma_pin': '9999'})
    assert r.status_code == 200 and 'Recibir material' in r.get_data(as_text=True)
    assert db_alm.execute('SELECT COUNT(*) FROM lotes WHERE material_id = ?', (mp,)).fetchone()[0] == 0


def test_imprimir_etiqueta_registra_la_impresion(client, gente, db_alm):
    login(client, 'alm_pan_almacen', PASSWORD)
    lote = crear_lote(db_alm, crear_material(db_alm, tipo='MP'), fecha_caducidad=_dias(300))
    r = client.post(f'/almacen/lote/{lote}/etiqueta', data={'copias': '2', 'formato': 'individual'})
    assert r.status_code == 302 and 'imprimir=1' in r.headers['Location']
    fila = db_alm.execute('SELECT estatus, copias FROM alm_etiquetas WHERE lote_id = ?', (lote,)).fetchone()
    assert tuple(fila) == ('CUARENTENA', 2)


def test_conteo_ciego_no_muestra_la_existencia(client, gente, db_alm):
    login(client, 'alm_pan_almacen', PASSWORD)
    mp = crear_material(db_alm, tipo='MP', requiere_analisis=0)
    lote = crear_lote(db_alm, mp, cantidad=777.25, fecha_caducidad=_dias(300))
    r = client.post('/almacen/conteos/nuevo', data={'alcance': 'materiales', 'material_ids': [str(mp)]})
    url = r.headers['Location']
    conteo_id = int(url.rstrip('/').rsplit('/', 1)[-1])
    html = client.get(url).get_data(as_text=True)
    assert 'contado-%d' % lote in html and '777.25' not in html and '777' not in html
    hoja = client.get(f'/almacen/conteos/{conteo_id}/hoja').get_data(as_text=True)
    assert '777' not in hoja
    client.post(f'{url}/capturar', data={f'contado-{lote}': '770'})
    html = client.get(url).get_data(as_text=True)
    assert '777.25' in html and '-7.25' in html
    # El almacenista sin facultad no puede autorizar los ajustes.
    partida = db_alm.execute('SELECT id FROM alm_conteo_partidas WHERE conteo_id = ?', (conteo_id,)).fetchone()[0]
    client.post(f'{url}/ajustar', data={f'motivo-{partida}': 'Merma', 'firma_numero': gente['almacen'],
                                        'firma_pin': '2580'})
    assert inventario.lote(db_alm, lote)['existencia'] == 777.25
    login(client, 'alm_pan_jefe', PASSWORD)
    client.post(f'{url}/ajustar', data={f'motivo-{partida}': 'Merma', 'firma_numero': gente['jefe'],
                                        'firma_pin': '2580'})
    db_alm.rollback()
    assert inventario.lote(db_alm, lote)['existencia'] == 770


def test_ficha_del_lote_muestra_kardex_historial_traza_y_secciones(client, gente, db_alm):
    login(client, 'alm_pan_calidad', PASSWORD)
    lote = crear_lote(db_alm, crear_material(db_alm, tipo='MP'), fecha_caducidad=_dias(300))
    html = client.get(f'/almacen/lote/{lote}').get_data(as_text=True)
    for texto in ('Kárdex', 'Entrada recepcion', 'Historial', 'El material requiere análisis', 'Trazabilidad',
                  f'<p>Lote {lote}</p>'):   # la última la pinta el módulo de prueba por el gancho secciones_lote
        assert texto in html, texto
    # Calidad corrige la caducidad con su firma; queda en el historial.
    client.post(f'/almacen/lote/{lote}/corregir', data={'campo': 'fecha_caducidad', 'valor': _dias(400),
                                                       'motivo': 'Reanálisis', 'firma_numero': gente['calidad'],
                                                       'firma_pin': '2580'})
    assert inventario.lote(db_alm, lote)['fecha_caducidad'] == _dias(400)
    # Calidad no cambia ubicaciones (eso es del almacén).
    client.post(f'/almacen/lote/{lote}/corregir', data={'campo': 'ubicacion', 'valor': 'MP-A1', 'motivo': 'x'})
    assert inventario.lote(db_alm, lote)['ubicacion'] != 'MP-A1'


def test_permisos_operador_y_produccion(client, gente, db_alm):
    login(client, 'alm_pan_operador', PASSWORD)
    assert client.get('/almacen').status_code == 302
    login(client, 'alm_pan_produccion', PASSWORD)
    assert client.get('/almacen').status_code == 200
    assert client.get('/almacen/recepciones/nueva').status_code == 302
    mp = crear_material(db_alm, tipo='MP')
    client.post('/almacen/recepciones/nueva', data={
        'proveedor_id': _proveedor(db_alm), 'p-0-material_id': mp, 'p-0-lote_proveedor': 'X', 'p-0-cantidad': '1',
        'p-0-fecha_caducidad': _dias(500)})
    assert db_alm.execute('SELECT COUNT(*) FROM lotes WHERE material_id = ?', (mp,)).fetchone()[0] == 0


def test_lote_caducado_aparece_en_rojo_en_el_tablero(client, db_alm):
    codigo = f'MP-CAD-{next(_n)}'
    crear_lote(db_alm, crear_material(db_alm, codigo=codigo, tipo='MP'), fecha_caducidad=_dias(-5))
    crear_usuario(db_alm, 'alm_pan_admin', rol='admin')
    login(client, 'alm_pan_admin', PASSWORD)
    html = client.get('/tablero').get_data(as_text=True)
    assert 'Lote caducado con existencia' in html and codigo in html
    assert 'Caducados con existencia' in html


def test_carga_inicial_desde_excel(client, gente, db_alm):
    import pandas as pd
    login(client, 'alm_pan_almacen', PASSWORD)
    assert client.get('/almacen/carga-inicial/plantilla-existencias').data[:2] == b'PK'
    codigo = f'CI-PAN-{next(_n)}'

    def hoja():
        return reportes._excel([('Existencias', pd.DataFrame([
            {'codigo_material': codigo, 'unidad': 'kg', 'lote': 'L-1', 'cantidad': 3, 'fecha_caducidad': '2030-01-01'}],
            columns=['codigo_material', 'unidad', 'lote', 'lote_proveedor', 'proveedor', 'cantidad', 'fecha_caducidad',
                     'fecha_reanalisis', 'estatus', 'ubicacion']))])
    r = client.post('/almacen/carga-inicial', data={'existencias': (hoja(), 'existencias.xlsx')},
                    content_type='multipart/form-data')
    html = r.get_data(as_text=True)
    assert 'no existe' in html and 'Confirmar carga' not in html
    mats = reportes._excel([('Materiales', pd.DataFrame([
        {'codigo': codigo, 'nombre': 'Material de carga', 'tipo': 'MP', 'unidad': 'kg'}]))])
    r = client.post('/almacen/carga-inicial', data={'existencias': (hoja(), 'existencias.xlsx'),
                                                    'materiales': (mats, 'materiales.xlsx')},
                    content_type='multipart/form-data')
    html = r.get_data(as_text=True)
    assert 'Confirmar carga' in html
    import html as html_mod
    import re
    datos = html_mod.unescape(re.search(r'name="datos" value="([^"]*)"', html).group(1))
    r = client.post('/almacen/carga-inicial/confirmar', data={'datos': datos})
    assert r.status_code == 302
    lote = db_alm.execute('SELECT l.* FROM lotes l JOIN materiales m ON m.id = l.material_id WHERE m.codigo = ?',
                          (codigo,)).fetchone()
    assert lote['numero_lote'] == 'L-1' and lote['origen'] == 'INICIAL' and lote['estatus'] == 'CUARENTENA'


def test_exportaciones_a_excel(client, gente):
    login(client, 'alm_pan_calidad', PASSWORD)
    for url in ('/almacen/exportar', '/almacen/exportar?r=por_caducar', '/almacen/exportar?r=cuarentena'):
        r = client.get(url)
        assert r.status_code == 200 and r.data[:2] == b'PK', url
