"""Parámetros ajustables por el admin (Configuración → Parámetros).

Cada módulo registra los suyos con su valor por defecto ('almacen.dias_alerta', '90',
'descripción'). parametro(clave) devuelve el valor guardado o, si nadie lo ha
cambiado, el default.
"""
import sqlite3

from nucleo.db import get_db_connection

# clave -> {'valor', 'modulo', 'descripcion'}
DEFAULTS = {}

PARAMETROS_NUCLEO = [
    ('nucleo.pin_intentos', '5', 'PIN equivocados seguidos que bloquean la firma'),
    ('nucleo.pin_minutos_bloqueo', '15', 'Minutos que dura el bloqueo del PIN'),
    ('nucleo.formato_lote_recepcion', '{TIPO}-{AA}-{####}',
     'Número de lote al recibir. Tokens: {AA} {AAAA} {MM} {DD} {COD} {TIPO} y {###} consecutivo'),
    ('nucleo.formato_lote_produccion', '{AA}{MM}{###}',
     'Número de lote al fabricar (mismos tokens). Lo usa Órdenes de producción'),
]


def registrar_parametros(modulo, lista):
    """lista: [(clave, default, descripcion)]. La clave debe empezar con '<modulo>.'."""
    for clave, valor, descripcion in lista or []:
        if not clave.startswith(modulo + '.'):
            raise ValueError(f"El parámetro '{clave}' debe empezar con '{modulo}.'")
        if clave in DEFAULTS and DEFAULTS[clave]['modulo'] != modulo:
            raise ValueError(f"El parámetro '{clave}' ya lo registró otro módulo")
        DEFAULTS[clave] = {'valor': str(valor), 'modulo': modulo, 'descripcion': descripcion}


registrar_parametros('nucleo', PARAMETROS_NUCLEO)


def sembrar_parametros(conn):
    """Guarda en la tabla los defaults que aún no existan (no pisa lo que ya editó el admin)."""
    for clave, p in DEFAULTS.items():
        conn.execute('INSERT OR IGNORE INTO parametros (clave, valor, modulo, descripcion) '
                     'VALUES (?, ?, ?, ?)', (clave, p['valor'], p['modulo'], p['descripcion']))


def parametro(clave, conn=None):
    """Valor guardado o el default que registró el módulo. Error si la clave no existe."""
    if clave not in DEFAULTS:
        raise KeyError(f"Parámetro no registrado: {clave}")
    propia = conn is None
    c = conn or get_db_connection()
    try:
        fila = c.execute('SELECT valor FROM parametros WHERE clave = ?', (clave,)).fetchone()
    except sqlite3.Error:
        fila = None
    finally:
        if propia:
            c.close()
    return fila[0] if fila else DEFAULTS[clave]['valor']


def parametro_int(clave, conn=None):
    try:
        return int(float(parametro(clave, conn)))
    except (TypeError, ValueError):
        return int(float(DEFAULTS[clave]['valor']))


def guardar_parametro(conn, clave, valor, usuario):
    if clave not in DEFAULTS:
        raise KeyError(f"Parámetro no registrado: {clave}")
    p = DEFAULTS[clave]
    conn.execute(
        'INSERT INTO parametros (clave, valor, modulo, descripcion, actualizado_por) VALUES (?, ?, ?, ?, ?) '
        'ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor, actualizado_por = excluded.actualizado_por, '
        "actualizado_en = datetime('now','localtime')",
        (clave, str(valor).strip(), p['modulo'], p['descripcion'], usuario))


def todos(conn):
    """[{clave, valor, default, modulo, descripcion, actualizado_por, actualizado_en}] por módulo."""
    guardados = {r['clave']: dict(r) for r in conn.execute('SELECT * FROM parametros').fetchall()}
    filas = []
    for clave, p in sorted(DEFAULTS.items(), key=lambda kv: (kv[1]['modulo'] != 'nucleo', kv[0])):
        g = guardados.get(clave, {})
        filas.append({'clave': clave, 'valor': g.get('valor', p['valor']), 'default': p['valor'],
                      'modulo': p['modulo'], 'descripcion': p['descripcion'],
                      'actualizado_por': g.get('actualizado_por'), 'actualizado_en': g.get('actualizado_en')})
    return filas
