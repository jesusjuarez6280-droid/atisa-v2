"""Firma con número de empleado + PIN.

Servicios sin Flask obligatorio: reciben la conexión abierta y NO hacen commit (la
firma se guarda en la misma transacción que lo firmado; si algo falla, se revierte
todo). La excepción es el contador de PIN equivocados: se guarda aparte y al momento,
para que 5 fallos bloqueen aunque quien llama revierta su transacción.

Recomendación para quien llama: firmar() ANTES de las demás escrituras de la
transacción (así el contador de fallos no espera a que se libere la base).
"""
import sqlite3
from datetime import datetime, timedelta

from flask import has_request_context, request, session, current_app, has_app_context
from werkzeug.security import check_password_hash, generate_password_hash

from nucleo.db import ruta_de
from nucleo.parametros import parametro_int

SIGNIFICADOS = ('Realizó', 'Verificó', 'Revisó', 'Aprobó', 'Liberó', 'Rechazó',
                'Retuvo', 'Autorizó', 'Cerró', 'Anuló')


class FirmaInvalida(Exception):
    """El mensaje se puede mostrar tal cual en pantalla (flash)."""


def _lista(texto):
    return [x.strip() for x in (texto or '').split(',') if x.strip()]


def roles_de(usuario):
    """Rol principal + áreas adicionales de una fila de usuarios."""
    roles = [usuario['rol']]
    for r in _lista(usuario['roles_extra'] if 'roles_extra' in usuario.keys() else ''):
        if r != 'admin' and r not in roles:
            roles.append(r)
    return roles


def facultades_de(usuario):
    return _lista(usuario['facultades'] if 'facultades' in usuario.keys() else '')


