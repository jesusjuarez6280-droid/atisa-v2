"""Reglas del módulo Equipos (diseño 05-equipos-calibracion, sección 9), probadas sin pantallas."""
import itertools
from datetime import date, timedelta

import pytest

from fabricas import conexion, crear_usuario, firma
from nucleo import equipos as base_equipos
from nucleo import registro
from modulos.equipos import ganchos, reportes
from modulos.equipos.servicios import calibraciones, catalogo, impactos, mantenimiento, verificaciones
from modulos.equipos.servicios.comun import ErrorEquipos, sumar_meses

PIN = '2580'
_n = itertools.count(1)


def _dias(n):
    return (date.today() + timedelta(days=n)).isoformat()


HOY = date.today().isoformat()


@pytest.fixture(scope='module', autouse=True)
def _cerrar_impactos_al_final():
    """Las evaluaciones de impacto que dejan estas pruebas no deben bloquear lotes de otras pruebas."""
    yield
    c = conexion()
    c.execute("UPDATE eq_impactos SET estatus = 'EVALUADO', conclusion = 'Prueba' WHERE estatus = 'PENDIENTE' "
              "AND equipo_id IN (SELECT id FROM equipos WHERE codigo LIKE '%-S%')")
    c.commit()
    c.close()


@pytest.fixture
def conn():
    c = conexion()
    yield c
    c.rollback()
    c.close()


@pytest.fixture
def gente(conn):
    """Un técnico de mantenimiento y una jefa de calidad con la facultad de dictaminar."""
    n = next(_n)
    tecnico = crear_usuario(conn, f'eq_tec{n}', rol='mantenimiento', pin=PIN)
    jefa = crear_usuario(conn, f'eq_jefa{n}', rol='calidad', facultades='dictaminar_equipos', pin=PIN)
    return {'tecnico': tecnico, 'jefa': jefa}


def _f(conn, numero, significado='Realizó'):
    return firma(conn, numero, PIN, significado, modulo='equipos', entidad='equipos')


def _codigo(prefijo='EQ'):
    return f'{prefijo}-S{next(_n):04d}'


def _lab(conn, aprobado=True):
    lab = calibraciones.crear_laboratorio(conn, nombre=f'Laboratorio {next(_n)}', usuario='p')
    if aprobado:
        conn.execute("UPDATE proveedores SET estatus = 'Aprobado' WHERE id = ?", (lab,))
    return lab


def _instrumento(conn, frecuencia=12, verificacion=0, **datos):
    eid = catalogo.alta(conn, datos={'codigo': _codigo('BAS'), 'nombre': 'Báscula', 'tipo': 'Báscula',
                                     'es_instrumento': 1, 'verificacion_diaria': verificacion, **datos}, usuario='p')
    if frecuencia:
        catalogo.guardar_programa(conn, eid, 'CALIBRACION', frecuencia_meses=frecuencia, usuario='p')
    return eid


def _calibrar(conn, gente, eid, encontrado=20.01, dejado=None, fecha=None, dictamen=True, lab=None):
    c = calibraciones.registrar(conn, eid, fecha=fecha or HOY, modalidad='Externa', proveedor_id=lab or _lab(conn),
                                certificado='C-1', con_certificado_pdf=True, firma_id=_f(conn, gente['tecnico']),
                                usuario='p', puntos=[{'nominal': 20, 'tolerancia': 0.05, 'unidad': 'kg',
                                                      'encontrado': encontrado, 'dejado': dejado}])
    if dictamen is not None:
        calibraciones.dictaminar(conn, c['id'], es_apto=dictamen, motivo=None if dictamen else 'No cumple',
                                 firma_id=_f(conn, gente['jefa'], 'Aprobó'), usuario='p')
    return c['id']


def _pesa(conn, gente, vencida=False):
    pid = catalogo.alta(conn, datos={'codigo': _codigo('PES'), 'nombre': 'Pesa 20 kg', 'tipo': 'Pesa patrón'},
                        ficha={'valor_nominal': 20, 'unidad_nominal': 'kg', 'clase': 'M1'}, usuario='p')
    catalogo.guardar_programa(conn, pid, 'CALIBRACION', frecuencia_meses=12, usuario='p')
    _calibrar(conn, gente, pid, encontrado=20.0, fecha=_dias(-400) if vencida else HOY)
    return pid


