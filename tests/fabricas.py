"""Fábricas para las pruebas de los módulos (base común).

Uso en tests/test_<clave>_*.py (con el fixture `db` de conftest):
    from fabricas import crear_usuario, crear_material, crear_lote, crear_equipo, firma, login
    crear_usuario(db, 'jefa_cal', rol='calidad', numero='5001', facultades='liberar_lotes', pin='2580')
    mat = crear_material(db, tipo='MP')
    lote = crear_lote(db, mat, cantidad=50)
    f = firma(db, '5001', '2580', 'Liberó', facultad='liberar_lotes')
Todo hace commit, para que la app (otra conexión) lo vea.
"""
import itertools

from werkzeug.security import generate_password_hash

import erp
from nucleo import inventario, equipos, firmas
from conftest import crear_usuario as _crear_usuario_base, login  # noqa: F401

PASSWORD = 'clave-prueba-1'
_consecutivo = itertools.count(1)


def _sig():
    return next(_consecutivo)


def crear_usuario(conn, username, password=PASSWORD, rol='operador', numero=None, roles_extra='',
                  facultades='', pin=None, **extra):
    """Usuario activo. pin='2580' le crea su PIN de firma. Devuelve el número de empleado."""
    numero = numero or str(60000 + _sig())
    _crear_usuario_base(conn, username, password, rol=rol, numero=numero,
                        roles_extra=roles_extra, facultades=facultades, **extra)
    if pin:
        conn.execute('UPDATE usuarios SET pin_hash = ?, pin_fallos = 0, pin_bloqueado_hasta = NULL '
                     'WHERE numero_empleado = ?', (generate_password_hash(pin), numero))
        conn.commit()
    return numero


def con_pin(conn, numero, pin='2580'):
    firmas.fijar_pin(conn, numero, pin, 'pruebas')
    conn.commit()


def firma(conn, numero, pin, significado='Aprobó', facultad=None, modulo='pruebas', entidad='pruebas',
          entidad_id=0, **kwargs):
    """Firma válida (sin sesión de Flask) y devuelve su id."""
    fid = firmas.firmar(conn, numero_empleado=numero, pin=pin, significado=significado, modulo=modulo,
                        entidad=entidad, entidad_id=entidad_id, facultad=facultad,
                        sesion={'username': 'pruebas', 'numero_empleado': numero, 'ip': None}, **kwargs)
    conn.commit()
    return fid


def crear_material(conn, codigo=None, nombre=None, tipo='MP', unidad='kg', **campos):
    n = _sig()
    mid = inventario.crear_material(conn, codigo=codigo or f'{tipo}-T{n:04d}', nombre=nombre or f'Material {n}',
                                    tipo=tipo, unidad=unidad, usuario='pruebas', **campos)
    conn.commit()
    return mid


def crear_lote(conn, material_id=None, cantidad=100, origen='RECEPCION', modulo='pruebas', **campos):
    """Lote con su entrada en el kárdex (nace en CUARENTENA si el material requiere análisis)."""
    material_id = material_id or crear_material(conn)
    lid = inventario.crear_lote(conn, material_id=material_id, cantidad=cantidad, origen=origen,
                                usuario='pruebas', modulo=modulo, **campos)
    conn.commit()
    return lid


def crear_equipo(conn, codigo=None, nombre=None, tipo='Báscula', **campos):
    n = _sig()
    eid = equipos.crear_equipo(conn, codigo=codigo or f'EQ-T{n:04d}', nombre=nombre or f'Equipo {n}',
                               tipo=tipo, usuario='pruebas', **campos)
    conn.commit()
    return eid


def conexion():
    """Conexión como la de la app (row_factory Row, llaves foráneas)."""
    conn = erp.sqlite3.connect(erp.DB_PATH)
    conn.row_factory = erp.sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn
