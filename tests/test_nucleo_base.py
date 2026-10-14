"""Base común: registro de módulos, roles/áreas/facultades, PIN, folios, archivos y parámetros
(00-base-comun.md, sección 11 puntos 2, 3, 4 y 7).

Estas pruebas corren junto con los módulos reales de modulos/: nunca suponen que el
módulo de prueba es el único que aporta alertas, kpis, pendientes, enlaces o bloqueos."""
import io
import os
import sqlite3

import pytest

import erp
from fabricas import crear_usuario, crear_material, crear_lote, firma, login, PASSWORD
from nucleo import firmas, folios, parametros, registro, menu, esquema
from nucleo.firmas import FirmaInvalida
from nucleo.inventario import ErrorInventario
import nucleo.inventario as inventario

import modulo_prueba
from modulo_prueba.esquema import CORRIDAS


def _sesion(numero):
    return {'username': 'x', 'numero_empleado': numero, 'ip': None}


# --- 2. Registro de módulos ----------------------------------------------------
def test_modulo_se_registra_con_menu_permisos_y_parametros():
    assert registro.modulo_instalado('prueba')
    assert erp.PERMISOS['prueba'] == ['admin', 'almacen']
    assert 'prueba.inicio' in erp.app.view_functions
    assert parametros.parametro('prueba.dias_aviso') == '30'
    logistica = next(d for d in erp.NAV_DEPARTAMENTOS if d['key'] == 'logistica')
    assert logistica['nombre'] == 'Almacén y Logística'
    assert ('/prueba', 'Prueba', 'ri-flask-line', '/prueba/') in logistica['modulos']
    # Las entradas de siempre quedan antes que las de los módulos
    assert logistica['modulos'][0][0] == '/gasolina'


def test_menu_solo_para_sus_roles():
    def rutas(roles):
        return {m[0] for d in menu.construir_nav(roles) for m in d['modulos_visibles']}
    assert '/prueba' in rutas(['almacen'])
    assert '/prueba' in rutas(['admin'])
    assert '/prueba' not in rutas(['rh'])
    assert '/prueba' in rutas(['calidad', 'almacen'])   # por área adicional


def test_ruta_del_modulo_niega_a_otros_roles(client, db):
    crear_usuario(db, 'reg_rh', rol='rh', numero='7101')
    crear_usuario(db, 'reg_alm', rol='almacen', numero='7102')
    login(client, 'reg_rh', PASSWORD)
    r = client.get('/prueba')
    assert r.status_code == 302 and '/prueba' not in r.headers['Location']
    client.get('/logout')
    login(client, 'reg_alm', PASSWORD)
    assert client.get('/prueba').status_code == 200


def test_migraciones_corren_una_sola_vez(db):
    assert CORRIDAS == {'v1': 1, 'v2': 1}
    assert esquema.version_actual(db, 'prueba') == 2
    assert esquema.version_actual(db, 'nucleo') == 1
    conn = erp.get_db_connection()
    try:
        esquema.aplicar_migraciones(conn)
        esquema.aplicar_migraciones(conn)
    finally:
        conn.close()
    assert CORRIDAS == {'v1': 1, 'v2': 1}


def test_migracion_que_falla_se_revierte_y_no_avanza(tmp_path):
    conn = sqlite3.connect(str(tmp_path / 'm.db'))

    def buena(c):
        c.execute('CREATE TABLE x_a (id INTEGER)')

    def mala(c):
        c.execute('CREATE TABLE x_b (id INTEGER)')
        raise RuntimeError('falla')
    with pytest.raises(RuntimeError):
        esquema.aplicar_migraciones_de(conn, 'x', [(1, buena), (2, mala)])
    assert esquema.version_actual(conn, 'x') == 1
    tablas = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'x_a' in tablas and 'x_b' not in tablas
    conn.close()


