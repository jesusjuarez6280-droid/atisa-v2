"""Ejecución en planta: despeje, pesadas y surtido, pasos, granel, envasado y conciliación.

Reglas (02-ordenes-produccion.md, sección 6, puntos 3 a 8 y 11):
- Despeje de fabricación antes de la primera pesada; todas las pesadas antes del primer paso de
  fabricación; los pasos de cada fase en orden; despeje de envasado antes de envasar o surtir.
- Pesar: lote disponible y del material correcto, báscula apta, sin pasar el límite superior de
  tolerancia, motivo si no es el lote sugerido y verificación de otra persona.
- Un valor fuera de rango marca el paso; el rendimiento fuera de límites marca la orden; la
  conciliación que no cuadra queda marcada. Todo eso pide desviación (o justificación firmada si
  el módulo de Desviaciones no está instalado) antes de terminar.
- Registro posterior: si se trabajó en papel, se captura después con la hora real y el motivo.
"""
from nucleo import equipos as srv_equipos
from nucleo import inventario
from nucleo.equipos import ErrorEquipo
from nucleo.inventario import ErrorInventario
from nucleo.parametros import parametro, parametro_int

from . import (_EPSILON, ErrorOrdenes, ahora, booleano, exigir_distintas, exigir_firma, fecha_hora, firma_fila,
               fmt, numero, texto)
from . import ordenes as srv_ordenes

CHECKS_DESPEJE = (
    ('area_limpia', 'Área limpia'),
    ('sin_materiales_previos', 'Sin materiales ni etiquetas de otro lote'),
    ('equipos_limpios', 'Equipos limpios y con su etiqueta'),
    ('documentos_retirados', 'Documentos del lote anterior retirados'),
)


def _orden_abierta(conn, orden_id):
    o = srv_ordenes.orden(conn, orden_id)
    if o['estatus'] not in srv_ordenes.ABIERTAS:
        raise ErrorOrdenes(f'La orden {o["folio"]} está {srv_ordenes.NOMBRE_ESTATUS[o["estatus"]].lower()}: '
                           'no se registra nada en planta.')
    return o


def _registro_posterior(registro_posterior, motivo, hora_real, campo='hora real'):
    """(bandera, motivo, hora) validados. Sin registro posterior la hora es ahora."""
    if not booleano(registro_posterior):
        return 0, None, ahora()
    motivo = texto(motivo)
    if not motivo:
        raise ErrorOrdenes('Para un registro posterior escribe el motivo (por qué se trabajó en papel).')
    hora = fecha_hora(hora_real, campo)
    if not hora:
        raise ErrorOrdenes(f'Para un registro posterior escribe la {campo}.')
    if hora > ahora():
        raise ErrorOrdenes(f'La {campo} no puede ser en el futuro.')
    return 1, motivo, hora


def _equipo_apto(conn, equipo_id, tipo=None, que='el equipo'):
    try:
        e = conn.execute('SELECT * FROM equipos WHERE id = ?', (equipo_id,)).fetchone()
        if not e:
            raise ErrorOrdenes(f'Elige {que}.')
        if tipo and (e['tipo'] or '').strip().lower() != tipo.strip().lower():
            raise ErrorOrdenes(f'{e["codigo"]} no es del tipo que pide el paso ({tipo}).')
        r = srv_equipos.equipo_apto(conn, equipo_id)
    except ErrorEquipo as ex:
        raise ErrorOrdenes(str(ex))
    if not r['apto']:
        raise ErrorOrdenes(' '.join(r['bloqueos']))
    return e, r['avisos']


def equipos_para(conn, tipo=None, basculas=False):
    """Equipos activos con su aptitud: [{id, codigo, nombre, tipo, apto, razon, avisos}]."""
    if basculas:
        filas = conn.execute("SELECT * FROM equipos WHERE estatus <> 'BAJA' AND (es_instrumento = 1 OR "
                             "LOWER(tipo) LIKE '%bascula%' OR LOWER(tipo) LIKE '%báscula%' OR LOWER(tipo) LIKE '%balanza%' "
                             "OR LOWER(tipo) LIKE '%tanque%') ORDER BY codigo").fetchall()
    elif tipo:
        filas = conn.execute("SELECT * FROM equipos WHERE estatus <> 'BAJA' AND LOWER(tipo) = LOWER(?) ORDER BY codigo",
                             (tipo,)).fetchall()
    else:
        filas = conn.execute("SELECT * FROM equipos WHERE estatus <> 'BAJA' ORDER BY codigo").fetchall()
    salida = []
    for e in filas:
        r = srv_equipos.equipo_apto(conn, e['id'])
        salida.append({'id': e['id'], 'codigo': e['codigo'], 'nombre': e['nombre'], 'tipo': e['tipo'],
                       'verificacion_diaria': e['verificacion_diaria'], 'apto': r['apto'],
                       'razon': ' '.join(r['bloqueos']), 'avisos': r['avisos']})
    return salida