def _bascula_diaria(conn, gente):
    eid = _instrumento(conn, verificacion=1)
    _calibrar(conn, gente, eid)
    catalogo.guardar_puntos(conn, eid, 'VERIFICACION', [{'nombre': 'Carga alta', 'nominal': 20, 'tolerancia': 0.05,
                                                         'unidad': 'kg', 'requiere_pesa': 1}], usuario='p')
    return eid, catalogo.puntos(conn, eid, 'VERIFICACION')[0]['id']


def _estatus(conn, eid):
    return conn.execute('SELECT * FROM equipos WHERE id = ?', (eid,)).fetchone()


# --- 1. Alta ---------------------------------------------------------------------

def test_instrumento_con_programa_nace_por_calibrar_hasta_su_primera_calibracion_apta(conn, gente):
    eid = _instrumento(conn)
    e = _estatus(conn, eid)
    assert e['estatus'] == 'FUERA_DE_SERVICIO'
    assert catalogo.causa_actual(conn, e) == 'POR_CALIBRAR'
    assert not calibraciones.apto(conn, eid)['apto']
    with pytest.raises(ErrorEquipos, match='por calibrar'):
        catalogo.regresar_a_servicio(conn, eid, motivo='x', firma_id=_f(conn, gente['jefa'], 'Aprobó'), usuario='p')
    _calibrar(conn, gente, eid)
    e = _estatus(conn, eid)
    assert e['estatus'] == 'EN_SERVICIO' and e['vence_calibracion'] == sumar_meses(HOY, 12)
    assert calibraciones.apto(conn, eid)['apto']


def test_instrumento_sin_programa_sale_en_alertas(conn):
    antes = ganchos._sin_programa(conn)
    _instrumento(conn, frecuencia=None)
    assert ganchos._sin_programa(conn) == antes + 1
    assert any('sin programa de calibración' in a['detalle'] for a in ganchos.alertas(conn))


def test_pesa_patron_pide_valor_nominal_y_es_instrumento(conn):
    with pytest.raises(ErrorEquipos, match='valor nominal'):
        catalogo.alta(conn, datos={'codigo': _codigo('PES'), 'nombre': 'Pesa', 'tipo': 'Pesa patrón'}, usuario='p')
    pid = catalogo.alta(conn, datos={'codigo': _codigo('PES'), 'nombre': 'Pesa', 'tipo': 'Pesa patrón'},
                        ficha={'valor_nominal': '5'}, usuario='p')
    assert _estatus(conn, pid)['es_instrumento'] == 1


# --- 2. Registrar calibración --------------------------------------------------------

def test_cada_punto_se_calcula_como_se_encontro_y_como_se_dejo(conn, gente):
    eid = _instrumento(conn)
    c = calibraciones.registrar(conn, eid, fecha=HOY, modalidad='Externa', proveedor_id=_lab(conn), certificado='X',
                                con_certificado_pdf=True, ajustado=True, firma_id=_f(conn, gente['tecnico']), usuario='p',
                                puntos=[{'nominal': 20, 'tolerancia': 0.05, 'unidad': 'kg', 'encontrado': 20.08,
                                         'dejado': 20.01},
                                        {'nominal': 5, 'tolerancia': 0.05, 'unidad': 'kg', 'encontrado': 5.05}])
    puntos = calibraciones.puntos_de(conn, c['id'])
    assert [(p['conforme_encontrado'], p['conforme_dejado']) for p in puntos] == [(0, 1), (1, 1)]
    assert puntos[1]['dejado'] == 5.05          # vacío = igual a como se encontró
    cal = calibraciones.calibracion(conn, c['id'])
    assert (cal['conforme_encontrado'], cal['conforme_dejado'], cal['folio'][:4]) == (0, 1, 'CAL-')
    assert any('fuera de tolerancia' in a for a in c['avisos'])


