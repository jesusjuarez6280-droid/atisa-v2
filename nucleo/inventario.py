"""Inventario por lote: materiales, proveedores, lotes, kárdex y trazabilidad.

Servicios sin Flask: reciben la conexión abierta y NO hacen commit (el que llama abre
la transacción y la confirma; si algo falla, se revierte todo, incluida la firma).
Los errores se lanzan como ErrorInventario con un mensaje listo para mostrarse.

Reglas (diseño 00-base-comun, D2, D3, D5, D7):
- Nada se consume ni se vende si el lote no está APROBADO, vigente y con reanálisis al día.
- Primero el que caduca antes; sin caducidad, el que entró primero.
- El kárdex no se edita: cada cambio de cantidad es un movimiento nuevo con su saldo.
- Calidad libera el granel (GR) y con eso quedan liberadas sus presentaciones (PT hijos).
"""
import sqlite3
from datetime import date, datetime

from nucleo import folios
from nucleo.firmas import firma_tiene_facultad
from nucleo.parametros import parametro


class ErrorInventario(Exception):
    """El mensaje se puede mostrar tal cual en pantalla (flash)."""


TIPOS_MATERIAL = ('MP', 'ME', 'EM', 'GR', 'PT')
TIPOS_PROVEEDOR = ('Materiales', 'Servicios', 'Ambos')
ESTATUS_PROVEEDOR = ('Aprobado', 'Condicionado', 'En evaluación', 'No aprobado')
ESTATUS_LOTE = ('CUARENTENA', 'APROBADO', 'RECHAZADO', 'RETENIDO')
ORIGENES = ('RECEPCION', 'PRODUCCION', 'INICIAL', 'DEVOLUCION')

# Movimiento de entrada que crea cada origen.
ENTRADA_POR_ORIGEN = {
    'RECEPCION': 'ENTRADA_RECEPCION',
    'PRODUCCION': 'ENTRADA_PRODUCCION',
    'INICIAL': 'ENTRADA_INICIAL',
    'DEVOLUCION': 'ENTRADA_DEVOLUCION',
}

# El signo lo pone el tipo: +1 entra, -1 sale.
SIGNO = {
    'ENTRADA_RECEPCION': 1, 'ENTRADA_PRODUCCION': 1, 'ENTRADA_INICIAL': 1, 'ENTRADA_DEVOLUCION': 1,
    'DEVOLUCION_PRODUCCION': 1, 'AJUSTE_MAS': 1,
    'CONSUMO_PRODUCCION': -1, 'MUESTRA': -1, 'VENTA': -1, 'DEVOLUCION_PROVEEDOR': -1,
    'DESTRUCCION': -1, 'AJUSTE_MENOS': -1,
}

TRANSICIONES = {
    'CUARENTENA': ('APROBADO', 'RECHAZADO', 'RETENIDO'),
    'APROBADO': ('RETENIDO', 'CUARENTENA'),
    'RETENIDO': ('APROBADO', 'RECHAZADO'),
    'RECHAZADO': (),
}

CAMPOS_MATERIAL = ('codigo', 'nombre', 'tipo', 'unidad', 'material_padre_id', 'contenido_por_pieza',
                   'registro_sanitario', 'forma_farmaceutica', 'vida_util_meses', 'requiere_analisis',
                   'es_impreso', 'condiciones_almacen', 'stock_minimo', 'activo')
CAMPOS_PROVEEDOR = ('nombre', 'rfc', 'tipo', 'contacto', 'telefono', 'correo', 'estatus', 'notas', 'activo')
CAMPOS_CORREGIBLES = ('fecha_caducidad', 'fecha_reanalisis', 'ubicacion', 'lote_proveedor')

_EPSILON = 1e-9


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _hoy(fecha=None):
    """Fecha de referencia como texto AAAA-MM-DD (hoy si no viene)."""
    if fecha is None:
        return date.today().isoformat()
    return _fecha(fecha, 'fecha')


def _fecha(valor, campo):
    """Normaliza una fecha a 'AAAA-MM-DD' (acepta date, datetime o texto). None/'' → None."""
    if valor is None or valor == '':
        return None
    if isinstance(valor, datetime):
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    texto = str(valor).strip()[:10]
    try:
        return datetime.strptime(texto, '%Y-%m-%d').date().isoformat()
    except ValueError:
        raise ErrorInventario(f'La fecha de {campo.replace("_", " ")} no es válida (usa AAAA-MM-DD).')


def _numero(valor, campo):
    try:
        return float(valor)
    except (TypeError, ValueError):
        raise ErrorInventario(f'{campo.capitalize()} debe ser un número.')


def _cantidad_positiva(cantidad):
    c = _numero(cantidad, 'la cantidad')
    if c <= 0:
        raise ErrorInventario('La cantidad debe ser mayor que cero.')
    return c


def _fmt(n):
    """12.5 → '12.5'; 3.0 → '3' (para mensajes)."""
    return f'{n:,.6f}'.rstrip('0').rstrip('.')


def _texto(valor):
    return (str(valor).strip() if valor is not None else '') or None