# ---------------------------------------------------------------------------
# Despeje de línea
# ---------------------------------------------------------------------------

def despeje(conn, orden_id, *, momento, checks, realizo_firma_id, verifico_firma_id, observaciones=None):
    o = _orden_abierta(conn, orden_id)
    if momento not in ('Fabricación', 'Envasado'):
        raise ErrorOrdenes('El despeje es de fabricación o de envasado.')
    if conn.execute('SELECT 1 FROM op_despejes WHERE orden_id = ? AND momento = ?', (orden_id, momento)).fetchone():
        raise ErrorOrdenes(f'El despeje de {momento.lower()} ya está registrado.')
    faltan = [nombre for clave, nombre in CHECKS_DESPEJE if not booleano((checks or {}).get(clave))]
    if faltan:
        raise ErrorOrdenes('No se puede iniciar sin: ' + ', '.join(faltan).lower() + '.')
    if momento == 'Envasado' and not o['granel_lote_id']:
        raise ErrorOrdenes('Primero registra el granel obtenido; después el despeje de envasado.')
    exigir_firma(conn, realizo_firma_id, 'quien hizo el despeje')
    exigir_firma(conn, verifico_firma_id, 'quien verificó el despeje')
    exigir_distintas(conn, realizo_firma_id, verifico_firma_id)
    srv_ordenes.marcar_inicio(conn, orden_id)
    conn.execute('INSERT INTO op_despejes (orden_id, momento, area_limpia, sin_materiales_previos, equipos_limpios, '
                 'documentos_retirados, observaciones, realizo_firma_id, verifico_firma_id) '
                 'VALUES (?, ?, 1, 1, 1, 1, ?, ?, ?)',
                 (orden_id, momento, texto(observaciones), realizo_firma_id, verifico_firma_id))


# ---------------------------------------------------------------------------
# Pesadas (materia prima) y surtido (envase y empaque)
# ---------------------------------------------------------------------------

def componente(conn, orden_id, componente_id):
    for c in srv_ordenes.componentes(conn, orden_id):
        if c['id'] == componente_id:
            return c
    raise ErrorOrdenes('Ese material no está en la orden.')


def lotes_para(conn, c):
    """Lotes del material con su disponibilidad y el sugerido primero:
    [{id, numero_lote, existencia, caducidad, disponible, razon, sugerido}]."""
    sugerido = sugerencia(conn, c)
    filas = conn.execute('SELECT id FROM lotes WHERE material_id = ? AND existencia > 0 '
                         'ORDER BY fecha_caducidad IS NULL, fecha_caducidad, fecha_entrada, id', (c['material_id'],)).fetchall()
    salida = []
    for f in filas:
        lote = inventario.lote(conn, f['id'])
        salida.append({'id': lote['id'], 'numero_lote': lote['numero_lote'], 'existencia': lote['existencia'],
                       'caducidad': lote['fecha_caducidad'], 'estatus': lote['estatus'],
                       'disponible': lote['disponible'], 'razon': lote['motivo_no_disponible'],
                       'sugerido': lote['id'] == sugerido})
    salida.sort(key=lambda x: (not x['sugerido'], not x['disponible']))
    return salida


def sugerencia(conn, c):
    """Id del lote que caduca primero entre los disponibles (o None)."""
    pedir = c['falta'] if c['falta'] > _EPSILON else c['cantidad_teorica']
    try:
        s = inventario.sugerir_lotes(conn, c['material_id'], pedir)
    except ErrorInventario:
        return None
    return s[0]['lote_id'] if s and 'lote_id' in s[0] else None


def verificacion_obligatoria(conn, c):
    modo = (parametro('ordenes.verificar_pesadas', conn) or 'todas').strip().lower()
    if modo == 'activos':
        return bool(c['es_activo'])
    return True


