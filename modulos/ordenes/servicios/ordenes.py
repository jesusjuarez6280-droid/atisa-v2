"""Órdenes de producción: crear, emitir, cancelar, terminar y revisar; estado y faltantes.

Reglas (02-ordenes-produccion.md, sección 6):
- Solo salen de una fórmula VIGENTE. Al emitir (firma de emitir_ordenes) se copia la fórmula
  escalada al tamaño del lote, se asigna el número de lote y se calcula la caducidad.
- Terminar solo con todo completo (faltantes() lo lista) y firma del supervisor.
- Revisar: persona distinta de quien terminó.
- Cancelar: nunca después de terminada; si ya se pesó, primero se devuelve lo pesado.
"""
from nucleo import folios, inventario, registro
from nucleo.inventario import ErrorInventario
from nucleo.parametros import parametro

from . import (MODULO, ErrorOrdenes, _EPSILON, ahora, exigir_distintas, exigir_facultad, exigir_firma,
               fin_de_mes_mas, fmt, hoy, numero, texto)
from . import formulas as srv_formulas

ESTATUS = ('BORRADOR', 'EMITIDA', 'EN_PROCESO', 'TERMINADA', 'REVISADA', 'CANCELADA')
ABIERTAS = ('EMITIDA', 'EN_PROCESO')
NOMBRE_ESTATUS = {'BORRADOR': 'Borrador', 'EMITIDA': 'Emitida', 'EN_PROCESO': 'En proceso',
                  'TERMINADA': 'Terminada', 'REVISADA': 'Revisada', 'CANCELADA': 'Cancelada'}

_SQL_ORDEN = ('SELECT o.*, m.codigo AS producto_codigo, m.nombre AS producto_nombre, m.unidad AS unidad, '
              'm.vida_util_meses AS vida_util_meses, f.version AS formula_version, '
              'f.codigo_documento AS formula_documento, f.rendimiento_min, f.rendimiento_max, '
              'f.tamano_lote AS tamano_estandar, l.estatus AS granel_estatus, l.existencia AS granel_existencia '
              'FROM op_ordenes o JOIN materiales m ON m.id = o.material_id JOIN op_formulas f ON f.id = o.formula_id '
              'LEFT JOIN lotes l ON l.id = o.granel_lote_id ')


def orden(conn, orden_id):
    fila = conn.execute(_SQL_ORDEN + 'WHERE o.id = ?', (orden_id,)).fetchone()
    if not fila:
        raise ErrorOrdenes('La orden no existe.')
    return fila


def orden_o_none(conn, orden_id):
    return conn.execute(_SQL_ORDEN + 'WHERE o.id = ?', (orden_id,)).fetchone()


# ---------------------------------------------------------------------------
# Desviaciones ligadas (si el módulo está instalado) y justificaciones
# ---------------------------------------------------------------------------

def desviaciones_instalado():
    return registro.hay_consulta('desviaciones_de')


def desviaciones(conn, origen, origen_id):
    """Desviaciones no anuladas de ese origen ([] si el módulo no está instalado)."""
    if not desviaciones_instalado():
        return []
    try:
        lista = registro.consulta('desviaciones_de', conn, origen, origen_id, default=[]) or []
    except Exception:  # un módulo roto no debe tumbar la orden; cuenta como "sin desviación"
        return []
    return [d for d in lista if str((d.get('estatus') if isinstance(d, dict) else '') or '').upper() != 'ANULADA']


# ---------------------------------------------------------------------------
# Estado completo de una orden (lo usan el tablero, el detalle, faltantes y ganchos)
# ---------------------------------------------------------------------------

def _pesado(conn, componente_id):
    return conn.execute('SELECT COALESCE(SUM(cantidad), 0) FROM op_pesadas WHERE componente_id = ?',
                        (componente_id,)).fetchone()[0]


def limites(c):
    """(mínimo, máximo) aceptables de una línea según su tolerancia."""
    tol = (c['tolerancia_pct'] or 0) / 100.0
    return c['cantidad_teorica'] * (1 - tol), c['cantidad_teorica'] * (1 + tol)


