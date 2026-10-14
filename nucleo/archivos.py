"""Archivos adjuntos privados (certificados, evidencias, informes).

Se guardan en la carpeta privado/ junto a la base (o la de ATISA_PRIVADO), fuera de
static/: nunca se sirven sin sesión. Salen solo por /archivo/<id> y solo para quien
tiene el permiso del módulo dueño.

Convención: los documentos de un lote (certificado del proveedor, informes de
laboratorio externo) se guardan con entidad='lotes' y el id del lote, para que
cualquier módulo los encuentre con archivos_de(conn, 'lotes', lote_id).
"""
import os
import secrets
from datetime import datetime

from werkzeug.utils import secure_filename

from nucleo import db

EXTENSIONES = {'pdf', 'jpg', 'jpeg', 'png'}
MAX_BYTES = 10 * 1024 * 1024


class ErrorArchivo(Exception):
    """Mensaje listo para mostrar."""


def carpeta_privada():
    return os.environ.get('ATISA_PRIVADO') or os.path.join(
        os.path.dirname(os.path.abspath(db.DB_PATH)), 'privado')


def guardar_archivo(conn, archivo, *, modulo, entidad, entidad_id, descripcion, usuario) -> int:
    """Solo pdf/jpg/jpeg/png, máximo 10 MB, nombre aleatorio. `archivo` es el
    FileStorage de request.files. No hace commit (si la transacción se revierte, el
    archivo queda huérfano en disco pero nunca se sirve)."""
    if not archivo or not archivo.filename:
        raise ErrorArchivo('No se seleccionó ningún archivo.')
    original = secure_filename(archivo.filename) or 'archivo'
    ext = original.rsplit('.', 1)[-1].lower() if '.' in original else ''
    if ext not in EXTENSIONES:
        raise ErrorArchivo('Solo se aceptan archivos PDF, JPG o PNG.')
    contenido = archivo.read(MAX_BYTES + 1)
    if len(contenido) > MAX_BYTES:
        raise ErrorArchivo('El archivo pesa más de 10 MB.')
    if not contenido:
        raise ErrorArchivo('El archivo está vacío.')
    relativa = os.path.join(modulo, datetime.now().strftime('%Y-%m'), f'{secrets.token_hex(16)}.{ext}')
    destino = os.path.join(carpeta_privada(), relativa)
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    with open(destino, 'wb') as fh:
        fh.write(contenido)
    cur = conn.execute(
        'INSERT INTO archivos (modulo, entidad, entidad_id, descripcion, nombre_original, ruta, tamano, subido_por) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (modulo, entidad, int(entidad_id), descripcion, original, relativa.replace(os.sep, '/'),
         len(contenido), usuario))
    return cur.lastrowid


def archivos_de(conn, entidad, entidad_id):
    return [dict(r) for r in conn.execute(
        'SELECT * FROM archivos WHERE entidad = ? AND entidad_id = ? ORDER BY id', (entidad, entidad_id)).fetchall()]


def archivo(conn, archivo_id):
    fila = conn.execute('SELECT * FROM archivos WHERE id = ?', (archivo_id,)).fetchone()
    return dict(fila) if fila else None


def ruta_absoluta(registro):
    """Ruta en disco de un registro de archivos; None si se sale de privado/."""
    base = os.path.abspath(carpeta_privada())
    ruta = os.path.abspath(os.path.join(base, *registro['ruta'].split('/')))
    if os.path.commonpath([base, ruta]) != base:
        return None
    return ruta