def pesar(conn, orden_id, componente_id, *, lote_id, cantidad, usuario, realizo_firma_id, verifico_firma_id=None,
          equipo_id=None, motivo_otro_lote=None, registro_posterior=0, motivo_registro_posterior=None,
          hora_real=None):
    """Una pesada (MP) o un surtido (ME/EM). Descuenta el lote con el folio de la orden. Devuelve el id."""
    o = _orden_abierta(conn, orden_id)
    c = componente(conn, orden_id, componente_id)
    desp = srv_ordenes.despejes(conn, orden_id)
    es_mp = c['tipo'] == 'MP'
    if es_mp and 'Fabricación' not in desp:
        raise ErrorOrdenes('Primero el despeje de línea de fabricación.')
    if not es_mp and 'Envasado' not in desp:
        raise ErrorOrdenes('Primero el despeje de línea de envasado.')
    cantidad = numero(cantidad, 'la cantidad')
    if cantidad <= 0:
        raise ErrorOrdenes('La cantidad debe ser mayor que cero.')
    lote = inventario.lote(conn, lote_id)
    if not lote:
        raise ErrorOrdenes('Elige el lote.')
    if lote['material_id'] != c['material_id']:
        raise ErrorOrdenes(f'El lote {lote["numero_lote"]} no es de {c["codigo"]}.')
    if not lote['disponible']:
        raise ErrorOrdenes(lote['motivo_no_disponible'])
    maximo_extra = c['maximo'] - c['pesado']
    if cantidad > maximo_extra + _EPSILON:
        raise ErrorOrdenes(f'Te pasas de la tolerancia: lo máximo que puedes agregar es {fmt(max(0, maximo_extra))} '
                           f'{c["unidad"]}.')
    sugerido = sugerencia(conn, c)
    motivo_otro_lote = texto(motivo_otro_lote)
    if sugerido and lote_id != sugerido and not motivo_otro_lote:
        raise ErrorOrdenes('Elegiste un lote distinto del sugerido (el que caduca primero): escribe el motivo.')
    if es_mp and not equipo_id:
        raise ErrorOrdenes('Elige la báscula con que pesaste.')
    if equipo_id:
        _equipo_apto(conn, equipo_id, que='la báscula')
    exigir_firma(conn, realizo_firma_id, 'quien pesó')
    if verificacion_obligatoria(conn, c) and not verifico_firma_id:
        raise ErrorOrdenes('Falta la firma de quien verifica (otra persona).')
    if verifico_firma_id:
        exigir_distintas(conn, realizo_firma_id, verifico_firma_id,
                         'Quien verifica la pesada no puede ser quien pesó.')
    rp, motivo_rp, hora = _registro_posterior(registro_posterior, motivo_registro_posterior, hora_real)
    srv_ordenes.marcar_inicio(conn, orden_id)
    try:
        mov = srv_ordenes._mover(conn, lote_id, 'CONSUMO_PRODUCCION', cantidad, usuario, o['folio'],
                                 f'{"Pesada" if es_mp else "Surtido"} de {c["codigo"]}', realizo_firma_id)
    except ErrorInventario as e:
        raise ErrorOrdenes(str(e))
    cur = conn.execute('INSERT INTO op_pesadas (orden_id, componente_id, lote_id, cantidad, equipo_id, lote_sugerido_id, '
                       'motivo_otro_lote, movimiento_id, realizo_firma_id, verifico_firma_id, registro_posterior, '
                       'motivo_registro_posterior, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (orden_id, componente_id, lote_id, cantidad, equipo_id or None, sugerido,
                        motivo_otro_lote if lote_id != sugerido else None, mov, realizo_firma_id,
                        verifico_firma_id or None, rp, motivo_rp, hora))
    return cur.lastrowid


