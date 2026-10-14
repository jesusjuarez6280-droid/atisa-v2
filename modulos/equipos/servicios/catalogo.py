"""Catálogo: alta y edición de equipos con su ficha, programas, puntos, y los cambios de
estatus a mano (fuera de servicio, regreso a servicio y baja)."""
from nucleo import equipos as base_equipos
from nucleo.parametros import parametro_int

from .comun import (ErrorEquipos, ACTIVIDADES, CRITICIDADES, MODALIDADES, TIPO_PESA, cambiar_estatus, causa_fuera,
                    equipo, exigir_facultad, exigir_firma, fecha_iso, fijar_vencimientos, hoy_iso, numero,
                    programa, si, sumar_meses, texto)

CAMPOS_FICHA = ('criticidad', 'rango_uso', 'tolerancia_proceso', 'valor_nominal', 'unidad_nominal', 'clase',
                'proveedor_id', 'fecha_alta', 'notas')


def _ficha_limpia(ficha):
    datos = {}
    for campo in CAMPOS_FICHA:
        if campo not in ficha:
            continue
        valor = ficha[campo]
        if campo == 'valor_nominal':
            valor = numero(valor, 'el valor nominal', obligatorio=False)
        elif campo == 'proveedor_id':
            valor = int(valor) if str(valor or '').strip() not in ('', '0', 'None') else None
        elif campo == 'fecha_alta':
            valor = fecha_iso(valor, 'fecha de alta', obligatoria=False)
        else:
            valor = texto(valor)
        datos[campo] = valor
    if datos.get('criticidad') and datos['criticidad'] not in CRITICIDADES:
        raise ErrorEquipos('La criticidad debe ser Alta, Media o Baja.')
    return datos


def _guardar_ficha(conn, equipo_id, ficha):
    datos = _ficha_limpia(ficha or {})
    if not conn.execute('SELECT 1 FROM eq_fichas WHERE equipo_id = ?', (equipo_id,)).fetchone():
        conn.execute('INSERT INTO eq_fichas (equipo_id) VALUES (?)', (equipo_id,))
    if datos:
        sets = ', '.join(f'{k} = ?' for k in datos)
        conn.execute(f'UPDATE eq_fichas SET {sets} WHERE equipo_id = ?', (*datos.values(), equipo_id))


def _validar_pesa(datos, ficha):
    if datos.get('tipo') == TIPO_PESA:
        if numero((ficha or {}).get('valor_nominal'), 'el valor nominal de la pesa', obligatorio=False) is None:
            raise ErrorEquipos('Una pesa patrón necesita su valor nominal (1, 5, 20…).')


def alta(conn, *, datos, ficha=None, usuario, hoy=None) -> int:
    """datos: los campos de la base (codigo, nombre, tipo, area, marca… es_instrumento, verificacion_diaria,
    competencia_tarea_id, activo_id). ficha: los de eq_fichas. Las pesas patrón son instrumentos."""
    datos = dict(datos)
    if datos.get('tipo') == TIPO_PESA:
        datos['es_instrumento'] = 1
    _validar_pesa(datos, ficha)
    datos.pop('vence_calibracion', None)        # el vencimiento lo fija el dictamen de su calibración
    try:
        equipo_id = base_equipos.crear_equipo(conn, usuario=usuario, **datos)
    except base_equipos.ErrorEquipo as e:
        raise ErrorEquipos(str(e))
    ficha = dict(ficha or {})
    ficha.setdefault('fecha_alta', hoy_iso(hoy))
    _guardar_ficha(conn, equipo_id, ficha)
    return equipo_id


def editar(conn, equipo_id, *, datos, ficha=None, usuario):
    e = equipo(conn, equipo_id)
    if e['estatus'] == 'BAJA':
        raise ErrorEquipos('El equipo está dado de baja: ya no se edita.')
    datos = dict(datos)
    tipo = datos.get('tipo') or e['tipo']
    if tipo == TIPO_PESA:
        datos['es_instrumento'] = 1
    if ficha is not None:
        _validar_pesa({'tipo': tipo}, ficha)
    try:
        base_equipos.editar_equipo(conn, equipo_id, usuario=usuario, **datos)
    except base_equipos.ErrorEquipo as e:
        raise ErrorEquipos(str(e))
    if ficha is not None:
        _guardar_ficha(conn, equipo_id, ficha)