def test_choques_al_registrar_son_error_al_arrancar():
    base = dict(clave='otro', nombre='Otro')
    with pytest.raises(registro.ErrorRegistro):     # permiso repetido
        registro.registrar_modulo(erp.app, registro.Modulo(**base, permisos={'prueba': ['admin']}))
    with pytest.raises(registro.ErrorRegistro):     # enlace repetido
        registro.registrar_modulo(erp.app, registro.Modulo(**base, enlaces={'prueba_cosa': lambda: '/'}))
    with pytest.raises(registro.ErrorRegistro):     # rol inexistente
        registro.registrar_modulo(erp.app, registro.Modulo(**base, permisos={'otro': ['jefe']}))
    with pytest.raises(registro.ErrorRegistro):     # departamento de menú inexistente
        registro.registrar_modulo(erp.app, registro.Modulo(
            **base, permisos={'otro': ['admin']}, menu=[('ventas', '/otro', 'Otro', 'ri-x', '', 'otro')]))
    with pytest.raises(registro.ErrorRegistro):     # módulo repetido
        registro.registrar_modulo(erp.app, registro.Modulo(clave='prueba', nombre='Doble'))
    assert not registro.modulo_instalado('otro')


def test_ganchos_llegan_a_tablero_pendientes_planta_y_liberacion(client, db):
    crear_usuario(db, 'reg_admin', rol='admin', numero='7103')
    db.execute("INSERT INTO prb_cosas (nombre, bloquea) VALUES ('Bloqueadora', 1)")
    db.commit()
    login(client, 'reg_admin', PASSWORD)
    html = client.get('/tablero').get_data(as_text=True)
    assert 'Cosa bloqueada' in html and 'Cosas de prueba' in html
    assert 'Revisar cosa' in client.get('/pendientes').get_data(as_text=True)
    assert 'Mis pendientes' in client.get('/dashboard').get_data(as_text=True)
    assert 'Cosas de prueba' in client.get('/planta').get_data(as_text=True)
    assert 'Hay una cosa de prueba que bloquea' in registro.verificaciones_liberacion(db, 1)
    assert registro.consulta('prueba_contar', db) >= 1
    assert registro.consulta('no_existe', db, default=[]) == []
    with erp.app.test_request_context():
        assert registro.enlace('prueba_cosa', cosa_id=5) == '/prueba/5'
        assert registro.enlace('no_existe', origen='x') is None
    db.execute("DELETE FROM prb_cosas WHERE nombre = 'Bloqueadora'")
    db.commit()


def test_gancho_que_truena_no_tumba_la_pantalla(monkeypatch, db):
    def roto(conn):
        raise RuntimeError('roto')
    monkeypatch.setattr(modulo_prueba.MODULO, 'kpis', roto)
    monkeypatch.setattr(modulo_prueba.MODULO, 'verificaciones_liberacion', lambda c, l: 1 / 0)
    with erp.app.app_context():
        assert not any(k['titulo'] == 'Cosas de prueba' for k in registro.recolectar('kpis', db))
        # En liberación, un módulo que truena IMPIDE liberar.
        assert ('No se pudo revisar Módulo de prueba; avisa a sistemas.'
                in registro.verificaciones_liberacion(db, 1))


# --- 3. Roles, áreas adicionales y facultades ----------------------------------
def test_area_adicional_da_acceso_a_ambos_y_no_a_rh(client, db):
    crear_usuario(db, 'cal_alm', rol='calidad', numero='7201', roles_extra='almacen,admin')
    login(client, 'cal_alm', PASSWORD)
    assert client.get('/cinco_s').status_code == 200      # por calidad
    assert client.get('/prueba').status_code == 200       # por el área adicional almacen
    assert client.get('/empleados').status_code == 302    # RH no
    assert client.get('/configuracion').status_code == 302  # 'admin' nunca es área adicional


def test_ruta_con_facultad_niega_sin_ella(client, db):
    crear_usuario(db, 'sin_fac', rol='admin', numero='7202')
    login(client, 'sin_fac', PASSWORD)
    r = client.post('/prueba/liberar')
    assert r.status_code == 302 and '/prueba' not in r.headers['Location'].replace('/prueba/liberar', '')
    with client.session_transaction() as s:
        assert any('facultad' in m for _, m in s.get('_flashes', []))
    db.execute("UPDATE usuarios SET facultades = 'liberar_lotes' WHERE numero_empleado = '7202'")
    db.commit()
    r = client.post('/prueba/liberar')     # login_required relee las facultades en cada request
    assert r.status_code == 302 and r.headers['Location'].endswith('/prueba')


def test_operador_arranca_en_planta(client, db):
    crear_usuario(db, 'op_planta', rol='operador', numero='7203')
    r = login(client, 'op_planta', PASSWORD)
    assert r.headers['Location'].endswith('/planta')
    html = client.get('/planta').get_data(as_text=True)
    assert 'Procesos' in html and 'Crear mi PIN' in html