def test_externa_sin_certificado_falla_y_laboratorio_no_aprobado_avisa(conn, gente):
    eid = _instrumento(conn)
    punto = [{'nominal': 20, 'tolerancia': 0.05, 'unidad': 'kg', 'encontrado': 20}]
    fid = _f(conn, gente['tecnico'])
    with pytest.raises(ErrorEquipos, match='certificado'):
        calibraciones.registrar(conn, eid, fecha=HOY, modalidad='Externa', proveedor_id=_lab(conn), certificado='',
                                con_certificado_pdf=True, firma_id=fid, usuario='p', puntos=punto)
    with pytest.raises(ErrorEquipos, match='PDF'):
        calibraciones.registrar(conn, eid, fecha=HOY, modalidad='Externa', proveedor_id=_lab(conn), certificado='A1',
                                con_certificado_pdf=False, firma_id=fid, usuario='p', puntos=punto)
    with pytest.raises(ErrorEquipos, match='futura'):
        calibraciones.registrar(conn, eid, fecha=_dias(3), modalidad='Externa', proveedor_id=_lab(conn),
                                certificado='A1', con_certificado_pdf=True, firma_id=fid, usuario='p', puntos=punto)
    c = calibraciones.registrar(conn, eid, fecha=HOY, modalidad='Externa', proveedor_id=_lab(conn, aprobado=False),
                                certificado='A1', con_certificado_pdf=True, firma_id=fid, usuario='p', puntos=punto)
    assert any('no está aprobado' in a for a in c['avisos'])


def test_interna_con_patron_vencido_falla(conn, gente):
    eid = _instrumento(conn)
    vencida = _pesa(conn, gente, vencida=True)
    vigente = _pesa(conn, gente)
    punto = [{'nominal': 20, 'tolerancia': 0.05, 'unidad': 'kg', 'encontrado': 20}]
    with pytest.raises(ErrorEquipos, match='patrones'):
        calibraciones.registrar(conn, eid, fecha=HOY, modalidad='Interna', patrones=[],
                                firma_id=_f(conn, gente['tecnico']), usuario='p', puntos=punto)
    with pytest.raises(ErrorEquipos, match='no está apto'):
        calibraciones.registrar(conn, eid, fecha=HOY, modalidad='Interna', patrones=[vencida],
                                firma_id=_f(conn, gente['tecnico']), usuario='p', puntos=punto)
    c = calibraciones.registrar(conn, eid, fecha=HOY, modalidad='Interna', patrones=[vigente],
                                firma_id=_f(conn, gente['tecnico']), usuario='p', puntos=punto)
    assert calibraciones.calibracion(conn, c['id'])['patrones'] == _estatus(conn, vigente)['codigo']
    assert vencida not in [p['id'] for p in calibraciones.patrones_aptos(conn)]


def test_anular_solo_antes_del_dictamen(conn, gente):
    eid = _instrumento(conn)
    cid = _calibrar(conn, gente, eid, dictamen=None)
    calibraciones.anular(conn, cid, motivo='Capturé mal', firma_id=_f(conn, gente['tecnico'], 'Anuló'), usuario='p')
    assert calibraciones.calibracion(conn, cid)['estatus'] == 'ANULADA'
    cid = _calibrar(conn, gente, eid)
    with pytest.raises(ErrorEquipos, match='aún no se dictamina'):
        calibraciones.anular(conn, cid, motivo='x', firma_id=_f(conn, gente['tecnico'], 'Anuló'), usuario='p')


# --- 3. Dictamen -----------------------------------------------------------------------

def test_apto_fija_el_vencimiento_y_no_apto_deja_fuera(conn, gente):
    eid = _instrumento(conn, frecuencia=6)
    _calibrar(conn, gente, eid, fecha=_dias(-10))
    e = _estatus(conn, eid)
    assert e['estatus'] == 'EN_SERVICIO' and e['vence_calibracion'] == sumar_meses(_dias(-10), 6)
    _calibrar(conn, gente, eid, encontrado=20.2, dictamen=False)
    e = _estatus(conn, eid)
    assert e['estatus'] == 'FUERA_DE_SERVICIO' and catalogo.causa_actual(conn, e) == 'CALIBRACION'
    # La siguiente calibración apta lo regresa a servicio
    _calibrar(conn, gente, eid, encontrado=20.2, dejado=20.0)
    assert _estatus(conn, eid)['estatus'] == 'EN_SERVICIO'


def test_no_puede_ser_apto_si_no_cumple_como_se_dejo(conn, gente):
    eid = _instrumento(conn)
    cid = _calibrar(conn, gente, eid, encontrado=20.2, dictamen=None)
    with pytest.raises(ErrorEquipos, match='como se dejaron'):
        calibraciones.dictaminar(conn, cid, es_apto=True, firma_id=_f(conn, gente['jefa'], 'Aprobó'), usuario='p')
    with pytest.raises(ErrorEquipos, match='por qué no es apto'):
        calibraciones.dictaminar(conn, cid, es_apto=False, firma_id=_f(conn, gente['jefa'], 'Rechazó'), usuario='p')


