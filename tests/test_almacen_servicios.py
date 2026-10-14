"""Almacén: reglas de negocio (diseño 01-almacen, sección 9)."""
import itertools
from datetime import date, timedelta

import pytest

from fabricas import conexion, crear_lote, crear_material, crear_usuario
from nucleo import firmas, inventario
from nucleo.inventario import ErrorInventario
from modulos.almacen import servicios
from modulos.almacen.servicios import ErrorAlmacen

SESION = {'username': 'pruebas', 'numero_empleado': None, 'ip': None}
_n = itertools.count(1)


def _dias(n):
    return (date.today() + timedelta(days=n)).isoformat()


@pytest.fixture
def conn():
    c = conexion()
    yield c
    c.rollback()
    c.close()


@pytest.fixture(scope='module')
def personas():
    """Almacenista sin facultades, jefa de calidad (liberar_lotes) y gerente (autorizar_ajustes)."""
    c = conexion()
    gente = {
        'almacen': crear_usuario(c, 'alm_srv_almacen', rol='almacen', pin='2580'),
        'calidad': crear_usuario(c, 'alm_srv_calidad', rol='calidad', facultades='liberar_lotes', pin='2580'),
        'ajustes': crear_usuario(c, 'alm_srv_ajustes', rol='almacen', facultades='autorizar_ajustes', pin='2580'),
    }
    c.close()
    return gente


def firmar(conn, numero, significado='Realizó', facultad=None):
    return servicios.firmante(conn, numero=numero, pin='2580', significado=significado, facultad=facultad,
                              sesion=SESION)


def proveedor(conn, estatus='Aprobado'):
    pid = inventario.crear_proveedor(conn, nombre=f'Proveedor alm {next(_n)}', usuario='pruebas', estatus=estatus)
    conn.commit()
    return pid


def partida(material_id, **extra):
    return {'material_id': material_id, 'lote_proveedor': f'LP-{next(_n)}', 'cantidad': 50, 'contenedores': 2,
            'fecha_caducidad': _dias(720), 'trae_certificado': 1, **extra}


def recibir(conn, numero, partidas, estatus_prov='Aprobado'):
    r = servicios.recibir(conn, proveedor_id=proveedor(conn, estatus_prov), partidas=partidas,
                          usuario='pruebas', firmar=firmar(conn, numero))
    conn.commit()
    return r


def liberar(conn, numero, lote_id, nuevo='APROBADO'):
    fid = firmas.firmar(conn, numero_empleado=numero, pin='2580', significado='Liberó', modulo='pruebas',
                        entidad='lotes', entidad_id=lote_id, facultad='liberar_lotes', sesion=SESION)
    inventario.cambiar_estatus(conn, lote_id=lote_id, nuevo=nuevo, usuario='pruebas', motivo='Dictamen',
                               firma_id=fid)
    conn.commit()


# 1. Recepción ------------------------------------------------------------------

def test_recibir_crea_un_lote_por_partida_en_cuarentena(conn, personas):
    mp = crear_material(conn, tipo='MP')
    r = recibir(conn, personas['almacen'], [partida(mp), partida(mp, no_se_recibe='1')])
    assert r['folio'].startswith('REC-')
    assert len(r['lotes']) == 1
    lote = inventario.lote(conn, r['lotes'][0])
    assert lote['estatus'] == 'CUARENTENA'
    assert lote['origen_folio'] == r['folio'] and lote['ubicacion'] == 'CUA-01'
    kardex = conn.execute('SELECT tipo, cantidad, folio FROM lote_movimientos WHERE lote_id = ?',
                          (lote['id'],)).fetchall()
    assert [tuple(k) for k in kardex] == [('ENTRADA_RECEPCION', 50, r['folio'])]
    partidas = conn.execute('SELECT aceptada, lote_id FROM alm_recepcion_partidas WHERE recepcion_id = ? '
                            'ORDER BY id', (r['recepcion_id'],)).fetchall()
    assert [(p['aceptada'], bool(p['lote_id'])) for p in partidas] == [(1, True), (0, False)]
    rec = conn.execute('SELECT firma_id FROM alm_recepciones WHERE id = ?', (r['recepcion_id'],)).fetchone()
    assert firmas.firma(conn, rec['firma_id'])['significado'] == 'Realizó'