def test_rol_nuevo_sin_modulo_arranca_en_pendientes(client, db, monkeypatch):
    crear_usuario(db, 'mtto1', rol='mantenimiento', numero='7204')
    # Sin la pantalla de arranque que pondría un módulo real (Equipos) para este rol
    monkeypatch.delitem(menu.INICIOS, 'mantenimiento', raising=False)
    r = login(client, 'mtto1', PASSWORD)
    assert r.headers['Location'].endswith('/pendientes')
    # Un módulo que registra pantalla de arranque para el rol la gana
    monkeypatch.setitem(menu.INICIOS, 'mantenimiento', 'prueba.inicio')
    client.get('/logout')
    assert login(client, 'mtto1', PASSWORD).headers['Location'].endswith('/prueba')


def test_admin_asigna_areas_y_facultades_y_queda_en_bitacora(client, db):
    crear_usuario(db, 'adm_rf', rol='admin', numero='7205')
    crear_usuario(db, 'quien', rol='calidad', numero='7206')
    uid = db.execute("SELECT id FROM usuarios WHERE numero_empleado = '7206'").fetchone()[0]
    login(client, 'adm_rf', PASSWORD)
    client.post('/configuracion/roles-firmas', data={'user_id': uid, 'roles_extra': ['almacen', 'admin', 'calidad'],
                                                    'facultades': ['liberar_lotes', 'inventada']})
    u = db.execute("SELECT roles_extra, facultades FROM usuarios WHERE id = ?", (uid,)).fetchone()
    assert u['roles_extra'] == 'almacen' and u['facultades'] == 'liberar_lotes'
    assert db.execute("SELECT COUNT(*) FROM auditoria WHERE accion = 'Roles y facultades de firma' "
                      "AND detalle LIKE '%7206%'").fetchone()[0] == 1
    assert 'Roles y firmas' in client.get('/configuracion').get_data(as_text=True)


def test_empleado_con_rol_nuevo(client, db):
    crear_usuario(db, 'adm_emp', rol='admin', numero='7207')
    login(client, 'adm_emp', PASSWORD)
    client.post('/crear_empleado', data={'numero_empleado': '7208', 'nombre_completo': 'Ana Almacén', 'username': 'ana_alm',
                                         'password': 'clave-larga-1', 'rol': 'almacen'})
    fila = db.execute("SELECT rol FROM usuarios WHERE numero_empleado = '7208'").fetchone()
    assert fila and fila['rol'] == 'almacen'


# --- 4. PIN y firma ---------------------------------------------------------------
@pytest.mark.parametrize('pin', ['0000', '1111', '1234', '4321', '123456', '987654', '1212', '12', '1234567', 'abcd'])
def test_pin_trivial_rechazado(pin):
    assert firmas.pin_trivial(pin)


@pytest.mark.parametrize('pin', ['2580', '1397', '804613'])
def test_pin_aceptable(pin):
    assert firmas.pin_trivial(pin) is None


def test_crear_pin_en_mi_pin(client, db):
    crear_usuario(db, 'con_pin', rol='calidad', numero='7301')
    login(client, 'con_pin', PASSWORD)
    client.post('/mi-pin', data={'password_actual': PASSWORD, 'pin': '1234', 'pin_confirma': '1234'})
    assert not firmas.tiene_pin(db, '7301')
    client.post('/mi-pin', data={'password_actual': PASSWORD, 'pin': '2580', 'pin_confirma': '2580'})
    assert firmas.tiene_pin(db, '7301')
    h = db.execute("SELECT pin_hash FROM usuarios WHERE numero_empleado='7301'").fetchone()[0]
    assert '2580' not in h
    # El PIN nunca queda escrito en la bitácora
    assert db.execute("SELECT COUNT(*) FROM auditoria WHERE detalle LIKE '%2580%'").fetchone()[0] == 0


def test_crear_pin_en_la_tablet(client, db):
    crear_usuario(db, 'tablet1', rol='operador', numero='7302')
    crear_usuario(db, 'pesador', rol='operador', numero='7303')
    login(client, 'tablet1', PASSWORD)
    client.post('/planta/crear-pin', data={'numero_empleado': '7303', 'password': 'mala',
                                          'pin': '2580', 'pin_confirma': '2580'})
    assert not firmas.tiene_pin(db, '7303')
    client.post('/planta/crear-pin', data={'numero_empleado': '7303', 'password': PASSWORD,
                                          'pin': '2580', 'pin_confirma': '2580'})
    assert firmas.tiene_pin(db, '7303')