def _material(conn, material_id):
    fila = conn.execute('SELECT * FROM materiales WHERE id = ?', (material_id,)).fetchone()
    if not fila:
        raise ErrorInventario('El material no existe.')
    return fila


def _lote_fila(conn, lote_id):
    fila = conn.execute(
        'SELECT l.*, m.codigo AS material_codigo, m.nombre AS material_nombre, m.tipo AS material_tipo, '
        'm.unidad AS unidad, m.requiere_analisis AS requiere_analisis '
        'FROM lotes l JOIN materiales m ON m.id = l.material_id WHERE l.id = ?', (lote_id,)).fetchone()
    if not fila:
        raise ErrorInventario('El lote no existe.')
    return fila


def _tiene_lotes(conn, material_id):
    return conn.execute('SELECT 1 FROM lotes WHERE material_id = ? LIMIT 1', (material_id,)).fetchone() is not None


def _exigir_facultad(conn, firma_id, facultades, para):
    """Error si la firma no es de alguien con alguna de `facultades`."""
    facultades = (facultades,) if isinstance(facultades, str) else tuple(facultades)
    if not firma_id:
        raise ErrorInventario(f'Para {para} se necesita la firma de alguien con la facultad '
                              f'«{" o ".join(facultades)}».')
    if not any(firma_tiene_facultad(conn, firma_id, f) for f in facultades):
        raise ErrorInventario(f'Quien firmó no tiene la facultad «{" o ".join(facultades)}» para {para}.')


def _caducado(fila, hoy):
    return bool(fila['fecha_caducidad']) and fila['fecha_caducidad'] < hoy


def _reanalisis_vencido(fila, hoy):
    return bool(fila['fecha_reanalisis']) and fila['fecha_reanalisis'] < hoy


def _motivo_no_disponible(fila, uso, hoy, ignorar_estatus=False):
    """None si el lote se puede usar; si no, el motivo legible."""
    n = fila['numero_lote']
    if uso == 'venta' and fila['material_tipo'] != 'PT':
        return f'El lote {n} no es de producto terminado: solo se vende producto terminado.'
    if not ignorar_estatus and fila['estatus'] != 'APROBADO':
        return f'El lote {n} está en {fila["estatus"]}: solo se usan lotes aprobados por calidad.'
    if (fila['existencia'] or 0) <= _EPSILON:
        return f'El lote {n} no tiene existencia.'
    if _caducado(fila, hoy):
        return f'El lote {n} caducó el {fila["fecha_caducidad"]}.'
    if not ignorar_estatus and _reanalisis_vencido(fila, hoy):
        return f'El lote {n} tiene el reanálisis vencido desde el {fila["fecha_reanalisis"]}.'
    return None


def _historial(conn, lote_id, campo, anterior, nuevo, motivo, usuario, folio=None, firma_id=None):
    conn.execute('INSERT INTO lote_historial (lote_id, campo, valor_anterior, valor_nuevo, motivo, folio, '
                 'firma_id, usuario) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                 (lote_id, campo, None if anterior is None else str(anterior),
                  None if nuevo is None else str(nuevo), motivo, folio, firma_id, usuario))


# ---------------------------------------------------------------------------
# Catálogos: materiales y proveedores
# ---------------------------------------------------------------------------

def _validar_material(conn, datos, material_id=None):
    """Valida y normaliza los campos de un material (en sitio)."""
    for campo in datos:
        if campo not in CAMPOS_MATERIAL:
            raise ErrorInventario(f'El campo «{campo}» no existe en materiales.')
    if 'codigo' in datos:
        datos['codigo'] = _texto(datos['codigo'])
        if not datos['codigo']:
            raise ErrorInventario('Falta el código del material.')
    if 'nombre' in datos:
        datos['nombre'] = _texto(datos['nombre'])
        if not datos['nombre']:
            raise ErrorInventario('Falta el nombre del material.')
    if 'tipo' in datos:
        datos['tipo'] = (_texto(datos['tipo']) or '').upper()
        if datos['tipo'] not in TIPOS_MATERIAL:
            raise ErrorInventario('Tipo de material no válido (MP, ME, EM, GR o PT).')
    if 'unidad' in datos:
        datos['unidad'] = _texto(datos['unidad'])
        if not datos['unidad']:
            raise ErrorInventario('Falta la unidad del material.')
    for campo in ('requiere_analisis', 'es_impreso', 'activo'):
        if campo in datos:
            datos[campo] = 1 if datos[campo] in (1, True, '1', 'on', 'si', 'sí', 'true') else 0
    for campo in ('contenido_por_pieza', 'stock_minimo'):
        if campo in datos and datos[campo] not in (None, ''):
            datos[campo] = _numero(datos[campo], campo.replace('_', ' '))
            if datos[campo] < 0:
                raise ErrorInventario(f'{campo.replace("_", " ").capitalize()} no puede ser negativo.')
        elif campo in datos:
            datos[campo] = 0 if campo == 'stock_minimo' else None
    if 'vida_util_meses' in datos:
        if datos['vida_util_meses'] in (None, ''):
            datos['vida_util_meses'] = None
        else:
            try:
                datos['vida_util_meses'] = int(datos['vida_util_meses'])
            except (TypeError, ValueError):
                raise ErrorInventario('La vida útil debe ser un número entero de meses.')
    if datos.get('material_padre_id') in ('', 0):
        datos['material_padre_id'] = None


