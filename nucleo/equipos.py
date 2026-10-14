"""Catálogo de equipos e instrumentos y la regla de "¿se puede usar hoy?".

Servicios sin Flask: reciben la conexión abierta y NO hacen commit. Los errores se lanzan
como ErrorEquipo con un mensaje listo para mostrarse.

Los datos descriptivos los edita cualquiera con permiso del catálogo (editar_equipo); el
estatus, los vencimientos y la última verificación solo los mueve el módulo Equipos con
actualizar_vencimientos(), que devuelve lo que cambió para que ese módulo lo registre en su
propio historial.
"""
from datetime import date, datetime

ESTATUS_EQUIPO = ('EN_SERVICIO', 'FUERA_DE_SERVICIO', 'BAJA')

CAMPOS_DESCRIPTIVOS = ('codigo', 'nombre', 'tipo', 'area', 'marca', 'modelo', 'serie', 'ubicacion',
                       'capacidad', 'resolucion', 'es_instrumento', 'verificacion_diaria',
                       'competencia_tarea_id', 'activo_id')
# Al dar de alta se pueden capturar los vencimientos que ya trae el equipo.
CAMPOS_ALTA = CAMPOS_DESCRIPTIVOS + ('vence_calibracion', 'vence_mantenimiento')

# Centinela: distingue "no cambiar este dato" de "dejarlo vacío" (None).
SIN_CAMBIO = object()


class ErrorEquipo(Exception):
    """El mensaje se puede mostrar tal cual en pantalla (flash)."""


def _fecha(valor, campo):
    if valor is None or valor == '':
        return None
    if isinstance(valor, datetime):
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    try:
        return datetime.strptime(str(valor).strip()[:10], '%Y-%m-%d').date().isoformat()
    except ValueError:
        raise ErrorEquipo(f'La fecha de {campo.replace("_", " ")} no es válida (usa AAAA-MM-DD).')


def _texto(valor):
    return (str(valor).strip() if valor is not None else '') or None


def _equipo(conn, equipo_id):
    fila = conn.execute('SELECT * FROM equipos WHERE id = ?', (equipo_id,)).fetchone()
    if not fila:
        raise ErrorEquipo('El equipo no existe.')
    return fila


def _normalizar(datos, permitidos):
    for campo in datos:
        if campo not in permitidos:
            if campo in ('estatus', 'vence_calibracion', 'vence_mantenimiento', 'ultima_verificacion'):
                raise ErrorEquipo('El estatus y los vencimientos solo se cambian desde el módulo Equipos.')
            raise ErrorEquipo(f'El campo «{campo}» no existe en equipos.')
    for campo in ('codigo', 'nombre', 'tipo'):
        if campo in datos:
            datos[campo] = _texto(datos[campo])
            if not datos[campo]:
                raise ErrorEquipo(f'Falta el {campo} del equipo.')
    for campo in ('area', 'marca', 'modelo', 'serie', 'ubicacion', 'capacidad', 'resolucion'):
        if campo in datos:
            datos[campo] = _texto(datos[campo])
    for campo in ('es_instrumento', 'verificacion_diaria'):
        if campo in datos:
            datos[campo] = 1 if datos[campo] in (1, True, '1', 'on', 'true') else 0
    for campo in ('competencia_tarea_id', 'activo_id'):
        if campo in datos and datos[campo] in ('', 0):
            datos[campo] = None
    for campo in ('vence_calibracion', 'vence_mantenimiento'):
        if campo in datos:
            datos[campo] = _fecha(datos[campo], campo)


def crear_equipo(conn, *, codigo, nombre, tipo, usuario, **campos) -> int:
    datos = {'codigo': codigo, 'nombre': nombre, 'tipo': tipo, **campos}
    _normalizar(datos, CAMPOS_ALTA)
    if conn.execute('SELECT 1 FROM equipos WHERE codigo = ?', (datos['codigo'],)).fetchone():
        raise ErrorEquipo(f'Ya existe un equipo con el código {datos["codigo"]}.')
    cur = conn.execute(f'INSERT INTO equipos ({", ".join(datos)}) VALUES ({", ".join("?" * len(datos))})',
                       tuple(datos.values()))
    return cur.lastrowid


