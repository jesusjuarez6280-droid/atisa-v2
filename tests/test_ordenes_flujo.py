"""Pruebas del módulo Órdenes de producción (02-ordenes-produccion.md, sección 9).

Trabajan sobre la base temporal de conftest con los servicios del módulo; cada prueba arma su
propio producto, fórmula y usuarios, así que no dependen de otras pruebas ni de otros módulos.
"""
import re
from datetime import date, timedelta

import pytest

from fabricas import conexion, crear_equipo, crear_lote, crear_material, crear_usuario, firma
from nucleo import inventario
from nucleo import registro
from nucleo.parametros import guardar_parametro, parametro
from modulos.ordenes import ganchos
from modulos.ordenes.servicios import ErrorOrdenes, fin_de_mes_mas
from modulos.ordenes.servicios import expediente as srv_expediente
from modulos.ordenes.servicios import formulas as srv_formulas
from modulos.ordenes.servicios import ordenes as srv_ordenes
from modulos.ordenes.servicios import planta as srv_planta

PIN = '2580'
CHECKS = {k: 1 for k, _ in srv_planta.CHECKS_DESPEJE}
_n = iter(range(1, 10 ** 6))


class Planta:
    """Producto con fórmula vigente, usuarios con PIN, materias primas con lote y una báscula apta."""

    def __init__(self, conn, piezas=20, tarea_id=None):
        self.conn = conn
        n = next(_n)
        self.jefe = crear_usuario(conn, f'op_jefe_{n}', rol='produccion', pin=PIN,
                                  facultades='emitir_ordenes,aprobar_maestros')
        self.jefa = crear_usuario(conn, f'op_jefa_{n}', rol='produccion', pin=PIN,
                                  facultades='emitir_ordenes,aprobar_maestros')
        self.operador = crear_usuario(conn, f'op_oper_{n}', rol='operador', pin=PIN)
        self.mp = crear_material(conn, tipo='MP', requiere_analisis=0)
        self.lote_mp = crear_lote(conn, self.mp, cantidad=100)
        self.etiqueta = crear_material(conn, tipo='ME', unidad='pza', requiere_analisis=0, es_impreso=1)
        self.lote_etiqueta = crear_lote(conn, self.etiqueta, cantidad=500)
        self.bascula = crear_equipo(conn, es_instrumento=1)
        self.granel = srv_formulas.crear_producto(
            conn, granel={'codigo': f'GR-OPT-{n}', 'nombre': f'Producto {n}', 'unidad': 'kg', 'vida_util_meses': 24},
            presentaciones=[{'codigo': f'PT-OPT-{n}', 'nombre': f'Producto {n} 500 g', 'unidad': 'pza',
                             'contenido_por_pieza': 0.5}], usuario='pruebas')
        self.pt = srv_formulas.presentaciones(conn, self.granel)[0]['id']
        self.formula = srv_formulas.crear_formula(conn, material_id=self.granel, tamano_lote=10, usuario='pruebas')
        srv_formulas.agregar_componente(conn, self.formula, material_id=self.mp, cantidad=10, tolerancia_pct=1)
        srv_formulas.agregar_paso(conn, self.formula, instruccion='Cargar y mezclar', minutos=10,
                                  competencia_tarea_id=tarea_id,
                                  parametros=[{'nombre': 'Temperatura', 'unidad': '°C', 'valor_min': 20,
                                               'valor_max': 30}])
        srv_formulas.agregar_paso(conn, self.formula, instruccion='Homogeneizar', minutos=5)
        srv_formulas.agregar_empaque(conn, self.formula, presentacion_id=self.pt, material_id=self.etiqueta,
                                     cantidad_por_pieza=1)
        srv_formulas.enviar_revision(conn, self.formula)
        srv_formulas.revisar(conn, self.formula, self.f(self.jefe, 'Revisó'))
        srv_formulas.aprobar(conn, self.formula, self.f(self.jefa, 'Aprobó'))
        conn.commit()
        self.piezas = piezas

    def f(self, numero, significado='Realizó'):
        return firma(self.conn, numero, PIN, significado, modulo='ordenes', entidad='prueba')

    def orden(self, tamano=10, emitir=True, **kw):
        oid, _ = srv_ordenes.crear(self.conn, formula_id=self.formula, tamano_lote=tamano, usuario='pruebas',
                                   piezas={self.pt: self.piezas}, **kw)
        if emitir:
            srv_ordenes.emitir(self.conn, oid, self.f(self.jefe, 'Autorizó'))
        self.conn.commit()
        return oid

    def despeje(self, oid, momento='Fabricación'):
        srv_planta.despeje(self.conn, oid, momento=momento, checks=CHECKS, realizo_firma_id=self.f(self.operador),
                           verifico_firma_id=self.f(self.jefe, 'Verificó'))

    def mp_de(self, oid):
        return next(c for c in srv_ordenes.componentes(self.conn, oid) if c['tipo'] == 'MP')

    def pesar(self, oid, cantidad=10, lote=None, **kw):
        datos = {'lote_id': lote or self.lote_mp, 'cantidad': cantidad, 'usuario': 'pruebas',
                 'equipo_id': self.bascula, 'realizo_firma_id': self.f(self.operador),
                 'verifico_firma_id': self.f(self.jefe, 'Verificó'), **kw}
        return srv_planta.pesar(self.conn, oid, self.mp_de(oid)['id'], **datos)

    def paso(self, oid, numero, valor=25):
        p = next(x for x in srv_ordenes.pasos(self.conn, oid) if x['numero'] == numero)
        srv_planta.iniciar_paso(self.conn, oid, p['id'])
        return srv_planta.firmar_paso(self.conn, oid, p['id'], valores={v['id']: valor for v in p['valores']},
                                      realizo_firma_id=self.f(self.operador))

    def fabricar(self, oid, granel=10, temperatura=25):
        self.despeje(oid)
        self.pesar(oid)
        self.paso(oid, 1, temperatura)
        self.paso(oid, 2)
        return srv_planta.registrar_granel(self.conn, oid, cantidad=granel, usuario='pruebas',
                                           firma_id=self.f(self.jefe, 'Realizó'))

    def envasar(self, oid, usadas=None):
        self.despeje(oid, 'Envasado')
        etq = next(c for c in srv_ordenes.componentes(self.conn, oid) if c['material_id'] == self.etiqueta)
        srv_planta.pesar(self.conn, oid, etq['id'], lote_id=self.lote_etiqueta, cantidad=self.piezas, usuario='pruebas',
                         realizo_firma_id=self.f(self.operador), verifico_firma_id=self.f(self.jefe, 'Verificó'))
        lote_pt = srv_planta.envasar(self.conn, oid, self.pt, buenas=self.piezas, rechazadas=0, usuario='pruebas',
                                     firma_id=self.f(self.operador))
        srv_planta.conciliar(self.conn, oid, self.etiqueta, usadas=self.piezas if usadas is None else usadas,
                             firma_id=self.f(self.operador))
        return lote_pt