def _validar_padre_material(conn, tipo, padre_id, material_id=None):
    """Solo un PT tiene material padre, y el padre es su granel (GR)."""
    if padre_id is None:
        return
    if tipo != 'PT':
        raise ErrorInventario('Solo un producto terminado (PT) lleva granel padre.')
    if material_id is not None and int(padre_id) == int(material_id):
        raise ErrorInventario('Un material no puede ser su propio granel.')
    padre = conn.execute('SELECT tipo FROM materiales WHERE id = ?', (padre_id,)).fetchone()
    if not padre:
        raise ErrorInventario('El granel padre no existe.')
    if padre['tipo'] != 'GR':
        raise ErrorInventario('El material padre de una presentación debe ser un granel (GR).')


def crear_material(conn, *, codigo, nombre, tipo, unidad, usuario, **campos) -> int:
    datos = {'codigo': codigo, 'nombre': nombre, 'tipo': tipo, 'unidad': unidad, **campos}
    _validar_material(conn, datos)
    _validar_padre_material(conn, datos['tipo'], datos.get('material_padre_id'))
    if conn.execute('SELECT 1 FROM materiales WHERE codigo = ?', (datos['codigo'],)).fetchone():
        raise ErrorInventario(f'Ya existe un material con el código {datos["codigo"]}.')
    datos['creado_por'] = usuario
    cur = conn.execute(f'INSERT INTO materiales ({", ".join(datos)}) VALUES ({", ".join("?" * len(datos))})',
                       tuple(datos.values()))
    return cur.lastrowid


def editar_material(conn, material_id, *, usuario, firma_id=None, **campos):
    """Código, tipo y unidad no cambian si el material ya tiene lotes; quitar requiere_analisis exige
    firma_id de alguien con aprobar_maestros. Un material se desactiva, no se borra."""
    actual = _material(conn, material_id)
    datos = dict(campos)
    _validar_material(conn, datos, material_id)
    cambios = {k: v for k, v in datos.items() if actual[k] != v}
    if not cambios:
        return
    fijos = [c for c in ('codigo', 'tipo', 'unidad') if c in cambios]
    if fijos and _tiene_lotes(conn, material_id):
        nombres = {'codigo': 'el código', 'tipo': 'el tipo', 'unidad': 'la unidad'}
        raise ErrorInventario(f'No se puede cambiar {", ".join(nombres[c] for c in fijos)} de '
                              f'{actual["codigo"]}: ya tiene lotes registrados.')
    if 'codigo' in cambios and conn.execute('SELECT 1 FROM materiales WHERE codigo = ? AND id <> ?',
                                            (cambios['codigo'], material_id)).fetchone():
        raise ErrorInventario(f'Ya existe un material con el código {cambios["codigo"]}.')
    tipo = cambios.get('tipo', actual['tipo'])
    padre = cambios['material_padre_id'] if 'material_padre_id' in cambios else actual['material_padre_id']
    if 'tipo' in cambios or 'material_padre_id' in cambios:
        _validar_padre_material(conn, tipo, padre, material_id)
    if cambios.get('requiere_analisis') == 0 and actual['requiere_analisis']:
        _exigir_facultad(conn, firma_id, 'aprobar_maestros', 'quitar el análisis obligatorio')
    sets = ', '.join(f'{k} = ?' for k in cambios)
    conn.execute(f'UPDATE materiales SET {sets} WHERE id = ?', (*cambios.values(), material_id))


def _validar_proveedor(datos):
    for campo in datos:
        if campo not in CAMPOS_PROVEEDOR:
            raise ErrorInventario(f'El campo «{campo}» no existe en proveedores.')
    if 'nombre' in datos:
        datos['nombre'] = _texto(datos['nombre'])
        if not datos['nombre']:
            raise ErrorInventario('Falta el nombre del proveedor.')
    if 'tipo' in datos and datos['tipo'] not in TIPOS_PROVEEDOR:
        raise ErrorInventario('Tipo de proveedor no válido (Materiales, Servicios o Ambos).')
    if 'estatus' in datos and datos['estatus'] not in ESTATUS_PROVEEDOR:
        raise ErrorInventario('Estatus de proveedor no válido.')
    if 'activo' in datos:
        datos['activo'] = 1 if datos['activo'] in (1, True, '1', 'on', 'true') else 0


def crear_proveedor(conn, *, nombre, tipo='Materiales', usuario, **campos) -> int:
    datos = {'nombre': nombre, 'tipo': tipo, **campos}
    _validar_proveedor(datos)
    cur = conn.execute(f'INSERT INTO proveedores ({", ".join(datos)}) VALUES ({", ".join("?" * len(datos))})',
                       tuple(datos.values()))
    return cur.lastrowid