def devolver(conn, orden_id, componente_id, *, lote_id, cantidad, usuario, firma_id, motivo=None):
    """Regresa a su lote lo surtido que no se usó (DEVOLUCION_PRODUCCION)."""
    o = _orden_abierta(conn, orden_id)
    c = componente(conn, orden_id, componente_id)
    cantidad = numero(cantidad, 'la cantidad')
    if cantidad <= 0:
        raise ErrorOrdenes('La cantidad debe ser mayor que cero.')
    neto = conn.execute('SELECT COALESCE(SUM(cantidad), 0) FROM op_pesadas WHERE componente_id = ? AND lote_id = ?',
                        (componente_id, lote_id)).fetchone()[0]
    if cantidad > neto + _EPSILON:
        raise ErrorOrdenes(f'De ese lote solo se han tomado {fmt(neto)} {c["unidad"]} para esta orden.')
    exigir_firma(conn, firma_id, 'quien devuelve')
    try:
        mov = srv_ordenes._mover(conn, lote_id, 'DEVOLUCION_PRODUCCION', cantidad, usuario, o['folio'],
                                 texto(motivo) or f'Sobrante de {c["codigo"]}', firma_id)
    except ErrorInventario as e:
        raise ErrorOrdenes(str(e))
    cur = conn.execute('INSERT INTO op_pesadas (orden_id, componente_id, lote_id, cantidad, movimiento_id, '
                       'realizo_firma_id) VALUES (?, ?, ?, ?, ?, ?)',
                       (orden_id, componente_id, lote_id, -cantidad, mov, firma_id))
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Pasos
# ---------------------------------------------------------------------------

def paso(conn, orden_id, paso_id):
    for p in srv_ordenes.pasos(conn, orden_id):
        if p['id'] == paso_id:
            return p
    raise ErrorOrdenes('Ese paso no está en la orden.')


def bloqueo_paso(conn, orden_id, p, e=None):
    """Por qué no se puede trabajar el paso todavía (o None)."""
    e = e or srv_ordenes.estado(conn, orden_id)
    if p['estatus'] == 'Hecho':
        return 'El paso ya está hecho.'
    if p['fase'] == 'Fabricación':
        if 'Fabricación' not in e['despejes']:
            return 'Primero el despeje de línea de fabricación.'
        incompletas = [c['codigo'] for c in e['mp'] if not c['completo']]
        if incompletas:
            return 'Faltan pesadas: ' + ', '.join(incompletas) + '.'
    else:
        if 'Envasado' not in e['despejes']:
            return 'Primero el despeje de línea de envasado.'
    anteriores = [x for x in e['pasos'] if x['fase'] == p['fase'] and x['numero'] < p['numero'] and x['estatus'] != 'Hecho']
    if anteriores:
        return f'Primero termina el paso {anteriores[0]["numero"]}.'
    return None


def iniciar_paso(conn, orden_id, paso_id):
    _orden_abierta(conn, orden_id)
    p = paso(conn, orden_id, paso_id)
    if p['estatus'] == 'En curso':
        return
    motivo = bloqueo_paso(conn, orden_id, p)
    if motivo:
        raise ErrorOrdenes(motivo)
    srv_ordenes.marcar_inicio(conn, orden_id)
    conn.execute("UPDATE op_orden_pasos SET estatus = 'En curso', inicio = ? WHERE id = ?", (ahora(), paso_id))


def nivel_expertise(conn, numero_empleado, tarea_id):
    fila = conn.execute('SELECT nivel_pct FROM expertise_evaluaciones WHERE numero_empleado = ? AND tarea_id = ?',
                        (numero_empleado, tarea_id)).fetchone()
    return fila['nivel_pct'] if fila else 0


def calificacion(conn, p, numero_empleado, equipo=None):
    """(nivel o None, aviso o None) de quien firma según la tarea de la Matriz de Expertise."""
    tarea = p['competencia_tarea_id'] or (equipo['competencia_tarea_id'] if equipo else None)
    if not tarea:
        return None, None
    nivel = nivel_expertise(conn, numero_empleado, tarea)
    minimo = parametro_int('ordenes.expertise_minimo', conn)
    if nivel < minimo:
        t = conn.execute('SELECT nombre FROM competencias_tareas WHERE id = ?', (tarea,)).fetchone()
        return nivel, (f'Quien firma tiene {nivel} % en «{t["nombre"] if t else "la tarea"}» y el mínimo es '
                       f'{minimo} %.')
    return nivel, None


