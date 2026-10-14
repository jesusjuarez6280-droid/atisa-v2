"""Bandeja, muestreo, captura de resultados, revisión y cancelación de un análisis (diseño 03, 4.1-4.2,
reglas 2 a 8).

EN_ANALISIS → POR_REVISAR (cada prueba de cada lote con resultado válido y sin investigaciones
abiertas) → CONFORME / NO_CONFORME con la firma Revisó de alguien que no capturó. Un resultado que no
cumple abre la investigación OOS; un OOS confirmado deja el análisis NO_CONFORME.
"""
import sqlite3

from nucleo import archivos, folios, inventario
from nucleo.equipos import equipo_apto, ErrorEquipo
from nucleo.parametros import parametro_int

from ._comun import (ErrorCalidad, ROLES_CALIDAD, texto, numero, hoy, ahora, fecha, firmar, redondear,
                     cumple_limites, numero_firmante, nombre_firmante, lote_fila, sigue_al_granel)
from . import especificaciones as esp

TIPOS = ('LIBERACION', 'REANALISIS', 'ESPECIAL')
FUENTES = esp.FUENTES
ABIERTOS = ('EN_ANALISIS', 'POR_REVISAR')
TERMINADOS = ('CONFORME', 'NO_CONFORME')


def analisis(conn, analisis_id):
    fila = conn.execute('SELECT * FROM cc_analisis WHERE id = ?', (analisis_id,)).fetchone()
    if not fila:
        raise ErrorCalidad('El análisis no existe.')
    return fila


def _abierto_de(conn, lote_id):
    return conn.execute(
        'SELECT a.folio, a.creado_en, f.nombre FROM cc_analisis a LEFT JOIN firmas f ON f.id = a.muestreo_firma_id '
        "WHERE a.lote_id = ? AND a.estatus IN ('EN_ANALISIS','POR_REVISAR')", (lote_id,)).fetchone()


def _error_abierto(conn, lote_id, numero_lote):
    otro = _abierto_de(conn, lote_id)
    if otro:
        return ErrorCalidad(f'El lote {numero_lote} ya tiene el análisis {otro["folio"]} abierto: lo empezó '
                            f'{otro["nombre"] or "otra persona"} el {otro["creado_en"][:16]}.')
    return ErrorCalidad(f'El lote {numero_lote} ya tiene un análisis abierto.')


# ---------------------------------------------------------------------------
# Muestreo
# ---------------------------------------------------------------------------