def componentes(conn, orden_id):
    filas = conn.execute(
        'SELECT c.*, m.codigo, m.nombre, m.unidad, p.codigo AS presentacion_codigo, p.nombre AS presentacion_nombre '
        'FROM op_orden_componentes c JOIN materiales m ON m.id = c.material_id '
        'LEFT JOIN materiales p ON p.id = c.presentacion_id WHERE c.orden_id = ? ORDER BY c.linea, c.id',
        (orden_id,)).fetchall()
    salida = []
    for c in filas:
        d = dict(c)
        d['pesado'] = round(_pesado(conn, c['id']), 6)
        d['minimo'], d['maximo'] = limites(c)
        d['falta'] = max(0.0, round(c['cantidad_teorica'] - d['pesado'], 6))
        d['completo'] = d['minimo'] - _EPSILON <= d['pesado'] <= d['maximo'] + _EPSILON
        salida.append(d)
    return salida


def pasos(conn, orden_id):
    filas = conn.execute(
        'SELECT p.*, e.codigo AS equipo_codigo, e.nombre AS equipo_nombre FROM op_orden_pasos p '
        'LEFT JOIN equipos e ON e.id = p.equipo_id WHERE p.orden_id = ? ORDER BY p.numero, p.id',
        (orden_id,)).fetchall()
    salida = []
    for p in filas:
        d = dict(p)
        d['valores'] = [dict(v) for v in conn.execute('SELECT * FROM op_paso_valores WHERE paso_id = ? ORDER BY id',
                                                      (p['id'],)).fetchall()]
        salida.append(d)
    return salida


def presentaciones(conn, orden_id):
    return [dict(p) for p in conn.execute(
        'SELECT op.*, m.codigo, m.nombre, m.unidad, m.contenido_por_pieza, l.numero_lote, l.estatus AS lote_estatus '
        'FROM op_orden_presentaciones op JOIN materiales m ON m.id = op.presentacion_id '
        'LEFT JOIN lotes l ON l.id = op.lote_id WHERE op.orden_id = ? ORDER BY m.codigo', (orden_id,)).fetchall()]


def despejes(conn, orden_id):
    return {d['momento']: dict(d) for d in conn.execute('SELECT * FROM op_despejes WHERE orden_id = ?',
                                                         (orden_id,)).fetchall()}


def conciliaciones(conn, orden_id):
    return {c['material_id']: dict(c) for c in conn.execute('SELECT * FROM op_conciliaciones WHERE orden_id = ?',
                                                             (orden_id,)).fetchall()}


def materiales_impresos(conn, orden_id):
    """[{material_id, codigo, nombre, unidad, entregadas, devueltas}] del material impreso de la orden."""
    return [dict(r) for r in conn.execute(
        'SELECT c.material_id, m.codigo, m.nombre, m.unidad, '
        'COALESCE(SUM(CASE WHEN p.cantidad > 0 THEN p.cantidad END), 0) AS entregadas, '
        'COALESCE(-SUM(CASE WHEN p.cantidad < 0 THEN p.cantidad END), 0) AS devueltas '
        'FROM op_orden_componentes c JOIN materiales m ON m.id = c.material_id '
        'LEFT JOIN op_pesadas p ON p.componente_id = c.id '
        'WHERE c.orden_id = ? AND c.es_impreso = 1 GROUP BY c.material_id ORDER BY m.codigo', (orden_id,)).fetchall()]


def problemas(conn, o, lista_pasos=None, concs=None):
    """Lo que salió mal y sigue sin desviación ni justificación: [{origen, origen_id, texto}]."""
    salida = []
    lista_pasos = pasos(conn, o['id']) if lista_pasos is None else lista_pasos
    for p in lista_pasos:
        if p['fuera_de_rango'] and not p['justificacion_firma_id'] and not desviaciones(conn, 'orden_paso', p['id']):
            salida.append({'origen': 'orden_paso', 'origen_id': p['id'],
                           'texto': f'Paso {p["numero"]}: valor fuera de rango sin desviación'})
    if o['rendimiento_fuera'] and not o['rendimiento_justificacion_firma_id'] \
            and not desviaciones(conn, 'orden_granel', o['id']):
        salida.append({'origen': 'orden_granel', 'origen_id': o['id'],
                       'texto': f'Rendimiento {fmt(o["rendimiento_pct"], 2)} % fuera de límites sin desviación'})
    concs = conciliaciones(conn, o['id']) if concs is None else concs
    for c in concs.values():
        if abs(c['diferencia']) > _EPSILON and not c['justificacion_firma_id'] \
                and not desviaciones(conn, 'orden_conciliacion', c['id']):
            m = conn.execute('SELECT codigo FROM materiales WHERE id = ?', (c['material_id'],)).fetchone()
            salida.append({'origen': 'orden_conciliacion', 'origen_id': c['id'],
                           'texto': f'Conciliación de {m["codigo"]} no cuadra ({fmt(c["diferencia"])}) sin desviación'})
    return salida