def test_dictaminar_sin_la_facultad_falla(conn, gente):
    eid = _instrumento(conn)
    cid = _calibrar(conn, gente, eid, dictamen=None)
    with pytest.raises(ErrorEquipos, match='dictaminar equipos'):
        calibraciones.dictaminar(conn, cid, es_apto=True, firma_id=_f(conn, gente['tecnico'], 'Aprobó'), usuario='p')
    assert calibraciones.calibracion(conn, cid)['estatus'] == 'POR_DICTAMINAR'


# --- 4. Fuera de tolerancia → evaluación de impacto ------------------------------------------

@pytest.fixture
def consultas_de_prueba(monkeypatch):
    """Simula lo que ofrecerán Órdenes (lotes_por_equipo) y Calidad (analisis_por_equipo)."""
    usados = {'lotes': [], 'analisis': []}

    def lotes_por_equipo(conn, equipo_id, desde):
        return [u for u in usados['lotes'] if u['equipo_id'] == equipo_id and u['fecha'] >= desde]

    def analisis_por_equipo(conn, equipo_id, desde):
        return [u for u in usados['analisis'] if u['equipo_id'] == equipo_id and u['fecha'] >= desde]

    monkeypatch.setitem(registro.CONSULTAS, 'lotes_por_equipo', ('ordenes', lotes_por_equipo))
    monkeypatch.setitem(registro.CONSULTAS, 'analisis_por_equipo', ('calidad', analisis_por_equipo))
    return usados


def test_fuera_de_tolerancia_bloquea_los_lotes_de_la_ventana_hasta_evaluar(conn, gente, consultas_de_prueba):
    eid, punto = _bascula_diaria(conn, gente)
    pesa = _pesa(conn, gente)
    # Verificaciones conformes: la última buena es hoy (las de días pasados se insertan a mano)
    fid = _f(conn, gente['tecnico'])
    conn.execute('INSERT INTO eq_verificaciones (equipo_id, fecha, conforme, realizo_firma_id) VALUES (?, ?, 1, ?)',
                 (eid, _dias(-20), fid))
    verificaciones.verificar(conn, eid, lecturas={punto: {'lectura': 20.01, 'pesa_id': pesa}}, firma_id=fid,
                             usuario='p')
    consultas_de_prueba['lotes'] += [
        {'equipo_id': eid, 'lote_id': 9001, 'numero_lote': 'L-VIEJO', 'fecha': _dias(-30)},
        {'equipo_id': eid, 'lote_id': 9002, 'numero_lote': 'L-HOY', 'folio': 'OP-2026-0099', 'fecha': HOY}]
    consultas_de_prueba['analisis'] += [{'equipo_id': eid, 'lote_id': 9003, 'folio': 'AN-2026-0001', 'fecha': HOY}]
    cid = _calibrar(conn, gente, eid, encontrado=20.3, dejado=20.0)
    i = conn.execute("SELECT * FROM eq_impactos WHERE origen = 'CALIBRACION' AND origen_id = ?", (cid,)).fetchone()
    assert (i['desde'], i['hasta'], i['estatus']) == (HOY, HOY, 'PENDIENTE')
    usos = impactos.usos_en_ventana(conn, eid, i['desde'], i['hasta'])
    assert impactos.lote_ids(usos) == [9002, 9003]
    assert ganchos.verificaciones_liberacion(conn, 9002) and ganchos.verificaciones_liberacion(conn, 9003)
    assert ganchos.verificaciones_liberacion(conn, 9001) == []
    assert any(a['url'].endswith(f'/impacto/{i["id"]}') for a in ganchos.alertas(conn))
    with pytest.raises(ErrorEquipos, match='dictaminar equipos'):
        impactos.evaluar(conn, i['id'], conclusion='x', firma_id=_f(conn, gente['tecnico']), usuario='p')
    impactos.evaluar(conn, i['id'], conclusion='Evaluado en DES-2026-0012', firma_id=_f(conn, gente['jefa'], 'Aprobó'),
                     usuario='p')
    assert ganchos.verificaciones_liberacion(conn, 9002) == []
    assert not any(a['url'].endswith(f'/impacto/{i["id"]}') for a in ganchos.alertas(conn))