def test_material_sin_analisis_nace_aprobado(conn, personas):
    em = crear_material(conn, tipo='EM', requiere_analisis=0)
    r = recibir(conn, personas['almacen'], [partida(em, fecha_caducidad='')])
    assert inventario.lote(conn, r['lotes'][0])['estatus'] == 'APROBADO'


# 2. Candados de la recepción -----------------------------------------------------

def test_proveedor_no_aprobado_bloquea(conn, personas):
    mp = crear_material(conn, tipo='MP')
    with pytest.raises(ErrorAlmacen, match='No aprobado'):
        recibir(conn, personas['almacen'], [partida(mp)], estatus_prov='No aprobado')


def test_proveedor_en_evaluacion_avisa_y_queda_anotado(conn, personas):
    mp = crear_material(conn, tipo='MP')
    r = recibir(conn, personas['almacen'], [partida(mp)], estatus_prov='En evaluación')
    assert any('en evaluación' in a for a in r['avisos'])
    aviso = conn.execute('SELECT aviso_proveedor FROM alm_recepcion_partidas WHERE recepcion_id = ?',
                         (r['recepcion_id'],)).fetchone()[0]
    assert aviso == 'Proveedor en evaluación'


def test_caducidad_vencida_bloquea_y_corta_solo_avisa(conn, personas):
    mp = crear_material(conn, tipo='MP')
    with pytest.raises(ErrorAlmacen, match='ya caducó'):
        recibir(conn, personas['almacen'], [partida(mp, fecha_caducidad=_dias(-1))])
    conn.rollback()
    with pytest.raises(ErrorAlmacen, match='obligatoria'):
        recibir(conn, personas['almacen'], [partida(mp, fecha_caducidad='')])
    conn.rollback()
    r = recibir(conn, personas['almacen'], [partida(mp, fecha_caducidad=_dias(30))])
    assert len(r['lotes']) == 1
    assert any('caduca en 30 días' in a for a in r['avisos'])


def test_un_lote_por_partida(conn, personas):
    mp = crear_material(conn, tipo='MP')
    p = partida(mp)
    with pytest.raises(ErrorAlmacen, match='un lote por partida'):
        recibir(conn, personas['almacen'], [p, dict(p)])


def test_anular_recepcion_solo_sin_movimientos(conn, personas):
    mp = crear_material(conn, tipo='MP', requiere_analisis=0)
    r = recibir(conn, personas['almacen'], [partida(mp)])
    with pytest.raises(firmas.FirmaInvalida):
        servicios.anular_recepcion(conn, r['recepcion_id'], motivo='Error', usuario='pruebas',
                                   firmar=firmar(conn, personas['almacen'], 'Anuló', 'autorizar_ajustes'))
    conn.rollback()
    servicios.anular_recepcion(conn, r['recepcion_id'], motivo='Capturada dos veces', usuario='pruebas',
                               firmar=firmar(conn, personas['ajustes'], 'Anuló', 'autorizar_ajustes'))
    conn.commit()
    assert inventario.lote(conn, r['lotes'][0])['existencia'] == 0
    assert conn.execute('SELECT estatus FROM alm_recepciones WHERE id = ?', (r['recepcion_id'],)).fetchone()[0] \
        == 'Anulada'
    # Una recepción con lotes ya movidos no se anula.
    r2 = recibir(conn, personas['almacen'], [partida(mp)])
    inventario.mover(conn, lote_id=r2['lotes'][0], tipo='MUESTRA', cantidad=1, usuario='pruebas', modulo='pruebas')
    conn.commit()
    with pytest.raises(ErrorAlmacen, match='ya tuvo movimientos'):
        servicios.anular_recepcion(conn, r2['recepcion_id'], motivo='x', usuario='pruebas',
                                   firmar=firmar(conn, personas['ajustes'], 'Anuló', 'autorizar_ajustes'))