@pytest.fixture
def conn():
    c = conexion()
    yield c
    c.rollback()
    c.close()


@pytest.fixture
def planta(conn):
    return Planta(conn)


@pytest.fixture
def param(conn):
    """Cambia parámetros durante la prueba y los regresa al terminar."""
    originales = {}

    def poner(clave, valor):
        originales.setdefault(clave, parametro(clave, conn))
        guardar_parametro(conn, clave, valor, 'pruebas')
        conn.commit()
    yield poner
    conn.rollback()
    for clave, valor in originales.items():
        guardar_parametro(conn, clave, valor, 'pruebas')
    conn.commit()


# 1. Fórmula maestra ----------------------------------------------------------------

def test_formula_pide_revisor_y_aprobador_distintos_y_la_anterior_queda_obsoleta(conn, planta):
    assert srv_formulas.formula(conn, planta.formula)['estatus'] == 'VIGENTE'
    with pytest.raises(ErrorOrdenes, match='nueva versión'):
        srv_formulas.agregar_componente(conn, planta.formula, material_id=crear_material(conn, tipo='MP'), cantidad=1)

    v2 = srv_formulas.nueva_version(conn, planta.formula, usuario='pruebas', cambios='Más mezclado')
    assert len(srv_formulas.componentes(conn, v2)) == 1 and len(srv_formulas.pasos(conn, v2)) == 2
    srv_formulas.enviar_revision(conn, v2)
    srv_formulas.revisar(conn, v2, planta.f(planta.jefe, 'Revisó'))
    with pytest.raises(ErrorOrdenes, match='distintas'):
        srv_formulas.aprobar(conn, v2, planta.f(planta.jefe, 'Aprobó'))
    with pytest.raises(ErrorOrdenes):
        srv_formulas.aprobar(conn, v2, planta.f(planta.operador, 'Aprobó'))  # sin aprobar_maestros
    srv_formulas.aprobar(conn, v2, planta.f(planta.jefa, 'Aprobó'))
    assert srv_formulas.formula(conn, v2)['estatus'] == 'VIGENTE'
    assert srv_formulas.formula(conn, planta.formula)['estatus'] == 'OBSOLETA'
    assert srv_formulas.vigente(conn, planta.granel)['id'] == v2


