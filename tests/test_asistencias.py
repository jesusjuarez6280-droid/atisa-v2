import io

import pandas as pd
import pytest

import erp
from conftest import crear_usuario, login


def _excel(filas):
    buf = io.BytesIO()
    pd.DataFrame(filas, columns=['user_id', 'nombre', 'fecha', 'hora']).to_excel(buf, index=False)
    buf.seek(0)
    return buf


def _importar(client, filas):
    return client.post('/importar_zkteco', data={'archivo_zkteco': (_excel(filas), 'checadas.xlsx')},
                       content_type='multipart/form-data')


@pytest.fixture
def rh(client, db):
    for t in ('registros_zkteco', 'resumen_asistencias', 'horarios_empleados'):
        db.execute(f'DELETE FROM {t}')
    db.execute("DELETE FROM usuarios WHERE numero_empleado LIKE '7%'")
    db.commit()
    crear_usuario(db, 'rh_asis', 'clave-segura-9', rol='rh', numero='7000')
    crear_usuario(db, 'jose', 'clave-segura-9', rol='operador', numero='7001',
                  nombre_completo='José Pérez López')
    crear_usuario(db, 'ana', 'clave-segura-9', rol='operador', numero='7002',
                  nombre_completo='Ana Ruiz')
    db.execute("INSERT INTO horarios_empleados (numero_empleado, hora_entrada, hora_salida) VALUES ('7001', '08:00', '17:00')")
    db.commit()
    login(client, 'rh_asis', 'clave-segura-9')
    return client


def _resumen(db):
    return {(r['numero_empleado'], r['fecha']): dict(r)
            for r in db.execute('SELECT * FROM resumen_asistencias')}


def test_liga_por_nombre_sin_acentos_y_aprende_el_id(rh, db):
    _importar(rh, [['55', 'JOSE  PEREZ LOPEZ', '2026-10-01', '8:05'],
                   ['55', 'JOSE  PEREZ LOPEZ', '2026-10-01', '17:30:00']])
    res = _resumen(db)
    fila = res[('7001', '2026-10-01')]
    assert fila['nombre'] == 'José Pérez López'
    assert fila['hora_entrada_real'] == '08:05:00'
    assert fila['minutos_retardo'] == 5           # contra su horario de 08:00
    assert fila['horas_extras'] == 0.5
    assert db.execute("SELECT id_checador FROM usuarios WHERE numero_empleado='7001'").fetchone()[0] == '55'


def test_liga_por_numero_de_empleado(rh, db):
    _importar(rh, [['7002', 'A. RUIZ', '2026-10-02', '08:20:00']])
    assert ('7002', '2026-10-02') in _resumen(db)


def test_id_desconocido_se_liga_al_capturarlo_en_el_expediente(rh, db):
    _importar(rh, [['99', 'NOMBRE RARO', '2026-10-03', '09:00:00']])
    assert ('99', '2026-10-03') in _resumen(db)
    r = rh.get('/asistencias')
    assert 'sin ligar' in r.get_data(as_text=True)

    rh.post('/expediente/guardar', data={'numero_empleado': '7002', 'id_checador': '99'})
    res = _resumen(db)
    assert ('99', '2026-10-03') not in res
    assert res[('7002', '2026-10-03')]['nombre'] == 'Ana Ruiz'


def test_id_del_checador_no_se_repite(rh, db):
    rh.post('/expediente/guardar', data={'numero_empleado': '7001', 'id_checador': '42'})
    rh.post('/expediente/guardar', data={'numero_empleado': '7002', 'id_checador': '42'})
    ids = [r[0] for r in db.execute("SELECT id_checador FROM usuarios WHERE numero_empleado IN ('7001','7002')")]
    assert ids.count('42') == 1


def test_importar_solo_recalcula_las_fechas_del_archivo(rh, db):
    _importar(rh, [['7002', 'Ana Ruiz', '2026-10-04', '08:00:00']])
    db.execute("UPDATE resumen_asistencias SET estatus = 'MARCA' WHERE fecha = '2026-10-04'")
    db.commit()
    _importar(rh, [['7002', 'Ana Ruiz', '2026-10-05', '08:00:00']])
    res = _resumen(db)
    assert res[('7002', '2026-10-04')]['estatus'] == 'MARCA'    # no se tocó
    assert ('7002', '2026-10-05') in res
    # El botón de recalcular sí recalcula todo
    rh.post('/asistencias/reprocesar')
    assert _resumen(db)[('7002', '2026-10-04')]['estatus'] != 'MARCA'


def test_reimportar_el_mismo_archivo_no_duplica(rh, db):
    filas = [['7002', 'Ana Ruiz', '2026-10-06', '08:00:00'], ['7002', 'Ana Ruiz', '2026-10-06', '18:00:00']]
    _importar(rh, filas)
    _importar(rh, filas)
    assert db.execute("SELECT COUNT(*) FROM registros_zkteco WHERE fecha='2026-10-06'").fetchone()[0] == 2


def test_asistencias_paginadas(rh, db):
    filas = [('7002', 'Ana Ruiz', f'2025-{m:02d}-{d:02d}', '08:00:00', None, 0, 0, 'Presente')
             for m in range(1, 13) for d in range(1, 21)]
    db.executemany('INSERT INTO resumen_asistencias (numero_empleado, nombre, fecha, hora_entrada_real, '
                   'hora_salida_real, minutos_retardo, horas_extras, estatus) VALUES (?,?,?,?,?,?,?,?)', filas)
    db.commit()
    html = rh.get('/asistencias').get_data(as_text=True)
    assert 'Página 1 de 3' in html and '240 registros' in html
    html = rh.get('/asistencias?pagina=3').get_data(as_text=True)
    assert 'Página 3 de 3' in html
    assert rh.get('/asistencias?pagina=abc').status_code == 200


def test_pantallas_optimizadas_responden(rh, db):
    for url in ('/reportes', '/reportes?periodo=mes', '/tablero', '/produccion'):
        assert rh.get(url).status_code == 200, url


def test_produccion_kpis_del_mes_completo(rh, db):
    db.execute('DELETE FROM produccion_llenado')
    mes = erp.datetime.now().strftime('%Y-%m')
    db.executemany('INSERT INTO produccion_llenado (fecha, producto, lote, turno_manana, turno_tarde) VALUES (?,?,?,?,?)',
                   [(f'{mes}-01', 'P', str(i), 1, 1) for i in range(250)])
    db.commit()
    html = rh.get('/produccion').get_data(as_text=True)
    assert 'leading-none">500</p><p class="text-xs text-slate-500 font-medium mt-1">Frascos este mes' in html