def test_sin_verificacion_diaria_la_ventana_va_desde_la_ultima_calibracion_buena(conn, gente):
    eid = _instrumento(conn)
    _calibrar(conn, gente, eid, fecha=_dias(-100))
    cid = _calibrar(conn, gente, eid, encontrado=20.3, dejado=20.0)
    i = conn.execute("SELECT * FROM eq_impactos WHERE origen = 'CALIBRACION' AND origen_id = ?", (cid,)).fetchone()
    assert (i['desde'], i['hasta']) == (_dias(-100), HOY)


def test_si_una_consulta_truena_se_bloquea_la_liberacion(conn, gente, monkeypatch):
    def rota(conn, equipo_id, desde):
        raise RuntimeError('rota')
    monkeypatch.setitem(registro.CONSULTAS, 'lotes_por_equipo', ('ordenes', rota))
    eid = _instrumento(conn)
    _calibrar(conn, gente, eid, encontrado=20.3, dejado=20.0)
    assert any('No se pudo revisar' in m for m in ganchos.verificaciones_liberacion(conn, 123456))


# --- 5. Verificación del día ---------------------------------------------------------------

def test_verificacion_conforme_deja_la_bascula_apta_hoy(conn, gente):
    eid, punto = _bascula_diaria(conn, gente)
    pesa = _pesa(conn, gente)
    assert not base_equipos.equipo_apto(conn, eid)['apto']
    assert eid in [e['id'] for e in verificaciones.pendientes_de_hoy(conn)]
    r = verificaciones.verificar(conn, eid, lecturas={punto: {'lectura': '20,03', 'pesa_id': pesa}},
                                 firma_id=_f(conn, gente['tecnico']), usuario='p')
    assert r['conforme'] == 1 and r['lecturas'][0]['error'] == pytest.approx(0.03)
    assert base_equipos.equipo_apto(conn, eid)['apto']
    assert eid not in [e['id'] for e in verificaciones.pendientes_de_hoy(conn)]
    with pytest.raises(ErrorEquipos, match='ya tiene su verificación'):
        verificaciones.verificar(conn, eid, lecturas={punto: {'lectura': 20, 'pesa_id': pesa}},
                                 firma_id=_f(conn, gente['tecnico']), usuario='p')


def test_pesa_vencida_no_se_puede_elegir(conn, gente):
    eid, punto = _bascula_diaria(conn, gente)
    vencida = _pesa(conn, gente, vencida=True)
    assert vencida not in [p['id'] for p in verificaciones.pesas_aptas(conn)]
    with pytest.raises(ErrorEquipos, match='no está apta'):
        verificaciones.verificar(conn, eid, lecturas={punto: {'lectura': 20, 'pesa_id': vencida}},
                                 firma_id=_f(conn, gente['tecnico']), usuario='p')
    with pytest.raises(ErrorEquipos, match='Elige la pesa'):
        verificaciones.verificar(conn, eid, lecturas={punto: {'lectura': 20}}, firma_id=_f(conn, gente['tecnico']),
                                 usuario='p')
    with pytest.raises(ErrorEquipos, match='nivelado'):
        verificaciones.verificar(conn, eid, lecturas={}, nivelada_limpia=False, firma_id=_f(conn, gente['tecnico']),
                                 usuario='p')


def test_dos_fallas_la_dejan_fuera_con_orden_correctiva_e_impacto(conn, gente):
    eid, punto = _bascula_diaria(conn, gente)
    pesa = _pesa(conn, gente)
    lect = {punto: {'lectura': 20.2, 'pesa_id': pesa}}
    r1 = verificaciones.verificar(conn, eid, lecturas=lect, firma_id=_f(conn, gente['tecnico']), usuario='p')
    assert (r1['conforme'], r1['repetir'], r1['fuera']) == (0, True, False)
    assert _estatus(conn, eid)['estatus'] == 'EN_SERVICIO'
    assert verificaciones.estado_hoy(conn, _estatus(conn, eid)) == 'fallo'
    r2 = verificaciones.verificar(conn, eid, lecturas=lect, firma_id=_f(conn, gente['tecnico']), usuario='p')
    assert r2['fuera'] and r2['ot_id'] and r2['impacto_id'] and r2['intento'] == 2
    e = _estatus(conn, eid)
    assert e['estatus'] == 'FUERA_DE_SERVICIO' and catalogo.causa_actual(conn, e) == 'VERIFICACION'
    ot = mantenimiento.ot(conn, r2['ot_id'])
    assert (ot['tipo'], ot['estatus'], ot['se_puede_usar']) == ('CORRECTIVO', 'ABIERTA', 0)
    assert impactos.impacto(conn, r2['impacto_id'])['origen'] == 'VERIFICACION'