def muestrear(conn, *, lote_id, usuario, firma, tipo='LIBERACION', muestra_lote_id=None, cantidad=0,
              en_gramos=False, contenedores=None, observaciones=None, retencion=None) -> int:
    """Crea el análisis (AN-AAAA-NNNN) con la especificación vigente y descuenta la muestra del lote donde
    se tomó (MUESTRA). cantidad 0 = solo inspección. retencion = {'envases', 'cantidad', 'ubicacion'}
    guarda además una muestra de retención con la misma firma. Devuelve el id del análisis."""
    if tipo not in TIPOS:
        raise ErrorCalidad('Tipo de análisis no válido.')
    lote = lote_fila(conn, lote_id)
    n = lote['numero_lote']
    if sigue_al_granel(lote) and tipo == 'LIBERACION':
        padre = lote_fila(conn, lote['lote_padre_id'])
        raise ErrorCalidad(f'La presentación {n} sigue al granel {padre["numero_lote"]}: se analiza el granel.')
    if lote['estatus'] == 'RECHAZADO':
        raise ErrorCalidad(f'El lote {n} está rechazado.')
    if tipo == 'LIBERACION':
        if lote['estatus'] not in ('CUARENTENA', 'RETENIDO'):
            raise ErrorCalidad(f'El lote {n} está {lote["estatus"]}: el análisis de liberación es para lotes en '
                               'cuarentena (usa reanálisis o análisis especial).')
        previo = conn.execute("SELECT folio FROM cc_analisis WHERE lote_id = ? AND tipo = 'LIBERACION' "
                              "AND estatus <> 'CANCELADO'", (lote_id,)).fetchone()
        if previo:
            raise ErrorCalidad(f'El lote {n} ya tiene el análisis de liberación {previo["folio"]}; '
                               'para analizarlo otra vez usa un análisis especial.')
    elif tipo == 'REANALISIS' and lote['estatus'] not in ('APROBADO', 'RETENIDO'):
        raise ErrorCalidad(f'El reanálisis es para lotes aprobados; {n} está {lote["estatus"]}.')
    if _abierto_de(conn, lote_id):
        raise _error_abierto(conn, lote_id, n)
    especificacion = esp.vigente(conn, lote['material_id'])
    if not especificacion:
        raise ErrorCalidad(f'{lote["material_codigo"]} no tiene especificación vigente: no se puede muestrear.')

    muestra_lote_id = int(muestra_lote_id or lote_id)
    if muestra_lote_id != int(lote_id):
        hijo = conn.execute('SELECT lote_padre_id FROM lotes WHERE id = ?', (muestra_lote_id,)).fetchone()
        if not hijo or hijo['lote_padre_id'] != int(lote_id):
            raise ErrorCalidad('La muestra se toma del mismo lote o de una de sus presentaciones.')
    cantidad = numero(cantidad, 'la cantidad muestreada') or 0
    if cantidad < 0:
        raise ErrorCalidad('La cantidad muestreada no puede ser negativa.')
    muestra_unidad = conn.execute('SELECT m.unidad FROM lotes l JOIN materiales m ON m.id = l.material_id '
                                  'WHERE l.id = ?', (muestra_lote_id,)).fetchone()['unidad']
    if en_gramos and (muestra_unidad or '').lower() == 'kg':
        cantidad = cantidad / 1000.0

    fid = firmar(conn, firma, 'Realizó', 'lotes', lote_id, f'Muestreó el lote {n} ({lote["material_codigo"]})',
                 roles=ROLES_CALIDAD)
    folio = folios.siguiente_folio(conn, 'AN')
    movimiento_id = None
    if cantidad > 0:
        inventario.mover(conn, lote_id=muestra_lote_id, tipo='MUESTRA', cantidad=cantidad, usuario=usuario,
                         modulo='calidad', folio=folio, motivo=f'Muestra para {folio}', firma_id=fid)
        movimiento_id = conn.execute('SELECT MAX(id) FROM lote_movimientos WHERE lote_id = ?',
                                     (muestra_lote_id,)).fetchone()[0]
    try:
        cur = conn.execute(
            'INSERT INTO cc_analisis (folio, lote_id, especificacion_id, tipo, muestra_lote_id, cantidad_muestra, '
            'contenedores, muestreo_obs, muestreo_firma_id, movimiento_id, creo) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (folio, lote_id, especificacion['id'], tipo, muestra_lote_id, cantidad, texto(contenedores),
             texto(observaciones), fid, movimiento_id, usuario))
    except sqlite3.IntegrityError:
        raise _error_abierto(conn, lote_id, n)
    analisis_id = cur.lastrowid
    if retencion and retencion.get('envases'):
        from . import retencion as ret
        ret.guardar(conn, lote_id=muestra_lote_id, envases=retencion.get('envases'),
                    cantidad=retencion.get('cantidad') or 0, ubicacion=retencion.get('ubicacion'),
                    usuario=usuario, firma_id=fid, analisis_id=analisis_id, en_gramos=en_gramos)
    return analisis_id


# ---------------------------------------------------------------------------
# Resultados
# ---------------------------------------------------------------------------

def _prueba_de(conn, a, prueba_id):
    p = conn.execute('SELECT * FROM cc_especificacion_pruebas WHERE id = ? AND especificacion_id = ?',
                     (prueba_id, a['especificacion_id'])).fetchone()
    if not p:
        raise ErrorCalidad('Esa prueba no es de la especificación de este análisis.')
    return p


def _validar_fuente(conn, a, p, fuente, equipo_id, laboratorio_id, informe, archivo, fecha_txt):
    """Devuelve (equipo_id, informe) ya validados."""
    if fuente not in FUENTES:
        raise ErrorCalidad('Indica de dónde sale el resultado: interno, externo o certificado del proveedor.')
    if fuente == 'Interno':
        if p['equipo_tipo'] and not equipo_id:
            raise ErrorCalidad(f'{p["prueba"]} pide equipo ({p["equipo_tipo"]}): elige cuál se usó.')
        if equipo_id:
            e = conn.execute('SELECT codigo, tipo FROM equipos WHERE id = ?', (equipo_id,)).fetchone()
            if not e:
                raise ErrorCalidad('El equipo no existe.')
            if p['equipo_tipo'] and (e['tipo'] or '').strip().lower() != p['equipo_tipo'].strip().lower():
                raise ErrorCalidad(f'{e["codigo"]} no es {p["equipo_tipo"]}.')
            try:
                apto = equipo_apto(conn, equipo_id, fecha=fecha_txt)
            except ErrorEquipo as err:
                raise ErrorCalidad(str(err))
            if not apto['apto']:
                raise ErrorCalidad('El equipo no está apto: ' + ' '.join(apto['bloqueos']))
        return (int(equipo_id) if equipo_id else None), None
    if fuente == 'Externo':
        lab = laboratorio_id or a['externo_laboratorio_id']
        if not lab:
            raise ErrorCalidad('Indica el laboratorio externo (Enviar a laboratorio externo).')
        if not texto(informe):
            raise ErrorCalidad('Escribe el número de informe del laboratorio externo.')
        tiene_informe = conn.execute("SELECT 1 FROM archivos WHERE entidad = 'cc_analisis' AND entidad_id = ?",
                                     (a['id'],)).fetchone()
        if not tiene_informe and not (archivo and getattr(archivo, 'filename', None)):
            raise ErrorCalidad('Adjunta el informe del laboratorio externo.')
        if laboratorio_id and not a['externo_laboratorio_id']:
            conn.execute('UPDATE cc_analisis SET externo_laboratorio_id = ? WHERE id = ?', (laboratorio_id, a['id']))
        return None, texto(informe)
    # Certificado del proveedor
    lote = lote_fila(conn, a['lote_id'])
    if lote['material_tipo'] == 'GR' or lote['origen'] != 'RECEPCION' or not lote['proveedor_id']:
        raise ErrorCalidad('El certificado del proveedor solo vale para materiales recibidos de un proveedor '
                           '(no para un granel ni un producto fabricado).')
    if lote['proveedor_estatus'] != 'Aprobado':
        raise ErrorCalidad(f'{lote["proveedor_nombre"]} no es un proveedor Aprobado '
                           f'({lote["proveedor_estatus"]}): no se acepta su certificado.')
    return None, texto(informe)


