"""Fórmulas maestras: producto (granel + presentaciones), versiones y su aprobación.

Reglas (02-ordenes-produccion.md, sección 6.1):
- Una sola versión VIGENTE por producto; las órdenes solo salen de una vigente.
- Solo se edita una fórmula en BORRADOR; una vigente se cambia con "Nueva versión".
- Revisó (producción) y Aprobó (calidad, aprobar_maestros) deben ser personas distintas.
- Al aprobar una versión, la anterior queda OBSOLETA.
"""
import os
import shutil
import uuid

from nucleo import inventario
from nucleo.inventario import ErrorInventario

from . import ErrorOrdenes, ahora, booleano, exigir_distintas, exigir_facultad, exigir_firma, numero, texto

FASES = ('Fabricación', 'Envasado')
CARPETA_IMAGENES = 'img_formulas'


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------

def formula(conn, formula_id):
    fila = conn.execute(
        'SELECT f.*, m.codigo AS material_codigo, m.nombre AS material_nombre, m.unidad AS unidad, '
        'm.vida_util_meses AS vida_util_meses FROM op_formulas f JOIN materiales m ON m.id = f.material_id '
        'WHERE f.id = ?', (formula_id,)).fetchone()
    if not fila:
        raise ErrorOrdenes('La fórmula no existe.')
    return fila


def componentes(conn, formula_id):
    return conn.execute(
        'SELECT c.*, m.codigo, m.nombre, m.unidad, m.tipo FROM op_formula_componentes c '
        'JOIN materiales m ON m.id = c.material_id WHERE c.formula_id = ? ORDER BY c.orden, c.id',
        (formula_id,)).fetchall()


def pasos(conn, formula_id):
    """Pasos con sus parámetros: [{...paso, 'parametros': [...]}]."""
    filas = conn.execute(
        'SELECT p.*, t.nombre AS tarea_nombre FROM op_formula_pasos p '
        'LEFT JOIN competencias_tareas t ON t.id = p.competencia_tarea_id '
        'WHERE p.formula_id = ? ORDER BY p.numero, p.id', (formula_id,)).fetchall()
    salida = []
    for p in filas:
        d = dict(p)
        d['parametros'] = [dict(x) for x in conn.execute(
            'SELECT * FROM op_formula_parametros WHERE paso_id = ? ORDER BY id', (p['id'],)).fetchall()]
        salida.append(d)
    return salida


def empaque(conn, formula_id):
    return conn.execute(
        'SELECT e.*, p.codigo AS presentacion_codigo, p.nombre AS presentacion_nombre, '
        'm.codigo, m.nombre, m.unidad, m.tipo, m.es_impreso FROM op_formula_empaque e '
        'JOIN materiales p ON p.id = e.presentacion_id JOIN materiales m ON m.id = e.material_id '
        'WHERE e.formula_id = ? ORDER BY p.codigo, e.id', (formula_id,)).fetchall()


def presentaciones(conn, granel_id):
    return conn.execute("SELECT * FROM materiales WHERE material_padre_id = ? AND tipo = 'PT' AND activo = 1 "
                        'ORDER BY codigo', (granel_id,)).fetchall()


def vigente(conn, material_id):
    return conn.execute("SELECT * FROM op_formulas WHERE material_id = ? AND estatus = 'VIGENTE'",
                        (material_id,)).fetchone()


def _editable(conn, formula_id):
    f = formula(conn, formula_id)
    if f['estatus'] != 'BORRADOR':
        if f['estatus'] == 'VIGENTE':
            raise ErrorOrdenes('Una fórmula vigente no se edita: crea una nueva versión.')
        raise ErrorOrdenes(f'La fórmula v{f["version"]} está {f["estatus"].replace("_", " ").lower()}: '
                           'solo se edita en borrador.')
    return f


# ---------------------------------------------------------------------------
# Producto
# ---------------------------------------------------------------------------