def editar_proveedor(conn, proveedor_id, *, usuario, motivo=None, **campos):
    """Cambiar el estatus (Aprobado, Condicionado…) pide motivo. Un proveedor se desactiva, no se borra.
    El cambio de estatus queda anotado al final de las notas del proveedor."""
    actual = conn.execute('SELECT * FROM proveedores WHERE id = ?', (proveedor_id,)).fetchone()
    if not actual:
        raise ErrorInventario('El proveedor no existe.')
    datos = dict(campos)
    _validar_proveedor(datos)
    cambios = {k: v for k, v in datos.items() if actual[k] != v}
    if not cambios:
        return
    if 'estatus' in cambios:
        motivo = _texto(motivo)
        if not motivo:
            raise ErrorInventario('Para cambiar el estatus del proveedor escribe el motivo.')
        notas = cambios.get('notas', actual['notas']) or ''
        renglon = (f'[{datetime.now():%Y-%m-%d}] Estatus {actual["estatus"]} → {cambios["estatus"]} '
                   f'({usuario}): {motivo}')
        cambios['notas'] = (notas + '\n' + renglon).strip()
    sets = ', '.join(f'{k} = ?' for k in cambios)
    conn.execute(f'UPDATE proveedores SET {sets} WHERE id = ?', (*cambios.values(), proveedor_id))


# ---------------------------------------------------------------------------
# Lotes y kárdex
# ---------------------------------------------------------------------------

def _generar_numero_lote(conn, material, origen):
    clave = 'nucleo.formato_lote_produccion' if origen == 'PRODUCCION' else 'nucleo.formato_lote_recepcion'
    formato = parametro(clave, conn)
    for _ in range(100):
        numero = folios.numero_lote(conn, formato, TIPO=material['tipo'], COD=material['codigo'])
        if not conn.execute('SELECT 1 FROM lotes WHERE material_id = ? AND numero_lote = ?',
                            (material['id'], numero)).fetchone():
            return numero
        if '{#' not in formato:
            break
    raise ErrorInventario(f'No se pudo generar un número de lote libre con el formato «{formato}»; '
                          'escríbelo a mano o revisa el parámetro.')


def crear_lote(conn, *, material_id, cantidad, origen, usuario, modulo,
               numero_lote=None, folio=None, fecha_entrada=None, fecha_fabricacion=None,
               fecha_caducidad=None, fecha_reanalisis=None, lote_proveedor=None,
               proveedor_id=None, lote_padre_id=None, ubicacion=None,
               estatus=None, firma_id=None) -> int:
    """Crea el lote y su movimiento de entrada (ENTRADA_RECEPCION / ENTRADA_PRODUCCION / ENTRADA_INICIAL /
    ENTRADA_DEVOLUCION). Una devolución de cliente siempre nace en CUARENTENA, aparte del lote vendido.
    Estatus inicial: CUARENTENA si el material requiere análisis; si no, APROBADO. Un PT hijo de un GR
    hereda el estatus del padre. estatus explícito solo con origen INICIAL y firma de liberar_lotes
    (forzar CUARENTENA siempre se permite: es el estatus más seguro).
    Proveedor 'No aprobado' → error; 'En evaluación' o 'Condicionado' → se permite (el módulo avisa).
    Si (material, numero_lote) ya existe → error: para sumar a un lote existente se usa mover()."""
    if origen not in ORIGENES:
        raise ErrorInventario('Origen de lote no válido.')
    if not _texto(usuario) or not _texto(modulo):
        raise ErrorInventario('Falta el usuario o el módulo que registra el lote.')
    cantidad = _cantidad_positiva(cantidad)
    material = _material(conn, material_id)
    if not material['activo']:
        raise ErrorInventario(f'El material {material["codigo"]} está desactivado.')

    if proveedor_id:
        prov = conn.execute('SELECT nombre, estatus FROM proveedores WHERE id = ?', (proveedor_id,)).fetchone()
        if not prov:
            raise ErrorInventario('El proveedor no existe.')
        if prov['estatus'] == 'No aprobado':
            raise ErrorInventario(f'El proveedor {prov["nombre"]} está como «No aprobado»: no se le recibe.')

    padre = None
    if lote_padre_id:
        padre = _lote_fila(conn, lote_padre_id)
        if origen == 'DEVOLUCION':
            if padre['material_id'] != material['id']:
                raise ErrorInventario('La devolución debe ser del mismo producto que el lote vendido.')
        elif material['material_padre_id'] and padre['material_id'] != material['material_padre_id']:
            raise ErrorInventario(f'El lote {padre["numero_lote"]} no es del granel de {material["codigo"]}.')

    # Estatus inicial
    if estatus is not None and estatus not in ESTATUS_LOTE:
        raise ErrorInventario('Estatus de lote no válido.')
    if origen == 'DEVOLUCION':
        inicial, razon = 'CUARENTENA', 'Devolución de cliente: nace en cuarentena'
    elif estatus is not None and estatus != 'CUARENTENA':
        if origen != 'INICIAL':
            raise ErrorInventario('Solo un inventario inicial puede registrarse con un estatus distinto '
                                  'de cuarentena.')
        _exigir_facultad(conn, firma_id, 'liberar_lotes', 'dar de alta un lote ya dictaminado')
        inicial, razon = estatus, 'Inventario inicial con estatus dictaminado'
    elif estatus == 'CUARENTENA':
        inicial, razon = 'CUARENTENA', 'Cuarentena indicada al registrar'
    elif padre is not None and padre['material_tipo'] == 'GR' and material['tipo'] == 'PT':
        inicial, razon = padre['estatus'], f'Hereda el estatus del granel {padre["numero_lote"]}'
    elif material['requiere_analisis']:
        inicial, razon = 'CUARENTENA', 'El material requiere análisis'
    else:
        inicial, razon = 'APROBADO', 'El material no requiere análisis'

    numero_lote = _texto(numero_lote)
    if numero_lote:
        if conn.execute('SELECT 1 FROM lotes WHERE material_id = ? AND numero_lote = ?',
                        (material['id'], numero_lote)).fetchone():
            raise ErrorInventario(f'El lote {numero_lote} de {material["codigo"]} ya existe; para sumarle '
                                  'cantidad registra un movimiento.')
    else:
        numero_lote = _generar_numero_lote(conn, material, origen)

    fecha_entrada = _fecha(fecha_entrada, 'fecha_entrada') or _hoy()
    try:
        cur = conn.execute(
            'INSERT INTO lotes (material_id, numero_lote, lote_proveedor, proveedor_id, lote_padre_id, origen, '
            'origen_folio, fecha_entrada, fecha_fabricacion, fecha_caducidad, fecha_reanalisis, estatus, '
            'existencia, ubicacion, creado_por) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (material['id'], numero_lote, _texto(lote_proveedor), proveedor_id or None, lote_padre_id or None,
             origen, _texto(folio), fecha_entrada, _fecha(fecha_fabricacion, 'fecha_fabricacion'),
             _fecha(fecha_caducidad, 'fecha_caducidad'), _fecha(fecha_reanalisis, 'fecha_reanalisis'),
             inicial, cantidad, _texto(ubicacion), usuario))
    except sqlite3.IntegrityError as e:
        raise ErrorInventario(f'No se pudo registrar el lote {numero_lote}: {e}')
    lote_id = cur.lastrowid
    conn.execute('INSERT INTO lote_movimientos (lote_id, tipo, cantidad, saldo, folio, motivo, modulo, usuario, '
                 'firma_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                 (lote_id, ENTRADA_POR_ORIGEN[origen], cantidad, cantidad, _texto(folio), None, modulo, usuario,
                  firma_id))
    _historial(conn, lote_id, 'estatus', None, inicial, razon, usuario, folio=_texto(folio),
               firma_id=firma_id if inicial != 'CUARENTENA' and origen == 'INICIAL' else None)
    return lote_id