def test_firma_guarda_nombre_puesto_y_no_se_edita(db):
    crear_usuario(db, 'firmante', rol='calidad', numero='7304', pin='2580', puesto='Jefa de calidad',
                  facultades='liberar_lotes')
    fid = firma(db, '7304', '2580', 'Liberó', facultad='liberar_lotes', detalle='Lote X')
    f = firmas.firma(db, fid)
    assert f['nombre'] == 'Prueba firmante' and f['puesto'] == 'Jefa de calidad' and f['significado'] == 'Liberó'
    with pytest.raises(sqlite3.DatabaseError):
        db.execute('UPDATE firmas SET nombre = ? WHERE id = ?', ('otro', fid))
    with pytest.raises(sqlite3.DatabaseError):
        db.execute('DELETE FROM firmas WHERE id = ?', (fid,))
    db.rollback()


def test_cinco_fallos_bloquean_aunque_despues_venga_el_correcto(db):
    crear_usuario(db, 'bloq', rol='operador', numero='7305', pin='2580')
    for _ in range(5):
        with pytest.raises(FirmaInvalida):
            firma(db, '7305', '9999', 'Realizó')
        db.rollback()          # el que llama revierte; el contador sobrevive
    with pytest.raises(FirmaInvalida, match='bloqueado'):
        firma(db, '7305', '2580', 'Realizó')
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM auditoria WHERE accion = 'Firma rechazada' "
                      "AND detalle LIKE '%7305%'").fetchone()[0] >= 5
    # Pasado el bloqueo, vuelve a funcionar
    db.execute("UPDATE usuarios SET pin_bloqueado_hasta = '2000-01-01 00:00:00' WHERE numero_empleado = '7305'")
    db.commit()
    assert firma(db, '7305', '2580', 'Realizó')


def test_firma_respeta_rol_facultad_distinto_de_y_critica(db):
    crear_usuario(db, 'f_op', rol='operador', numero='7306', pin='2580')
    crear_usuario(db, 'f_cal', rol='calidad', numero='7307', pin='1397', facultades='liberar_lotes')
    with pytest.raises(FirmaInvalida, match='facultad'):
        firma(db, '7306', '2580', 'Liberó', facultad='liberar_lotes')
    with pytest.raises(FirmaInvalida, match='rol'):
        firma(db, '7306', '2580', 'Verificó', roles=['calidad'])
    with pytest.raises(FirmaInvalida, match='misma persona'):
        firma(db, '7306', '2580', 'Verificó', distinto_de='7306')
    assert firma(db, '7307', '1397', 'Verificó', distinto_de=['7306'])
    with pytest.raises(FirmaInvalida, match='sesión'):
        firmas.firmar(db, numero_empleado='7307', pin='1397', significado='Liberó', modulo='x', entidad='x',
                      entidad_id=1, facultad='liberar_lotes', critica=True, sesion=_sesion('7306'))
    assert firmas.firmar(db, numero_empleado='7307', pin='1397', significado='Liberó', modulo='x', entidad='x',
                         entidad_id=1, facultad='liberar_lotes', critica=True, sesion=_sesion('7307'))
    with pytest.raises(FirmaInvalida):
        firma(db, '7307', '1397', 'Firmó porque sí')
    db.rollback()


def test_rh_borra_el_pin(client, db):
    crear_usuario(db, 'rh_pin', rol='rh', numero='7308')
    crear_usuario(db, 'olvido', rol='operador', numero='7309', pin='2580')
    login(client, 'rh_pin', PASSWORD)
    client.post('/pin/borrar', data={'numero_empleado': '7309'})
    assert not firmas.tiene_pin(db, '7309')


# --- Inventario con firma real (integración con las facultades) ---------------
def test_aprobar_lote_con_firma_de_liberar_lotes(db):
    crear_usuario(db, 'lib', rol='calidad', numero='7310', pin='2580', facultades='liberar_lotes')
    crear_usuario(db, 'nolib', rol='calidad', numero='7311', pin='2580')
    lid = crear_lote(db, crear_material(db), cantidad=10)
    assert inventario.lote(db, lid)['estatus'] == 'CUARENTENA'
    sin = firma(db, '7311', '2580', 'Liberó')
    with pytest.raises(ErrorInventario):
        inventario.cambiar_estatus(db, lote_id=lid, nuevo='APROBADO', usuario='x', motivo='ok', firma_id=sin)
    db.rollback()
    con = firma(db, '7310', '2580', 'Liberó', facultad='liberar_lotes')
    inventario.cambiar_estatus(db, lote_id=lid, nuevo='APROBADO', usuario='x', motivo='Análisis conforme',
                               firma_id=con)
    db.commit()
    assert inventario.lote_disponible(db, lid)[0]