def crear_producto(conn, *, granel, presentaciones=(), usuario):
    """granel = {codigo, nombre, unidad, vida_util_meses, registro_sanitario, forma_farmaceutica};
    presentaciones = [{codigo, nombre, unidad, contenido_por_pieza}]. Devuelve el id del granel."""
    try:
        gid = inventario.crear_material(
            conn, codigo=granel.get('codigo'), nombre=granel.get('nombre'), tipo='GR',
            unidad=granel.get('unidad') or 'kg', usuario=usuario, requiere_analisis=1,
            vida_util_meses=granel.get('vida_util_meses'),
            registro_sanitario=texto(granel.get('registro_sanitario')),
            forma_farmaceutica=texto(granel.get('forma_farmaceutica')))
        for p in presentaciones:
            agregar_presentacion(conn, gid, p, usuario=usuario)
    except ErrorInventario as e:
        raise ErrorOrdenes(str(e))
    return gid


def agregar_presentacion(conn, granel_id, datos, *, usuario):
    g = conn.execute('SELECT * FROM materiales WHERE id = ?', (granel_id,)).fetchone()
    if not g or g['tipo'] != 'GR':
        raise ErrorOrdenes('El producto (granel) no existe.')
    contenido = numero(datos.get('contenido_por_pieza'), 'el contenido por pieza')
    if contenido <= 0:
        raise ErrorOrdenes('El contenido por pieza debe ser mayor que cero.')
    try:
        return inventario.crear_material(
            conn, codigo=datos.get('codigo'), nombre=datos.get('nombre'), tipo='PT',
            unidad=datos.get('unidad') or 'pza', usuario=usuario, material_padre_id=granel_id,
            contenido_por_pieza=contenido, vida_util_meses=g['vida_util_meses'],
            registro_sanitario=g['registro_sanitario'], forma_farmaceutica=g['forma_farmaceutica'],
            requiere_analisis=1)
    except ErrorInventario as e:
        raise ErrorOrdenes(str(e))


# ---------------------------------------------------------------------------
# Fórmula: alta y edición (solo en borrador)
# ---------------------------------------------------------------------------

def _datos_generales(datos):
    salida = {}
    if 'tamano_lote' in datos:
        salida['tamano_lote'] = numero(datos['tamano_lote'], 'el tamaño de lote')
        if salida['tamano_lote'] <= 0:
            raise ErrorOrdenes('El tamaño de lote debe ser mayor que cero.')
    for campo, nombre in (('rendimiento_min', 'el rendimiento mínimo'), ('rendimiento_max', 'el rendimiento máximo')):
        if campo in datos and datos[campo] not in (None, ''):
            salida[campo] = numero(datos[campo], nombre, minimo=0)
    for campo in ('codigo_documento', 'cambios'):
        if campo in datos:
            salida[campo] = texto(datos[campo])
    if salida.get('rendimiento_min', 0) > salida.get('rendimiento_max', 10 ** 9):
        raise ErrorOrdenes('El rendimiento mínimo no puede ser mayor que el máximo.')
    return salida


def crear_formula(conn, *, material_id, tamano_lote, usuario, rendimiento_min=98, rendimiento_max=102,
                  codigo_documento=None, cambios=None):
    m = conn.execute('SELECT * FROM materiales WHERE id = ?', (material_id,)).fetchone()
    if not m or m['tipo'] != 'GR':
        raise ErrorOrdenes('Una fórmula es de un granel (GR): elige el producto.')
    abierta = conn.execute("SELECT version FROM op_formulas WHERE material_id = ? AND estatus IN "
                           "('BORRADOR','EN_REVISION')", (material_id,)).fetchone()
    if abierta:
        raise ErrorOrdenes(f'{m["codigo"]} ya tiene la versión {abierta["version"]} en borrador o revisión.')
    datos = _datos_generales({'tamano_lote': tamano_lote, 'rendimiento_min': rendimiento_min,
                              'rendimiento_max': rendimiento_max, 'codigo_documento': codigo_documento,
                              'cambios': cambios})
    version = conn.execute('SELECT COALESCE(MAX(version), 0) + 1 FROM op_formulas WHERE material_id = ?',
                           (material_id,)).fetchone()[0]
    cur = conn.execute(
        'INSERT INTO op_formulas (material_id, version, codigo_documento, tamano_lote, rendimiento_min, '
        'rendimiento_max, cambios, elaboro) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (material_id, version, datos.get('codigo_documento'), datos['tamano_lote'],
         datos.get('rendimiento_min', 98), datos.get('rendimiento_max', 102), datos.get('cambios'), usuario))
    return cur.lastrowid