def lote_disponible(conn, lote_id, uso='consumo', fecha=None):
    """APROBADO, existencia > 0, no caducado, reanálisis no vencido (y, para uso='venta', que sea PT).
    Devuelve (True, '') o (False, motivo legible)."""
    fila = _lote_fila(conn, lote_id)
    motivo = _motivo_no_disponible(fila, uso, _hoy(fecha))
    return (False, motivo) if motivo else (True, '')


def mover(conn, *, lote_id, tipo, cantidad, usuario, modulo,
          folio=None, motivo=None, firma_id=None) -> float:
    """Registra un movimiento y actualiza la existencia en la MISMA transacción, con un UPDATE atómico.
    `cantidad` siempre positiva; el signo lo pone el tipo. Devuelve el saldo del lote.

    Las entradas de devolución de cliente no van aquí: crean su propio lote (crear_lote, origen
    DEVOLUCION)."""
    if tipo not in SIGNO:
        raise ErrorInventario('Tipo de movimiento no válido.')
    if tipo == 'ENTRADA_DEVOLUCION':
        raise ErrorInventario('Una devolución de cliente se registra como un lote nuevo en cuarentena.')
    if not _texto(usuario) or not _texto(modulo):
        raise ErrorInventario('Falta el usuario o el módulo que registra el movimiento.')
    cantidad = _cantidad_positiva(cantidad)
    motivo = _texto(motivo)
    fila = _lote_fila(conn, lote_id)
    hoy = _hoy()
    n = fila['numero_lote']

    if tipo == 'CONSUMO_PRODUCCION':
        # Un granel en cuarentena sí se puede envasar: sus presentaciones nacen en cuarentena.
        envasar_granel = fila['material_tipo'] == 'GR' and fila['estatus'] == 'CUARENTENA'
        problema = _motivo_no_disponible(fila, 'consumo', hoy, ignorar_estatus=envasar_granel)
        if problema:
            raise ErrorInventario(problema)
    elif tipo == 'VENTA':
        problema = _motivo_no_disponible(fila, 'venta', hoy)
        if problema:
            raise ErrorInventario(problema)
        if not motivo:
            raise ErrorInventario('Para registrar una venta escribe el nombre del cliente.')
    elif tipo in ('DESTRUCCION', 'DEVOLUCION_PROVEEDOR'):
        if fila['estatus'] != 'RECHAZADO' and not _caducado(fila, hoy):
            raise ErrorInventario(f'El lote {n} no está rechazado ni caducado: no se puede destruir ni '
                                  'devolver al proveedor.')
        _exigir_facultad(conn, firma_id, 'autorizar_ajustes',
                         'destruir' if tipo == 'DESTRUCCION' else 'devolver al proveedor')
    elif tipo in ('AJUSTE_MAS', 'AJUSTE_MENOS'):
        if not motivo:
            raise ErrorInventario('Un ajuste de inventario necesita motivo.')
        _exigir_facultad(conn, firma_id, 'autorizar_ajustes', 'ajustar el inventario')
    # MUESTRA: en cualquier estatus. Entradas y DEVOLUCION_PRODUCCION: sin validación de estatus.

    delta = SIGNO[tipo] * cantidad
    fila_saldo = conn.execute('UPDATE lotes SET existencia = existencia + ? WHERE id = ? AND existencia + ? >= 0 '
                              'RETURNING existencia', (delta, lote_id, delta)).fetchone()
    if fila_saldo is None:
        disponible = conn.execute('SELECT existencia FROM lotes WHERE id = ?', (lote_id,)).fetchone()
        hay = disponible['existencia'] if disponible else 0
        raise ErrorInventario(f'No hay suficiente en el lote {n}: hay {_fmt(hay)} {fila["unidad"]} '
                              f'y se piden {_fmt(cantidad)}.')
    saldo = fila_saldo[0]
    conn.execute('INSERT INTO lote_movimientos (lote_id, tipo, cantidad, saldo, folio, motivo, modulo, usuario, '
                 'firma_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                 (lote_id, tipo, delta, saldo, _texto(folio), motivo, modulo, usuario, firma_id))
    return saldo


def sugerir_lotes(conn, material_id, cantidad) -> list:
    """Lotes disponibles en orden: caducidad más próxima primero; sin caducidad, el más antiguo.
    [{'lote_id', 'numero_lote', 'existencia', 'caducidad', 'tomar'}] solo con los lotes necesarios;
    si no alcanza, agrega {'faltante': x} al final."""
    restante = _cantidad_positiva(cantidad)
    hoy = _hoy()
    filas = conn.execute(
        'SELECT l.*, m.tipo AS material_tipo FROM lotes l JOIN materiales m ON m.id = l.material_id '
        "WHERE l.material_id = ? AND l.estatus = 'APROBADO' AND l.existencia > 0 "
        'ORDER BY l.fecha_caducidad IS NULL, l.fecha_caducidad, l.fecha_entrada, l.id',
        (material_id,)).fetchall()
    sugerencias = []
    for f in filas:
        if restante <= _EPSILON:
            break
        if _caducado(f, hoy) or _reanalisis_vencido(f, hoy):
            continue
        tomar = min(f['existencia'], restante)
        restante -= tomar
        sugerencias.append({'lote_id': f['id'], 'numero_lote': f['numero_lote'], 'existencia': f['existencia'],
                            'caducidad': f['fecha_caducidad'], 'tomar': round(tomar, 6)})
    if restante > _EPSILON:
        sugerencias.append({'faltante': round(restante, 6)})
    return sugerencias


def cambiar_estatus(conn, *, lote_id, nuevo, usuario, motivo, firma_id, folio=None, cascada=True) -> list:
    """Transiciones: CUARENTENA→APROBADO|RECHAZADO|RETENIDO · APROBADO→RETENIDO|CUARENTENA (reanálisis)
    · RETENIDO→APROBADO|RECHAZADO · RECHAZADO es final. Exige firma_id de alguien con liberar_lotes
    (o gestionar_desviaciones para RETENIDO). Si el lote es GR y cascada=True, aplica lo mismo a sus
    presentaciones (PT hijos) que estaban en el mismo estatus que el granel. Todo queda en lote_historial.
    Devuelve la lista de ids de lotes que cambiaron (el primero es el pedido)."""
    fila = _lote_fila(conn, lote_id)
    anterior = fila['estatus']
    if nuevo not in ESTATUS_LOTE:
        raise ErrorInventario('Estatus de lote no válido.')
    if nuevo == anterior:
        raise ErrorInventario(f'El lote {fila["numero_lote"]} ya está en {anterior}.')
    if nuevo not in TRANSICIONES[anterior]:
        if anterior == 'RECHAZADO':
            raise ErrorInventario(f'El lote {fila["numero_lote"]} está RECHAZADO: es un estatus final.')
        raise ErrorInventario(f'No se puede pasar un lote de {anterior} a {nuevo}.')
    motivo = _texto(motivo)
    if not motivo:
        raise ErrorInventario('Escribe el motivo del cambio de estatus.')
    facultades = ('liberar_lotes', 'gestionar_desviaciones') if nuevo == 'RETENIDO' else ('liberar_lotes',)
    _exigir_facultad(conn, firma_id, facultades, f'pasar el lote a {nuevo}')

    folio = _texto(folio)
    afectados = [lote_id]
    conn.execute('UPDATE lotes SET estatus = ? WHERE id = ?', (nuevo, lote_id))
    _historial(conn, lote_id, 'estatus', anterior, nuevo, motivo, usuario, folio=folio, firma_id=firma_id)
    if cascada and fila['material_tipo'] == 'GR':
        hijos = conn.execute(
            "SELECT l.id FROM lotes l JOIN materiales m ON m.id = l.material_id "
            "WHERE l.lote_padre_id = ? AND m.tipo = 'PT' AND l.estatus = ? AND l.origen <> 'DEVOLUCION' "
            'ORDER BY l.id', (lote_id, anterior)).fetchall()
        for h in hijos:
            conn.execute('UPDATE lotes SET estatus = ? WHERE id = ?', (nuevo, h['id']))
            _historial(conn, h['id'], 'estatus', anterior, nuevo,
                       f'{motivo} (en cascada del granel {fila["numero_lote"]})', usuario,
                       folio=folio, firma_id=firma_id)
            afectados.append(h['id'])
    return afectados


def corregir_lote(conn, *, lote_id, campo, valor, usuario, motivo, firma_id=None):
    """fecha_caducidad, fecha_reanalisis, ubicacion o lote_proveedor, con historial. Cambiar caducidad
    o reanálisis exige firma de liberar_lotes."""
    if campo not in CAMPOS_CORREGIBLES:
        raise ErrorInventario('Ese dato del lote no se puede corregir aquí.')
    fila = _lote_fila(conn, lote_id)
    motivo = _texto(motivo)
    if not motivo:
        raise ErrorInventario('Escribe el motivo de la corrección.')
    nuevo = _fecha(valor, campo) if campo.startswith('fecha_') else _texto(valor)
    if nuevo == fila[campo]:
        raise ErrorInventario('El valor nuevo es igual al actual.')
    if campo.startswith('fecha_'):
        _exigir_facultad(conn, firma_id, 'liberar_lotes', 'cambiar la ' + campo.replace('_', ' '))
    conn.execute(f'UPDATE lotes SET {campo} = ? WHERE id = ?', (nuevo, lote_id))
    _historial(conn, lote_id, campo, fila[campo], nuevo, motivo, usuario, firma_id=firma_id)


def lote(conn, lote_id):
    """El lote con los datos de su material y las banderas caducado, reanalisis_vencido, disponible
    y motivo_no_disponible. None si no existe."""
    try:
        fila = _lote_fila(conn, lote_id)
    except ErrorInventario:
        return None
    hoy = _hoy()
    d = dict(fila)
    d['caducado'] = _caducado(fila, hoy)
    d['reanalisis_vencido'] = _reanalisis_vencido(fila, hoy)
    motivo = _motivo_no_disponible(fila, 'consumo', hoy)
    d['disponible'] = motivo is None
    d['motivo_no_disponible'] = motivo or ''
    return d


def existencia_material(conn, material_id, solo_disponible=True) -> float:
    """Suma de existencias de los lotes del material; con solo_disponible, solo lotes aprobados,
    vigentes y con reanálisis al día."""
    if not solo_disponible:
        fila = conn.execute('SELECT COALESCE(SUM(existencia), 0) FROM lotes WHERE material_id = ?',
                            (material_id,)).fetchone()
        return float(fila[0])
    hoy = _hoy()
    fila = conn.execute(
        "SELECT COALESCE(SUM(existencia), 0) FROM lotes WHERE material_id = ? AND estatus = 'APROBADO' "
        'AND (fecha_caducidad IS NULL OR fecha_caducidad >= ?) '
        'AND (fecha_reanalisis IS NULL OR fecha_reanalisis >= ?)', (material_id, hoy, hoy)).fetchone()
    return float(fila[0])


# ---------------------------------------------------------------------------
# Trazabilidad
# ---------------------------------------------------------------------------

_SQL_LOTE_TRAZA = ('SELECT l.id, l.numero_lote, l.estatus, l.origen, l.origen_folio, l.lote_padre_id, '
                   'l.fecha_entrada, m.codigo AS material_codigo, m.nombre AS material_nombre, '
                   'm.tipo AS material_tipo FROM lotes l JOIN materiales m ON m.id = l.material_id ')


def _consumido(conn, lote_id, folio):
    """Cantidad neta que el lote dio a un folio (consumos menos devoluciones a producción)."""
    fila = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN tipo = 'CONSUMO_PRODUCCION' THEN -cantidad "
        "WHEN tipo = 'DEVOLUCION_PRODUCCION' THEN -cantidad ELSE 0 END), 0) "
        'FROM lote_movimientos WHERE lote_id = ? AND folio = ?', (lote_id, folio)).fetchone()
    return round(fila[0], 6)