def estado(conn, orden_id):
    """Todo lo de la orden en un dict: orden, componentes (MP y envase/empaque), pasos, despejes,
    presentaciones, impresos, conciliaciones, problemas, faltantes, bloques y avance."""
    o = orden(conn, orden_id)
    comps = componentes(conn, orden_id)
    lpasos = pasos(conn, orden_id)
    desp = despejes(conn, orden_id)
    pres = presentaciones(conn, orden_id)
    concs = conciliaciones(conn, orden_id)
    impresos = materiales_impresos(conn, orden_id)
    mp = [c for c in comps if c['tipo'] == 'MP']
    surtido = [c for c in comps if c['tipo'] != 'MP']
    pasos_fab = [p for p in lpasos if p['fase'] == 'Fabricación']
    pasos_env = [p for p in lpasos if p['fase'] == 'Envasado']
    pres_plan = [p for p in pres if p['piezas_planeadas'] > 0]
    hay_envasado = bool(pres_plan or pasos_env)
    probs = problemas(conn, o, lpasos, concs) if o['estatus'] not in ('BORRADOR', 'CANCELADA') else []

    faltan = []
    if o['estatus'] in ('BORRADOR', 'EMITIDA', 'EN_PROCESO'):
        if 'Fabricación' not in desp:
            faltan.append('Despeje de línea de fabricación')
        for c in mp:
            if not c['completo']:
                faltan.append(f'Pesar {c["codigo"]} {c["nombre"]} (lleva {fmt(c["pesado"])} de '
                              f'{fmt(c["cantidad_teorica"])} {c["unidad"]})')
        for p in pasos_fab:
            if p['estatus'] != 'Hecho':
                faltan.append(f'Paso {p["numero"]} de fabricación')
        if not o['granel_lote_id']:
            faltan.append('Registrar el granel obtenido')
        if hay_envasado:
            if 'Envasado' not in desp:
                faltan.append('Despeje de línea de envasado')
            for p in pasos_env:
                if p['estatus'] != 'Hecho':
                    faltan.append(f'Paso {p["numero"]} de envasado')
            for p in pres_plan:
                if p['piezas_buenas'] + p['piezas_rechazadas'] <= 0:
                    faltan.append(f'Envasar {p["codigo"]} {p["nombre"]}')
            for c in surtido:
                if c['pesado'] <= _EPSILON and not c['es_impreso']:
                    faltan.append(f'Surtir {c["codigo"]} {c["nombre"]}')
            for m in impresos:
                if m['material_id'] not in concs:
                    faltan.append(f'Conciliar {m["codigo"]} {m["nombre"]}')
        faltan += [p['texto'] for p in probs]

    bloques = [
        {'clave': 'despeje', 'titulo': 'Despeje de fabricación', 'listo': 'Fabricación' in desp},
        {'clave': 'pesadas', 'titulo': 'Pesadas', 'listo': bool(mp) and all(c['completo'] for c in mp),
         'detalle': f'{sum(1 for c in mp if c["completo"])} de {len(mp)}'},
        {'clave': 'pasos', 'titulo': 'Pasos de fabricación', 'listo': all(p['estatus'] == 'Hecho' for p in pasos_fab),
         'detalle': f'{sum(1 for p in pasos_fab if p["estatus"] == "Hecho")} de {len(pasos_fab)}'},
        {'clave': 'granel', 'titulo': 'Granel', 'listo': bool(o['granel_lote_id'])},
    ]
    if hay_envasado:
        bloques.append({'clave': 'envasado', 'titulo': 'Envasado',
                        'listo': 'Envasado' in desp and all(p['piezas_buenas'] + p['piezas_rechazadas'] > 0
                                                             for p in pres_plan)
                        and all(p['estatus'] == 'Hecho' for p in pasos_env)
                        and all(m['material_id'] in concs for m in impresos)})
    bloques.append({'clave': 'terminar', 'titulo': 'Terminar', 'listo': o['estatus'] in ('TERMINADA', 'REVISADA')})

    # Avance: renglones hechos / renglones totales
    total = 1 + len(mp) + len(lpasos) + 1 + (1 + len(pres_plan) + len(impresos) if hay_envasado else 0) + 1
    hechos = (('Fabricación' in desp) + sum(c['completo'] for c in mp)
              + sum(p['estatus'] == 'Hecho' for p in lpasos) + bool(o['granel_lote_id'])
              + ((('Envasado' in desp) + sum(p['piezas_buenas'] + p['piezas_rechazadas'] > 0 for p in pres_plan)
                  + sum(m['material_id'] in concs for m in impresos)) if hay_envasado else 0)
              + (o['estatus'] in ('TERMINADA', 'REVISADA')))
    avance = 100 if o['estatus'] in ('TERMINADA', 'REVISADA') else int(round(100.0 * hechos / total)) if total else 0
    return {'orden': o, 'componentes': comps, 'mp': mp, 'surtido': surtido, 'pasos': lpasos,
            'pasos_fab': pasos_fab, 'pasos_env': pasos_env, 'despejes': desp, 'presentaciones': pres,
            'impresos': impresos, 'conciliaciones': concs, 'problemas': probs, 'faltantes': faltan,
            'bloques': bloques, 'avance': avance, 'hay_envasado': hay_envasado}