# 3. Re-etiquetado ------------------------------------------------------------------

def test_cambio_de_estatus_pide_reetiquetar_hasta_imprimir(conn, personas):
    mp = crear_material(conn, tipo='MP')
    lote_id = recibir(conn, personas['almacen'], [partida(mp)])['lotes'][0]
    pendientes = lambda: {p['id'] for p in servicios.pendientes_reetiquetar(conn)}  # noqa: E731
    assert lote_id not in pendientes()
    liberar(conn, personas['calidad'], lote_id)
    assert lote_id in pendientes()
    servicios.registrar_impresion(conn, lote_id, copias=2, usuario='pruebas')
    conn.commit()
    assert lote_id not in pendientes()


# 4. Venta: primero el que caduca antes ---------------------------------------------

def test_venta_solo_pt_liberado_vigente_en_orden_de_caducidad(conn, personas):
    pt = crear_material(conn, tipo='PT', requiere_analisis=0, unidad='pza')
    tarde = crear_lote(conn, pt, cantidad=10, origen='INICIAL', fecha_caducidad=_dias(400))
    pronto = crear_lote(conn, pt, cantidad=10, origen='INICIAL', fecha_caducidad=_dias(200))
    caducado = crear_lote(conn, pt, cantidad=10, origen='INICIAL', fecha_caducidad=_dias(-3))
    mp = crear_lote(conn, crear_material(conn, tipo='MP', requiere_analisis=0), fecha_caducidad=_dias(300))
    ofrecidos = [l['id'] for l in servicios.lotes_para_salida(conn, 'VENTA') if l['material_id'] == pt]
    assert ofrecidos == [pronto, tarde]
    ids = {l['id'] for l in servicios.lotes_para_salida(conn, 'VENTA')}
    assert caducado not in ids and mp not in ids

    with pytest.raises(ErrorAlmacen, match='no es el que caduca primero'):
        servicios.registrar_salida(conn, tipo='VENTA', destino='Cliente A', usuario='pruebas',
                                   firmar=firmar(conn, personas['almacen']),
                                   partidas=[{'lote_id': tarde, 'cantidad': 2}])
    conn.rollback()
    r = servicios.registrar_salida(conn, tipo='VENTA', destino='Cliente A', usuario='pruebas',
                                   firmar=firmar(conn, personas['almacen']),
                                   partidas=[{'lote_id': tarde, 'cantidad': 2, 'motivo_lote': 'Lo pidió el cliente'}])
    conn.commit()
    assert r['folio'].startswith('SAL-')
    venta = inventario.trazabilidad(conn, tarde)['hacia_adelante']
    assert [(v['relacion'], v['cliente']) for v in venta] == [('VENTA', 'Cliente A')]
    # El sugerido no pide motivo; un lote caducado o una materia prima no se venden.
    servicios.registrar_salida(conn, tipo='VENTA', destino='Cliente B', usuario='pruebas',
                               firmar=firmar(conn, personas['almacen']), partidas=[{'lote_id': pronto, 'cantidad': 1}])
    for lote in (caducado, mp):
        with pytest.raises((ErrorAlmacen, ErrorInventario)):
            servicios.registrar_salida(conn, tipo='VENTA', destino='Cliente C', usuario='pruebas', firmar=None,
                                       partidas=[{'lote_id': lote, 'cantidad': 1, 'motivo_lote': 'x'}])


# 5. Destrucción y devolución al proveedor --------------------------------------------