def guardar_programa(conn, equipo_id, actividad, *, frecuencia_meses, modalidad='Externa', proveedor_id=None,
                     tareas=None, dias_aviso=None, activo=True, proximo=None, usuario, hoy=None):
    """Crea o cambia el programa de calibración o de mantenimiento.

    Calibración: un instrumento que todavía no tiene una calibración apta queda fuera de servicio
    "por calibrar" (regla 1). Mantenimiento: si el equipo no tiene vencimiento se fija `proximo`
    o, si no se da, hoy + frecuencia."""
    e = equipo(conn, equipo_id)
    if actividad not in ACTIVIDADES:
        raise ErrorEquipos('Actividad no válida.')
    if e['estatus'] == 'BAJA':
        raise ErrorEquipos('El equipo está dado de baja.')
    try:
        frecuencia = int(str(frecuencia_meses).strip())
    except (TypeError, ValueError):
        raise ErrorEquipos('La frecuencia debe ser un número entero de meses.')
    if frecuencia <= 0:
        raise ErrorEquipos('La frecuencia debe ser de al menos 1 mes.')
    if modalidad not in MODALIDADES:
        raise ErrorEquipos('La modalidad debe ser Externa o Interna.')
    if dias_aviso in (None, ''):
        clave = 'equipos.dias_aviso_calibracion' if actividad == 'CALIBRACION' else 'equipos.dias_aviso_mantenimiento'
        dias_aviso = parametro_int(clave, conn)
    try:
        dias_aviso = int(str(dias_aviso).strip())
    except ValueError:
        raise ErrorEquipos('Los días de aviso deben ser un número entero.')
    tareas = texto(tareas)
    if actividad == 'MANTENIMIENTO' and not tareas:
        raise ErrorEquipos('Escribe las tareas del mantenimiento (una por renglón).')
    proveedor_id = int(proveedor_id) if str(proveedor_id or '').strip() not in ('', '0', 'None') else None
    conn.execute(
        'INSERT INTO eq_programas (equipo_id, actividad, frecuencia_meses, modalidad, proveedor_id, tareas, '
        'dias_aviso, activo) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(equipo_id, actividad) DO UPDATE SET '
        'frecuencia_meses = excluded.frecuencia_meses, modalidad = excluded.modalidad, '
        'proveedor_id = excluded.proveedor_id, tareas = excluded.tareas, dias_aviso = excluded.dias_aviso, '
        'activo = excluded.activo',
        (equipo_id, actividad, frecuencia, modalidad, proveedor_id, tareas, dias_aviso, 1 if activo else 0))
    if not activo:
        return
    if actividad == 'CALIBRACION':
        apta = conn.execute("SELECT 1 FROM eq_calibraciones WHERE equipo_id = ? AND estatus = 'APTO'",
                            (equipo_id,)).fetchone()
        if not apta and not e['vence_calibracion'] and e['estatus'] == 'EN_SERVICIO':
            cambiar_estatus(conn, equipo_id, 'FUERA_DE_SERVICIO', causa='POR_CALIBRAR',
                            motivo='Por calibrar: falta dictaminar apta su primera calibración', usuario=usuario)
    elif proximo not in (None, '') or not e['vence_mantenimiento']:
        vence = fecha_iso(proximo, 'fecha del próximo mantenimiento') if proximo not in (None, '') \
            else sumar_meses(hoy_iso(hoy), frecuencia)
        fijar_vencimientos(conn, equipo_id, vence_mantenimiento=vence, usuario=usuario,
                           motivo='Programa de mantenimiento')


def desactivar_programa(conn, equipo_id, actividad, *, usuario):
    """Quita el programa (el vencimiento que ya tenía se queda: no desbloquea un equipo vencido)."""
    conn.execute('UPDATE eq_programas SET activo = 0 WHERE equipo_id = ? AND actividad = ?', (equipo_id, actividad))