def faltantes(conn, orden_id):
    return estado(conn, orden_id)['faltantes']


def avance(conn, orden_id):
    return estado(conn, orden_id)['avance']


# ---------------------------------------------------------------------------
# Crear y disponibilidad
# ---------------------------------------------------------------------------

def requerimientos(conn, formula_id, factor, piezas=None):
    """Lo que pediría una orden: [{material_id, codigo, nombre, unidad, tipo, presentacion_id, cantidad,
    tolerancia_pct, es_activo, es_impreso}] (MP escaladas; envase/empaque por piezas planeadas)."""
    salida = []
    for c in srv_formulas.componentes(conn, formula_id):
        salida.append({'material_id': c['material_id'], 'codigo': c['codigo'], 'nombre': c['nombre'],
                       'unidad': c['unidad'], 'tipo': 'MP', 'presentacion_id': None,
                       'cantidad': round(c['cantidad'] * factor, 6), 'tolerancia_pct': c['tolerancia_pct'],
                       'es_activo': c['es_activo'], 'es_impreso': 0})
    piezas = piezas or {}
    for e in srv_formulas.empaque(conn, formula_id):
        n = int(piezas.get(e['presentacion_id'], 0) or 0)
        if n <= 0:
            continue
        salida.append({'material_id': e['material_id'], 'codigo': e['codigo'], 'nombre': e['nombre'],
                       'unidad': e['unidad'], 'tipo': e['tipo'], 'presentacion_id': e['presentacion_id'],
                       'cantidad': round(e['cantidad_por_pieza'] * n, 6), 'tolerancia_pct': e['tolerancia_pct'],
                       'es_activo': 0, 'es_impreso': e['es_impreso']})
    return salida


def disponibilidad(conn, formula_id, factor, piezas=None):
    """Por material: lo que pide la orden, lo aprobado y vigente, lo que está en cuarentena y lo que falta.
    Es aviso, no candado: el candado está al pesar."""
    juntos = {}
    for r in requerimientos(conn, formula_id, factor, piezas):
        d = juntos.setdefault(r['material_id'], {**r, 'cantidad': 0.0})
        d['cantidad'] += r['cantidad']
    salida = []
    for d in juntos.values():
        aprobado = inventario.existencia_material(conn, d['material_id'])
        cuarentena = conn.execute("SELECT COALESCE(SUM(existencia), 0) FROM lotes WHERE material_id = ? "
                                  "AND estatus = 'CUARENTENA'", (d['material_id'],)).fetchone()[0]
        salida.append({**d, 'aprobado': aprobado, 'cuarentena': cuarentena,
                       'falta': max(0.0, round(d['cantidad'] - aprobado, 6))})
    return salida


