"""Conexión a la base SQLite (movido de erp.py)."""
import os
import sqlite3

from flask import g, has_app_context

# Ruta de la base de datos (configurable para pruebas o para moverla de carpeta).
DB_PATH = os.environ.get('ATISA_DB', 'base_planta.db')


def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # PRAGMAs de rendimiento (por conexión, costo ~microsegundos)
    conn.execute('PRAGMA synchronous = NORMAL;')   # rápido y seguro junto a WAL
    conn.execute('PRAGMA temp_store = MEMORY;')     # tablas temporales en RAM
    conn.execute('PRAGMA cache_size = -16000;')     # ~16 MB de page cache
    conn.execute('PRAGMA foreign_keys = ON;')
    conn.execute('PRAGMA busy_timeout = 5000;')     # espera en vez de "database is locked"
    # Red de seguridad: registra la conexión para cerrarla al final del request
    # aunque la ruta truene o el dev olvide conn.close(). Evita bloqueos en red (WAL).
    if has_app_context():
        g.setdefault('_db_conns', []).append(conn)
    return conn


def cerrar_conexiones_db(exc):
    """Cierra TODA conexión abierta durante el request (idempotente con conn.close()).
    erp.py la registra con @app.teardown_appcontext."""
    conns = g.pop('_db_conns', None)
    if conns:
        for c in conns:
            try:
                c.close()
            except Exception:
                pass


def ruta_de(conn):
    """Archivo de la base a la que apunta una conexión abierta."""
    for fila in conn.execute('PRAGMA database_list').fetchall():
        if fila[1] == 'main':
            return fila[2]
    return DB_PATH
