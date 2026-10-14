"""Utilidades compartidas por los servicios de Equipos."""
import calendar
from datetime import date, datetime

from nucleo import equipos as base_equipos
from nucleo import firmas

FACULTAD = 'dictaminar_equipos'
TIPO_PESA = 'Pesa patrón'
TIPOS = ('Báscula', 'Balanza analítica', TIPO_PESA, 'Mezcladora', 'Tanque', 'Caldera', 'Potenciómetro',
         'Termómetro', 'Bomba', 'Llenadora', 'Otro')
CRITICIDADES = ('Alta', 'Media', 'Baja')
ACTIVIDADES = ('CALIBRACION', 'MANTENIMIENTO')
MODALIDADES = ('Externa', 'Interna')


class ErrorEquipos(Exception):
    """El mensaje se puede mostrar tal cual en pantalla (flash)."""


def hoy_iso(hoy=None):
    return fecha_iso(hoy, 'fecha') if hoy else date.today().isoformat()


def fecha_iso(valor, campo='fecha', obligatoria=True):
    if valor is None or str(valor).strip() == '':
        if obligatoria:
            raise ErrorEquipos(f'Falta la {campo}.')
        return None
    if isinstance(valor, datetime):
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    try:
        return datetime.strptime(str(valor).strip()[:10], '%Y-%m-%d').date().isoformat()
    except ValueError:
        raise ErrorEquipos(f'La {campo} no es válida (usa AAAA-MM-DD).')


def sumar_meses(fecha, meses):
    """'2026-01-31' + 1 mes = '2026-02-28' (se queda en el último día del mes)."""
    d = date.fromisoformat(fecha_iso(fecha))
    total = d.month - 1 + int(meses)
    anio, mes = d.year + total // 12, total % 12 + 1
    return date(anio, mes, min(d.day, calendar.monthrange(anio, mes)[1])).isoformat()


def numero(valor, campo, obligatorio=True):
    if valor is None or str(valor).strip() == '':
        if obligatorio:
            raise ErrorEquipos(f'Falta {campo}.')
        return None
    try:
        return float(str(valor).strip().replace(',', '.'))
    except ValueError:
        raise ErrorEquipos(f'{campo[0].upper()}{campo[1:]} debe ser un número.')


def texto(valor):
    return (str(valor).strip() if valor is not None else '') or None


def si(valor):
    return 1 if valor in (1, True, '1', 'on', 'true', 'si', 'sí') else 0


def equipo(conn, equipo_id):
    fila = conn.execute('SELECT * FROM equipos WHERE id = ?', (equipo_id,)).fetchone()
    if not fila:
        raise ErrorEquipos('El equipo no existe.')
    return fila


def exigir_firma(conn, firma_id):
    if not firma_id or not firmas.firma(conn, firma_id):
        raise ErrorEquipos('Falta la firma.')


def exigir_facultad(conn, firma_id):
    exigir_firma(conn, firma_id)
    if not firmas.firma_tiene_facultad(conn, firma_id, FACULTAD):
        raise ErrorEquipos('Esto lo firma solo quien tiene la facultad de dictaminar equipos.')


def cumple(punto, lectura):
    """(conforme, error) de una lectura contra un punto: con nominal, |lectura − nominal| ≤ tolerancia;
    sin nominal, mínimo ≤ lectura ≤ máximo. Se compara con margen de redondeo de 1e-9."""
    lectura = float(lectura)
    if punto['nominal'] is not None:
        error = lectura - float(punto['nominal'])
        tolerancia = float(punto['tolerancia'] or 0)
        return (1 if abs(error) <= tolerancia + 1e-9 else 0), round(error, 6)
    ok = True
    if punto['minimo'] is not None and lectura < float(punto['minimo']) - 1e-9:
        ok = False
    if punto['maximo'] is not None and lectura > float(punto['maximo']) + 1e-9:
        ok = False
    return (1 if ok else 0), None


def cambiar_estatus(conn, equipo_id, nuevo, *, causa, motivo, usuario, firma_id=None, folio=None):
    """Mueve el estatus con el servicio de la base y lo deja en el historial. Devuelve True si cambió."""
    motivo = texto(motivo)
    if not motivo:
        raise ErrorEquipos('Escribe el motivo.')
    try:
        cambios = base_equipos.actualizar_vencimientos(conn, equipo_id, estatus=nuevo, usuario=usuario,
                                                       motivo=motivo)
    except base_equipos.ErrorEquipo as e:
        raise ErrorEquipos(str(e))
    if 'estatus' not in cambios:
        return False
    anterior = cambios['estatus'][0]
    conn.execute('INSERT INTO eq_estatus_historial (equipo_id, anterior, nuevo, causa, motivo, folio, firma_id, '
                 'usuario) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                 (equipo_id, anterior, nuevo, causa, motivo, folio, firma_id, usuario))
    return True


def fijar_vencimientos(conn, equipo_id, *, usuario, motivo, **campos):
    try:
        return base_equipos.actualizar_vencimientos(conn, equipo_id, usuario=usuario, motivo=motivo, **campos)
    except base_equipos.ErrorEquipo as e:
        raise ErrorEquipos(str(e))


def causa_fuera(conn, equipo_id):
    """Por qué está fuera de servicio (la causa del último paso a FUERA_DE_SERVICIO) o None."""
    fila = conn.execute("SELECT causa FROM eq_estatus_historial WHERE equipo_id = ? AND nuevo = 'FUERA_DE_SERVICIO' "
                        'ORDER BY id DESC LIMIT 1', (equipo_id,)).fetchone()
    return fila['causa'] if fila else None


def programa(conn, equipo_id, actividad, solo_activo=True):
    sql = 'SELECT * FROM eq_programas WHERE equipo_id = ? AND actividad = ?'
    if solo_activo:
        sql += ' AND activo = 1'
    return conn.execute(sql, (equipo_id, actividad)).fetchone()


def apto(conn, equipo_id, fecha=None):
    try:
        return base_equipos.equipo_apto(conn, equipo_id, fecha)
    except base_equipos.ErrorEquipo as e:
        raise ErrorEquipos(str(e))