def firmar_paso(conn, orden_id, paso_id, *, valores, realizo_firma_id, verifico_firma_id=None, equipo_id=None,
                comentario=None, registro_posterior=0, motivo_registro_posterior=None, inicio_real=None, fin_real=None):
    """Cierra el paso. valores = {valor_id: número}. Devuelve {'fuera_de_rango': bool, 'avisos': [...]}."""
    _orden_abierta(conn, orden_id)
    p = paso(conn, orden_id, paso_id)
    rp = booleano(registro_posterior)
    if p['estatus'] == 'Hecho':
        raise ErrorOrdenes('El paso ya está hecho.')
    if p['estatus'] != 'En curso' and not rp:
        raise ErrorOrdenes('Primero presiona Iniciar en el paso.')
    if rp:
        motivo = bloqueo_paso(conn, orden_id, p)
        if motivo:
            raise ErrorOrdenes(motivo)
    avisos = []
    equipo = None
    if p['equipo_tipo']:
        if not equipo_id:
            raise ErrorOrdenes(f'Elige el equipo ({p["equipo_tipo"]}) que usaste.')
        equipo, av = _equipo_apto(conn, equipo_id, p['equipo_tipo'])
        avisos += av
    elif equipo_id:
        equipo, av = _equipo_apto(conn, equipo_id)
        avisos += av
    medidos, fuera = [], False
    for v in p['valores']:
        bruto = (valores or {}).get(v['id'], (valores or {}).get(str(v['id'])))
        valor = numero(bruto, f'el valor de {v["nombre"]}')
        en_rango = (v['valor_min'] is None or valor >= v['valor_min'] - _EPSILON) and \
                   (v['valor_max'] is None or valor <= v['valor_max'] + _EPSILON)
        fuera = fuera or not en_rango
        medidos.append((v['id'], valor, 1 if en_rango else 0))
    f_realizo = exigir_firma(conn, realizo_firma_id, 'quien hizo el paso')
    if p['es_critico']:
        if not verifico_firma_id:
            raise ErrorOrdenes('Es un paso crítico: falta la firma de quien verifica.')
        exigir_distintas(conn, realizo_firma_id, verifico_firma_id)
    elif verifico_firma_id:
        exigir_distintas(conn, realizo_firma_id, verifico_firma_id)
    nivel, aviso = calificacion(conn, p, f_realizo['numero_empleado'], equipo)
    if aviso:
        if parametro_int('ordenes.bloquear_sin_calificacion', conn):
            raise ErrorOrdenes(aviso + ' No se puede firmar.')
        avisos.append(aviso)
    if rp:
        rp, motivo_rp, fin = _registro_posterior(1, motivo_registro_posterior, fin_real, 'hora de término')
        inicio = fecha_hora(inicio_real, 'hora de inicio') or p['inicio'] or fin
        if inicio > fin:
            raise ErrorOrdenes('La hora de inicio no puede ser después de la de término.')
        srv_ordenes.marcar_inicio(conn, orden_id, inicio)
    else:
        motivo_rp, inicio, fin = None, p['inicio'], ahora()
    for vid, valor, en_rango in medidos:
        conn.execute('UPDATE op_paso_valores SET valor = ?, en_rango = ? WHERE id = ?', (valor, en_rango, vid))
    conn.execute("UPDATE op_orden_pasos SET estatus = 'Hecho', equipo_id = ?, inicio = ?, fin = ?, realizo_firma_id = ?, "
                 'verifico_firma_id = ?, nivel_competencia = ?, fuera_de_rango = ?, comentario = ?, '
                 'registro_posterior = ?, motivo_registro_posterior = ? WHERE id = ?',
                 (equipo_id or None, inicio, fin, realizo_firma_id, verifico_firma_id or None, nivel, 1 if fuera else 0,
                  texto(comentario), rp, motivo_rp, paso_id))
    return {'fuera_de_rango': fuera, 'avisos': avisos}


# ---------------------------------------------------------------------------
# Granel, envasado y conciliación
# ---------------------------------------------------------------------------

