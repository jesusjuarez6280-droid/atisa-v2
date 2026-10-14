"""Utilidades compartidas por los servicios de calidad (sin Flask)."""
import calendar
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from nucleo import firmas
from nucleo.parametros import parametro_int

MODULO = 'calidad'
# Quién firma las tareas de laboratorio (muestrear, capturar, revisar, investigar, retención).
ROLES_CALIDAD = ('calidad', 'admin')


class ErrorCalidad(Exception):
    """El mensaje se puede mostrar tal cual en pantalla (flash)."""


def texto(valor):
    return (str(valor).strip() if valor is not None else '') or None


def hoy():
    return date.today().isoformat()


def ahora():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def fecha(valor, campo='fecha'):
    """'AAAA-MM-DD' o None. Lanza ErrorCalidad si no es válida."""
    if valor in (None, ''):
        return None
    if isinstance(valor, datetime):
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    try:
        return datetime.strptime(str(valor).strip()[:10], '%Y-%m-%d').date().isoformat()
    except ValueError:
        raise ErrorCalidad(f'La {campo} no es válida (usa AAAA-MM-DD).')


def sumar_meses(fecha_txt, meses):
    """'2026-01-31' + 1 mes → '2026-02-28'."""
    d = date.fromisoformat(fecha_txt[:10])
    m = d.month - 1 + int(meses)
    anio, mes = d.year + m // 12, m % 12 + 1
    return date(anio, mes, min(d.day, calendar.monthrange(anio, mes)[1])).isoformat()


def numero(valor, campo, entero=False, opcional=True):
    if valor in (None, ''):
        if opcional:
            return None
        raise ErrorCalidad(f'Falta {campo}.')
    try:
        n = float(str(valor).replace(',', '').strip())
    except ValueError:
        raise ErrorCalidad(f'{campo.capitalize()} debe ser un número.')
    if entero:
        if n != int(n):
            raise ErrorCalidad(f'{campo.capitalize()} debe ser un número entero.')
        return int(n)
    return n


def booleano(valor):
    return 1 if valor in (1, True, '1', 'on', 'si', 'sí', 'true', 'Sí', 'SI', 'SÍ', 'x', 'X') else 0


def redondear(valor, decimales):
    """Redondeo de farmacopea: el 5 sube (97.95 → 98.0). Con Decimal, nunca con round()."""
    try:
        d = Decimal(str(valor).replace(',', '').strip())
    except (InvalidOperation, ValueError):
        raise ErrorCalidad('El resultado debe ser un número.')
    if not d.is_finite():
        raise ErrorCalidad('El resultado debe ser un número.')
    return d.quantize(Decimal(1).scaleb(-int(decimales or 0)), rounding=ROUND_HALF_UP)


def cumple_limites(redondeado, limite_min, limite_max):
    if limite_min is not None and redondeado < Decimal(str(limite_min)):
        return False
    if limite_max is not None and redondeado > Decimal(str(limite_max)):
        return False
    return True


def firmar(conn, firma, significado, entidad, entidad_id, detalle, **kwargs):
    """firma = {'numero_empleado', 'pin', 'sesion' (opcional, fuera de una petición)}."""
    firma = firma or {}
    return firmas.firmar(conn, numero_empleado=firma.get('numero_empleado'), pin=firma.get('pin'),
                         significado=significado, modulo=MODULO, entidad=entidad, entidad_id=entidad_id,
                         detalle=detalle, sesion=firma.get('sesion'), **kwargs)


def numero_firmante(conn, firma_id):
    fila = conn.execute('SELECT numero_empleado FROM firmas WHERE id = ?', (firma_id,)).fetchone()
    return fila[0] if fila else None


def nombre_firmante(conn, firma_id):
    if not firma_id:
        return None
    fila = conn.execute('SELECT nombre FROM firmas WHERE id = ?', (firma_id,)).fetchone()
    return fila[0] if fila else None


def lote_fila(conn, lote_id):
    """Lote con datos de su material y del material de su lote padre (o ErrorCalidad)."""
    fila = conn.execute(
        'SELECT l.*, m.codigo AS material_codigo, m.nombre AS material_nombre, m.tipo AS material_tipo, '
        'm.unidad AS unidad, m.requiere_analisis, m.registro_sanitario, m.es_impreso, '
        'm.material_padre_id, mp.tipo AS padre_material_tipo, p.nombre AS proveedor_nombre, '
        'p.estatus AS proveedor_estatus '
        'FROM lotes l JOIN materiales m ON m.id = l.material_id '
        'LEFT JOIN lotes lp ON lp.id = l.lote_padre_id '
        'LEFT JOIN materiales mp ON mp.id = lp.material_id '
        'LEFT JOIN proveedores p ON p.id = l.proveedor_id WHERE l.id = ?', (lote_id,)).fetchone()
    if not fila:
        raise ErrorCalidad('El lote no existe.')
    return fila


def sigue_al_granel(lote):
    """True si es una presentación (PT) envasada de un granel: se analiza y dictamina el granel."""
    return bool(lote['lote_padre_id']) and lote['material_tipo'] == 'PT' \
        and lote['padre_material_tipo'] == 'GR' and lote['origen'] != 'DEVOLUCION'


def lote_analizado_id(conn, lote_id):
    """El lote cuyo análisis vale para `lote_id`: su granel si es una presentación; si no, él mismo."""
    lote = lote_fila(conn, lote_id)
    return lote['lote_padre_id'] if sigue_al_granel(lote) else lote['id']


def meses_retencion(conn):
    return parametro_int('calidad.meses_retencion', conn)


def a_json(valor):
    return json.dumps(valor, ensure_ascii=False)


def de_json(texto_json, default=None):
    try:
        return json.loads(texto_json) if texto_json else default
    except ValueError:
        return default