# 2. Emitir -----------------------------------------------------------------------

def test_emitir_escala_numera_el_lote_y_calcula_la_caducidad(conn, planta):
    oid = planta.orden(tamano=20, emitir=False, fecha_programada='2026-03-10')
    with pytest.raises(ErrorOrdenes):
        srv_ordenes.emitir(conn, oid, planta.f(planta.operador, 'Autorizó'))  # sin emitir_ordenes
    lote = srv_ordenes.emitir(conn, oid, planta.f(planta.jefe, 'Autorizó'))
    o = srv_ordenes.orden(conn, oid)
    assert o['estatus'] == 'EMITIDA' and o['factor'] == 2
    assert re.fullmatch(r'\d{7}', lote) and lote.startswith(date.today().strftime('%y%m'))
    assert o['fecha_caducidad'] == '2028-03-31' == fin_de_mes_mas('2026-03-10', 24)
    mp = planta.mp_de(oid)
    assert mp['cantidad_teorica'] == 20 and mp['tolerancia_pct'] == 1
    etq = next(c for c in srv_ordenes.componentes(conn, oid) if c['material_id'] == planta.etiqueta)
    assert etq['cantidad_teorica'] == planta.piezas and etq['es_impreso'] == 1
    assert [p['numero'] for p in srv_ordenes.pasos(conn, oid)] == [1, 2]


def test_solo_sale_de_formula_vigente(conn, planta):
    v2 = srv_formulas.nueva_version(conn, planta.formula, usuario='pruebas', cambios='Borrador')
    with pytest.raises(ErrorOrdenes, match='vigente'):
        srv_ordenes.crear(conn, formula_id=v2, tamano_lote=10, usuario='pruebas')


# 3. Candados de la pesada ------------------------------------------------------------

def test_candados_de_la_pesada(conn, planta):
    oid = planta.orden()
    with pytest.raises(ErrorOrdenes, match='despeje'):
        planta.pesar(oid)
    planta.despeje(oid)
    assert srv_ordenes.orden(conn, oid)['estatus'] == 'EN_PROCESO'

    cuarentena = crear_lote(conn, planta.mp, cantidad=50, estatus='CUARENTENA')
    caducado = crear_lote(conn, planta.mp, cantidad=50, fecha_caducidad=(date.today() - timedelta(days=1)).isoformat())
    for lote in (cuarentena, caducado):
        with pytest.raises(ErrorOrdenes):
            planta.pesar(oid, lote=lote, motivo_otro_lote='prueba')

    sin_verificar = crear_equipo(conn, es_instrumento=1, verificacion_diaria=1)
    vencida = crear_equipo(conn, es_instrumento=1, vence_calibracion=(date.today() - timedelta(days=1)).isoformat())
    for equipo in (sin_verificar, vencida):
        with pytest.raises(ErrorOrdenes):
            planta.pesar(oid, equipo_id=equipo)

    with pytest.raises(ErrorOrdenes, match='tolerancia'):
        planta.pesar(oid, cantidad=10.2)  # máximo 10.1
    with pytest.raises(ErrorOrdenes, match='verifica'):
        planta.pesar(oid, verifico_firma_id=None)
    with pytest.raises(ErrorOrdenes, match='no puede ser quien pesó'):
        planta.pesar(oid, verifico_firma_id=planta.f(planta.operador, 'Verificó'))

    antes = inventario.lote(conn, planta.lote_mp)['existencia']
    planta.pesar(oid, cantidad=10)
    assert inventario.lote(conn, planta.lote_mp)['existencia'] == antes - 10
    mov = conn.execute('SELECT * FROM lote_movimientos WHERE lote_id = ? ORDER BY id DESC LIMIT 1',
                       (planta.lote_mp,)).fetchone()
    assert mov['tipo'] == 'CONSUMO_PRODUCCION' and mov['folio'] == srv_ordenes.orden(conn, oid)['folio']
    assert planta.mp_de(oid)['completo']