def granel_necesario(conn, formula_id, piezas):
    """Granel teórico que piden las piezas planeadas (en la unidad del granel)."""
    total = 0.0
    for pid, n in (piezas or {}).items():
        m = conn.execute('SELECT contenido_por_pieza FROM materiales WHERE id = ?', (pid,)).fetchone()
        if m and m['contenido_por_pieza']:
            total += (m['contenido_por_pieza'] or 0) * int(n or 0)
    return total


def crear(conn, *, formula_id, tamano_lote, usuario, fecha_programada=None, piezas=None, observaciones=None):
    """Orden en BORRADOR (con su folio OP-AAAA-####). piezas = {presentacion_id: piezas planeadas}.
    Devuelve (orden_id, avisos)."""
    f = srv_formulas.formula(conn, formula_id)
    if f['estatus'] != 'VIGENTE':
        raise ErrorOrdenes('Las órdenes solo salen de una fórmula vigente.')
    tamano = numero(tamano_lote, 'el tamaño de lote') if tamano_lote not in (None, '') else f['tamano_lote']
    if tamano <= 0:
        raise ErrorOrdenes('El tamaño de lote debe ser mayor que cero.')
    validas = {p['id'] for p in srv_formulas.presentaciones(conn, f['material_id'])}
    limpias = {}
    for pid, n in (piezas or {}).items():
        n = numero(n or 0, 'las piezas planeadas', minimo=0, entero=True)
        if int(pid) not in validas:
            raise ErrorOrdenes('Una de las presentaciones no es de este producto.')
        limpias[int(pid)] = n
    avisos = []
    necesario = granel_necesario(conn, formula_id, limpias)
    if necesario > tamano + _EPSILON:
        avisos.append(f'Las piezas planeadas piden {fmt(necesario)} {f["unidad"]} de granel y el lote es de '
                      f'{fmt(tamano)} {f["unidad"]}: no alcanza.')
    folio = folios.siguiente_folio(conn, 'OP')
    cur = conn.execute('INSERT INTO op_ordenes (folio, formula_id, material_id, tamano_lote, factor, fecha_programada, '
                       'observaciones, creo) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       (folio, formula_id, f['material_id'], tamano, tamano / f['tamano_lote'],
                        texto(fecha_programada), texto(observaciones), usuario))
    orden_id = cur.lastrowid
    for p in srv_formulas.presentaciones(conn, f['material_id']):
        conn.execute('INSERT INTO op_orden_presentaciones (orden_id, presentacion_id, piezas_planeadas) VALUES (?, ?, ?)',
                     (orden_id, p['id'], limpias.get(p['id'], 0)))
    return orden_id, avisos


# ---------------------------------------------------------------------------
# Emitir
# ---------------------------------------------------------------------------

def _numero_lote_libre(conn, material_id):
    formato = parametro('nucleo.formato_lote_produccion', conn)
    for _ in range(200):
        n = folios.numero_lote(conn, formato)
        ocupado = conn.execute('SELECT 1 FROM op_ordenes WHERE numero_lote = ?', (n,)).fetchone() or \
            conn.execute('SELECT 1 FROM lotes WHERE material_id = ? AND numero_lote = ?', (material_id, n)).fetchone()
        if not ocupado:
            return n
        if '{#' not in formato:
            break
    raise ErrorOrdenes(f'No se pudo generar un número de lote libre con el formato «{formato}»; revisa el parámetro.')


def caducidad(conn, material_id, fecha_fabricacion):
    m = conn.execute('SELECT vida_util_meses FROM materiales WHERE id = ?', (material_id,)).fetchone()
    if not m or not m['vida_util_meses']:
        return None
    return fin_de_mes_mas(fecha_fabricacion, m['vida_util_meses'])