def test_destruccion_solo_rechazado_o_caducado_y_con_firma(conn, personas):
    mp = crear_material(conn, tipo='MP')
    vigente = crear_lote(conn, mp, fecha_caducidad=_dias(300))
    liberar(conn, personas['calidad'], vigente)
    with pytest.raises(ErrorAlmacen, match='no está rechazado ni caducado'):
        servicios.registrar_salida(conn, tipo='DESTRUCCION', destino='Incineradora', usuario='pruebas',
                                   firmar=firmar(conn, personas['ajustes'], 'Autorizó', 'autorizar_ajustes'),
                                   partidas=[{'lote_id': vigente, 'cantidad': 1}])
    conn.rollback()
    rechazado = crear_lote(conn, mp, fecha_caducidad=_dias(300))
    liberar(conn, personas['calidad'], rechazado, 'RECHAZADO')
    with pytest.raises(firmas.FirmaInvalida):
        servicios.registrar_salida(conn, tipo='DEVOLUCION_PROVEEDOR', destino='Proveedor', usuario='pruebas',
                                   firmar=firmar(conn, personas['almacen'], 'Autorizó', 'autorizar_ajustes'),
                                   partidas=[{'lote_id': rechazado, 'cantidad': 100}])
    conn.rollback()
    with pytest.raises(ErrorInventario, match='autorizar_ajustes'):
        servicios.registrar_salida(conn, tipo='DESTRUCCION', destino='Incineradora', usuario='pruebas', firmar=None,
                                   partidas=[{'lote_id': rechazado, 'cantidad': 100}])
    conn.rollback()
    servicios.registrar_salida(conn, tipo='DESTRUCCION', destino='Incineradora', usuario='pruebas',
                               firmar=firmar(conn, personas['ajustes'], 'Autorizó', 'autorizar_ajustes'),
                               partidas=[{'lote_id': rechazado, 'cantidad': 100}])
    conn.commit()
    assert inventario.lote(conn, rechazado)['existencia'] == 0
    caducado = crear_lote(conn, mp, fecha_caducidad=_dias(-1))
    servicios.registrar_salida(conn, tipo='DEVOLUCION_PROVEEDOR', destino='Proveedor X', usuario='pruebas',
                               firmar=firmar(conn, personas['ajustes'], 'Autorizó', 'autorizar_ajustes'),
                               partidas=[{'lote_id': caducado, 'cantidad': 40}])
    conn.commit()
    assert inventario.lote(conn, caducado)['existencia'] == 60


# 6. Conteo ciego -------------------------------------------------------------------

def test_conteo_genera_ajustes_solo_con_firma_y_marca_recontar(conn, personas):
    mp = crear_material(conn, tipo='MP', requiere_analisis=0)
    a = crear_lote(conn, mp, cantidad=100, fecha_caducidad=_dias(300))
    b = crear_lote(conn, mp, cantidad=50, fecha_caducidad=_dias(300))
    c = servicios.abrir_conteo(conn, usuario='pruebas', material_ids=[mp])
    conn.commit()
    assert c['folio'].startswith('CNT-') and c['lotes'] == 2
    with pytest.raises(ErrorAlmacen, match='no está capturado'):
        servicios.ajustar_conteo(conn, c['conteo_id'], motivos={}, usuario='pruebas',
                                 firmar=firmar(conn, personas['ajustes'], 'Autorizó', 'autorizar_ajustes'))
    faltan = servicios.capturar_conteo(conn, c['conteo_id'], cantidades={a: 97, b: 50}, usuario='pruebas')
    conn.commit()
    assert faltan == 0
    partidas = {p['lote_id']: p for p in servicios.partidas_conteo(conn, c['conteo_id'])}
    assert partidas[a]['diferencia'] == -3 and partidas[b]['diferencia'] == 0
    with pytest.raises(ErrorAlmacen, match='motivo'):
        servicios.ajustar_conteo(conn, c['conteo_id'], motivos={}, usuario='pruebas',
                                 firmar=firmar(conn, personas['ajustes'], 'Autorizó', 'autorizar_ajustes'))
    conn.rollback()
    with pytest.raises(firmas.FirmaInvalida):
        servicios.ajustar_conteo(conn, c['conteo_id'], motivos={partidas[a]['id']: 'Merma'}, usuario='pruebas',
                                 firmar=firmar(conn, personas['almacen'], 'Autorizó', 'autorizar_ajustes'))
    conn.rollback()
    # Un lote que se mueve después de capturarse queda en «recontar».
    inventario.mover(conn, lote_id=b, tipo='MUESTRA', cantidad=1, usuario='pruebas', modulo='pruebas')
    conn.commit()
    assert {p['lote_id'] for p in servicios.partidas_conteo(conn, c['conteo_id']) if p['recontar']} == {b}
    with pytest.raises(ErrorAlmacen, match='vuelve a contarlos'):
        servicios.ajustar_conteo(conn, c['conteo_id'], motivos={partidas[a]['id']: 'Merma'}, usuario='pruebas',
                                 firmar=firmar(conn, personas['ajustes'], 'Autorizó', 'autorizar_ajustes'))
    conn.rollback()
    servicios.capturar_conteo(conn, c['conteo_id'], cantidades={b: 49}, usuario='pruebas')
    n = servicios.ajustar_conteo(conn, c['conteo_id'], motivos={partidas[a]['id']: 'Merma'}, usuario='pruebas',
                                 firmar=firmar(conn, personas['ajustes'], 'Autorizó', 'autorizar_ajustes'))
    conn.commit()
    assert n == 1
    assert inventario.lote(conn, a)['existencia'] == 97
    mov = conn.execute("SELECT tipo, folio, firma_id FROM lote_movimientos WHERE lote_id = ? ORDER BY id DESC",
                       (a,)).fetchone()
    assert mov['tipo'] == 'AJUSTE_MENOS' and mov['folio'] == c['folio'] and mov['firma_id']
    assert conn.execute('SELECT estatus FROM alm_conteos WHERE id = ?', (c['conteo_id'],)).fetchone()[0] == 'Ajustado'


