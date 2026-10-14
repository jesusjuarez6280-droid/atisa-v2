"""Pruebas de nucleo/inventario.py (diseño 00-base-comun, sección 11 punto 5).

Autocontenidas: cada prueba usa su propia base SQLite temporal con las tablas mínimas que ya
existen en el ERP (usuarios, competencias_tareas, inventario_activos) y la migración 1 de nucleo.
"""
import os
import sqlite3
import threading
from datetime import date, timedelta

import pytest

from nucleo import inventario as inv
from nucleo.esquema import MIGRACIONES_NUCLEO, aplicar_migraciones_de
from nucleo.firmas import firmar, fijar_pin
from nucleo.inventario import ErrorInventario

PIN = '2580'
SESION = {'username': 'prueba', 'numero_empleado': '', 'ip': None}

# numero_empleado -> (rol, facultades)
USUARIOS = {
    '100': ('calidad', 'liberar_lotes'),
    '200': ('almacen', 'autorizar_ajustes'),
    '300': ('operador', ''),
    '400': ('calidad', 'aprobar_maestros,gestionar_desviaciones'),
}


def _conectar(ruta):
    conn = sqlite3.connect(ruta, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def _preparar_base(ruta):
    conn = _conectar(ruta)
    conn.executescript('''
        CREATE TABLE usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_empleado TEXT UNIQUE,
            nombre_completo TEXT,
            username TEXT,
            password_hash TEXT,
            rol TEXT,
            estatus INTEGER DEFAULT 1,
            puesto TEXT
        );
        CREATE TABLE competencias_tareas (id INTEGER PRIMARY KEY);
        CREATE TABLE inventario_activos (id INTEGER PRIMARY KEY);
    ''')
    aplicar_migraciones_de(conn, 'nucleo', MIGRACIONES_NUCLEO)
    for numero, (rol, facultades) in USUARIOS.items():
        conn.execute('INSERT INTO usuarios (numero_empleado, nombre_completo, username, password_hash, rol, '
                     'puesto, facultades) VALUES (?, ?, ?, ?, ?, ?, ?)',
                     (numero, f'Empleado {numero}', f'u{numero}', 'x', rol, 'Puesto', facultades))
        fijar_pin(conn, numero, PIN, 'prueba')
    conn.commit()
    return conn


@pytest.fixture
def ruta_db(tmp_path):
    return os.path.join(str(tmp_path), 'nucleo_inventario.db')


@pytest.fixture
def conn(ruta_db):
    c = _preparar_base(ruta_db)
    yield c
    c.close()


def _firma(conn, numero, entidad_id=0, significado='Liberó'):
    return firmar(conn, numero_empleado=numero, pin=PIN, significado=significado, modulo='calidad',
                  entidad='lotes', entidad_id=entidad_id, sesion={**SESION, 'numero_empleado': numero})


def _material(conn, codigo='MP-0001', tipo='MP', unidad='kg', **campos):
    return inv.crear_material(conn, codigo=codigo, nombre=f'Material {codigo}', tipo=tipo, unidad=unidad,
                              usuario='prueba', **campos)


def _lote(conn, material_id, cantidad=100, origen='RECEPCION', **campos):
    return inv.crear_lote(conn, material_id=material_id, cantidad=cantidad, origen=origen, usuario='prueba',
                          modulo='almacen', **campos)


def _dias(n):
    return (date.today() + timedelta(days=n)).isoformat()


# ---------------------------------------------------------------------------
# crear_lote
# ---------------------------------------------------------------------------

def test_crear_lote_nace_en_cuarentena_con_su_entrada(conn):
    mp = _material(conn)
    lote_id = _lote(conn, mp, cantidad=100, folio='REC-2026-0001')
    l = inv.lote(conn, lote_id)
    assert l['estatus'] == 'CUARENTENA'
    assert l['existencia'] == 100
    assert l['numero_lote'].startswith(f'MP-{date.today():%y}-')
    assert l['disponible'] is False and 'CUARENTENA' in l['motivo_no_disponible']
    movs = conn.execute('SELECT * FROM lote_movimientos WHERE lote_id = ?', (lote_id,)).fetchall()
    assert [(m['tipo'], m['cantidad'], m['saldo'], m['folio']) for m in movs] == [
        ('ENTRADA_RECEPCION', 100, 100, 'REC-2026-0001')]
    hist = conn.execute('SELECT * FROM lote_historial WHERE lote_id = ?', (lote_id,)).fetchall()
    assert hist[0]['campo'] == 'estatus' and hist[0]['valor_nuevo'] == 'CUARENTENA'
    # El número generado es consecutivo
    otro = inv.lote(conn, _lote(conn, mp, cantidad=5))
    assert otro['numero_lote'] != l['numero_lote']


def test_crear_lote_sin_analisis_nace_aprobado_y_reglas_de_alta(conn):
    me = _material(conn, 'ME-0001', 'ME', 'pza', requiere_analisis=0)
    assert inv.lote(conn, _lote(conn, me, numero_lote='F-1'))['estatus'] == 'APROBADO'
    with pytest.raises(ErrorInventario, match='ya existe'):
        _lote(conn, me, numero_lote='F-1')
    malo = inv.crear_proveedor(conn, nombre='Proveedor Malo', usuario='prueba', estatus='No aprobado')
    with pytest.raises(ErrorInventario, match='No aprobado'):
        _lote(conn, me, proveedor_id=malo)
    # Estatus explícito solo en inventario inicial y con firma de liberar_lotes
    mp = _material(conn)
    with pytest.raises(ErrorInventario, match='inventario inicial'):
        _lote(conn, mp, estatus='APROBADO', firma_id=_firma(conn, '100'))
    with pytest.raises(ErrorInventario, match='liberar_lotes'):
        _lote(conn, mp, origen='INICIAL', estatus='APROBADO', firma_id=_firma(conn, '200'))
    inicial = _lote(conn, mp, origen='INICIAL', estatus='APROBADO', firma_id=_firma(conn, '100'))
    assert inv.lote(conn, inicial)['estatus'] == 'APROBADO'


# ---------------------------------------------------------------------------
# mover
# ---------------------------------------------------------------------------

def test_consumir_en_cuarentena_falla(conn):
    mp = _material(conn)
    lote_id = _lote(conn, mp, cantidad=50)
    with pytest.raises(ErrorInventario, match='CUARENTENA'):
        inv.mover(conn, lote_id=lote_id, tipo='CONSUMO_PRODUCCION', cantidad=5, usuario='prueba',
                  modulo='ordenes', folio='OP-2026-0001')
    assert inv.lote(conn, lote_id)['existencia'] == 50
    # Calidad sí puede muestrear un lote en cuarentena
    assert inv.mover(conn, lote_id=lote_id, tipo='MUESTRA', cantidad=0.5, usuario='prueba',
                     modulo='calidad') == 49.5


def test_granel_en_cuarentena_si_se_puede_envasar(conn):
    gr = _material(conn, 'GR-0001', 'GR')
    lote_gr = _lote(conn, gr, cantidad=100, origen='PRODUCCION', folio='OP-2026-0001')
    assert inv.lote(conn, lote_gr)['estatus'] == 'CUARENTENA'
    saldo = inv.mover(conn, lote_id=lote_gr, tipo='CONSUMO_PRODUCCION', cantidad=40, usuario='prueba',
                      modulo='ordenes', folio='OP-2026-0002')
    assert saldo == 60


def test_no_alcanza_y_validaciones_por_tipo(conn):
    pt = _material(conn, 'PT-0001', 'PT', 'pza', requiere_analisis=0)
    mp = _material(conn, 'MP-0002', 'MP', requiere_analisis=0)
    lote_pt = _lote(conn, pt, cantidad=10)
    lote_mp = _lote(conn, mp, cantidad=10)
    with pytest.raises(ErrorInventario, match='No hay suficiente'):
        inv.mover(conn, lote_id=lote_pt, tipo='CONSUMO_PRODUCCION', cantidad=11, usuario='p', modulo='ordenes')
    with pytest.raises(ErrorInventario, match='cliente'):
        inv.mover(conn, lote_id=lote_pt, tipo='VENTA', cantidad=1, usuario='p', modulo='almacen')
    with pytest.raises(ErrorInventario, match='producto terminado'):
        inv.mover(conn, lote_id=lote_mp, tipo='VENTA', cantidad=1, usuario='p', modulo='almacen',
                  motivo='Cliente')
    with pytest.raises(ErrorInventario, match='mayor que cero'):
        inv.mover(conn, lote_id=lote_mp, tipo='MUESTRA', cantidad=0, usuario='p', modulo='calidad')
    # Ajustes: motivo y firma de autorizar_ajustes
    with pytest.raises(ErrorInventario, match='motivo'):
        inv.mover(conn, lote_id=lote_mp, tipo='AJUSTE_MENOS', cantidad=1, usuario='p', modulo='almacen',
                  firma_id=_firma(conn, '200', significado='Autorizó'))
    with pytest.raises(ErrorInventario, match='autorizar_ajustes'):
        inv.mover(conn, lote_id=lote_mp, tipo='AJUSTE_MENOS', cantidad=1, usuario='p', modulo='almacen',
                  motivo='Conteo', firma_id=_firma(conn, '300', significado='Realizó'))
    assert inv.mover(conn, lote_id=lote_mp, tipo='AJUSTE_MENOS', cantidad=1, usuario='p', modulo='almacen',
                     motivo='Conteo físico', firma_id=_firma(conn, '200', significado='Autorizó')) == 9
    # Destrucción: solo rechazado o caducado
    with pytest.raises(ErrorInventario, match='rechazado ni caducado'):
        inv.mover(conn, lote_id=lote_mp, tipo='DESTRUCCION', cantidad=1, usuario='p', modulo='almacen',
                  firma_id=_firma(conn, '200', significado='Autorizó'))
    inv.cambiar_estatus(conn, lote_id=lote_mp, nuevo='RETENIDO', usuario='p', motivo='Sospecha',
                        firma_id=_firma(conn, '400', significado='Retuvo'))
    inv.cambiar_estatus(conn, lote_id=lote_mp, nuevo='RECHAZADO', usuario='p', motivo='Fuera de especificación',
                        firma_id=_firma(conn, '100', significado='Rechazó'))
    assert inv.mover(conn, lote_id=lote_mp, tipo='DESTRUCCION', cantidad=9, usuario='p', modulo='almacen',
                     firma_id=_firma(conn, '200', significado='Autorizó')) == 0
    # La venta queda con el cliente en el motivo
    assert inv.mover(conn, lote_id=lote_pt, tipo='VENTA', cantidad=4, usuario='p', modulo='almacen',
                     motivo='Granja El Sol', folio='FAC-1') == 6


def test_saldo_nunca_negativo_con_dos_conexiones_a_la_vez(conn, ruta_db):
    mp = _material(conn, requiere_analisis=0)
    lote_id = _lote(conn, mp, cantidad=10)
    conn.commit()

    hilos_n = 4
    barrera = threading.Barrier(hilos_n)
    resultados, errores = [], []

    def consumir():
        c = _conectar(ruta_db)
        try:
            barrera.wait()
            try:
                saldo = inv.mover(c, lote_id=lote_id, tipo='CONSUMO_PRODUCCION', cantidad=3, usuario='hilo',
                                  modulo='ordenes', folio='OP-2026-0009')
                c.commit()
                resultados.append(saldo)
            except ErrorInventario as e:
                c.rollback()
                errores.append(str(e))
        finally:
            c.close()

    hilos = [threading.Thread(target=consumir) for _ in range(hilos_n)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(30)

    assert len(resultados) == 3 and len(errores) == 1
    assert 'No hay suficiente' in errores[0]
    assert sorted(resultados) == [1, 4, 7]
    assert inv.lote(conn, lote_id)['existencia'] == 1
    saldos = [r['saldo'] for r in conn.execute('SELECT saldo FROM lote_movimientos WHERE lote_id = ? ORDER BY id',
                                                (lote_id,)).fetchall()]
    assert saldos == [10, 7, 4, 1]


# ---------------------------------------------------------------------------
# cambiar_estatus
# ---------------------------------------------------------------------------

def test_aprobar_sin_firma_o_sin_facultad_falla(conn):
    lote_id = _lote(conn, _material(conn))
    with pytest.raises(ErrorInventario, match='liberar_lotes'):
        inv.cambiar_estatus(conn, lote_id=lote_id, nuevo='APROBADO', usuario='p', motivo='Cumple', firma_id=None)
    with pytest.raises(ErrorInventario, match='liberar_lotes'):
        inv.cambiar_estatus(conn, lote_id=lote_id, nuevo='APROBADO', usuario='p', motivo='Cumple',
                            firma_id=_firma(conn, '200'))
    assert inv.lote(conn, lote_id)['estatus'] == 'CUARENTENA'
    f = _firma(conn, '100', lote_id)
    inv.cambiar_estatus(conn, lote_id=lote_id, nuevo='APROBADO', usuario='p', motivo='Cumple',
                        firma_id=f, folio='AN-2026-0001')
    assert inv.lote(conn, lote_id)['estatus'] == 'APROBADO'
    h = conn.execute("SELECT * FROM lote_historial WHERE lote_id = ? ORDER BY id DESC", (lote_id,)).fetchone()
    assert (h['valor_anterior'], h['valor_nuevo'], h['firma_id'], h['folio']) == (
        'CUARENTENA', 'APROBADO', f, 'AN-2026-0001')
    # Ya aprobado se puede consumir
    assert inv.mover(conn, lote_id=lote_id, tipo='CONSUMO_PRODUCCION', cantidad=10, usuario='p',
                     modulo='ordenes') == 90


def test_transiciones_y_rechazado_es_final(conn):
    lote_id = _lote(conn, _material(conn))
    with pytest.raises(ErrorInventario, match='motivo'):
        inv.cambiar_estatus(conn, lote_id=lote_id, nuevo='RECHAZADO', usuario='p', motivo=' ',
                            firma_id=_firma(conn, '100'))
    inv.cambiar_estatus(conn, lote_id=lote_id, nuevo='RECHAZADO', usuario='p', motivo='No cumple',
                        firma_id=_firma(conn, '100'))
    with pytest.raises(ErrorInventario, match='final'):
        inv.cambiar_estatus(conn, lote_id=lote_id, nuevo='APROBADO', usuario='p', motivo='x',
                            firma_id=_firma(conn, '100'))


def test_aprobar_el_granel_aprueba_sus_presentaciones(conn):
    gr = _material(conn, 'GR-0001', 'GR')
    pt1 = _material(conn, 'PT-0001', 'PT', 'pza', material_padre_id=gr, contenido_por_pieza=1)
    pt2 = _material(conn, 'PT-0002', 'PT', 'pza', material_padre_id=gr, contenido_por_pieza=25)
    lote_gr = _lote(conn, gr, cantidad=100, origen='PRODUCCION', folio='OP-2026-0001', numero_lote='2610001')
    hijo1 = _lote(conn, pt1, cantidad=40, origen='PRODUCCION', folio='OP-2026-0002', lote_padre_id=lote_gr,
                  numero_lote='2610001')
    hijo2 = _lote(conn, pt2, cantidad=2, origen='PRODUCCION', folio='OP-2026-0003', lote_padre_id=lote_gr,
                  numero_lote='2610001')
    assert inv.lote(conn, hijo1)['estatus'] == 'CUARENTENA'
    # Una presentación retenida por separado no cambia con el granel
    inv.cambiar_estatus(conn, lote_id=hijo2, nuevo='RETENIDO', usuario='p', motivo='Sello dañado',
                        firma_id=_firma(conn, '400', significado='Retuvo'))
    afectados = inv.cambiar_estatus(conn, lote_id=lote_gr, nuevo='APROBADO', usuario='p', motivo='Cumple',
                                    firma_id=_firma(conn, '100', lote_gr))
    assert afectados == [lote_gr, hijo1]
    assert inv.lote(conn, lote_gr)['estatus'] == 'APROBADO'
    assert inv.lote(conn, hijo1)['estatus'] == 'APROBADO'
    assert inv.lote(conn, hijo2)['estatus'] == 'RETENIDO'
    h = conn.execute('SELECT motivo FROM lote_historial WHERE lote_id = ? ORDER BY id DESC', (hijo1,)).fetchone()
    assert 'cascada' in h['motivo']
    # Una presentación nueva de un granel ya aprobado nace aprobada
    hijo3 = _lote(conn, pt1, cantidad=10, origen='PRODUCCION', folio='OP-2026-0004', lote_padre_id=lote_gr,
                  numero_lote='2610001-B')
    assert inv.lote(conn, hijo3)['estatus'] == 'APROBADO'


# ---------------------------------------------------------------------------
# sugerir_lotes, existencias, correcciones
# ---------------------------------------------------------------------------

def test_sugerir_lotes_respeta_caducidad_y_luego_antiguedad(conn):
    mp = _material(conn, requiere_analisis=0)
    a = _lote(conn, mp, 10, numero_lote='A', fecha_caducidad=_dias(200), fecha_entrada=_dias(-30))
    b = _lote(conn, mp, 10, numero_lote='B', fecha_caducidad=_dias(100), fecha_entrada=_dias(-5))
    c = _lote(conn, mp, 10, numero_lote='C', fecha_entrada=_dias(-60))
    d = _lote(conn, mp, 10, numero_lote='D', fecha_entrada=_dias(-10))
    _lote(conn, mp, 10, numero_lote='CADUCO', fecha_caducidad=_dias(-1), fecha_entrada=_dias(-400))
    _lote(conn, mp, 10, numero_lote='CUAR', estatus='CUARENTENA', fecha_entrada=_dias(-500))
    _lote(conn, mp, 10, numero_lote='REANAL', fecha_reanalisis=_dias(-2), fecha_entrada=_dias(-450))

    sug = inv.sugerir_lotes(conn, mp, 35)
    assert [(s['lote_id'], s['tomar']) for s in sug] == [(b, 10), (a, 10), (c, 10), (d, 5)]
    assert sug[0]['caducidad'] == _dias(100)

    sug = inv.sugerir_lotes(conn, mp, 100)
    assert sug[-1] == {'faltante': 60}
    assert inv.existencia_material(conn, mp) == 40
    assert inv.existencia_material(conn, mp, solo_disponible=False) == 70

    caduco = conn.execute("SELECT id FROM lotes WHERE numero_lote = 'CADUCO'").fetchone()['id']
    ok, motivo = inv.lote_disponible(conn, caduco)
    assert not ok and 'caducó' in motivo


def test_corregir_lote_caducidad_exige_firma(conn):
    lote_id = _lote(conn, _material(conn))
    with pytest.raises(ErrorInventario, match='liberar_lotes'):
        inv.corregir_lote(conn, lote_id=lote_id, campo='fecha_caducidad', valor='2027-01-31', usuario='p',
                          motivo='Error de captura')
    inv.corregir_lote(conn, lote_id=lote_id, campo='fecha_caducidad', valor='2027-01-31', usuario='p',
                      motivo='Error de captura', firma_id=_firma(conn, '100'))
    inv.corregir_lote(conn, lote_id=lote_id, campo='ubicacion', valor='Rack 3', usuario='p', motivo='Reacomodo')
    l = inv.lote(conn, lote_id)
    assert (l['fecha_caducidad'], l['ubicacion']) == ('2027-01-31', 'Rack 3')
    with pytest.raises(ErrorInventario):
        inv.corregir_lote(conn, lote_id=lote_id, campo='existencia', valor=5, usuario='p', motivo='x')


# ---------------------------------------------------------------------------
# trazabilidad
# ---------------------------------------------------------------------------

def test_trazabilidad_en_los_dos_sentidos(conn):
    mp = _material(conn, 'MP-0001', 'MP', requiere_analisis=0)
    em = _material(conn, 'EM-0001', 'EM', 'pza', requiere_analisis=0)
    gr = _material(conn, 'GR-0001', 'GR')
    pt = _material(conn, 'PT-0001', 'PT', 'pza', material_padre_id=gr, contenido_por_pieza=1)
    lote_mp = _lote(conn, mp, 50, numero_lote='MP-A')
    lote_em = _lote(conn, em, 100, numero_lote='EM-A')
    _lote(conn, mp, 50, numero_lote='MP-NO-USADO')

    # Fabricación del granel (OP-1): consume MP
    inv.mover(conn, lote_id=lote_mp, tipo='CONSUMO_PRODUCCION', cantidad=20, usuario='p', modulo='ordenes',
              folio='OP-2026-0001')
    inv.mover(conn, lote_id=lote_mp, tipo='DEVOLUCION_PRODUCCION', cantidad=2, usuario='p', modulo='ordenes',
              folio='OP-2026-0001')
    lote_gr = _lote(conn, gr, 18, origen='PRODUCCION', folio='OP-2026-0001', numero_lote='G-1')
    inv.cambiar_estatus(conn, lote_id=lote_gr, nuevo='APROBADO', usuario='p', motivo='Cumple',
                        firma_id=_firma(conn, '100'))
    # Envasado (OP-2): consume granel y etiquetas
    inv.mover(conn, lote_id=lote_gr, tipo='CONSUMO_PRODUCCION', cantidad=10, usuario='p', modulo='ordenes',
              folio='OP-2026-0002')
    inv.mover(conn, lote_id=lote_em, tipo='CONSUMO_PRODUCCION', cantidad=10, usuario='p', modulo='ordenes',
              folio='OP-2026-0002')
    lote_pt = _lote(conn, pt, 10, origen='PRODUCCION', folio='OP-2026-0002', lote_padre_id=lote_gr,
                    numero_lote='G-1')
    assert inv.lote(conn, lote_pt)['estatus'] == 'APROBADO'
    inv.mover(conn, lote_id=lote_pt, tipo='VENTA', cantidad=4, usuario='p', modulo='almacen',
              motivo='Granja El Sol', folio='FAC-77')

    # Hacia adelante desde la materia prima: granel, presentación y cliente
    adelante = inv.trazabilidad(conn, lote_mp)['hacia_adelante']
    por_lote = {r['lote_id']: r for r in adelante if r['relacion'] != 'VENTA'}
    assert set(por_lote) == {lote_gr, lote_pt}
    assert por_lote[lote_gr]['relacion'] == 'PRODUCIDO' and por_lote[lote_gr]['cantidad'] == 18
    assert por_lote[lote_pt]['nivel'] == 2
    ventas = [r for r in adelante if r['relacion'] == 'VENTA']
    assert [(v['cliente'], v['cantidad'], v['folio'], v['lote_id']) for v in ventas] == [
        ('Granja El Sol', 4, 'FAC-77', lote_pt)]

    # Hacia atrás desde el producto vendido: granel, etiquetas y materia prima (no el lote no usado)
    atras = inv.trazabilidad(conn, lote_pt)['hacia_atras']
    assert {r['lote_id'] for r in atras} == {lote_gr, lote_em, lote_mp}
    relaciones = {r['lote_id']: r['relacion'] for r in atras}
    assert relaciones[lote_gr] == 'GRANEL' and relaciones[lote_mp] == 'CONSUMO'
    assert next(r for r in atras if r['lote_id'] == lote_mp)['nivel'] == 2

    # Devolución de cliente: lote nuevo en cuarentena ligado al vendido
    dev = _lote(conn, pt, 1, origen='DEVOLUCION', lote_padre_id=lote_pt, folio='DEV-1')
    assert inv.lote(conn, dev)['estatus'] == 'CUARENTENA'
    assert any(r['lote_id'] == dev and r['relacion'] == 'DEVOLUCION'
               for r in inv.trazabilidad(conn, lote_pt)['hacia_adelante'])
    assert inv.trazabilidad(conn, dev)['hacia_atras'][0]['relacion'] == 'LOTE_VENDIDO'


# ---------------------------------------------------------------------------
# Catálogos
# ---------------------------------------------------------------------------

def test_editar_material_no_cambia_unidad_con_lotes(conn):
    mp = _material(conn, unidad='kg')
    sin_lotes = _material(conn, 'MP-0099', unidad='kg')
    inv.editar_material(conn, sin_lotes, usuario='p', unidad='g', nombre='Otro nombre')
    assert conn.execute('SELECT unidad FROM materiales WHERE id = ?', (sin_lotes,)).fetchone()[0] == 'g'
    _lote(conn, mp)
    with pytest.raises(ErrorInventario, match='unidad'):
        inv.editar_material(conn, mp, usuario='p', unidad='g')
    with pytest.raises(ErrorInventario, match='código'):
        inv.editar_material(conn, mp, usuario='p', codigo='MP-9999')
    inv.editar_material(conn, mp, usuario='p', nombre='Nuevo nombre', stock_minimo=5, activo=0)
    fila = conn.execute('SELECT * FROM materiales WHERE id = ?', (mp,)).fetchone()
    assert (fila['unidad'], fila['nombre'], fila['activo']) == ('kg', 'Nuevo nombre', 0)
    # Quitar el análisis obligatorio exige aprobar_maestros
    with pytest.raises(ErrorInventario, match='aprobar_maestros'):
        inv.editar_material(conn, mp, usuario='p', requiere_analisis=0)
    with pytest.raises(ErrorInventario, match='aprobar_maestros'):
        inv.editar_material(conn, mp, usuario='p', requiere_analisis=0, firma_id=_firma(conn, '100'))
    inv.editar_material(conn, mp, usuario='p', requiere_analisis=0,
                        firma_id=_firma(conn, '400', significado='Aprobó'))
    assert conn.execute('SELECT requiere_analisis FROM materiales WHERE id = ?', (mp,)).fetchone()[0] == 0


def test_catalogos_validan(conn):
    _material(conn, 'MP-0001')
    with pytest.raises(ErrorInventario, match='Ya existe'):
        _material(conn, 'MP-0001')
    with pytest.raises(ErrorInventario, match='Tipo de material'):
        _material(conn, 'XX-1', tipo='XX')
    mp = _material(conn, 'MP-0002')
    with pytest.raises(ErrorInventario, match='granel'):
        _material(conn, 'PT-0001', 'PT', 'pza', material_padre_id=mp)
    prov = inv.crear_proveedor(conn, nombre='Químicos SA', usuario='p')
    with pytest.raises(ErrorInventario, match='motivo'):
        inv.editar_proveedor(conn, prov, usuario='p', estatus='Aprobado')
    inv.editar_proveedor(conn, prov, usuario='p', estatus='Aprobado', motivo='Auditoría satisfactoria')
    fila = conn.execute('SELECT * FROM proveedores WHERE id = ?', (prov,)).fetchone()
    assert fila['estatus'] == 'Aprobado' and 'Auditoría satisfactoria' in fila['notas']