def guardar_puntos(conn, equipo_id, uso, lista, *, usuario):
    """Reemplaza los puntos de calibración o de verificación. Cada punto: nombre, unidad y
    (nominal + tolerancia) o (mínimo y/o máximo); requiere_pesa en los de verificación con pesa.
    Los anteriores se desactivan (los ya usados siguen en su historial)."""
    equipo(conn, equipo_id)
    if uso not in ('CALIBRACION', 'VERIFICACION'):
        raise ErrorEquipos('Uso de punto no válido.')
    limpios = []
    for i, p in enumerate(lista or [], start=1):
        nominal = numero(p.get('nominal'), f'el nominal del punto {i}', obligatorio=False)
        tolerancia = numero(p.get('tolerancia'), f'la tolerancia del punto {i}', obligatorio=False)
        minimo = numero(p.get('minimo'), f'el mínimo del punto {i}', obligatorio=False)
        maximo = numero(p.get('maximo'), f'el máximo del punto {i}', obligatorio=False)
        nombre, unidad = texto(p.get('nombre')), texto(p.get('unidad'))
        if all(v is None for v in (nominal, tolerancia, minimo, maximo)) and not nombre:
            continue                                   # renglón vacío del formulario
        if not unidad:
            raise ErrorEquipos(f'Falta la unidad del punto {i}.')
        if nominal is not None:
            if tolerancia is None or tolerancia < 0:
                raise ErrorEquipos(f'El punto {i} tiene nominal: escribe su tolerancia (± …).')
            minimo = maximo = None
        else:
            if uso == 'CALIBRACION':
                raise ErrorEquipos(f'El punto {i} de calibración necesita valor nominal y tolerancia.')
            if minimo is None and maximo is None:
                raise ErrorEquipos(f'El punto {i} necesita valor nominal y tolerancia, o un rango mínimo–máximo.')
            if minimo is not None and maximo is not None and minimo > maximo:
                raise ErrorEquipos(f'En el punto {i} el mínimo es mayor que el máximo.')
            tolerancia = None
        limpios.append((nombre, nominal, tolerancia, minimo, maximo, unidad,
                        si(p.get('requiere_pesa')) if (uso == 'VERIFICACION' and nominal is not None) else 0))
    actuales = [(r['nombre'], r['nominal'], r['tolerancia'], r['minimo'], r['maximo'], r['unidad'], r['requiere_pesa'])
                for r in puntos(conn, equipo_id, uso)]
    if actuales == limpios:
        return len(limpios)                            # sin cambios: se conservan los mismos puntos
    conn.execute('UPDATE eq_puntos SET activo = 0 WHERE equipo_id = ? AND uso = ? AND activo = 1', (equipo_id, uso))
    for orden, (nombre, nominal, tolerancia, minimo, maximo, unidad, pesa) in enumerate(limpios, start=1):
        conn.execute('INSERT INTO eq_puntos (equipo_id, uso, orden, nombre, nominal, tolerancia, minimo, maximo, '
                     'unidad, requiere_pesa) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (equipo_id, uso, orden, nombre, nominal, tolerancia, minimo, maximo, unidad, pesa))
    return len(limpios)


def puntos(conn, equipo_id, uso):
    return conn.execute('SELECT * FROM eq_puntos WHERE equipo_id = ? AND uso = ? AND activo = 1 ORDER BY orden, id',
                        (equipo_id, uso)).fetchall()


# --- Estatus ------------------------------------------------------------------

def poner_fuera(conn, equipo_id, *, motivo, firma_id, usuario, causa='MANUAL', folio=None):
    """Cualquiera con acceso, con motivo y firma; bloquea al instante (regla 7)."""
    e = equipo(conn, equipo_id)
    exigir_firma(conn, firma_id)
    if e['estatus'] == 'BAJA':
        raise ErrorEquipos('El equipo está dado de baja.')
    if e['estatus'] == 'FUERA_DE_SERVICIO':
        raise ErrorEquipos(f'{e["codigo"]} ya está fuera de servicio.')
    cambiar_estatus(conn, equipo_id, 'FUERA_DE_SERVICIO', causa=causa, motivo=motivo, usuario=usuario,
                    firma_id=firma_id, folio=folio)


def motivos_para_no_regresar(conn, equipo_id):
    """Lo que impide regresar el equipo a servicio (lista vacía = se puede)."""
    e = equipo(conn, equipo_id)
    motivos = []
    if e['estatus'] != 'FUERA_DE_SERVICIO':
        return [f'{e["codigo"]} no está fuera de servicio.']
    abiertas = conn.execute("SELECT folio FROM eq_ordenes_trabajo WHERE equipo_id = ? AND estatus = 'ABIERTA' "
                            'ORDER BY id', (equipo_id,)).fetchall()
    if abiertas:
        motivos.append('Tiene órdenes de trabajo abiertas: ' + ', '.join(r['folio'] for r in abiertas) + '.')
    if programa(conn, equipo_id, 'CALIBRACION') and not conn.execute(
            "SELECT 1 FROM eq_calibraciones WHERE equipo_id = ? AND estatus = 'APTO'", (equipo_id,)).fetchone():
        motivos.append('Está por calibrar: regresa a servicio cuando se dictamine apta su calibración.')
    ot = conn.execute("SELECT folio, fin FROM eq_ordenes_trabajo WHERE equipo_id = ? AND estatus = 'TERMINADA' "
                      'AND afecta_medicion = 1 ORDER BY fin DESC, id DESC LIMIT 1', (equipo_id,)).fetchone()
    if ot:
        fin = (ot['fin'] or '')[:10]
        cal = conn.execute("SELECT 1 FROM eq_calibraciones WHERE equipo_id = ? AND estatus = 'APTO' AND fecha >= ?",
                           (equipo_id, fin)).fetchone()
        ver = conn.execute('SELECT 1 FROM eq_verificaciones WHERE equipo_id = ? AND conforme = 1 AND fecha >= ?',
                           (equipo_id, fin)).fetchone()
        if not cal and not ver:
            motivos.append(f'La reparación {ot["folio"]} pudo afectar la medición: falta una calibración o '
                           'verificación conforme posterior.')
    return motivos


def regresar_a_servicio(conn, equipo_id, *, motivo, firma_id, usuario):
    """Firma crítica de dictaminar_equipos; sin OT abiertas; si una reparación pudo afectar la
    medición, con una calibración o verificación conforme posterior (regla 7)."""
    exigir_facultad(conn, firma_id)
    motivos = motivos_para_no_regresar(conn, equipo_id)
    if motivos:
        raise ErrorEquipos(' '.join(motivos))
    cambiar_estatus(conn, equipo_id, 'EN_SERVICIO', causa=None, motivo=motivo, usuario=usuario, firma_id=firma_id)


def dar_de_baja(conn, equipo_id, *, motivo, firma_id, usuario):
    """Firma de dictaminar_equipos y motivo; cancela sus órdenes abiertas; el historial se conserva."""
    e = equipo(conn, equipo_id)
    exigir_facultad(conn, firma_id)
    if e['estatus'] == 'BAJA':
        raise ErrorEquipos(f'{e["codigo"]} ya está dado de baja.')
    motivo = texto(motivo)
    if not motivo:
        raise ErrorEquipos('Escribe el motivo de la baja.')
    conn.execute("UPDATE eq_ordenes_trabajo SET estatus = 'CANCELADA', motivo_cancelacion = ? "
                 "WHERE equipo_id = ? AND estatus = 'ABIERTA'", (f'Equipo dado de baja: {motivo}', equipo_id))
    conn.execute('UPDATE eq_programas SET activo = 0 WHERE equipo_id = ?', (equipo_id,))
    cambiar_estatus(conn, equipo_id, 'BAJA', causa=None, motivo=motivo, usuario=usuario, firma_id=firma_id)


# --- Consultas para pantallas -----------------------------------------------------

def personal_calificado(conn, equipo_row):
    """Quiénes tienen la tarea del equipo en la Matriz de Expertise y con qué porcentaje."""
    if not equipo_row['competencia_tarea_id']:
        return []
    try:
        return [dict(r) for r in conn.execute(
            'SELECT u.numero_empleado, u.nombre_completo, e.nivel_pct FROM expertise_evaluaciones e '
            'JOIN usuarios u ON u.numero_empleado = e.numero_empleado '
            'WHERE e.tarea_id = ? AND u.estatus = 1 AND e.nivel_pct > 0 ORDER BY e.nivel_pct DESC, u.nombre_completo',
            (equipo_row['competencia_tarea_id'],)).fetchall()]
    except Exception:
        return []


def causa_actual(conn, equipo_row):
    return causa_fuera(conn, equipo_row['id']) if equipo_row['estatus'] == 'FUERA_DE_SERVICIO' else None