def editar_equipo(conn, equipo_id, *, usuario, **campos):
    """Solo datos descriptivos; estatus y vencimientos solo con actualizar_vencimientos()."""
    actual = _equipo(conn, equipo_id)
    datos = dict(campos)
    _normalizar(datos, CAMPOS_DESCRIPTIVOS)
    cambios = {k: v for k, v in datos.items() if actual[k] != v}
    if not cambios:
        return
    if 'codigo' in cambios and conn.execute('SELECT 1 FROM equipos WHERE codigo = ? AND id <> ?',
                                            (cambios['codigo'], equipo_id)).fetchone():
        raise ErrorEquipo(f'Ya existe un equipo con el código {cambios["codigo"]}.')
    sets = ', '.join(f'{k} = ?' for k in cambios)
    conn.execute(f'UPDATE equipos SET {sets} WHERE id = ?', (*cambios.values(), equipo_id))


def equipo_apto(conn, equipo_id, fecha=None) -> dict:
    """{'apto': bool, 'bloqueos': [...], 'avisos': [...]}.
    Bloquea: estatus distinto de EN_SERVICIO; calibración vencida; verificación diaria pendiente hoy.
    Avisa (no bloquea): mantenimiento vencido. Sin programa (vence_* NULL) = sin bloqueo.
    Una fecha de vencimiento igual a `fecha` todavía es vigente (vence al terminar ese día)."""
    e = _equipo(conn, equipo_id)
    hoy = _fecha(fecha, 'fecha') if fecha is not None else date.today().isoformat()
    bloqueos, avisos = [], []
    if e['estatus'] != 'EN_SERVICIO':
        estatus = 'fuera de servicio' if e['estatus'] == 'FUERA_DE_SERVICIO' else 'dado de baja'
        bloqueos.append(f'{e["codigo"]} está {estatus}.')
    if e['vence_calibracion'] and e['vence_calibracion'] < hoy:
        bloqueos.append(f'{e["codigo"]} tiene la calibración vencida desde el {e["vence_calibracion"]}.')
    if e['verificacion_diaria'] and (e['ultima_verificacion'] or '')[:10] != hoy:
        bloqueos.append(f'{e["codigo"]} no tiene la verificación del día ({hoy}).')
    if e['vence_mantenimiento'] and e['vence_mantenimiento'] < hoy:
        avisos.append(f'{e["codigo"]} tiene el mantenimiento vencido desde el {e["vence_mantenimiento"]}.')
    return {'apto': not bloqueos, 'bloqueos': bloqueos, 'avisos': avisos}


def actualizar_vencimientos(conn, equipo_id, *, vence_calibracion=SIN_CAMBIO, vence_mantenimiento=SIN_CAMBIO,
                            ultima_verificacion=SIN_CAMBIO, estatus=SIN_CAMBIO, usuario, motivo) -> dict:
    """Solo lo usa el módulo Equipos. Un dato que no se pasa no cambia; None lo deja vacío
    (p. ej. vence_calibracion=None = sin programa de calibración). Pide motivo.
    Devuelve {campo: (anterior, nuevo)} con lo que cambió, para que Equipos lo registre."""
    if not _texto(motivo):
        raise ErrorEquipo('Escribe el motivo del cambio.')
    if not _texto(usuario):
        raise ErrorEquipo('Falta el usuario que hace el cambio.')
    actual = _equipo(conn, equipo_id)
    pedidos = {}
    for campo, valor in (('vence_calibracion', vence_calibracion), ('vence_mantenimiento', vence_mantenimiento),
                         ('ultima_verificacion', ultima_verificacion)):
        if valor is not SIN_CAMBIO:
            pedidos[campo] = _fecha(valor, campo)
    if estatus is not SIN_CAMBIO:
        if estatus not in ESTATUS_EQUIPO:
            raise ErrorEquipo('Estatus de equipo no válido (EN_SERVICIO, FUERA_DE_SERVICIO o BAJA).')
        pedidos['estatus'] = estatus
    cambios = {k: (actual[k], v) for k, v in pedidos.items() if actual[k] != v}
    if cambios:
        sets = ', '.join(f'{k} = ?' for k in cambios)
        conn.execute(f'UPDATE equipos SET {sets} WHERE id = ?', (*(v for _, v in cambios.values()), equipo_id))
    return cambios
