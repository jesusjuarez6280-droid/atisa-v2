"""Pruebas de nucleo/equipos.py (diseño 00-base-comun, sección 11 punto 6).

Autocontenidas: base SQLite temporal con las tablas mínimas existentes y la migración 1 de nucleo.
"""
import os
import sqlite3
from datetime import date, timedelta

import pytest

from nucleo import equipos as eq
from nucleo.equipos import ErrorEquipo
from nucleo.esquema import MIGRACIONES_NUCLEO, aplicar_migraciones_de


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(os.path.join(str(tmp_path), 'nucleo_equipos.db'), timeout=10.0)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys = ON')
    c.executescript('''
        CREATE TABLE usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT, numero_empleado TEXT UNIQUE, nombre_completo TEXT,
            username TEXT, password_hash TEXT, rol TEXT, estatus INTEGER DEFAULT 1, puesto TEXT);
        CREATE TABLE competencias_tareas (id INTEGER PRIMARY KEY);
        CREATE TABLE inventario_activos (id INTEGER PRIMARY KEY);
    ''')
    aplicar_migraciones_de(c, 'nucleo', MIGRACIONES_NUCLEO)
    yield c
    c.close()


def _dias(n):
    return (date.today() + timedelta(days=n)).isoformat()


def _bascula(conn, codigo='BAS-01', **campos):
    datos = {'es_instrumento': 1, 'verificacion_diaria': 1, **campos}
    return eq.crear_equipo(conn, codigo=codigo, nombre='Báscula de piso', tipo='Báscula', usuario='p', **datos)


def test_sin_programa_y_verificado_hoy_es_apto(conn):
    equipo = eq.crear_equipo(conn, codigo='MEZ-01', nombre='Mezcladora', tipo='Mezcladora', usuario='p')
    assert eq.equipo_apto(conn, equipo) == {'apto': True, 'bloqueos': [], 'avisos': []}


def test_calibracion_vencida_bloquea(conn):
    equipo = _bascula(conn, vence_calibracion=_dias(-1))
    eq.actualizar_vencimientos(conn, equipo, ultima_verificacion=date.today(), usuario='p', motivo='Verificada')
    r = eq.equipo_apto(conn, equipo)
    assert r['apto'] is False
    assert len(r['bloqueos']) == 1 and 'calibración vencida' in r['bloqueos'][0]
    # El día del vencimiento todavía es vigente
    eq.actualizar_vencimientos(conn, equipo, vence_calibracion=date.today(), usuario='p', motivo='Certificado')
    assert eq.equipo_apto(conn, equipo)['apto'] is True


def test_falta_verificacion_del_dia_bloquea(conn):
    equipo = _bascula(conn, vence_calibracion=_dias(100))
    r = eq.equipo_apto(conn, equipo)
    assert r['apto'] is False and 'verificación del día' in r['bloqueos'][0]
    eq.actualizar_vencimientos(conn, equipo, ultima_verificacion=_dias(-1), usuario='p', motivo='Ayer')
    assert eq.equipo_apto(conn, equipo)['apto'] is False
    eq.actualizar_vencimientos(conn, equipo, ultima_verificacion=date.today(), usuario='p', motivo='Hoy')
    assert eq.equipo_apto(conn, equipo)['apto'] is True
    # Con otra fecha de referencia vuelve a pedir la verificación
    assert eq.equipo_apto(conn, equipo, fecha=_dias(1))['apto'] is False


def test_mantenimiento_vencido_solo_avisa(conn):
    equipo = _bascula(conn, verificacion_diaria=0, vence_mantenimiento=_dias(-10))
    r = eq.equipo_apto(conn, equipo)
    assert r['apto'] is True and r['bloqueos'] == []
    assert len(r['avisos']) == 1 and 'mantenimiento vencido' in r['avisos'][0]


def test_fuera_de_servicio_bloquea(conn):
    equipo = _bascula(conn, verificacion_diaria=0)
    cambios = eq.actualizar_vencimientos(conn, equipo, estatus='FUERA_DE_SERVICIO', usuario='p',
                                         motivo='Falla de celda')
    assert cambios == {'estatus': ('EN_SERVICIO', 'FUERA_DE_SERVICIO')}
    r = eq.equipo_apto(conn, equipo)
    assert r['apto'] is False and 'fuera de servicio' in r['bloqueos'][0]


def test_actualizar_vencimientos_distingue_no_cambiar_de_vaciar(conn):
    equipo = _bascula(conn, vence_calibracion=_dias(30), vence_mantenimiento=_dias(60))
    with pytest.raises(ErrorEquipo, match='motivo'):
        eq.actualizar_vencimientos(conn, equipo, vence_calibracion=None, usuario='p', motivo='')
    cambios = eq.actualizar_vencimientos(conn, equipo, vence_calibracion=None, usuario='p',
                                         motivo='Ya no se calibra')
    assert cambios == {'vence_calibracion': (_dias(30), None)}
    fila = conn.execute('SELECT * FROM equipos WHERE id = ?', (equipo,)).fetchone()
    assert fila['vence_calibracion'] is None and fila['vence_mantenimiento'] == _dias(60)
    with pytest.raises(ErrorEquipo, match='Estatus'):
        eq.actualizar_vencimientos(conn, equipo, estatus='ROTO', usuario='p', motivo='x')


def test_editar_equipo_solo_datos_descriptivos(conn):
    equipo = _bascula(conn)
    eq.editar_equipo(conn, equipo, usuario='p', ubicacion='Pesado', capacidad='300 kg')
    fila = conn.execute('SELECT * FROM equipos WHERE id = ?', (equipo,)).fetchone()
    assert (fila['ubicacion'], fila['capacidad']) == ('Pesado', '300 kg')
    with pytest.raises(ErrorEquipo, match='módulo Equipos'):
        eq.editar_equipo(conn, equipo, usuario='p', estatus='BAJA')
    with pytest.raises(ErrorEquipo, match='módulo Equipos'):
        eq.editar_equipo(conn, equipo, usuario='p', vence_calibracion=_dias(10))
    _bascula(conn, 'BAS-02')
    with pytest.raises(ErrorEquipo, match='Ya existe'):
        eq.editar_equipo(conn, equipo, usuario='p', codigo='BAS-02')