def registrar_granel(conn, orden_id, *, cantidad, usuario, firma_id, equipo_id=None):
    """Crea el lote de granel (en CUARENTENA, con el número de lote de la orden) y calcula el rendimiento."""
    o = _orden_abierta(conn, orden_id)
    if o['granel_lote_id']:
        raise ErrorOrdenes('El granel ya está registrado.')
    e = srv_ordenes.estado(conn, orden_id)
    pendientes = [p['numero'] for p in e['pasos_fab'] if p['estatus'] != 'Hecho']
    if pendientes or not e['mp'] or not all(c['completo'] for c in e['mp']):
        raise ErrorOrdenes('Primero completa las pesadas y todos los pasos de fabricación.')
    cantidad = numero(cantidad, 'la cantidad obtenida')
    if cantidad <= 0:
        raise ErrorOrdenes('La cantidad obtenida debe ser mayor que cero.')
    if equipo_id:
        _equipo_apto(conn, equipo_id, que='la báscula o el tanque')
    exigir_firma(conn, firma_id, 'el supervisor')
    rendimiento = round(100.0 * cantidad / o['tamano_lote'], 2)
    fuera = not (o['rendimiento_min'] - _EPSILON <= rendimiento <= o['rendimiento_max'] + _EPSILON)
    try:
        lote_id = inventario.crear_lote(conn, material_id=o['material_id'], cantidad=cantidad, origen='PRODUCCION',
                                        usuario=usuario, modulo='ordenes', numero_lote=o['numero_lote'],
                                        folio=o['folio'], fecha_fabricacion=(o['fecha_inicio'] or ahora())[:10],
                                        fecha_caducidad=o['fecha_caducidad'], estatus='CUARENTENA', firma_id=firma_id)
    except ErrorInventario as ex:
        raise ErrorOrdenes(str(ex))
    conn.execute('UPDATE op_ordenes SET granel_obtenido = ?, granel_equipo_id = ?, granel_ts = ?, rendimiento_pct = ?, '
                 'rendimiento_fuera = ?, granel_lote_id = ?, granel_firma_id = ? WHERE id = ?',
                 (cantidad, equipo_id or None, ahora(), rendimiento, 1 if fuera else 0, lote_id, firma_id, orden_id))
    return {'lote_id': lote_id, 'rendimiento': rendimiento, 'fuera': fuera}


def envasar(conn, orden_id, presentacion_id, *, buenas, rechazadas, usuario, firma_id):
    """Piezas buenas y rechazadas de una presentación. Sale el granel envasado y nace (o crece) el lote de la
    presentación: mismo número de lote, hijo del granel."""
    o = _orden_abierta(conn, orden_id)
    if not o['granel_lote_id']:
        raise ErrorOrdenes('Primero registra el granel.')
    if 'Envasado' not in srv_ordenes.despejes(conn, orden_id):
        raise ErrorOrdenes('Primero el despeje de línea de envasado.')
    fila = conn.execute('SELECT op.*, m.codigo, m.contenido_por_pieza, m.unidad FROM op_orden_presentaciones op '
                        'JOIN materiales m ON m.id = op.presentacion_id WHERE op.orden_id = ? AND op.presentacion_id = ?',
                        (orden_id, presentacion_id)).fetchone()
    if not fila:
        raise ErrorOrdenes('Esa presentación no es de esta orden.')
    if not fila['contenido_por_pieza']:
        raise ErrorOrdenes(f'{fila["codigo"]} no tiene contenido por pieza en el catálogo: corrígelo antes de envasar.')
    buenas = numero(buenas or 0, 'las piezas buenas', minimo=0, entero=True)
    rechazadas = numero(rechazadas or 0, 'las piezas rechazadas', minimo=0, entero=True)
    if buenas + rechazadas <= 0:
        raise ErrorOrdenes('Captura las piezas buenas o rechazadas.')
    exigir_firma(conn, firma_id, 'quien envasó')
    granel = (buenas + rechazadas) * fila['contenido_por_pieza']
    try:
        srv_ordenes._mover(conn, o['granel_lote_id'], 'CONSUMO_PRODUCCION', granel, usuario, o['folio'],
                           f'Envasado de {buenas + rechazadas} pzas de {fila["codigo"]}', firma_id)
        lote_id = fila['lote_id']
        if buenas:
            if lote_id:
                srv_ordenes._mover(conn, lote_id, 'ENTRADA_PRODUCCION', buenas, usuario, o['folio'],
                                   'Envasado adicional', firma_id)
            else:
                lote_id = inventario.crear_lote(
                    conn, material_id=presentacion_id, cantidad=buenas, origen='PRODUCCION', usuario=usuario,
                    modulo='ordenes', numero_lote=o['numero_lote'], folio=o['folio'], lote_padre_id=o['granel_lote_id'],
                    fecha_fabricacion=(o['fecha_inicio'] or ahora())[:10], fecha_caducidad=o['fecha_caducidad'],
                    firma_id=firma_id)
    except ErrorInventario as ex:
        raise ErrorOrdenes(str(ex))
    conn.execute('UPDATE op_orden_presentaciones SET piezas_buenas = piezas_buenas + ?, '
                 'piezas_rechazadas = piezas_rechazadas + ?, lote_id = ?, firma_id = ? WHERE id = ?',
                 (buenas, rechazadas, lote_id, firma_id, fila['id']))
    return lote_id