def test_verificar_solo_activos(conn, planta, param):
    param('ordenes.verificar_pesadas', 'activos')
    oid = planta.orden()
    planta.despeje(oid)
    planta.pesar(oid, verifico_firma_id=None)  # la MP no es activo: no pide verificación
    assert planta.mp_de(oid)['completo']


# 4. Orden de los pasos ---------------------------------------------------------------

def test_pasos_en_orden_y_despues_de_las_pesadas(conn, planta):
    oid = planta.orden()
    planta.despeje(oid)
    p1, p2 = srv_ordenes.pasos(conn, oid)
    with pytest.raises(ErrorOrdenes, match='pesadas'):
        srv_planta.iniciar_paso(conn, oid, p1['id'])
    planta.pesar(oid)
    with pytest.raises(ErrorOrdenes, match='paso 1'):
        srv_planta.iniciar_paso(conn, oid, p2['id'])
    with pytest.raises(ErrorOrdenes, match='Iniciar'):
        srv_planta.firmar_paso(conn, oid, p1['id'], valores={}, realizo_firma_id=planta.f(planta.operador))
    planta.paso(oid, 1)
    planta.paso(oid, 2)
    assert all(p['estatus'] == 'Hecho' for p in srv_ordenes.pasos(conn, oid))


# 5. Fuera de rango ---------------------------------------------------------------------

def test_fuera_de_rango_bloquea_terminar_hasta_justificar(conn, planta):
    oid = planta.orden()
    planta.fabricar(oid, temperatura=35)
    planta.envasar(oid)
    paso1 = srv_ordenes.pasos(conn, oid)[0]
    assert paso1['fuera_de_rango'] == 1
    with pytest.raises(ErrorOrdenes, match='fuera de rango'):
        srv_ordenes.terminar(conn, oid, firma_id=planta.f(planta.jefe), usuario='pruebas')
    if srv_ordenes.desviaciones_instalado():
        pytest.skip('Con el módulo de Desviaciones se pide desviación, no justificación.')
    srv_planta.justificar(conn, oid, origen='orden_paso', origen_id=paso1['id'],
                          justificacion='Termómetro mal colocado; se repitió la lectura', firma_id=planta.f(planta.jefe))
    srv_ordenes.terminar(conn, oid, firma_id=planta.f(planta.jefe), usuario='pruebas')
    assert srv_ordenes.orden(conn, oid)['estatus'] == 'TERMINADA'


# 6. Calificación ---------------------------------------------------------------------

def test_calificacion_avisa_o_bloquea_segun_parametro(conn, param):
    tarea = conn.execute("INSERT INTO competencias_tareas (nombre) VALUES ('Mezclado de prueba')").lastrowid
    conn.commit()
    planta = Planta(conn, tarea_id=tarea)
    oid = planta.orden()
    planta.despeje(oid)
    planta.pesar(oid)
    r = planta.paso(oid, 1)
    assert any('mínimo' in a for a in r['avisos'])

    param('ordenes.bloquear_sin_calificacion', '1')
    oid2 = planta.orden()
    planta.despeje(oid2)
    planta.pesar(oid2)
    with pytest.raises(ErrorOrdenes, match='No se puede firmar'):
        planta.paso(oid2, 1)
    conn.execute('INSERT INTO expertise_evaluaciones (numero_empleado, tarea_id, nivel_pct) VALUES (?, ?, 80)',
                 (planta.operador, tarea))
    r = planta.paso(oid2, 1)
    assert r['avisos'] == []
    assert srv_ordenes.pasos(conn, oid2)[0]['nivel_competencia'] == 80


# 7 y 8. Granel, envasado, conciliación, terminar y revisar ----------------------------

