"""Calibraciones: registrar (con cada punto como se encontró y como se dejó), anular antes del
dictamen, dictaminar apto / no apto y la calibración vigente en una fecha."""
from nucleo import folios
from nucleo import inventario

from . import impactos
from .comun import (ErrorEquipos, MODALIDADES, TIPO_PESA, apto, cambiar_estatus, causa_fuera, equipo,
                    exigir_facultad, exigir_firma, fecha_iso, fijar_vencimientos, hoy_iso, numero, programa,
                    si, sumar_meses, texto)


def calibracion(conn, calibracion_id):
    fila = conn.execute('SELECT c.*, e.codigo, e.nombre AS equipo_nombre, p.nombre AS proveedor_nombre, '
                        'p.estatus AS proveedor_estatus FROM eq_calibraciones c JOIN equipos e ON e.id = c.equipo_id '
                        'LEFT JOIN proveedores p ON p.id = c.proveedor_id WHERE c.id = ?', (calibracion_id,)).fetchone()
    if not fila:
        raise ErrorEquipos('La calibración no existe.')
    return fila


def puntos_de(conn, calibracion_id):
    return conn.execute('SELECT * FROM eq_calibracion_puntos WHERE calibracion_id = ? ORDER BY id',
                        (calibracion_id,)).fetchall()


def patrones_aptos(conn, fecha=None):
    """Pesas y patrones que hoy se pueden usar (para elegir en la calibración interna y la verificación)."""
    filas = conn.execute("SELECT e.*, f.valor_nominal, f.unidad_nominal, f.clase FROM equipos e "
                         "LEFT JOIN eq_fichas f ON f.equipo_id = e.id WHERE e.es_instrumento = 1 "
                         "AND e.estatus = 'EN_SERVICIO' ORDER BY e.tipo <> ?, f.valor_nominal, e.codigo",
                         (TIPO_PESA,)).fetchall()
    return [f for f in filas if apto(conn, f['id'], fecha)['apto']]