def editar_formula(conn, formula_id, **datos):
    f = _editable(conn, formula_id)
    cambios = _datos_generales(datos)
    rmin = cambios.get('rendimiento_min', f['rendimiento_min'])
    rmax = cambios.get('rendimiento_max', f['rendimiento_max'])
    if rmin > rmax:
        raise ErrorOrdenes('El rendimiento mínimo no puede ser mayor que el máximo.')
    if cambios:
        sets = ', '.join(f'{k} = ?' for k in cambios)
        conn.execute(f'UPDATE op_formulas SET {sets} WHERE id = ?', (*cambios.values(), formula_id))


def agregar_componente(conn, formula_id, *, material_id, cantidad, tolerancia_pct=1, es_activo=0, notas=None):
    _editable(conn, formula_id)
    m = conn.execute('SELECT * FROM materiales WHERE id = ?', (material_id,)).fetchone()
    if not m or m['tipo'] != 'MP':
        raise ErrorOrdenes('Elige una materia prima (MP).')
    if not m['activo']:
        raise ErrorOrdenes(f'La materia prima {m["codigo"]} está desactivada.')
    if conn.execute('SELECT 1 FROM op_formula_componentes WHERE formula_id = ? AND material_id = ?',
                    (formula_id, material_id)).fetchone():
        raise ErrorOrdenes(f'{m["codigo"]} ya está en la fórmula.')
    cantidad = numero(cantidad, 'la cantidad')
    if cantidad <= 0:
        raise ErrorOrdenes('La cantidad debe ser mayor que cero.')
    tolerancia = numero(tolerancia_pct, 'la tolerancia', minimo=0)
    orden = conn.execute('SELECT COALESCE(MAX(orden), 0) + 1 FROM op_formula_componentes WHERE formula_id = ?',
                         (formula_id,)).fetchone()[0]
    cur = conn.execute('INSERT INTO op_formula_componentes (formula_id, orden, material_id, cantidad, tolerancia_pct, '
                       'es_activo, notas) VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (formula_id, orden, material_id, cantidad, tolerancia, booleano(es_activo), texto(notas)))
    return cur.lastrowid


def quitar_componente(conn, formula_id, componente_id):
    _editable(conn, formula_id)
    cur = conn.execute('DELETE FROM op_formula_componentes WHERE id = ? AND formula_id = ?', (componente_id, formula_id))
    if not cur.rowcount:
        raise ErrorOrdenes('Esa materia prima no está en la fórmula.')


def _validar_parametros(lista):
    salida = []
    for p in lista or []:
        nombre = texto(p.get('nombre'))
        if not nombre:
            continue
        vmin = numero(p.get('valor_min'), f'el mínimo de {nombre}', opcional=True)
        vmax = numero(p.get('valor_max'), f'el máximo de {nombre}', opcional=True)
        if vmin is not None and vmax is not None and vmin > vmax:
            raise ErrorOrdenes(f'En {nombre} el mínimo es mayor que el máximo.')
        salida.append({'nombre': nombre, 'unidad': texto(p.get('unidad')), 'valor_min': vmin, 'valor_max': vmax})
    return salida


def agregar_paso(conn, formula_id, *, instruccion, fase='Fabricación', minutos=0, es_critico=0, equipo_tipo=None,
                 competencia_tarea_id=None, imagen_ruta=None, parametros=()):
    _editable(conn, formula_id)
    instruccion = texto(instruccion)
    if not instruccion:
        raise ErrorOrdenes('Escribe la instrucción del paso.')
    if fase not in FASES:
        raise ErrorOrdenes('La fase debe ser Fabricación o Envasado.')
    minutos = numero(minutos or 0, 'los minutos', minimo=0, entero=True)
    tarea = competencia_tarea_id or None
    if tarea and not conn.execute('SELECT 1 FROM competencias_tareas WHERE id = ?', (tarea,)).fetchone():
        raise ErrorOrdenes('La tarea de la Matriz de Expertise no existe.')
    params = _validar_parametros(parametros)
    numero_paso = conn.execute('SELECT COALESCE(MAX(numero), 0) + 1 FROM op_formula_pasos WHERE formula_id = ?',
                               (formula_id,)).fetchone()[0]
    cur = conn.execute('INSERT INTO op_formula_pasos (formula_id, numero, fase, instruccion, minutos, imagen_ruta, '
                       'es_critico, equipo_tipo, competencia_tarea_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (formula_id, numero_paso, fase, instruccion, minutos, texto(imagen_ruta),
                        booleano(es_critico), texto(equipo_tipo), tarea))
    paso_id = cur.lastrowid
    for p in params:
        conn.execute('INSERT INTO op_formula_parametros (paso_id, nombre, unidad, valor_min, valor_max) '
                     'VALUES (?, ?, ?, ?, ?)', (paso_id, p['nombre'], p['unidad'], p['valor_min'], p['valor_max']))
    return paso_id