# --- 7. Folios, archivos y parámetros -------------------------------------------
def test_folios_consecutivos_por_serie_y_anio(db):
    from datetime import datetime
    a = folios.siguiente_folio(db, 'TST')
    b = folios.siguiente_folio(db, 'TST')
    c = folios.siguiente_folio(db, 'OTR')
    anio = datetime.now().year
    assert a == f'TST-{anio}-0001' and b == f'TST-{anio}-0002' and c == f'OTR-{anio}-0001'
    assert folios.siguiente_folio(db, 'TST', fecha=datetime(anio + 1, 1, 1)) == f'TST-{anio + 1}-0001'
    db.rollback()   # una transacción revertida no gasta el número
    assert folios.siguiente_folio(db, 'TST') == f'TST-{anio}-0001'
    db.rollback()


def test_numero_de_lote_con_formato(db):
    from datetime import datetime
    f = datetime(2026, 10, 14)
    assert folios.numero_lote(db, 'T{TIPO}-{AA}-{####}', fecha=f, TIPO='MP') == 'TMP-26-0001'
    assert folios.numero_lote(db, 'T{TIPO}-{AA}-{####}', fecha=f, TIPO='MP') == 'TMP-26-0002'
    assert folios.numero_lote(db, 'T{TIPO}-{AA}-{####}', fecha=f, TIPO='EM') == 'TEM-26-0001'
    assert folios.numero_lote(db, 'T{AA}{MM}{DD}{###}', fecha=f) == 'T261014001'
    assert folios.numero_lote(db, 'T{COD}', fecha=f, COD='MP-0012') == 'TMP-0012'
    db.rollback()


def test_archivos_privados_solo_con_permiso_del_modulo(client, db):
    from werkzeug.datastructures import FileStorage
    from nucleo import archivos
    fs = FileStorage(stream=io.BytesIO(b'%PDF-1.4 cert'), filename='certificado.pdf')
    aid = archivos.guardar_archivo(db, fs, modulo='prueba', entidad='lotes', entidad_id=1,
                                   descripcion='Certificado', usuario='x')
    db.commit()
    reg = archivos.archivo(db, aid)
    ruta = archivos.ruta_absoluta(reg)
    assert os.path.exists(ruta)
    assert 'static' not in ruta.split(os.sep)
    assert archivos.archivos_de(db, 'lotes', 1)[0]['id'] == aid
    with pytest.raises(archivos.ErrorArchivo):
        archivos.guardar_archivo(db, FileStorage(stream=io.BytesIO(b'<html>'), filename='x.html'),
                                 modulo='prueba', entidad='lotes', entidad_id=1, descripcion='', usuario='x')

    assert client.get(f'/archivo/{aid}').status_code == 302          # sin sesión → login
    crear_usuario(db, 'arch_rh', rol='rh', numero='7401')
    crear_usuario(db, 'arch_alm', rol='almacen', numero='7402')
    login(client, 'arch_rh', PASSWORD)
    assert client.get(f'/archivo/{aid}').status_code == 404          # rh no tiene 'prueba'
    client.get('/logout')
    login(client, 'arch_alm', PASSWORD)
    r = client.get(f'/archivo/{aid}')
    assert r.status_code == 200 and r.data.startswith(b'%PDF')
    assert client.get('/archivo/999999').status_code == 404


def test_parametros_editables_por_admin(client, db):
    assert parametros.parametro_int('nucleo.pin_intentos') == 5
    crear_usuario(db, 'adm_par', rol='admin', numero='7501')
    login(client, 'adm_par', PASSWORD)
    assert 'nucleo.pin_intentos' in client.get('/configuracion/parametros').get_data(as_text=True)
    client.post('/configuracion/parametros', data={'clave': 'prueba.dias_aviso', 'valor': '45'})
    assert parametros.parametro('prueba.dias_aviso') == '45'
    client.post('/configuracion/parametros', data={'clave': 'prueba.dias_aviso', 'valor': '30'})
    with pytest.raises(KeyError):
        parametros.parametro('nadie.registro_esto')