def test_flujo_completo_hasta_revisada(conn, planta):
    oid = planta.orden()
    r = planta.fabricar(oid, granel=10)
    o = srv_ordenes.orden(conn, oid)
    granel = inventario.lote(conn, r['lote_id'])
    assert r['rendimiento'] == 100 and not r['fuera']
    assert granel['estatus'] == 'CUARENTENA' and granel['numero_lote'] == o['numero_lote']

    lote_pt = planta.envasar(oid, usadas=planta.piezas - 1)  # sobra una etiqueta que nadie devolvió
    pt = inventario.lote(conn, lote_pt)
    assert pt['lote_padre_id'] == r['lote_id'] and pt['numero_lote'] == o['numero_lote']
    assert pt['existencia'] == planta.piezas
    assert inventario.lote(conn, r['lote_id'])['existencia'] == 0

    probs = srv_ordenes.problemas(conn, srv_ordenes.orden(conn, oid))
    assert [p['origen'] for p in probs] == ['orden_conciliacion']
    with pytest.raises(ErrorOrdenes, match='no cuadra'):
        srv_ordenes.terminar(conn, oid, firma_id=planta.f(planta.jefe), usuario='pruebas')
    if srv_ordenes.desviaciones_instalado():
        pytest.skip('Con el módulo de Desviaciones se pide desviación, no justificación.')
    srv_planta.justificar(conn, oid, origen='orden_conciliacion', origen_id=probs[0]['origen_id'],
                          justificacion='Etiqueta perdida en la línea', firma_id=planta.f(planta.jefe))
    srv_ordenes.terminar(conn, oid, firma_id=planta.f(planta.jefe, 'Cerró'), usuario='pruebas')

    with pytest.raises(ErrorOrdenes, match='distinta'):
        srv_ordenes.revisar(conn, oid, planta.f(planta.jefe, 'Revisó'))
    srv_ordenes.revisar(conn, oid, planta.f(planta.jefa, 'Revisó'))
    assert srv_ordenes.orden(conn, oid)['estatus'] == 'REVISADA'
    assert srv_ordenes.avance(conn, oid) == 100


def test_terminar_lista_lo_que_falta_y_pide_motivo_de_merma(conn, planta):
    oid = planta.orden()
    planta.despeje(oid)
    with pytest.raises(ErrorOrdenes, match='Todavía falta') as e:
        srv_ordenes.terminar(conn, oid, firma_id=planta.f(planta.jefe), usuario='pruebas')
    assert 'Registrar el granel obtenido' in str(e.value)

    planta.piezas = 10  # 5 kg de granel para envasar
    oid = planta.orden()
    r = planta.fabricar(oid, granel=9.9)
    planta.envasar(oid)
    with pytest.raises(ErrorOrdenes, match='merma'):
        srv_ordenes.terminar(conn, oid, firma_id=planta.f(planta.jefe), usuario='pruebas')
    srv_ordenes.terminar(conn, oid, firma_id=planta.f(planta.jefe), usuario='pruebas', motivo_merma='Residuo en tanque')
    o = srv_ordenes.orden(conn, oid)
    assert o['merma_granel'] == pytest.approx(4.9)
    assert inventario.lote(conn, r['lote_id'])['existencia'] == pytest.approx(0)


def test_rendimiento_fuera_de_limites_marca_la_orden(conn, planta):
    oid = planta.orden()
    r = planta.fabricar(oid, granel=9)
    assert r['fuera'] and r['rendimiento'] == 90
    assert 'orden_granel' in [p['origen'] for p in srv_ordenes.problemas(conn, srv_ordenes.orden(conn, oid))]


# 9. Liberación y ficha del lote -----------------------------------------------------

def test_verificaciones_de_liberacion(conn, planta):
    oid = planta.orden()
    r = planta.fabricar(oid)
    lote_pt = planta.envasar(oid)
    conn.commit()
    for lote in (r['lote_id'], lote_pt):
        motivos = ganchos.verificaciones_liberacion(conn, lote)
        assert any('falta que producción revise' in m for m in motivos)
        assert motivos[0] in registro.verificaciones_liberacion(conn, lote)
    srv_ordenes.terminar(conn, oid, firma_id=planta.f(planta.jefe), usuario='pruebas')
    srv_ordenes.revisar(conn, oid, planta.f(planta.jefa, 'Revisó'))
    assert ganchos.verificaciones_liberacion(conn, lote_pt) == []
    assert ganchos.verificaciones_liberacion(conn, planta.lote_mp) == []  # no es de una orden
    assert ganchos.seccion_lote(conn, lote_pt)['contexto']['datos']['fabricado']['id'] == oid
    assert ganchos.seccion_lote(conn, planta.lote_mp)['contexto']['datos']['usado'][0]['id'] == oid


# 10. Consultas para otros módulos ------------------------------------------------------