def quitar_paso(conn, formula_id, paso_id):
    _editable(conn, formula_id)
    if not conn.execute('SELECT 1 FROM op_formula_pasos WHERE id = ? AND formula_id = ?',
                        (paso_id, formula_id)).fetchone():
        raise ErrorOrdenes('Ese paso no está en la fórmula.')
    conn.execute('DELETE FROM op_formula_parametros WHERE paso_id = ?', (paso_id,))
    conn.execute('DELETE FROM op_formula_pasos WHERE id = ?', (paso_id,))
    for i, p in enumerate(conn.execute('SELECT id FROM op_formula_pasos WHERE formula_id = ? ORDER BY numero, id',
                                       (formula_id,)).fetchall(), start=1):
        conn.execute('UPDATE op_formula_pasos SET numero = ? WHERE id = ?', (i, p['id']))


def _copiar_imagen(ruta, carpeta_static):
    """Copia static/<ruta> a static/img_formulas/ para que borrar el proceso no rompa la fórmula."""
    if not ruta or not carpeta_static:
        return ruta
    origen = os.path.join(carpeta_static, *ruta.replace('\\', '/').split('/'))
    if not os.path.isfile(origen):
        return None
    destino_dir = os.path.join(carpeta_static, CARPETA_IMAGENES)
    os.makedirs(destino_dir, exist_ok=True)
    nombre = f'{uuid.uuid4().hex}{os.path.splitext(origen)[1].lower()}'
    shutil.copy2(origen, os.path.join(destino_dir, nombre))
    return f'{CARPETA_IMAGENES}/{nombre}'