# 7. Devolución de cliente ------------------------------------------------------------

def test_devolucion_crea_lote_aparte_en_cuarentena(conn, personas):
    pt = crear_material(conn, tipo='PT', requiere_analisis=0, unidad='pza')
    vendido = crear_lote(conn, pt, cantidad=20, origen='INICIAL', numero_lote=f'26100{next(_n)}',
                         fecha_caducidad=_dias(300))
    with pytest.raises(ErrorAlmacen, match='no tiene ventas'):
        servicios.registrar_devolucion(conn, cliente='Rancho', lote_vendido_id=vendido, cantidad=1,
                                       motivo='Dañado', usuario='pruebas')
    servicios.registrar_salida(conn, tipo='VENTA', destino='Rancho', usuario='pruebas', firmar=None,
                               partidas=[{'lote_id': vendido, 'cantidad': 5}])
    r = servicios.registrar_devolucion(conn, cliente='Rancho', lote_vendido_id=vendido, cantidad=2,
                                       motivo='Queja', usuario='pruebas')
    conn.commit()
    nuevo = inventario.lote(conn, r['lote_id'])
    padre = inventario.lote(conn, vendido)
    assert nuevo['estatus'] == 'CUARENTENA' and nuevo['origen'] == 'DEVOLUCION'
    assert nuevo['lote_padre_id'] == vendido and nuevo['numero_lote'] == f'{padre["numero_lote"]}-D1'
    assert nuevo['fecha_caducidad'] == padre['fecha_caducidad']
    with pytest.raises(ErrorAlmacen, match='ya se devolvieron'):
        servicios.registrar_devolucion(conn, cliente='Rancho', lote_vendido_id=vendido, cantidad=4,
                                       motivo='Queja', usuario='pruebas')


# 8. Carga inicial ----------------------------------------------------------------------

