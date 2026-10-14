from flask import url_for


def alertas(conn):
    n = conn.execute('SELECT COUNT(*) FROM prb_cosas WHERE bloquea = 1').fetchone()[0]
    if not n:
        return []
    return [{'tipo': 'Cosa bloqueada', 'icono': 'ri-flask-line', 'nivel': 'rojo', 'nombre': 'Prueba',
             'area': 'Almacén', 'detalle': f'{n} bloqueadas', 'url': '/prueba'}]


def kpis(conn):
    n = conn.execute('SELECT COUNT(*) FROM prb_cosas').fetchone()[0]
    return [{'titulo': 'Cosas de prueba', 'valor': n, 'detalle': 'total', 'nivel': 'verde', 'url': '/prueba'}]


def pendientes(conn, usuario):
    if 'almacen' not in usuario['roles'] and 'admin' not in usuario['roles']:
        return []
    return [{'texto': f"Revisar cosa {r['nombre']}", 'url': f"/prueba/{r['id']}", 'nivel': 'amarillo', 'fecha': None}
            for r in conn.execute('SELECT id, nombre FROM prb_cosas ORDER BY id LIMIT 3').fetchall()]


def accesos_planta(conn, usuario):
    return [{'titulo': 'Cosas de prueba', 'icono': 'ri-flask-line', 'url': '/prueba', 'contador': 1}]


def verificaciones_liberacion(conn, lote_id):
    fila = conn.execute('SELECT COUNT(*) FROM prb_cosas WHERE bloquea = 1').fetchone()
    return ['Hay una cosa de prueba que bloquea'] if fila[0] else []


def seccion_lote(conn, lote_id):
    return {'titulo': 'Prueba', 'plantilla': 'prueba/seccion_lote.html', 'contexto': {'lote_id': lote_id}}


def url_cosa(cosa_id):
    return url_for('prueba.cosa', cosa_id=cosa_id)


def contar(conn):
    return conn.execute('SELECT COUNT(*) FROM prb_cosas').fetchone()[0]


def datos_humo(conn):
    cur = conn.execute("INSERT INTO prb_cosas (nombre) VALUES ('Cosa humo')")
    return {'cosa_id': cur.lastrowid}