# --- 6. Falla, fuera de servicio y regreso -------------------------------------------------------

def test_falla_que_impide_usarlo_bloquea_al_instante_y_regresar_pide_todo(conn, gente):
    eid = _instrumento(conn)
    _calibrar(conn, gente, eid, fecha=_dias(-5))
    r = mantenimiento.reportar_falla(conn, eid, descripcion='No enciende', se_puede_usar='0',
                                     firma_id=_f(conn, gente['tecnico']), usuario='p')
    assert r['fuera'] and _estatus(conn, eid)['estatus'] == 'FUERA_DE_SERVICIO'
    assert not base_equipos.equipo_apto(conn, eid)['apto']
    with pytest.raises(ErrorEquipos, match='dictaminar equipos'):
        catalogo.regresar_a_servicio(conn, eid, motivo='Listo', firma_id=_f(conn, gente['tecnico']), usuario='p')
    with pytest.raises(ErrorEquipos, match='órdenes de trabajo abiertas'):
        catalogo.regresar_a_servicio(conn, eid, motivo='Listo', firma_id=_f(conn, gente['jefa'], 'Aprobó'), usuario='p')
    mantenimiento.terminar_ot(conn, r['ot_id'], trabajo_realizado='Cambio de celda', afecta_medicion=True,
                              firma_id=_f(conn, gente['tecnico']), usuario='p')
    with pytest.raises(ErrorEquipos, match='calibración o verificación conforme posterior'):
        catalogo.regresar_a_servicio(conn, eid, motivo='Listo', firma_id=_f(conn, gente['jefa'], 'Aprobó'), usuario='p')
    _calibrar(conn, gente, eid)             # calibración apta posterior (fuera por falla: no regresa sola)
    assert _estatus(conn, eid)['estatus'] == 'FUERA_DE_SERVICIO'
    catalogo.regresar_a_servicio(conn, eid, motivo='Reparado y calibrado', firma_id=_f(conn, gente['jefa'], 'Aprobó'),
                                 usuario='p')
    assert _estatus(conn, eid)['estatus'] == 'EN_SERVICIO'
    hist = conn.execute('SELECT anterior, nuevo, causa FROM eq_estatus_historial WHERE equipo_id = ? ORDER BY id',
                        (eid,)).fetchall()
    assert [tuple(h) for h in hist][-2:] == [('EN_SERVICIO', 'FUERA_DE_SERVICIO', 'FALLA'),
                                             ('FUERA_DE_SERVICIO', 'EN_SERVICIO', None)]


def test_falla_que_permite_usarlo_no_bloquea(conn, gente):
    eid = _instrumento(conn)
    _calibrar(conn, gente, eid)
    r = mantenimiento.reportar_falla(conn, eid, descripcion='Ruido raro', se_puede_usar='1',
                                     firma_id=_f(conn, gente['tecnico']), usuario='p')
    assert not r['fuera'] and _estatus(conn, eid)['estatus'] == 'EN_SERVICIO'
    with pytest.raises(ErrorEquipos, match='se puede seguir usando'):
        mantenimiento.reportar_falla(conn, eid, descripcion='x', se_puede_usar=None, firma_id=_f(conn, gente['tecnico']),
                                     usuario='p')