def test_carga_inicial_reporta_errores_y_exige_firma_para_aprobados(conn, personas):
    n = next(_n)
    materiales = servicios.normalizar_renglones([
        {'codigo': f'CI-MP-{n}', 'nombre': 'Carga MP', 'tipo': 'MP', 'unidad': 'kg', 'stock_minimo': '10'}],
        servicios.COLUMNAS_MATERIALES)
    malas = servicios.normalizar_renglones([
        {'codigo_material': 'NO-EXISTE', 'lote': 'X1', 'cantidad': '5', 'fecha_caducidad': '2027-01-01'},
        {'codigo_material': f'CI-MP-{n}', 'unidad': 'L', 'lote': 'A1', 'cantidad': '5',
         'fecha_caducidad': '31/12/2027'},
        {'codigo_material': f'CI-MP-{n}', 'lote': 'A1', 'cantidad': '5', 'fecha_caducidad': '2027-01-01'}],
        servicios.COLUMNAS_EXISTENCIAS)
    errores = servicios.validar_carga(conn, materiales, malas)
    textos = ' | '.join(e['error'] for e in errores)
    assert 'no existe' in textos and 'Unidad distinta' in textos and 'inválida' in textos and 'repetido' in textos
    assert {e['renglon'] for e in errores} == {2, 3, 4}
    with pytest.raises(ErrorAlmacen, match='errores'):
        servicios.importar_carga_inicial(conn, materiales=materiales, existencias=malas, usuario='pruebas')
    conn.rollback()
    assert conn.execute('SELECT 1 FROM materiales WHERE codigo = ?', (f'CI-MP-{n}',)).fetchone() is None

    buenas = servicios.normalizar_renglones([
        {'codigo_material': f'CI-MP-{n}', 'unidad': 'kg', 'lote': 'A1', 'cantidad': 5,
         'fecha_caducidad': date(2030, 1, 31), 'estatus': 'APROBADO', 'ubicacion': 'MP-A1'},
        {'codigo_material': f'CI-MP-{n}', 'lote': 'A2', 'cantidad': '7.5', 'fecha_caducidad': '2030-02-28'}],
        servicios.COLUMNAS_EXISTENCIAS)
    with pytest.raises(ErrorAlmacen, match='liberar_lotes'):
        servicios.importar_carga_inicial(conn, materiales=materiales, existencias=buenas, usuario='pruebas')
    conn.rollback()
    with pytest.raises(firmas.FirmaInvalida):
        servicios.importar_carga_inicial(conn, materiales=materiales, existencias=buenas, usuario='pruebas',
                                         firmar=firmar(conn, personas['almacen'], 'Liberó', 'liberar_lotes'))
    conn.rollback()
    r = servicios.importar_carga_inicial(conn, materiales=materiales, existencias=buenas, usuario='pruebas',
                                         firmar=firmar(conn, personas['calidad'], 'Liberó', 'liberar_lotes'))
    conn.commit()
    assert r['folio'].startswith('INI-') and r['materiales'] == 1 and r['lotes'] == 2
    lotes = {l['numero_lote']: l for l in conn.execute(
        'SELECT l.* FROM lotes l JOIN materiales m ON m.id = l.material_id WHERE m.codigo = ?',
        (f'CI-MP-{n}',)).fetchall()}
    assert lotes['A1']['estatus'] == 'APROBADO' and lotes['A1']['origen'] == 'INICIAL'
    assert lotes['A1']['fecha_caducidad'] == '2030-01-31' and lotes['A1']['ubicacion'] == 'MP-A1'
    assert lotes['A2']['estatus'] == 'CUARENTENA' and lotes['A2']['existencia'] == 7.5


# 10. Alertas ---------------------------------------------------------------------------

def test_lote_caducado_con_existencia_es_alerta_roja(conn):
    from modulos.almacen import ganchos
    mp = crear_material(conn, tipo='MP')
    crear_lote(conn, mp, fecha_caducidad=_dias(-10))
    codigo = conn.execute('SELECT codigo FROM materiales WHERE id = ?', (mp,)).fetchone()[0]
    from erp import app
    with app.test_request_context():
        rojas = [a for a in ganchos.alertas(conn) if a['nivel'] == 'rojo']
        kpis = {k['titulo']: k for k in ganchos.kpis(conn)}
    assert any(codigo in a['nombre'] for a in rojas)
    assert kpis['Caducados con existencia']['valor'] >= 1 and kpis['Caducados con existencia']['nivel'] == 'rojo'