def registrar(conn, equipo_id, *, fecha, modalidad, puntos, firma_id, usuario, proveedor_id=None,
              acreditacion=None, certificado=None, con_certificado_pdf=False, patrones=None, ajustado=False,
              observaciones=None, hoy=None) -> dict:
    """Externa: laboratorio, número de certificado y el PDF. Interna: patrones aptos.
    puntos: [{nominal, unidad, tolerancia, encontrado, dejado (vacío = el mismo), incertidumbre}].
    Devuelve {'id', 'folio', 'avisos'}."""
    e = equipo(conn, equipo_id)
    exigir_firma(conn, firma_id)
    if e['estatus'] == 'BAJA':
        raise ErrorEquipos('El equipo está dado de baja.')
    fecha = fecha_iso(fecha, 'fecha de la calibración')
    if fecha > hoy_iso(hoy):
        raise ErrorEquipos('La fecha de la calibración no puede ser futura.')
    if modalidad not in MODALIDADES:
        raise ErrorEquipos('La calibración debe ser Externa o Interna.')
    avisos = []
    patrones_txt = None
    proveedor_id = int(proveedor_id) if str(proveedor_id or '').strip() not in ('', '0', 'None') else None
    if modalidad == 'Externa':
        if not proveedor_id:
            raise ErrorEquipos('Elige el laboratorio que calibró.')
        prov = conn.execute('SELECT nombre, estatus FROM proveedores WHERE id = ?', (proveedor_id,)).fetchone()
        if not prov:
            raise ErrorEquipos('El laboratorio no existe.')
        if prov['estatus'] != 'Aprobado':
            avisos.append(f'Ojo: el laboratorio {prov["nombre"]} no está aprobado ({prov["estatus"]}).')
        if not texto(certificado):
            raise ErrorEquipos('Escribe el número de certificado del laboratorio.')
        if not con_certificado_pdf:
            raise ErrorEquipos('Adjunta el certificado de calibración en PDF.')
    else:
        ids = [int(p) for p in (patrones or []) if str(p).strip()]
        if not ids:
            raise ErrorEquipos('En la calibración interna elige los patrones usados.')
        codigos = []
        for pid in ids:
            if pid == equipo_id:
                raise ErrorEquipos('Un equipo no puede ser su propio patrón.')
            pat = equipo(conn, pid)
            r = apto(conn, pid, fecha)
            if not r['apto']:
                raise ErrorEquipos(f'El patrón {pat["codigo"]} no está apto: ' + ' '.join(r['bloqueos']))
            codigos.append(pat['codigo'])
        patrones_txt = ', '.join(codigos)
        proveedor_id = None
    limpios = []
    for i, p in enumerate(puntos or [], start=1):
        if all(str(p.get(k) or '').strip() == '' for k in ('nominal', 'encontrado', 'dejado', 'tolerancia')):
            continue
        nominal = numero(p.get('nominal'), f'el nominal del punto {i}')
        tolerancia = numero(p.get('tolerancia'), f'la tolerancia del punto {i}')
        if tolerancia < 0:
            raise ErrorEquipos(f'La tolerancia del punto {i} no puede ser negativa.')
        unidad = texto(p.get('unidad'))
        if not unidad:
            raise ErrorEquipos(f'Falta la unidad del punto {i}.')
        encontrado = numero(p.get('encontrado'), f'la lectura como se encontró del punto {i}')
        dejado = numero(p.get('dejado'), f'la lectura como se dejó del punto {i}', obligatorio=False)
        if dejado is None:
            dejado = encontrado
        incert = numero(p.get('incertidumbre'), f'la incertidumbre del punto {i}', obligatorio=False)
        ok_e = 1 if abs(encontrado - nominal) <= tolerancia + 1e-9 else 0
        ok_d = 1 if abs(dejado - nominal) <= tolerancia + 1e-9 else 0
        limpios.append((nominal, unidad, tolerancia, encontrado, dejado, incert, ok_e, ok_d))
    if not limpios:
        raise ErrorEquipos('Captura al menos un punto de calibración.')
    conf_e = int(all(x[6] for x in limpios))
    conf_d = int(all(x[7] for x in limpios))
    folio = folios.siguiente_folio(conn, 'CAL')
    cur = conn.execute(
        'INSERT INTO eq_calibraciones (folio, equipo_id, fecha, modalidad, proveedor_id, acreditacion, certificado, '
        'patrones, ajustado, conforme_encontrado, conforme_dejado, observaciones, realizo_firma_id) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (folio, equipo_id, fecha, modalidad, proveedor_id, texto(acreditacion), texto(certificado), patrones_txt,
         si(ajustado), conf_e, conf_d, texto(observaciones), firma_id))
    cal_id = cur.lastrowid
    for x in limpios:
        conn.execute('INSERT INTO eq_calibracion_puntos (calibracion_id, nominal, unidad, tolerancia, encontrado, '
                     'dejado, incertidumbre, conforme_encontrado, conforme_dejado) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (cal_id, *x))
    if not conf_e:
        avisos.append('El equipo llegó fuera de tolerancia: al dictaminar se abre la evaluación de impacto.')
    return {'id': cal_id, 'folio': folio, 'avisos': avisos}


def anular(conn, calibracion_id, *, motivo, firma_id, usuario):
    c = calibracion(conn, calibracion_id)
    exigir_firma(conn, firma_id)
    if c['estatus'] != 'POR_DICTAMINAR':
        raise ErrorEquipos('Solo se anula una calibración que aún no se dictamina.')
    motivo = texto(motivo)
    if not motivo:
        raise ErrorEquipos('Escribe el motivo de la anulación.')
    conn.execute("UPDATE eq_calibraciones SET estatus = 'ANULADA', motivo_anulacion = ? WHERE id = ?",
                 (motivo, calibracion_id))


def dictaminar(conn, calibracion_id, *, es_apto, motivo=None, firma_id, usuario) -> dict:
    """Firma crítica de dictaminar_equipos. Apto solo si todos los puntos cumplen como se dejaron:
    fija el vencimiento (fecha + frecuencia) y, si estaba fuera por calibración, lo regresa a servicio.
    No apto lo deja fuera de servicio. Si llegó fuera de tolerancia, abre la evaluación de impacto.
    Devuelve {'proxima', 'impacto_id'}."""
    c = calibracion(conn, calibracion_id)
    exigir_facultad(conn, firma_id)
    if c['estatus'] != 'POR_DICTAMINAR':
        raise ErrorEquipos('Esta calibración ya se dictaminó o se anuló.')
    e = equipo(conn, c['equipo_id'])
    motivo = texto(motivo)
    proxima = None
    if es_apto:
        if not c['conforme_dejado']:
            raise ErrorEquipos('No puede ser apto: hay puntos que no cumplen como se dejaron.')
        prog = programa(conn, c['equipo_id'], 'CALIBRACION')
        posterior = conn.execute("SELECT 1 FROM eq_calibraciones WHERE equipo_id = ? AND estatus = 'APTO' "
                                 'AND fecha > ?', (c['equipo_id'], c['fecha'])).fetchone()
        if prog and not posterior:
            proxima = sumar_meses(c['fecha'], prog['frecuencia_meses'])
            fijar_vencimientos(conn, c['equipo_id'], vence_calibracion=proxima, usuario=usuario,
                               motivo=f'Calibración {c["folio"]} apta')
        conn.execute("UPDATE eq_calibraciones SET estatus = 'APTO', dictamen_motivo = ?, proxima = ?, "
                     'dictamino_firma_id = ? WHERE id = ?', (motivo, proxima, firma_id, calibracion_id))
        if e['estatus'] == 'FUERA_DE_SERVICIO' and causa_fuera(conn, e['id']) in ('POR_CALIBRAR', 'CALIBRACION') \
                and not conn.execute("SELECT 1 FROM eq_ordenes_trabajo WHERE equipo_id = ? AND estatus = 'ABIERTA'",
                                     (e['id'],)).fetchone():
            cambiar_estatus(conn, e['id'], 'EN_SERVICIO', causa=None, motivo=f'Calibración {c["folio"]} apta',
                            usuario=usuario, firma_id=firma_id, folio=c['folio'])
    else:
        if not motivo:
            raise ErrorEquipos('Escribe por qué no es apto.')
        conn.execute("UPDATE eq_calibraciones SET estatus = 'NO_APTO', dictamen_motivo = ?, dictamino_firma_id = ? "
                     'WHERE id = ?', (motivo, firma_id, calibracion_id))
        if e['estatus'] == 'EN_SERVICIO':
            cambiar_estatus(conn, e['id'], 'FUERA_DE_SERVICIO', causa='CALIBRACION',
                            motivo=f'Calibración {c["folio"]} no apta: {motivo}', usuario=usuario,
                            firma_id=firma_id, folio=c['folio'])
    impacto_id = None
    if not c['conforme_encontrado']:
        impacto_id = impactos.abrir(conn, c['equipo_id'], origen='CALIBRACION', origen_id=calibracion_id,
                                    hasta=c['fecha'])
    return {'proxima': proxima, 'impacto_id': impacto_id}


def calibracion_vigente(conn, equipo_id, fecha):
    """La calibración apta que estaba vigente ese día: {id, folio, fecha, proxima, vigente}.
    vigente = False si la última apta ya había vencido ese día. None si no había ninguna."""
    fecha = fecha_iso(fecha)
    fila = conn.execute("SELECT id, folio, fecha, proxima FROM eq_calibraciones WHERE equipo_id = ? AND estatus = 'APTO' "
                        'AND fecha <= ? ORDER BY fecha DESC, id DESC LIMIT 1', (equipo_id, fecha)).fetchone()
    if not fila:
        return None
    d = dict(fila)
    d['vigente'] = not d['proxima'] or d['proxima'] >= fecha
    return d


def laboratorios(conn):
    return conn.execute("SELECT id, nombre, estatus FROM proveedores WHERE activo = 1 AND tipo IN ('Servicios','Ambos') "
                        'ORDER BY nombre').fetchall()


def crear_laboratorio(conn, *, nombre, usuario, contacto=None, telefono=None, correo=None):
    """Laboratorio o taller como proveedor de servicios (sin depender de Almacén). Nace 'En evaluación'."""
    nombre = texto(nombre)
    if not nombre:
        raise ErrorEquipos('Escribe el nombre del laboratorio o taller.')
    if conn.execute('SELECT 1 FROM proveedores WHERE lower(nombre) = lower(?)', (nombre,)).fetchone():
        raise ErrorEquipos(f'Ya existe un proveedor llamado {nombre}.')
    try:
        return inventario.crear_proveedor(conn, nombre=nombre, tipo='Servicios', usuario=usuario,
                                          contacto=texto(contacto), telefono=texto(telefono), correo=texto(correo))
    except inventario.ErrorInventario as e:
        raise ErrorEquipos(str(e))