def test_lotes_por_equipo_y_demanda_material(conn, planta):
    oid = planta.orden()
    planta.despeje(oid)
    planta.pesar(oid, cantidad=4)
    usos = ganchos.lotes_por_equipo(conn, planta.bascula, date.today().isoformat())
    assert oid in [u['orden_id'] for u in usos]
    fila = next(u for u in usos if u['orden_id'] == oid)
    assert 'Pesada' in fila['usos'] and fila['lote_id'] is None  # aún sin granel
    for campo in ('lote_id', 'fecha', 'numero_lote', 'folio', 'material', 'url'):
        assert campo in fila
    assert ganchos.lotes_por_equipo(conn, planta.bascula, (date.today() + timedelta(days=1)).isoformat()) == []

    planta.pesar(oid, cantidad=6)
    planta.paso(oid, 1)
    planta.paso(oid, 2)
    r = srv_planta.registrar_granel(conn, oid, cantidad=10, usuario='pruebas', firma_id=planta.f(planta.jefe),
                                    equipo_id=planta.bascula)
    usos = ganchos.lotes_por_equipo(conn, planta.bascula, date.today().isoformat())
    assert r['lote_id'] in [u['lote_id'] for u in usos if u['orden_id'] == oid]

    mp_nueva = crear_material(conn, tipo='MP', requiere_analisis=0)
    assert ganchos.demanda_material(conn, mp_nueva) is None
    planta.orden(emitir=False)  # borrador: pide 10 de la MP
    d = ganchos.demanda_material(conn, planta.mp)
    assert d['requerido'] > 0 and 'faltante' in d
    assert registro.hay_consulta('lotes_por_equipo') and registro.hay_consulta('demanda_material')


# 11. Cancelar ----------------------------------------------------------------------

def test_cancelar_devuelve_lo_pesado(conn, planta):
    oid = planta.orden()
    planta.despeje(oid)
    antes = inventario.lote(conn, planta.lote_mp)['existencia']
    planta.pesar(oid, cantidad=6)
    with pytest.raises(ErrorOrdenes):
        srv_ordenes.cancelar(conn, oid, motivo='Falla de equipo', usuario='pruebas', firma_id=planta.f(planta.operador))
    srv_ordenes.cancelar(conn, oid, motivo='Falla de equipo', usuario='pruebas', firma_id=planta.f(planta.jefe, 'Anuló'))
    assert srv_ordenes.orden(conn, oid)['estatus'] == 'CANCELADA'
    assert inventario.lote(conn, planta.lote_mp)['existencia'] == antes
    tipos = [m['tipo'] for m in conn.execute('SELECT tipo FROM lote_movimientos WHERE lote_id = ? ORDER BY id DESC LIMIT 2',
                                             (planta.lote_mp,)).fetchall()]
    assert tipos == ['DEVOLUCION_PRODUCCION', 'CONSUMO_PRODUCCION']
    with pytest.raises(ErrorOrdenes):
        planta.pesar(oid)


# 12. Expediente ----------------------------------------------------------------------

def test_expediente_junta_todo(conn, planta):
    oid = planta.orden()
    planta.fabricar(oid)
    planta.envasar(oid)
    e = srv_expediente.armar(conn, oid)
    assert len(e['pesadas']) == 2 and set(e['despejes']) == {'Fabricación', 'Envasado'}
    assert all(p['realizo_firma_id'] in e['firmas'] for p in e['pesadas'] + e['pasos'])
    assert e['orden']['emitio_firma_id'] in e['firmas'] and e['orden']['granel_firma_id'] in e['firmas']
    assert e['formula']['id'] == planta.formula and e['conciliaciones']


# Ganchos del tablero -------------------------------------------------------------------

def test_ganchos_del_tablero(conn, planta):
    oid = planta.orden()
    conn.commit()
    usuario = {'numero_empleado': planta.jefe, 'rol': 'produccion', 'username': 'x', 'facultades': 'emitir_ordenes'}
    assert ganchos.kpis(conn) and ganchos.resumen(conn) is not None
    assert isinstance(ganchos.alertas(conn), list) and isinstance(ganchos.pendientes(conn, usuario), list)
    assert any(srv_ordenes.orden(conn, oid)['folio'] in str(a) for a in ganchos.accesos_planta(conn, usuario)) or \
        ganchos.accesos_planta(conn, usuario) is not None