def conciliar(conn, orden_id, material_id, *, usadas, danadas=0, firma_id):
    """Material impreso: entregadas = usadas + dañadas + devueltas. Las entregadas y devueltas salen del
    surtido (las devoluciones se registran con devolver()). Devuelve la diferencia."""
    _orden_abierta(conn, orden_id)
    m = next((x for x in srv_ordenes.materiales_impresos(conn, orden_id) if x['material_id'] == material_id), None)
    if not m:
        raise ErrorOrdenes('Ese material impreso no está en la orden.')
    if 'Envasado' not in srv_ordenes.despejes(conn, orden_id):
        raise ErrorOrdenes('Primero el despeje de línea de envasado.')
    usadas = numero(usadas, 'las piezas usadas', minimo=0)
    danadas = numero(danadas or 0, 'las piezas dañadas', minimo=0)
    exigir_firma(conn, firma_id, 'quien concilia')
    diferencia = round(m['entregadas'] - usadas - danadas - m['devueltas'], 6)
    conn.execute('INSERT INTO op_conciliaciones (orden_id, material_id, entregadas, usadas, danadas, devueltas, '
                 'diferencia, firma_id, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) '
                 'ON CONFLICT(orden_id, material_id) DO UPDATE SET entregadas = excluded.entregadas, '
                 'usadas = excluded.usadas, danadas = excluded.danadas, devueltas = excluded.devueltas, '
                 'diferencia = excluded.diferencia, firma_id = excluded.firma_id, ts = excluded.ts, '
                 'justificacion = NULL, justificacion_firma_id = NULL',
                 (orden_id, material_id, m['entregadas'], usadas, danadas, m['devueltas'], diferencia, firma_id, ahora()))
    return diferencia


# ---------------------------------------------------------------------------
# Justificación firmada (solo si no está instalado el módulo de Desviaciones)
# ---------------------------------------------------------------------------

def justificar(conn, orden_id, *, origen, origen_id, justificacion, firma_id):
    """origen: orden_paso (id del paso) · orden_granel (id de la orden) · orden_conciliacion (id)."""
    if srv_ordenes.desviaciones_instalado():
        raise ErrorOrdenes('Está instalado el módulo de Desviaciones: reporta una desviación en lugar de justificar.')
    o = srv_ordenes.orden(conn, orden_id)
    if o['estatus'] not in ('EMITIDA', 'EN_PROCESO', 'TERMINADA'):
        raise ErrorOrdenes('La orden ya no admite justificaciones.')
    justificacion = texto(justificacion)
    if not justificacion:
        raise ErrorOrdenes('Escribe la justificación.')
    f = exigir_firma(conn, firma_id, 'el supervisor')
    if origen == 'orden_paso':
        cur = conn.execute('UPDATE op_orden_pasos SET justificacion = ?, justificacion_firma_id = ? '
                           'WHERE id = ? AND orden_id = ? AND fuera_de_rango = 1', (justificacion, f['id'], origen_id,
                                                                                    orden_id))
    elif origen == 'orden_granel':
        cur = conn.execute('UPDATE op_ordenes SET rendimiento_justificacion = ?, rendimiento_justificacion_firma_id = ? '
                           'WHERE id = ? AND rendimiento_fuera = 1', (justificacion, f['id'], orden_id))
    elif origen == 'orden_conciliacion':
        cur = conn.execute('UPDATE op_conciliaciones SET justificacion = ?, justificacion_firma_id = ? '
                           'WHERE id = ? AND orden_id = ? AND ABS(diferencia) > 0.000001',
                           (justificacion, f['id'], origen_id, orden_id))
    else:
        raise ErrorOrdenes('No hay nada que justificar ahí.')
    if not cur.rowcount:
        raise ErrorOrdenes('No hay nada que justificar ahí.')


def nombre_firmante(conn, firma_id):
    f = firma_fila(conn, firma_id)
    return f['nombre'] if f else None