def importar_de_proceso(conn, formula_id, receta_id, carpeta_static=None, fase='Fabricación'):
    """Copia los pasos de un proceso (Procesos/recetas): instrucción, minutos e imagen. Devuelve cuántos."""
    _editable(conn, formula_id)
    receta = conn.execute('SELECT * FROM recetas WHERE id = ?', (receta_id,)).fetchone()
    if not receta:
        raise ErrorOrdenes('El proceso no existe.')
    filas = conn.execute('SELECT * FROM recetas_pasos WHERE receta_id = ? ORDER BY numero_paso, id',
                         (receta_id,)).fetchall()
    if not filas:
        raise ErrorOrdenes(f'El proceso «{receta["nombre"]}» no tiene pasos.')
    for p in filas:
        agregar_paso(conn, formula_id, instruccion=p['instruccion'], fase=fase,
                     minutos=p['minutos'] if p['tiene_tiempo'] else 0,
                     imagen_ruta=_copiar_imagen(p['imagen_ruta'], carpeta_static))
    conn.execute('UPDATE op_formulas SET receta_id = ? WHERE id = ?', (receta_id, formula_id))
    return len(filas)


def agregar_empaque(conn, formula_id, *, presentacion_id, material_id, cantidad_por_pieza, tolerancia_pct=2):
    f = _editable(conn, formula_id)
    p = conn.execute('SELECT * FROM materiales WHERE id = ?', (presentacion_id,)).fetchone()
    if not p or p['tipo'] != 'PT' or p['material_padre_id'] != f['material_id']:
        raise ErrorOrdenes('Elige una presentación de este producto.')
    m = conn.execute('SELECT * FROM materiales WHERE id = ?', (material_id,)).fetchone()
    if not m or m['tipo'] not in ('ME', 'EM'):
        raise ErrorOrdenes('Elige un material de envase (ME) o empaque (EM).')
    if conn.execute('SELECT 1 FROM op_formula_empaque WHERE formula_id = ? AND presentacion_id = ? AND material_id = ?',
                    (formula_id, presentacion_id, material_id)).fetchone():
        raise ErrorOrdenes(f'{m["codigo"]} ya está en {p["codigo"]}.')
    cantidad = numero(cantidad_por_pieza, 'la cantidad por pieza')
    if cantidad <= 0:
        raise ErrorOrdenes('La cantidad por pieza debe ser mayor que cero.')
    cur = conn.execute('INSERT INTO op_formula_empaque (formula_id, presentacion_id, material_id, cantidad_por_pieza, '
                       'tolerancia_pct) VALUES (?, ?, ?, ?, ?)',
                       (formula_id, presentacion_id, material_id, cantidad,
                        numero(tolerancia_pct, 'la tolerancia', minimo=0)))
    return cur.lastrowid


def quitar_empaque(conn, formula_id, empaque_id):
    _editable(conn, formula_id)
    cur = conn.execute('DELETE FROM op_formula_empaque WHERE id = ? AND formula_id = ?', (empaque_id, formula_id))
    if not cur.rowcount:
        raise ErrorOrdenes('Ese material no está en la fórmula.')


# ---------------------------------------------------------------------------
# Flujo: borrador → en revisión → (revisó) → (aprobó) → vigente
# ---------------------------------------------------------------------------

def enviar_revision(conn, formula_id):
    f = _editable(conn, formula_id)
    if not componentes(conn, formula_id):
        raise ErrorOrdenes('La fórmula no tiene materias primas.')
    if not conn.execute("SELECT 1 FROM op_formula_pasos WHERE formula_id = ? AND fase = 'Fabricación'",
                        (formula_id,)).fetchone():
        raise ErrorOrdenes('La fórmula no tiene pasos de fabricación.')
    if f['version'] > 1 and not f['cambios']:
        raise ErrorOrdenes('Escribe qué cambia respecto a la versión anterior.')
    conn.execute("UPDATE op_formulas SET estatus = 'EN_REVISION' WHERE id = ?", (formula_id,))


def regresar_a_borrador(conn, formula_id):
    """Quien revisa o aprueba encontró algo: vuelve a borrador y se borran las firmas de revisión."""
    f = formula(conn, formula_id)
    if f['estatus'] != 'EN_REVISION':
        raise ErrorOrdenes('Solo una fórmula en revisión regresa a borrador.')
    conn.execute("UPDATE op_formulas SET estatus = 'BORRADOR', reviso_firma_id = NULL WHERE id = ?", (formula_id,))