def _renglon(nivel, relacion, desde, f=None, folio=None, cantidad=None, cliente=None, fecha=None):
    r = {'nivel': nivel, 'relacion': relacion, 'desde_lote_id': desde, 'lote_id': None, 'numero_lote': None,
         'material_codigo': None, 'material_nombre': None, 'material_tipo': None, 'estatus': None,
         'folio': folio, 'cantidad': cantidad, 'cliente': cliente, 'fecha': fecha}
    if f is not None:
        r.update({'lote_id': f['id'], 'numero_lote': f['numero_lote'], 'material_codigo': f['material_codigo'],
                  'material_nombre': f['material_nombre'], 'material_tipo': f['material_tipo'],
                  'estatus': f['estatus'], 'fecha': fecha or f['fecha_entrada']})
    return r


def _hacia_atras(conn, inicio):
    """De un lote a todo lo que lo formó: su lote padre y lo que consumió su orden (origen_folio)."""
    vistos, salida = {inicio['id']}, []
    pendientes = [(inicio, 1)]
    while pendientes:
        actual, nivel = pendientes.pop(0)
        if actual['lote_padre_id'] and actual['lote_padre_id'] not in vistos:
            padre = conn.execute(_SQL_LOTE_TRAZA + 'WHERE l.id = ?', (actual['lote_padre_id'],)).fetchone()
            if padre:
                vistos.add(padre['id'])
                relacion = 'LOTE_VENDIDO' if actual['origen'] == 'DEVOLUCION' else 'GRANEL'
                salida.append(_renglon(nivel, relacion, actual['id'], padre))
                pendientes.append((padre, nivel + 1))
        if actual['origen_folio'] and actual['origen'] == 'PRODUCCION':
            consumidos = conn.execute(
                _SQL_LOTE_TRAZA + 'WHERE l.id IN (SELECT lote_id FROM lote_movimientos '
                "WHERE tipo = 'CONSUMO_PRODUCCION' AND folio = ?) ORDER BY l.id",
                (actual['origen_folio'],)).fetchall()
            for c in consumidos:
                if c['id'] in vistos:
                    continue
                vistos.add(c['id'])
                salida.append(_renglon(nivel, 'CONSUMO', actual['id'], c, folio=actual['origen_folio'],
                                       cantidad=_consumido(conn, c['id'], actual['origen_folio'])))
                pendientes.append((c, nivel + 1))
    return salida