def test_baja_cancela_ordenes_y_conserva_el_historial(conn, gente):
    eid = _instrumento(conn)
    _calibrar(conn, gente, eid)
    r = mantenimiento.reportar_falla(conn, eid, descripcion='Roto', se_puede_usar='1',
                                     firma_id=_f(conn, gente['tecnico']), usuario='p')
    with pytest.raises(ErrorEquipos, match='dictaminar equipos'):
        catalogo.dar_de_baja(conn, eid, motivo='Obsoleto', firma_id=_f(conn, gente['tecnico']), usuario='p')
    catalogo.dar_de_baja(conn, eid, motivo='Obsoleto', firma_id=_f(conn, gente['jefa'], 'Aprobó'), usuario='p')
    assert _estatus(conn, eid)['estatus'] == 'BAJA'
    assert mantenimiento.ot(conn, r['ot_id'])['estatus'] == 'CANCELADA'
    assert conn.execute('SELECT COUNT(*) FROM eq_calibraciones WHERE equipo_id = ?', (eid,)).fetchone()[0] == 1
    with pytest.raises(Exception):
        conn.execute('DELETE FROM eq_calibraciones WHERE equipo_id = ?', (eid,))


# --- 7. Mantenimiento preventivo -------------------------------------------------------------

def test_terminar_preventivo_fija_el_proximo_y_vencido_solo_avisa(conn, gente):
    eid = catalogo.alta(conn, datos={'codigo': _codigo('MEZ'), 'nombre': 'Mezcladora', 'tipo': 'Mezcladora'}, usuario='p')
    catalogo.guardar_programa(conn, eid, 'MANTENIMIENTO', frecuencia_meses=3, tareas='Engrasar\nRevisar bandas',
                              proximo=_dias(-2), usuario='p')
    r = base_equipos.equipo_apto(conn, eid)
    assert r['apto'] and r['avisos']                       # vencido: solo avisa
    assert eid in [p['equipo_id'] for p in mantenimiento.por_hacer(conn)]
    ot_id = mantenimiento.abrir_ot(conn, eid, tipo='PREVENTIVO', usuario='p')
    assert eid not in [p['equipo_id'] for p in mantenimiento.por_hacer(conn)]
    with pytest.raises(ErrorEquipos, match='ya tiene una orden preventiva'):
        mantenimiento.abrir_ot(conn, eid, tipo='PREVENTIVO', usuario='p')
    with pytest.raises(ErrorEquipos, match='Revisar bandas'):
        mantenimiento.terminar_ot(conn, ot_id, tareas=[{'tarea': 'Engrasar', 'hecha': True}],
                                  firma_id=_f(conn, gente['tecnico']), usuario='p')
    res = mantenimiento.terminar_ot(conn, ot_id, tareas=[{'tarea': 'Engrasar', 'hecha': True},
                                                         {'tarea': 'Revisar bandas', 'nota': 'Sin refacción'}],
                                    firma_id=_f(conn, gente['tecnico']), usuario='p')
    assert res['proximo'] == sumar_meses(HOY, 3) == _estatus(conn, eid)['vence_mantenimiento']
    assert mantenimiento.tareas_hechas(mantenimiento.ot(conn, ot_id))[1] == {'tarea': 'Revisar bandas', 'hecha': False,
                                                                             'nota': 'Sin refacción'}


# --- 8. Rondas de caldera (rango mínimo–máximo) ------------------------------------------------

def test_ronda_de_caldera_marca_lo_que_sale_de_rango(conn, gente):
    eid = catalogo.alta(conn, datos={'codigo': _codigo('CAL'), 'nombre': 'Caldera', 'tipo': 'Caldera',
                                     'verificacion_diaria': 1}, usuario='p')
    catalogo.guardar_puntos(conn, eid, 'VERIFICACION', [
        {'nombre': 'Presión', 'minimo': 4, 'maximo': 6, 'unidad': 'kg/cm²'},
        {'nombre': 'Nivel de agua', 'minimo': 50, 'unidad': '%'}], usuario='p')
    pres, nivel = [p['id'] for p in catalogo.puntos(conn, eid, 'VERIFICACION')]
    r = verificaciones.verificar(conn, eid, lecturas={pres: {'lectura': 6.5}, nivel: {'lectura': 70}},
                                 firma_id=_f(conn, gente['tecnico']), usuario='p')
    assert [x['conforme'] for x in r['lecturas']] == [0, 1] and r['repetir']
    r = verificaciones.verificar(conn, eid, lecturas={pres: {'lectura': 6}, nivel: {'lectura': 50}},
                                 firma_id=_f(conn, gente['tecnico']), usuario='p')
    assert r['conforme'] == 1