def revisar(conn, formula_id, firma_id):
    f = formula(conn, formula_id)
    if f['estatus'] != 'EN_REVISION':
        raise ErrorOrdenes('Primero envía la fórmula a revisión.')
    if f['reviso_firma_id']:
        raise ErrorOrdenes('La fórmula ya tiene la firma de revisión.')
    exigir_firma(conn, firma_id, 'quien revisa')
    conn.execute('UPDATE op_formulas SET reviso_firma_id = ? WHERE id = ?', (firma_id, formula_id))


def aprobar(conn, formula_id, firma_id):
    """Queda VIGENTE y la vigente anterior del producto pasa a OBSOLETA."""
    f = formula(conn, formula_id)
    if f['estatus'] != 'EN_REVISION':
        raise ErrorOrdenes('Solo se aprueba una fórmula en revisión.')
    if not f['reviso_firma_id']:
        raise ErrorOrdenes('Falta la firma de revisión (producción) antes de aprobar.')
    exigir_facultad(conn, firma_id, 'aprobar_maestros', 'aprobar la fórmula')
    exigir_distintas(conn, f['reviso_firma_id'], firma_id, 'Revisó y Aprobó deben ser personas distintas.')
    conn.execute("UPDATE op_formulas SET estatus = 'OBSOLETA' WHERE material_id = ? AND estatus = 'VIGENTE'",
                 (f['material_id'],))
    conn.execute("UPDATE op_formulas SET estatus = 'VIGENTE', aprobo_firma_id = ?, vigente_desde = ? WHERE id = ?",
                 (firma_id, ahora(), formula_id))


def nueva_version(conn, formula_id, *, usuario, cambios):
    """Copia la versión indicada (normalmente la vigente) como borrador nuevo."""
    f = formula(conn, formula_id)
    cambios = texto(cambios)
    if not cambios:
        raise ErrorOrdenes('Escribe qué va a cambiar en la nueva versión.')
    nueva = crear_formula(conn, material_id=f['material_id'], tamano_lote=f['tamano_lote'], usuario=usuario,
                          rendimiento_min=f['rendimiento_min'], rendimiento_max=f['rendimiento_max'],
                          codigo_documento=f['codigo_documento'], cambios=cambios)
    conn.execute('UPDATE op_formulas SET receta_id = ? WHERE id = ?', (f['receta_id'], nueva))
    conn.execute('INSERT INTO op_formula_componentes (formula_id, orden, material_id, cantidad, tolerancia_pct, '
                 'es_activo, notas) SELECT ?, orden, material_id, cantidad, tolerancia_pct, es_activo, notas '
                 'FROM op_formula_componentes WHERE formula_id = ? ORDER BY orden, id', (nueva, formula_id))
    for p in pasos(conn, formula_id):
        cur = conn.execute('INSERT INTO op_formula_pasos (formula_id, numero, fase, instruccion, minutos, imagen_ruta, '
                           'es_critico, equipo_tipo, competencia_tarea_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                           (nueva, p['numero'], p['fase'], p['instruccion'], p['minutos'], p['imagen_ruta'],
                            p['es_critico'], p['equipo_tipo'], p['competencia_tarea_id']))
        for x in p['parametros']:
            conn.execute('INSERT INTO op_formula_parametros (paso_id, nombre, unidad, valor_min, valor_max) '
                         'VALUES (?, ?, ?, ?, ?)', (cur.lastrowid, x['nombre'], x['unidad'], x['valor_min'],
                                                    x['valor_max']))
    conn.execute('INSERT INTO op_formula_empaque (formula_id, presentacion_id, material_id, cantidad_por_pieza, '
                 'tolerancia_pct) SELECT ?, presentacion_id, material_id, cantidad_por_pieza, tolerancia_pct '
                 'FROM op_formula_empaque WHERE formula_id = ?', (nueva, formula_id))
    return nueva