def _hacia_adelante(conn, inicio):
    """De un lote a todo lo que salió de él: órdenes que lo consumieron, lotes que produjeron,
    presentaciones envasadas y a quién se vendió (el cliente va en el motivo de la VENTA)."""
    vistos, salida = {inicio['id']}, []
    pendientes = [(inicio, 1)]
    while pendientes:
        actual, nivel = pendientes.pop(0)
        hijos = []
        folios_consumo = [r[0] for r in conn.execute(
            "SELECT DISTINCT folio FROM lote_movimientos WHERE lote_id = ? AND tipo = 'CONSUMO_PRODUCCION' "
            'AND folio IS NOT NULL ORDER BY folio', (actual['id'],)).fetchall()]
        for folio in folios_consumo:
            for p in conn.execute(_SQL_LOTE_TRAZA + "WHERE l.origen_folio = ? AND l.origen = 'PRODUCCION' "
                                  'ORDER BY l.id', (folio,)).fetchall():
                hijos.append((p, 'PRODUCIDO', folio, _consumido(conn, actual['id'], folio)))
        for h in conn.execute(_SQL_LOTE_TRAZA + 'WHERE l.lote_padre_id = ? ORDER BY l.id',
                              (actual['id'],)).fetchall():
            hijos.append((h, 'DEVOLUCION' if h['origen'] == 'DEVOLUCION' else 'PRESENTACION',
                          h['origen_folio'], None))
        for h, relacion, folio, cantidad in hijos:
            if h['id'] in vistos:
                continue
            vistos.add(h['id'])
            salida.append(_renglon(nivel, relacion, actual['id'], h, folio=folio, cantidad=cantidad))
            pendientes.append((h, nivel + 1))
        for v in conn.execute("SELECT folio, motivo, cantidad, ts FROM lote_movimientos "
                              "WHERE lote_id = ? AND tipo = 'VENTA' ORDER BY id", (actual['id'],)).fetchall():
            r = _renglon(nivel, 'VENTA', actual['id'], folio=v['folio'], cantidad=-v['cantidad'],
                         cliente=v['motivo'], fecha=(v['ts'] or '')[:10])
            r.update({'lote_id': actual['id'], 'numero_lote': actual['numero_lote'],
                      'material_codigo': actual['material_codigo'], 'material_nombre': actual['material_nombre'],
                      'material_tipo': actual['material_tipo'], 'estatus': actual['estatus']})
            salida.append(r)
    return salida


def trazabilidad(conn, lote_id) -> dict:
    """{'hacia_atras': [...], 'hacia_adelante': [...]} desde lotes y movimientos.

    Cada renglón: nivel (1 = directo), relacion, desde_lote_id (de qué lote de la cadena sale),
    lote_id, numero_lote, material_codigo, material_nombre, material_tipo, estatus, folio, cantidad,
    cliente y fecha.
      hacia_atras:    GRANEL (lote_padre_id de una presentación) · LOTE_VENDIDO (de una devolución)
                      · CONSUMO (lo que consumió la orden origen_folio del lote).
      hacia_adelante: PRODUCIDO (lotes de las órdenes que consumieron este) · PRESENTACION (PT
                      envasados de este granel) · DEVOLUCION (devoluciones de cliente)
                      · VENTA (cliente = motivo del movimiento; el lote es el vendido)."""
    inicio = conn.execute(_SQL_LOTE_TRAZA + 'WHERE l.id = ?', (lote_id,)).fetchone()
    if not inicio:
        raise ErrorInventario('El lote no existe.')
    return {'hacia_atras': _hacia_atras(conn, inicio), 'hacia_adelante': _hacia_adelante(conn, inicio)}