def pin_trivial(pin):
    """Mensaje si el PIN no sirve, o None si es aceptable."""
    if not pin or not pin.isdigit() or not 4 <= len(pin) <= 6:
        return 'El PIN debe tener de 4 a 6 números.'
    if len(set(pin)) == 1:
        return 'El PIN no puede ser el mismo número repetido.'
    pasos = {int(b) - int(a) for a, b in zip(pin, pin[1:])}
    if pasos in ({1}, {-1}):
        return 'El PIN no puede ser una serie consecutiva (1234, 4321…).'
    if len(pin) % 2 == 0 and pin[:2] * (len(pin) // 2) == pin:
        return 'El PIN no puede ser un par repetido (1212, 2525…).'
    return None


def _usuario(conn, numero_empleado):
    return conn.execute('SELECT * FROM usuarios WHERE numero_empleado = ? AND estatus = 1',
                        (str(numero_empleado or '').strip(),)).fetchone()


def fijar_pin(conn, numero_empleado, pin_nuevo, usuario):
    """Crea o cambia el PIN (con hash, como las contraseñas). `usuario` = quién lo hace."""
    u = _usuario(conn, numero_empleado)
    if not u:
        raise FirmaInvalida('No existe un empleado activo con ese número.')
    error = pin_trivial(pin_nuevo)
    if error:
        raise FirmaInvalida(error)
    conn.execute('UPDATE usuarios SET pin_hash = ?, pin_fallos = 0, pin_bloqueado_hasta = NULL '
                 'WHERE id = ?', (generate_password_hash(pin_nuevo), u['id']))


def borrar_pin(conn, numero_empleado, usuario):
    """RH o el admin lo borran (nunca lo ven); la persona crea otro."""
    cur = conn.execute('UPDATE usuarios SET pin_hash = NULL, pin_fallos = 0, pin_bloqueado_hasta = NULL '
                       'WHERE numero_empleado = ?', (str(numero_empleado).strip(),))
    if cur.rowcount == 0:
        raise FirmaInvalida('No existe ese número de empleado.')


def tiene_pin(conn, numero_empleado):
    fila = conn.execute('SELECT pin_hash FROM usuarios WHERE numero_empleado = ?',
                        (str(numero_empleado or ''),)).fetchone()
    return bool(fila and fila['pin_hash'])


def _escribir_aparte(conn, sql, params):
    """Escribe en su propia conexión y la confirma de inmediato (sobrevive al rollback
    del que llama). Si la base está ocupada por la misma transacción, cae a `conn`."""
    try:
        otra = sqlite3.connect(ruta_de(conn), timeout=2.0)
        try:
            otra.execute(sql, params)
            otra.commit()
        finally:
            otra.close()
    except sqlite3.Error:
        conn.execute(sql, params)


def _auditar_fallo(numero, motivo):
    try:
        from nucleo.auditoria import _auditar
        _auditar('acceso', 'Firma rechazada', request.path if has_request_context() else '',
                 detalle=f'empleado {numero}: {motivo}',
                 usuario=(session.get('nombre') if has_request_context() else None) or str(numero),
                 rol=(session.get('rol') if has_request_context() else None) or '—')
    except Exception:
        if has_app_context():
            current_app.logger.warning('No se pudo auditar la firma rechazada', exc_info=True)


def _ahora():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def verificar_pin(conn, numero_empleado, pin):
    """Valida usuario activo, PIN creado, no bloqueado y correcto. Devuelve la fila
    del usuario o lanza FirmaInvalida. Cada fallo cuenta para el bloqueo."""
    numero = str(numero_empleado or '').strip()
    u = _usuario(conn, numero)
    if not u:
        _auditar_fallo(numero, 'número inexistente o inactivo')
        raise FirmaInvalida('Número de empleado o PIN incorrecto.')
    if not u['pin_hash']:
        raise FirmaInvalida(f"{u['nombre_completo']} aún no crea su PIN de firma.")
    if u['pin_bloqueado_hasta'] and u['pin_bloqueado_hasta'] > _ahora():
        _auditar_fallo(numero, 'PIN bloqueado')
        raise FirmaInvalida(f"PIN bloqueado por intentos fallidos hasta las {u['pin_bloqueado_hasta'][11:16]}.")
    if not check_password_hash(u['pin_hash'], str(pin or '')):
        fallos = (u['pin_fallos'] or 0) + 1
        intentos = parametro_int('nucleo.pin_intentos', conn)
        if fallos >= intentos:
            hasta = (datetime.now() + timedelta(minutes=parametro_int('nucleo.pin_minutos_bloqueo', conn)))
            _escribir_aparte(conn, 'UPDATE usuarios SET pin_fallos = 0, pin_bloqueado_hasta = ? WHERE id = ?',
                             (hasta.strftime('%Y-%m-%d %H:%M:%S'), u['id']))
            _auditar_fallo(numero, f'PIN equivocado; bloqueado hasta {hasta:%H:%M}')
            raise FirmaInvalida(f'PIN incorrecto. Se bloqueó por {intentos} intentos fallidos; '
                                f'espera hasta las {hasta:%H:%M}.')
        _escribir_aparte(conn, 'UPDATE usuarios SET pin_fallos = ? WHERE id = ?', (fallos, u['id']))
        _auditar_fallo(numero, f'PIN equivocado ({fallos} de {intentos})')
        raise FirmaInvalida('Número de empleado o PIN incorrecto.')
    if u['pin_fallos'] or u['pin_bloqueado_hasta']:
        _escribir_aparte(conn, 'UPDATE usuarios SET pin_fallos = 0, pin_bloqueado_hasta = NULL WHERE id = ?',
                         (u['id'],))
    return u


def firmar(conn, *, numero_empleado, pin, significado, modulo, entidad, entidad_id, detalle='',
           roles=None, facultad=None, distinto_de=None, critica=False, sesion=None) -> int:
    """Valida usuario activo, PIN creado, no bloqueado y correcto; que tenga alguno de
    `roles` y la `facultad`; que no sea `distinto_de` (número o lista: doble
    verificación); si `critica`, que sea el usuario de la sesión. Guarda la firma y
    devuelve su id. Si falla → FirmaInvalida con el motivo.

    sesion: {'username', 'numero_empleado', 'ip'} cuando se llama sin request de Flask
    (en una ruta se toma solo de la sesión)."""
    if significado not in SIGNIFICADOS:
        raise FirmaInvalida(f'Significado de firma no válido: {significado}')
    u = verificar_pin(conn, numero_empleado, pin)
    numero = u['numero_empleado']
    if roles:
        roles = [roles] if isinstance(roles, str) else list(roles)
        if not set(roles_de(u)) & set(roles):
            raise FirmaInvalida(f"{u['nombre_completo']} no tiene el rol para firmar esto.")
    if facultad and facultad not in facultades_de(u):
        raise FirmaInvalida(f"{u['nombre_completo']} no tiene la facultad de firma «{facultad}».")
    if distinto_de:
        prohibidos = [distinto_de] if isinstance(distinto_de, (str, int)) else list(distinto_de)
        if numero in {str(p) for p in prohibidos}:
            raise FirmaInvalida('Quien verifica no puede ser la misma persona que realizó.')

    if sesion is None and has_request_context():
        sesion = {'username': session.get('username'), 'numero_empleado': session.get('numero_empleado'),
                  'ip': request.remote_addr}
    sesion = sesion or {}
    if critica and str(sesion.get('numero_empleado') or '') != numero:
        raise FirmaInvalida('Esta firma solo la puede hacer quien tiene la sesión abierta.')

    cur = conn.execute(
        'INSERT INTO firmas (numero_empleado, nombre, puesto, significado, modulo, entidad, entidad_id, '
        'detalle, sesion_usuario, ip) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (numero, u['nombre_completo'], u['puesto'], significado, modulo, entidad, int(entidad_id),
         (detalle or '')[:500], sesion.get('username'), sesion.get('ip')))
    return cur.lastrowid


def firma(conn, firma_id):
    fila = conn.execute('SELECT * FROM firmas WHERE id = ?', (firma_id,)).fetchone()
    return dict(fila) if fila else None


def firma_tiene_facultad(conn, firma_id, facultad):
    """True si quien hizo la firma `firma_id` tiene (hoy) la facultad. Lo usan los
    servicios que exigen 'firma de alguien con liberar_lotes', etc."""
    if not firma_id:
        return False
    fila = conn.execute('SELECT u.facultades FROM firmas f JOIN usuarios u '
                        'ON u.numero_empleado = f.numero_empleado WHERE f.id = ?', (firma_id,)).fetchone()
    return bool(fila) and facultad in _lista(fila[0])
