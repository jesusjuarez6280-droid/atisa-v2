"""Blueprint del módulo y ayudas comunes de sus rutas (oficina y tablet)."""
from flask import Blueprint, current_app, flash, request, session

from nucleo import firmas

bp = Blueprint('ordenes', __name__, url_prefix='/ordenes', template_folder='templates')

# Quién puede firmar en planta: operadores y supervisores; lo de supervisor, solo producción.
ROLES_PLANTA = ('operador', 'produccion')
ROLES_SUPERVISOR = ('produccion',)


def firmar(conn, prefijo, significado, entidad, entidad_id, detalle='', **kwargs):
    """Firma con los campos <prefijo>_numero y <prefijo>_pin del formulario (ver _firmas.html)."""
    return firmas.firmar(conn, numero_empleado=request.form.get(f'{prefijo}_numero'),
                         pin=request.form.get(f'{prefijo}_pin'), significado=significado, modulo='ordenes',
                         entidad=entidad, entidad_id=entidad_id, detalle=detalle, **kwargs)


def numero_firmante(prefijo):
    return (request.form.get(f'{prefijo}_numero') or '').strip()


def avisar_error(e):
    flash(str(e), 'error')


def registrar_fallo(conn, mensaje):
    """Error inesperado: rollback, log y aviso claro (nunca un 500 en planta)."""
    try:
        conn.rollback()
    finally:
        current_app.logger.exception(mensaje)
    flash('Ocurrió un error inesperado; quedó registrado. Avisa a sistemas.', 'error')


def usuario():
    return session.get('nombre') or session.get('username') or 'sistema'


def accion(fn, exito, destino, error_destino=None):
    """Corre fn(conn) en una transacción: commit y aviso de éxito, o rollback y el motivo.
    exito: texto o fn(resultado) -> texto. Si el resultado trae 'avisos', se muestran también.
    destino / error_destino: URL o fn(resultado) -> URL."""
    from nucleo.db import get_db_connection
    from nucleo.inventario import ErrorInventario
    from nucleo.vistas import hx_redirect
    from .servicios import ErrorOrdenes

    conn = get_db_connection()
    ok, resultado = False, None
    try:
        resultado = fn(conn)
        conn.commit()
        ok = True
        if exito:
            flash(exito(resultado) if callable(exito) else exito, 'success')
        if isinstance(resultado, dict):
            for a in resultado.get('avisos') or []:
                flash(a, 'warning')
    except (firmas.FirmaInvalida, ErrorOrdenes, ErrorInventario) as e:
        conn.rollback()
        avisar_error(e)
    except Exception:
        registrar_fallo(conn, f'Falló {request.path}')
    finally:
        conn.close()
    url = destino if ok or error_destino is None else error_destino
    return hx_redirect(url(resultado) if callable(url) else url)