def test_puntos_mal_capturados_se_rechazan_y_los_iguales_no_se_duplican(conn):
    eid = catalogo.alta(conn, datos={'codigo': _codigo('BAL'), 'nombre': 'Balanza', 'tipo': 'Balanza analítica'},
                        usuario='p')
    with pytest.raises(ErrorEquipos, match='tolerancia'):
        catalogo.guardar_puntos(conn, eid, 'CALIBRACION', [{'nominal': 100, 'unidad': 'g'}], usuario='p')
    with pytest.raises(ErrorEquipos, match='mínimo es mayor'):
        catalogo.guardar_puntos(conn, eid, 'VERIFICACION', [{'minimo': 5, 'maximo': 1, 'unidad': 'g'}], usuario='p')
    puntos = [{'nominal': 100, 'tolerancia': 0.001, 'unidad': 'g'}, {'nominal': '', 'unidad': ''}]
    catalogo.guardar_puntos(conn, eid, 'CALIBRACION', puntos, usuario='p')
    primero = catalogo.puntos(conn, eid, 'CALIBRACION')[0]['id']
    catalogo.guardar_puntos(conn, eid, 'CALIBRACION', puntos, usuario='p')
    assert [p['id'] for p in catalogo.puntos(conn, eid, 'CALIBRACION')] == [primero]


# --- 9. Calibración vigente en una fecha pasada ---------------------------------------------------

def test_calibracion_vigente_en_una_fecha_pasada(conn, gente):
    eid = _instrumento(conn)
    vieja = _calibrar(conn, gente, eid, fecha=_dias(-400))
    nueva = _calibrar(conn, gente, eid, fecha=_dias(-20))
    assert calibraciones.calibracion_vigente(conn, eid, _dias(-500)) is None
    v = registro.consulta('calibracion_vigente', conn, eid, _dias(-300))
    assert v['id'] == vieja and v['vigente'] is True and v['proxima'] == sumar_meses(_dias(-400), 12)
    assert calibraciones.calibracion_vigente(conn, eid, _dias(-25))['vigente'] is False   # la vieja ya había vencido
    assert calibraciones.calibracion_vigente(conn, eid, HOY)['id'] == nueva
    # Dictaminar tarde la vieja no pisa el vencimiento de la nueva
    assert _estatus(conn, eid)['vence_calibracion'] == sumar_meses(_dias(-20), 12)


# --- 10. Programa anual -------------------------------------------------------------------------

def test_programa_anual_muestra_los_meses_y_lo_hecho(conn, gente):
    eid = _instrumento(conn, frecuencia=6)
    _calibrar(conn, gente, eid, fecha=f'{date.today().year}-01-15' if date.today().month > 1 else HOY)
    fila = next(f for f in reportes.programa_anual(conn, date.today().year) if f['id'] == eid)
    hecho = [m for m, c in fila['cal'].items() if c['hecho']]
    toca = sorted(m for m, c in fila['cal'].items() if c['toca'])
    assert hecho == [1] if date.today().month > 1 else hecho == [date.today().month]
    assert len(toca) == 2 and toca[1] - toca[0] == 6
    assert reportes.excel_programa([fila], date.today().year).getvalue()[:2] == b'PK'


def test_sumar_meses_se_queda_en_el_ultimo_dia():
    assert sumar_meses('2026-01-31', 1) == '2026-02-28'
    assert sumar_meses('2026-03-15', -6) == '2025-09-15'
    assert sumar_meses('2024-02-29', 12) == '2025-02-28'


# --- Ganchos -----------------------------------------------------------------------------------

def test_kpis_y_pendientes_por_rol(conn, gente):
    eid = _instrumento(conn)
    _calibrar(conn, gente, eid, dictamen=None)
    titulos = [k['titulo'] for k in ganchos.kpis(conn)]
    assert 'Instrumentos con calibración vigente' in titulos and 'Verificaciones de hoy' in titulos
    jefa = {'roles': ['calidad'], 'facultades': ['dictaminar_equipos']}
    assert any('Dictaminar' in p['texto'] for p in ganchos.pendientes(conn, jefa))
    assert ganchos.pendientes(conn, {'roles': ['rh'], 'facultades': []}) == []
    assert ganchos.accesos_planta(conn, {'roles': ['rh']}) == []
    assert [a['titulo'] for a in ganchos.accesos_planta(conn, {'roles': ['operador']})] == [
        'Verificaciones del día', 'Reportar falla de equipo']