def _insertar_resultado(conn, a, p, *, firma_id, fuente, valor=None, texto_obs=None, cumple=None,
                        equipo_id=None, informe=None, fecha_txt=None):
    if p['tipo'] == 'NUMERICA':
        if valor in (None, ''):
            raise ErrorCalidad(f'Escribe el resultado de {p["prueba"]}.')
        red = redondear(valor, p['decimales'])
        ok = cumple_limites(red, p['limite_min'], p['limite_max'])
        try:
            crudo = float(str(valor).replace(',', '').strip())
        except ValueError:
            raise ErrorCalidad('El resultado debe ser un número.')
        datos = (crudo, float(red), texto(texto_obs))
    else:
        if not texto(texto_obs):
            raise ErrorCalidad(f'Escribe lo observado en {p["prueba"]}.')
        if cumple in (None, ''):
            raise ErrorCalidad(f'Indica si {p["prueba"]} cumple o no cumple.')
        ok = str(cumple) in ('1', 'True', 'true', 'si', 'sí', 'cumple')
        datos = (None, None, texto(texto_obs))
    cur = conn.execute(
        'INSERT INTO cc_resultados (analisis_id, prueba_id, valor, valor_redondeado, texto, cumple, fuente, '
        'equipo_id, informe_externo, fecha_analisis, realizo_firma_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (a['id'], p['id'], *datos, 1 if ok else 0, fuente, equipo_id, informe, fecha_txt or hoy(), firma_id))
    return cur.lastrowid, ok


def capturar(conn, *, analisis_id, prueba_id, usuario, firma, valor=None, texto_obs=None, cumple=None,
             fuente=None, equipo_id=None, laboratorio_id=None, informe=None, archivo=None,
             fecha_analisis=None) -> dict:
    """Guarda un resultado. Las numéricas se redondean a los decimales de la especificación (el 5 sube) y el
    sistema decide si cumplen. Si no cumple, abre la investigación OOS.
    Devuelve {'resultado_id', 'cumple', 'investigacion_id'}."""
    a = analisis(conn, analisis_id)
    if a['estatus'] not in ABIERTOS:
        raise ErrorCalidad(f'El análisis {a["folio"]} ya no admite resultados ({a["estatus"]}).')
    p = _prueba_de(conn, a, prueba_id)
    if conn.execute('SELECT 1 FROM cc_resultados WHERE analisis_id = ? AND prueba_id = ? AND valido = 1',
                    (analisis_id, prueba_id)).fetchone():
        raise ErrorCalidad(f'{p["prueba"]} ya tiene resultado: corrígelo (si cumple) o investígalo.')
    fecha_txt = fecha(fecha_analisis, 'fecha del análisis') or hoy()
    fuente = texto(fuente) or p['fuente_default']
    equipo_id = int(equipo_id) if equipo_id not in (None, '', 0, '0') else None
    laboratorio_id = int(laboratorio_id) if laboratorio_id not in (None, '', 0, '0') else None
    equipo_id, informe = _validar_fuente(conn, a, p, fuente, equipo_id, laboratorio_id, informe, archivo, fecha_txt)

    fid = firmar(conn, firma, 'Realizó', 'cc_analisis', analisis_id, f'{a["folio"]}: capturó {p["prueba"]}',
                 roles=ROLES_CALIDAD)
    if fuente == 'Externo' and archivo and getattr(archivo, 'filename', None):
        archivos.guardar_archivo(conn, archivo, modulo='calidad', entidad='cc_analisis', entidad_id=analisis_id,
                                 descripcion=f'Informe externo {informe}', usuario=usuario)
    rid, ok = _insertar_resultado(conn, a, p, firma_id=fid, fuente=fuente, valor=valor, texto_obs=texto_obs,
                                  cumple=cumple, equipo_id=equipo_id, informe=informe, fecha_txt=fecha_txt)
    inv_id = None
    if not ok:
        from . import investigaciones
        inv_id = investigaciones.abrir(conn, analisis_id=analisis_id, resultado_id=rid)
    actualizar_estatus(conn, analisis_id)
    return {'resultado_id': rid, 'cumple': ok, 'investigacion_id': inv_id}


def corregir(conn, *, resultado_id, motivo, usuario, firma, valor=None, texto_obs=None, cumple=None,
             fecha_analisis=None) -> dict:
    """Error de captura: solo antes de la revisión, solo un resultado que cumple y solo quien lo capturó.
    El anterior queda anulado (tachado) con el motivo; el nuevo es otro renglón."""
    r = conn.execute('SELECT * FROM cc_resultados WHERE id = ?', (resultado_id,)).fetchone()
    if not r or not r['valido']:
        raise ErrorCalidad('Ese resultado no existe o ya está anulado.')
    a = analisis(conn, r['analisis_id'])
    if a['estatus'] not in ABIERTOS or a['reviso_firma_id']:
        raise ErrorCalidad('Una captura solo se corrige antes de la revisión.')
    if not r['cumple']:
        raise ErrorCalidad('Un resultado fuera de especificación no se corrige: se investiga.')
    motivo = texto(motivo)
    if not motivo:
        raise ErrorCalidad('Escribe el motivo de la corrección.')
    p = _prueba_de(conn, a, r['prueba_id'])
    fid = firmar(conn, firma, 'Anuló', 'cc_resultados', resultado_id,
                 f'{a["folio"]}: corrigió la captura de {p["prueba"]}', roles=ROLES_CALIDAD)
    if numero_firmante(conn, fid) != numero_firmante(conn, r['realizo_firma_id']):
        raise ErrorCalidad('Solo quien capturó el resultado lo puede corregir.')
    conn.execute('UPDATE cc_resultados SET valido = 0, motivo_anulacion = ?, anulo_firma_id = ? WHERE id = ?',
                 (f'Error de captura: {motivo}', fid, resultado_id))
    fid_nuevo = firmar(conn, firma, 'Realizó', 'cc_analisis', a['id'], f'{a["folio"]}: recapturó {p["prueba"]}',
                       roles=ROLES_CALIDAD)
    rid, ok = _insertar_resultado(conn, a, p, firma_id=fid_nuevo, fuente=r['fuente'], valor=valor, texto_obs=texto_obs,
                                  cumple=cumple, equipo_id=r['equipo_id'], informe=r['informe_externo'],
                                  fecha_txt=fecha(fecha_analisis, 'fecha del análisis') or r['fecha_analisis'])
    inv_id = None
    if not ok:
        from . import investigaciones
        inv_id = investigaciones.abrir(conn, analisis_id=a['id'], resultado_id=rid)
    actualizar_estatus(conn, a['id'])
    return {'resultado_id': rid, 'cumple': ok, 'investigacion_id': inv_id}


def pendientes_de_capturar(conn, a):
    """Pruebas que se hacen en cada lote y aún no tienen resultado válido."""
    return conn.execute(
        'SELECT p.* FROM cc_especificacion_pruebas p WHERE p.especificacion_id = ? AND p.cada_lote = 1 '
        'AND NOT EXISTS (SELECT 1 FROM cc_resultados r WHERE r.analisis_id = ? AND r.prueba_id = p.id '
        'AND r.valido = 1) ORDER BY p.orden', (a['especificacion_id'], a['id'])).fetchall()


def investigaciones_abiertas(conn, analisis_id):
    return conn.execute("SELECT COUNT(*) FROM cc_investigaciones WHERE analisis_id = ? AND estatus = 'ABIERTA'",
                        (analisis_id,)).fetchone()[0]


def _todo_cumple(conn, analisis_id):
    return not conn.execute('SELECT 1 FROM cc_resultados WHERE analisis_id = ? AND valido = 1 AND cumple = 0',
                            (analisis_id,)).fetchone()


def actualizar_estatus(conn, analisis_id):
    """Pasa a POR_REVISAR (o termina, si la especificación no pide revisión) cuando cada prueba de cada lote
    tiene resultado válido y no hay investigaciones abiertas; regresa a EN_ANALISIS si deja de cumplirse."""
    a = analisis(conn, analisis_id)
    if a['estatus'] not in ABIERTOS:
        return a['estatus']
    listo = not investigaciones_abiertas(conn, analisis_id) and not pendientes_de_capturar(conn, a)
    if not listo:
        nuevo = 'EN_ANALISIS'
    else:
        e = esp.especificacion(conn, a['especificacion_id'])
        if e['requiere_revision']:
            nuevo = 'POR_REVISAR'
        else:
            nuevo = 'CONFORME' if _todo_cumple(conn, analisis_id) else 'NO_CONFORME'
    if nuevo != a['estatus']:
        conn.execute('UPDATE cc_analisis SET estatus = ?, terminado_en = ? WHERE id = ?',
                     (nuevo, ahora() if nuevo in TERMINADOS else None, analisis_id))
    return nuevo


def capturistas(conn, analisis_id):
    """Números de empleado de quienes capturaron (o corrigieron) resultados del análisis."""
    return sorted({r[0] for r in conn.execute(
        'SELECT f.numero_empleado FROM cc_resultados r JOIN firmas f ON f.id = r.realizo_firma_id '
        'WHERE r.analisis_id = ?', (analisis_id,)).fetchall()})


def revisar(conn, *, analisis_id, firma, observaciones=None) -> str:
    """Firma Revisó de alguien que no capturó resultados. Queda CONFORME (todo cumple) o NO_CONFORME."""
    a = analisis(conn, analisis_id)
    if a['estatus'] != 'POR_REVISAR':
        if investigaciones_abiertas(conn, analisis_id):
            raise ErrorCalidad('Hay investigaciones abiertas: no se puede revisar.')
        if a['estatus'] == 'EN_ANALISIS':
            faltan = pendientes_de_capturar(conn, a)
            raise ErrorCalidad('Faltan resultados: ' + ', '.join(p['prueba'] for p in faltan) + '.')
        raise ErrorCalidad(f'El análisis {a["folio"]} no está por revisar ({a["estatus"]}).')
    fid = firmar(conn, firma, 'Revisó', 'cc_analisis', analisis_id, f'Revisó resultados de {a["folio"]}',
                 roles=ROLES_CALIDAD, distinto_de=capturistas(conn, analisis_id) or None)
    nuevo = 'CONFORME' if _todo_cumple(conn, analisis_id) else 'NO_CONFORME'
    conn.execute('UPDATE cc_analisis SET estatus = ?, reviso_firma_id = ?, terminado_en = ?, '
                 'observaciones = COALESCE(?, observaciones) WHERE id = ?',
                 (nuevo, fid, ahora(), texto(observaciones), analisis_id))
    return nuevo


def cancelar(conn, *, analisis_id, motivo, firma) -> int:
    """Por ejemplo, muestra mal tomada. La muestra ya tomada no regresa al lote."""
    a = analisis(conn, analisis_id)
    if a['estatus'] not in ABIERTOS:
        raise ErrorCalidad(f'El análisis {a["folio"]} ya está {a["estatus"]}.')
    motivo = texto(motivo)
    if not motivo:
        raise ErrorCalidad('Escribe el motivo de la cancelación.')
    if investigaciones_abiertas(conn, analisis_id):
        raise ErrorCalidad('Cierra primero la investigación abierta del análisis.')
    fid = firmar(conn, firma, 'Anuló', 'cc_analisis', analisis_id, f'Canceló {a["folio"]}: {motivo}',
                 roles=ROLES_CALIDAD)
    conn.execute("UPDATE cc_analisis SET estatus = 'CANCELADO', motivo_cancelacion = ?, cancelo_firma_id = ?, "
                 'terminado_en = ? WHERE id = ?', (motivo, fid, ahora(), analisis_id))
    return fid


def enviar_externo(conn, *, analisis_id, usuario, laboratorio_id=None, laboratorio_nuevo=None, fecha_envio=None):
    """Anota a qué laboratorio externo se mandó la muestra (o da de alta el laboratorio como proveedor
    de servicios). Devuelve el id del laboratorio."""
    a = analisis(conn, analisis_id)
    if a['estatus'] not in ABIERTOS:
        raise ErrorCalidad(f'El análisis {a["folio"]} ya está {a["estatus"]}.')
    if texto(laboratorio_nuevo):
        laboratorio_id = inventario.crear_proveedor(conn, nombre=texto(laboratorio_nuevo), tipo='Servicios',
                                                    usuario=usuario, notas='Laboratorio externo (alta desde Calidad)')
    if not laboratorio_id:
        raise ErrorCalidad('Elige el laboratorio externo o escribe el nombre de uno nuevo.')
    if not conn.execute('SELECT 1 FROM proveedores WHERE id = ?', (laboratorio_id,)).fetchone():
        raise ErrorCalidad('El laboratorio no existe.')
    conn.execute('UPDATE cc_analisis SET externo_laboratorio_id = ?, externo_enviado = ? WHERE id = ?',
                 (laboratorio_id, fecha(fecha_envio, 'fecha de envío') or hoy(), analisis_id))
    return int(laboratorio_id)


def guardar_archivo(conn, *, analisis_id, archivo, descripcion, usuario) -> int:
    a = analisis(conn, analisis_id)
    return archivos.guardar_archivo(conn, archivo, modulo='calidad', entidad='cc_analisis', entidad_id=a['id'],
                                    descripcion=texto(descripcion) or 'Archivo del análisis', usuario=usuario)


# ---------------------------------------------------------------------------
# Consultas
# ---------------------------------------------------------------------------

def detalle(conn, analisis_id) -> dict:
    a = dict(analisis(conn, analisis_id))
    for campo in ('muestreo_firma_id', 'reviso_firma_id', 'cancelo_firma_id'):
        a[campo.replace('_firma_id', '_nombre')] = nombre_firmante(conn, a[campo])
    lab = conn.execute('SELECT nombre FROM proveedores WHERE id = ?', (a['externo_laboratorio_id'],)).fetchone() \
        if a['externo_laboratorio_id'] else None
    a['laboratorio'] = lab['nombre'] if lab else None
    lote = dict(lote_fila(conn, a['lote_id']))
    muestra = lote_fila(conn, a['muestra_lote_id']) if a['muestra_lote_id'] != a['lote_id'] else None
    e = esp.especificacion(conn, a['especificacion_id'])
    resultados = conn.execute(
        'SELECT r.*, f.nombre AS realizo, f.numero_empleado AS realizo_numero, fa.nombre AS anulo, '
        'eq.codigo AS equipo_codigo FROM cc_resultados r JOIN firmas f ON f.id = r.realizo_firma_id '
        'LEFT JOIN firmas fa ON fa.id = r.anulo_firma_id LEFT JOIN equipos eq ON eq.id = r.equipo_id '
        'WHERE r.analisis_id = ? ORDER BY r.id', (analisis_id,)).fetchall()
    filas, capturadas, total = [], 0, 0
    for p in esp.pruebas(conn, a['especificacion_id']):
        propios = [dict(r) for r in resultados if r['prueba_id'] == p['id']]
        valido = next((r for r in reversed(propios) if r['valido']), None)
        if p['cada_lote']:
            total += 1
            capturadas += 1 if valido else 0
        filas.append({'prueba': dict(p), 'resultados': propios, 'valido': valido})
    invs = conn.execute('SELECT i.*, p.prueba FROM cc_investigaciones i JOIN cc_resultados r ON r.id = i.resultado_id '
                        'JOIN cc_especificacion_pruebas p ON p.id = r.prueba_id WHERE i.analisis_id = ? '
                        'ORDER BY i.id', (analisis_id,)).fetchall()
    return {
        'a': a, 'lote': lote, 'muestra_lote': dict(muestra) if muestra else None, 'esp': dict(e), 'filas': filas,
        'avance': (capturadas, total), 'investigaciones': [dict(i) for i in invs],
        'archivos': archivos.archivos_de(conn, 'cc_analisis', analisis_id),
        'archivos_lote': archivos.archivos_de(conn, 'lotes', a['lote_id']),
        'capturistas': capturistas(conn, analisis_id),
    }


def equipos_aptos(conn, equipo_tipo=None):
    """Equipos en servicio (del tipo pedido) que hoy están aptos, para elegir al capturar."""
    sql, params = "SELECT * FROM equipos WHERE estatus = 'EN_SERVICIO'", []
    if equipo_tipo:
        sql += ' AND lower(tipo) = lower(?)'
        params.append(equipo_tipo)
    salida = []
    for e in conn.execute(sql + ' ORDER BY codigo', params).fetchall():
        if equipo_apto(conn, e['id'])['apto']:
            salida.append(dict(e))
    return salida


def laboratorios(conn):
    return conn.execute("SELECT id, nombre, estatus FROM proveedores WHERE activo = 1 "
                        "AND tipo IN ('Servicios','Ambos') ORDER BY nombre").fetchall()


def analisis_por_equipo(conn, equipo_id, desde) -> list:
    """Análisis y lotes donde se usó un instrumento desde una fecha (lo usa Equipos cuando una calibración
    sale fuera de tolerancia). Un renglón por resultado capturado con ese equipo."""
    return [dict(r) for r in conn.execute(
        'SELECT a.id AS analisis_id, a.folio, a.estatus AS analisis_estatus, l.id AS lote_id, l.numero_lote, '
        'l.estatus AS lote_estatus, m.codigo AS material_codigo, m.nombre AS material_nombre, '
        "m.codigo || ' · ' || m.nombre AS material, p.prueba, r.fecha_analisis, r.fecha_analisis AS fecha, r.valido, "
        "'/calidad/analisis/' || a.id AS url "
        'FROM cc_resultados r JOIN cc_analisis a ON a.id = r.analisis_id JOIN lotes l ON l.id = a.lote_id '
        'JOIN materiales m ON m.id = l.material_id JOIN cc_especificacion_pruebas p ON p.id = r.prueba_id '
        'WHERE r.equipo_id = ? AND r.fecha_analisis >= ? ORDER BY r.fecha_analisis, a.id',
        (equipo_id, fecha(desde, 'fecha desde') or '0000-00-00')).fetchall()]


# ---------------------------------------------------------------------------
# Bandeja
# ---------------------------------------------------------------------------

_SQL_LOTE = ('SELECT l.id AS lote_id, l.numero_lote, l.lote_proveedor, l.origen, l.origen_folio, l.estatus, '
             'l.fecha_entrada, l.fecha_caducidad, l.fecha_reanalisis, l.existencia, l.material_id, '
             'm.codigo AS material_codigo, m.nombre AS material_nombre, m.tipo AS material_tipo, m.unidad, '
             'p.nombre AS proveedor, '
             "CAST(julianday('now','localtime') - julianday(l.fecha_entrada) AS INTEGER) AS dias "
             'FROM lotes l JOIN materiales m ON m.id = l.material_id '
             'LEFT JOIN proveedores p ON p.id = l.proveedor_id '
             'LEFT JOIN lotes lp ON lp.id = l.lote_padre_id LEFT JOIN materiales mp ON mp.id = lp.material_id ')
# Las presentaciones envasadas de un granel no aparecen aparte: siguen al granel.
_NO_SIGUE_GRANEL = "NOT (m.tipo = 'PT' AND mp.tipo = 'GR' AND l.origen <> 'DEVOLUCION')"

_SQL_POR_MUESTREAR = (
    _SQL_LOTE + "WHERE l.estatus = 'CUARENTENA' AND m.requiere_analisis = 1 AND " + _NO_SIGUE_GRANEL +
    " AND NOT EXISTS (SELECT 1 FROM cc_analisis a WHERE a.lote_id = l.id AND a.tipo = 'LIBERACION' "
    "AND a.estatus <> 'CANCELADO') "
    "AND NOT EXISTS (SELECT 1 FROM cc_analisis a WHERE a.lote_id = l.id AND a.estatus IN ('EN_ANALISIS','POR_REVISAR'))")

_SQL_ANALISIS = ('SELECT a.*, l.numero_lote, l.estatus AS lote_estatus, l.fecha_entrada, m.codigo AS material_codigo, '
                 'm.nombre AS material_nombre, m.tipo AS material_tipo, pr.nombre AS laboratorio, '
                 "(SELECT COUNT(*) FROM cc_investigaciones i WHERE i.analisis_id = a.id AND i.estatus = 'ABIERTA') "
                 'AS inv_abiertas, '
                 '(SELECT COUNT(*) FROM cc_especificacion_pruebas p WHERE p.especificacion_id = a.especificacion_id '
                 'AND p.cada_lote = 1) AS total_pruebas, '
                 '(SELECT COUNT(DISTINCT r.prueba_id) FROM cc_resultados r JOIN cc_especificacion_pruebas p '
                 'ON p.id = r.prueba_id WHERE r.analisis_id = a.id AND r.valido = 1 AND p.cada_lote = 1) AS capturadas '
                 'FROM cc_analisis a JOIN lotes l ON l.id = a.lote_id JOIN materiales m ON m.id = l.material_id '
                 'LEFT JOIN proveedores pr ON pr.id = a.externo_laboratorio_id ')

_SQL_POR_DICTAMINAR = (
    _SQL_LOTE + "WHERE l.estatus = 'CUARENTENA' AND " + _NO_SIGUE_GRANEL +
    " AND EXISTS (SELECT 1 FROM cc_analisis a WHERE a.lote_id = l.id AND a.tipo = 'LIBERACION' "
    "AND a.estatus IN ('CONFORME','NO_CONFORME'))")


def _demanda(conn, material_id):
    from flask import current_app, has_app_context
    from nucleo import registro
    try:
        return registro.consulta('demanda_material', conn, material_id, default=None)
    except Exception:
        if has_app_context():
            current_app.logger.exception('Falló la consulta demanda_material')
        return None


def por_muestrear(conn, limite=None):
    filas = []
    for r in conn.execute(_SQL_POR_MUESTREAR + ' ORDER BY l.fecha_entrada, l.id').fetchall():
        d = dict(r)
        d['especificacion_id'] = (esp.vigente(conn, d['material_id']) or {'id': None})['id']
        d['demanda'] = _demanda(conn, d['material_id'])
        filas.append(d)

    def clave(d):
        dem = d['demanda'] if isinstance(d['demanda'], dict) else None
        cuando = str(dem.get('fecha') or dem.get('fecha_programada') or '9999') if dem else '9999'
        return (0 if dem else 1, cuando, -(d['dias'] or 0))
    filas.sort(key=clave)
    return filas[:limite] if limite else filas


def lotes_por_dictaminar(conn):
    return [dict(r) for r in conn.execute(_SQL_POR_DICTAMINAR + ' ORDER BY l.fecha_entrada').fetchall()]


def ultimo_cambio_estatus(conn, lote_id):
    return conn.execute("SELECT * FROM lote_historial WHERE lote_id = ? AND campo = 'estatus' ORDER BY id DESC "
                        'LIMIT 1', (lote_id,)).fetchone()


def reanalisis_proximos(conn):
    dias = parametro_int('calidad.dias_aviso_reanalisis', conn)
    filas = conn.execute(
        _SQL_LOTE + "WHERE l.estatus IN ('APROBADO','RETENIDO') AND l.fecha_reanalisis IS NOT NULL "
        "AND l.fecha_reanalisis <= date('now','localtime', ?) AND l.existencia > 0 ORDER BY l.fecha_reanalisis",
        (f'+{dias} days',)).fetchall()
    salida = []
    for r in filas:
        d = dict(r)
        ultimo = conn.execute("SELECT id, folio, estatus FROM cc_analisis WHERE lote_id = ? AND tipo = 'REANALISIS' "
                              "AND estatus <> 'CANCELADO' ORDER BY id DESC LIMIT 1", (d['lote_id'],)).fetchone()
        d['reanalisis'] = dict(ultimo) if ultimo else None
        d['vencido'] = d['fecha_reanalisis'] < hoy()
        salida.append(d)
    return salida


def bandeja(conn, q=None) -> dict:
    """Todo lo de la bandeja de Calidad, por pestaña, con los indicadores."""
    from . import dictamen
    muestrear_ = por_muestrear(conn)
    en_analisis = [dict(r) for r in conn.execute(_SQL_ANALISIS + "WHERE a.estatus = 'EN_ANALISIS' ORDER BY a.id")]
    por_revisar = [dict(r) for r in conn.execute(_SQL_ANALISIS + "WHERE a.estatus = 'POR_REVISAR' ORDER BY a.id")]
    por_dictaminar = []
    for d in lotes_por_dictaminar(conn):
        v = dictamen.verificaciones(conn, d['lote_id'])
        d['pendientes'] = v['motivos']
        d['analisis'] = v['analisis']
        por_dictaminar.append(d)
    retenidos = []
    for r in conn.execute(_SQL_LOTE + "WHERE l.estatus = 'RETENIDO' ORDER BY l.id").fetchall():
        d = dict(r)
        h = ultimo_cambio_estatus(conn, d['lote_id'])
        d['motivo'] = h['motivo'] if h else ''
        d['folio_retencion'] = h['folio'] if h else ''
        retenidos.append(d)
    reanalisis = reanalisis_proximos(conn)
    sql_term = _SQL_ANALISIS + "WHERE a.estatus IN ('CONFORME','NO_CONFORME','CANCELADO') "
    params = []
    if texto(q):
        sql_term += 'AND (a.folio LIKE ? OR l.numero_lote LIKE ? OR m.codigo LIKE ? OR m.nombre LIKE ?) '
        params = [f'%{q.strip()}%'] * 4
    terminados = [dict(r) for r in conn.execute(sql_term + 'ORDER BY a.id DESC LIMIT 50', params)]
    inv_abiertas = conn.execute("SELECT COUNT(*) FROM cc_investigaciones WHERE estatus = 'ABIERTA'").fetchone()[0]
    prom = conn.execute(
        "SELECT AVG(julianday(d.ts) - julianday(l.fecha_entrada)) FROM cc_dictamenes d JOIN lotes l ON l.id = d.lote_id "
        "WHERE d.decision = 'APROBADO' AND strftime('%Y-%m', d.ts) = strftime('%Y-%m', 'now', 'localtime')").fetchone()[0]
    return {
        'por_muestrear': muestrear_, 'en_analisis': en_analisis, 'por_revisar': por_revisar,
        'por_dictaminar': por_dictaminar, 'retenidos': retenidos, 'reanalisis': reanalisis,
        'terminados': terminados,
        'kpis': {'por_muestrear': len(muestrear_), 'en_analisis': len(en_analisis), 'por_revisar': len(por_revisar),
                 'por_dictaminar': len(por_dictaminar), 'investigaciones': inv_abiertas,
                 'dias_liberacion': round(prom, 1) if prom is not None else None},
    }
