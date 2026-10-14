"""Reglas de negocio de Órdenes de producción, sin Flask.

Igual que los servicios de la base: reciben la conexión abierta, NO hacen commit y
lanzan ErrorOrdenes con un mensaje listo para flash(). Las firmas las hace la ruta
(nucleo.firmas.firmar) ANTES de llamar; aquí llegan como firma_id y se revisa que
sean de quien deben ser (facultad, persona distinta).
"""
import calendar
from datetime import date, datetime

from nucleo.firmas import firma_tiene_facultad

MODULO = 'ordenes'
_EPSILON = 1e-9


class ErrorOrdenes(Exception):
    """El mensaje se puede mostrar tal cual en pantalla (flash)."""


def ahora():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def hoy():
    return date.today().isoformat()


def texto(valor):
    return (str(valor).strip() if valor is not None else '') or None


def numero(valor, campo, minimo=None, entero=False, opcional=False):
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        if opcional:
            return None
        raise ErrorOrdenes(f'Falta {campo}.')
    try:
        n = float(str(valor).replace(',', '').strip()) if isinstance(valor, str) else float(valor)
    except (TypeError, ValueError):
        raise ErrorOrdenes(f'{campo[0].upper() + campo[1:]} debe ser un número.')
    if entero:
        if n != int(n):
            raise ErrorOrdenes(f'{campo[0].upper() + campo[1:]} debe ser un número entero.')
        n = int(n)
    if minimo is not None and n < minimo:
        raise ErrorOrdenes(f'{campo[0].upper() + campo[1:]} no puede ser menor que {fmt(minimo)}.')
    return n


def booleano(valor):
    return 1 if valor in (1, True, '1', 'on', 'si', 'sí', 'true') else 0


def fmt(n, decimales=4):
    """12.5 → '12.5'; 3.0 → '3' (para mensajes)."""
    if n is None:
        return '—'
    return f'{n:,.{decimales}f}'.rstrip('0').rstrip('.')


def fecha_hora(valor, campo):
    """'AAAA-MM-DD HH:MM' o 'AAAA-MM-DDTHH:MM' → 'AAAA-MM-DD HH:MM:SS'."""
    t = texto(valor)
    if not t:
        return None
    t = t.replace('T', ' ')
    for formato in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(t, formato).strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue
    raise ErrorOrdenes(f'La {campo} no es válida (usa AAAA-MM-DD HH:MM).')


def fin_de_mes_mas(fecha, meses):
    """Último día del mes de (fecha + meses)."""
    d = date.fromisoformat(str(fecha)[:10])
    total = d.month - 1 + int(meses)
    anio, mes = d.year + total // 12, total % 12 + 1
    return date(anio, mes, calendar.monthrange(anio, mes)[1]).isoformat()


def firma_fila(conn, firma_id):
    if not firma_id:
        return None
    return conn.execute('SELECT * FROM firmas WHERE id = ?', (firma_id,)).fetchone()


def exigir_firma(conn, firma_id, que):
    f = firma_fila(conn, firma_id)
    if not f:
        raise ErrorOrdenes(f'Falta la firma de {que}.')
    return f


def exigir_distintas(conn, firma_a, firma_b, mensaje='Quien verifica no puede ser la misma persona que realizó.'):
    a, b = firma_fila(conn, firma_a), firma_fila(conn, firma_b)
    if a and b and a['numero_empleado'] == b['numero_empleado']:
        raise ErrorOrdenes(mensaje)


def exigir_facultad(conn, firma_id, facultad, para):
    exigir_firma(conn, firma_id, para)
    if not firma_tiene_facultad(conn, firma_id, facultad):
        raise ErrorOrdenes(f'Quien firmó no tiene la facultad «{facultad}» para {para}.')
