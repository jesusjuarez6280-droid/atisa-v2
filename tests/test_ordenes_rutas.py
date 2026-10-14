"""Pantallas del módulo Órdenes: abren con datos reales, 404 si el id no existe y permisos por rol."""
import pytest

from fabricas import PASSWORD, conexion, crear_usuario, login
from modulos.ordenes.servicios import ordenes as srv_ordenes
from test_ordenes_flujo import Planta


@pytest.fixture
def orden_en_proceso():
    conn = conexion()
    planta = Planta(conn)
    oid = planta.orden()
    planta.despeje(oid)
    planta.pesar(oid)
    conn.commit()
    folio = srv_ordenes.orden(conn, oid)['folio']
    conn.close()
    return oid, folio


def _entrar(client, rol):
    conn = conexion()
    crear_usuario(conn, f'rutas_op_{rol}', rol=rol, numero=f'77{len(rol):02d}{rol[:3]}')
    conn.close()
    login(client, f'rutas_op_{rol}', PASSWORD)


def test_pantallas_con_la_orden(client, orden_en_proceso):
    oid, folio = orden_en_proceso
    _entrar(client, 'produccion')
    for url in ('/ordenes', f'/ordenes/{oid}', f'/ordenes/{oid}/expediente', f'/ordenes/planta/{oid}'):
        r = client.get(url)
        assert r.status_code == 200, url
        assert folio in r.get_data(as_text=True), url


def test_id_inexistente_es_404(client):
    _entrar(client, 'produccion')
    for url in ('/ordenes/999999', '/ordenes/999999/expediente', '/ordenes/planta/999999',
                '/ordenes/formulas/999999'):
        assert client.get(url).status_code == 404, url


def test_operador_no_entra_a_oficina(client, orden_en_proceso):
    oid, _ = orden_en_proceso
    _entrar(client, 'operador')
    assert client.get(f'/ordenes/planta/{oid}').status_code == 200
    assert client.get('/ordenes/formulas').status_code != 200
    assert client.get(f'/ordenes/{oid}').status_code != 200