def emitir(conn, orden_id, firma_id):
    """Copia escalada de componentes, pasos, valores y envase/empaque; número de lote y caducidad."""
    o = orden(conn, orden_id)
    if o['estatus'] != 'BORRADOR':
        raise ErrorOrdenes(f'La orden {o["folio"]} ya no está en borrador.')
    exigir_facultad(conn, firma_id, 'emitir_ordenes', 'emitir la orden')
    f = srv_formulas.formula(conn, o['formula_id'])
    if f['estatus'] != 'VIGENTE':
        raise ErrorOrdenes(f'La fórmula v{f["version"]} ya no está vigente: cancela esta orden y crea otra.')
    piezas = {p['presentacion_id']: p['piezas_planeadas'] for p in presentaciones(conn, orden_id)}
    for linea, r in enumerate(requerimientos(conn, o['formula_id'], o['factor'], piezas), start=1):
        conn.execute('INSERT INTO op_orden_componentes (orden_id, linea, material_id, tipo, presentacion_id, '
                     'cantidad_teorica, tolerancia_pct, es_activo, es_impreso) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (orden_id, linea, r['material_id'], r['tipo'], r['presentacion_id'], r['cantidad'],
                      r['tolerancia_pct'], r['es_activo'], r['es_impreso']))
    for p in srv_formulas.pasos(conn, o['formula_id']):
        cur = conn.execute('INSERT INTO op_orden_pasos (orden_id, numero, fase, instruccion, minutos, imagen_ruta, '
                           'es_critico, equipo_tipo, competencia_tarea_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                           (orden_id, p['numero'], p['fase'], p['instruccion'], p['minutos'], p['imagen_ruta'],
                            p['es_critico'], p['equipo_tipo'], p['competencia_tarea_id']))
        for x in p['parametros']:
            conn.execute('INSERT INTO op_paso_valores (paso_id, nombre, unidad, valor_min, valor_max) '
                         'VALUES (?, ?, ?, ?, ?)', (cur.lastrowid, x['nombre'], x['unidad'], x['valor_min'],
                                                    x['valor_max']))
    lote = _numero_lote_libre(conn, o['material_id'])
    conn.execute("UPDATE op_ordenes SET estatus = 'EMITIDA', numero_lote = ?, fecha_emision = ?, fecha_caducidad = ?, "
                 'emitio_firma_id = ? WHERE id = ?',
                 (lote, ahora(), caducidad(conn, o['material_id'], o['fecha_programada'] or hoy()), firma_id, orden_id))
    return lote


def marcar_inicio(conn, orden_id, cuando=None):
    """Primera firma en planta: EMITIDA → EN_PROCESO; la fecha de fabricación fija la caducidad."""
    o = orden(conn, orden_id)
    if o['estatus'] == 'EMITIDA':
        cuando = cuando or ahora()
        conn.execute("UPDATE op_ordenes SET estatus = 'EN_PROCESO', fecha_inicio = ?, fecha_caducidad = ? WHERE id = ?",
                     (cuando, caducidad(conn, o['material_id'], cuando[:10]), orden_id))


# ---------------------------------------------------------------------------
# Cancelar, terminar, revisar
# ---------------------------------------------------------------------------

def cancelar(conn, orden_id, *, motivo, usuario, firma_id=None):
    """BORRADOR: con motivo. EMITIDA / EN_PROCESO (sin granel): firma de emitir_ordenes; lo pesado
    regresa a sus lotes. Nunca después de terminada."""
    o = orden(conn, orden_id)
    motivo = texto(motivo)
    if not motivo:
        raise ErrorOrdenes('Escribe el motivo de la cancelación.')
    if o['estatus'] in ('TERMINADA', 'REVISADA', 'CANCELADA'):
        raise ErrorOrdenes(f'La orden {o["folio"]} está {NOMBRE_ESTATUS[o["estatus"]].lower()}: ya no se cancela.')
    if o['estatus'] != 'BORRADOR':
        exigir_facultad(conn, firma_id, 'emitir_ordenes', 'cancelar una orden emitida')
        if o['granel_lote_id']:
            raise ErrorOrdenes('El granel ya se registró: la orden ya no se cancela; termínala y reporta una desviación.')
        netos = conn.execute('SELECT p.componente_id, p.lote_id, SUM(p.cantidad) AS neto FROM op_pesadas p '
                             'WHERE p.orden_id = ? GROUP BY p.componente_id, p.lote_id HAVING SUM(p.cantidad) > 0',
                             (orden_id,)).fetchall()
        for n in netos:
            try:
                mov = _mover(conn, n['lote_id'], 'DEVOLUCION_PRODUCCION', n['neto'], usuario, o['folio'],
                             f'Cancelación de {o["folio"]}: {motivo}', firma_id)
            except ErrorInventario as e:
                raise ErrorOrdenes(f'No se pudo devolver lo pesado: {e} Reporta una desviación.')
            conn.execute('INSERT INTO op_pesadas (orden_id, componente_id, lote_id, cantidad, movimiento_id, '
                         'realizo_firma_id) VALUES (?, ?, ?, ?, ?, ?)',
                         (orden_id, n['componente_id'], n['lote_id'], -n['neto'], mov, firma_id))
    conn.execute("UPDATE op_ordenes SET estatus = 'CANCELADA', motivo_cancelacion = ?, cancelo_firma_id = ?, "
                 'fecha_fin = ? WHERE id = ?', (motivo, firma_id, ahora(), orden_id))


