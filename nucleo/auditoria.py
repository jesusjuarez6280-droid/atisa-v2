"""Bitácora de auditoría (movido de erp.py)."""
import sqlite3

from flask import request, session, current_app

from nucleo import db


def _auditar(tipo, accion, ruta='', detalle='', usuario=None, rol=None):
    """Inserta una línea en el registro de auditoría. A prueba de fallos:
    si algo sale mal (BD ocupada, tabla vieja), solo queda un aviso en el log."""
    try:
        conn = sqlite3.connect(db.DB_PATH, timeout=3.0)
        try:
            ip = request.remote_addr if request else None
        except Exception:
            ip = None
        conn.execute(
            "INSERT INTO auditoria (usuario, rol, tipo, accion, ruta, detalle, ip) VALUES (?,?,?,?,?,?,?)",
            (usuario or session.get('nombre') or session.get('username'),
             rol or session.get('rol'), tipo, accion, ruta, detalle, ip))
        conn.commit()
        conn.close()
    except Exception:
        current_app.logger.warning('No se pudo registrar en la bitácora: %s %s', tipo, ruta, exc_info=True)