def _mover(conn, lote_id, tipo, cantidad, usuario, folio, motivo=None, firma_id=None):
    """inventario.mover() y devuelve el id del movimiento que quedó en el kárdex."""
    inventario.mover(conn, lote_id=lote_id, tipo=tipo, cantidad=cantidad, usuario=usuario, modulo=MODULO,
                     folio=folio, motivo=motivo, firma_id=firma_id)
    return conn.execute('SELECT MAX(id) FROM lote_movimientos WHERE lote_id = ?', (lote_id,)).fetchone()[0]


def terminar(conn, orden_id, *, firma_id, usuario, motivo_merma=None):
    """Con todo completo. El granel que sobra se da de baja como merma (con motivo)."""
    e = estado(conn, orden_id)
    o = e['orden']
    if o['estatus'] != 'EN_PROCESO':
        raise ErrorOrdenes(f'La orden {o["folio"]} no está en proceso.')
    if e['faltantes']:
        raise ErrorOrdenes('Todavía falta: ' + '; '.join(e['faltantes']) + '.')
    exigir_firma(conn, firma_id, 'el supervisor')
    sobrante = o['granel_existencia'] or 0
    if sobrante > _EPSILON:
        motivo_merma = texto(motivo_merma)
        if not motivo_merma:
            raise ErrorOrdenes(f'Quedan {fmt(sobrante)} {o["unidad"]} de granel: escribe el motivo de la merma.')
        try:
            _mover(conn, o['granel_lote_id'], 'CONSUMO_PRODUCCION', sobrante, usuario, o['folio'],
                   f'Merma de granel: {motivo_merma}', firma_id)
        except ErrorInventario as ex:
            raise ErrorOrdenes(str(ex))
    conn.execute("UPDATE op_ordenes SET estatus = 'TERMINADA', termino_firma_id = ?, fecha_fin = ?, merma_granel = ?, "
                 'motivo_merma = ? WHERE id = ?',
                 (firma_id, ahora(), round(sobrante, 6) if sobrante > _EPSILON else 0,
                  texto(motivo_merma) if sobrante > _EPSILON else None, orden_id))


def revisar(conn, orden_id, firma_id):
    o = orden(conn, orden_id)
    if o['estatus'] != 'TERMINADA':
        raise ErrorOrdenes('Solo se revisa el expediente de una orden terminada.')
    exigir_firma(conn, firma_id, 'quien revisa')
    exigir_distintas(conn, o['termino_firma_id'], firma_id, 'La revisión la hace una persona distinta de quien terminó.')
    probs = problemas(conn, o)
    if probs:
        raise ErrorOrdenes('Antes de revisar: ' + '; '.join(p['texto'] for p in probs) + '.')
    conn.execute("UPDATE op_ordenes SET estatus = 'REVISADA', reviso_firma_id = ?, fecha_revision = ? WHERE id = ?",
                 (firma_id, ahora(), orden_id))


def lote_de_orden(conn, lote_id):
    """La orden donde nació un lote (granel o presentación), o None."""
    fila = conn.execute('SELECT id FROM op_ordenes WHERE granel_lote_id = ?', (lote_id,)).fetchone()
    if not fila:
        fila = conn.execute('SELECT orden_id AS id FROM op_orden_presentaciones WHERE lote_id = ?', (lote_id,)).fetchone()
    return orden(conn, fila['id']) if fila else None
