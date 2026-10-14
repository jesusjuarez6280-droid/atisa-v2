"""Verificación del día (básculas, balanzas y rondas de caldera) en la tablet (regla 6)."""
from nucleo.parametros import parametro_int

from . import catalogo, impactos, mantenimiento
from .comun import (ErrorEquipos, TIPO_PESA, apto, cumple, equipo, exigir_firma, fijar_vencimientos, hoy_iso,
                    numero, si, texto)


def de_hoy(conn, equipo_id, hoy=None):
    return conn.execute('SELECT * FROM eq_verificaciones WHERE equipo_id = ? AND fecha = ? ORDER BY id',
                        (equipo_id, hoy_iso(hoy))).fetchall()


def estado_hoy(conn, equipo_row, hoy=None):
    """'hecha', 'fallo' (falló y falta repetir), 'pendiente' o None si no pide verificación."""
    if not equipo_row['verificacion_diaria']:
        return None
    hoy = hoy_iso(hoy)
    if (equipo_row['ultima_verificacion'] or '')[:10] == hoy:
        return 'hecha'
    filas = de_hoy(conn, equipo_row['id'], hoy)
    return 'fallo' if filas and not filas[-1]['conforme'] else 'pendiente'


def pendientes_de_hoy(conn, hoy=None):
    """Equipos en servicio que piden verificación diaria y hoy no la tienen."""
    hoy = hoy_iso(hoy)
    filas = conn.execute("SELECT * FROM equipos WHERE verificacion_diaria = 1 AND estatus = 'EN_SERVICIO' "
                         "AND COALESCE(substr(ultima_verificacion, 1, 10), '') <> ? ORDER BY area, codigo",
                         (hoy,)).fetchall()
    return [dict(f, estado=estado_hoy(conn, f, hoy)) for f in filas]


def conteo_hoy(conn, hoy=None):
    """(hechas, que tocan) entre los equipos en servicio con verificación diaria."""
    hoy = hoy_iso(hoy)
    fila = conn.execute("SELECT COUNT(*) AS total, SUM(CASE WHEN substr(ultima_verificacion, 1, 10) = ? THEN 1 ELSE 0 END) "
                        "AS hechas FROM equipos WHERE verificacion_diaria = 1 AND estatus = 'EN_SERVICIO'",
                        (hoy,)).fetchone()
    return (fila['hechas'] or 0), (fila['total'] or 0)


def pesas_aptas(conn, hoy=None):
    filas = conn.execute("SELECT e.*, f.valor_nominal, f.unidad_nominal, f.clase FROM equipos e "
                         "LEFT JOIN eq_fichas f ON f.equipo_id = e.id WHERE e.tipo = ? AND e.estatus = 'EN_SERVICIO' "
                         'ORDER BY f.valor_nominal, e.codigo', (TIPO_PESA,)).fetchall()
    return [f for f in filas if apto(conn, f['id'], hoy)['apto']]


def verificar(conn, equipo_id, *, lecturas, firma_id, usuario, nivelada_limpia=True, observaciones=None,
              hoy=None) -> dict:
    """lecturas: {punto_id: {'lectura': x, 'pesa_id': y}}. Una conforme por día. Primer fallo: limpiar,
    nivelar y repetir. Al llegar a equipos.intentos_verificacion fallos seguidos: fuera de servicio,
    OT correctiva y evaluación de impacto.
    Devuelve {'id', 'conforme', 'intento', 'repetir', 'fuera', 'ot_id', 'impacto_id', 'lecturas'}."""
    e = equipo(conn, equipo_id)
    exigir_firma(conn, firma_id)
    hoy = hoy_iso(hoy)
    if not e['verificacion_diaria']:
        raise ErrorEquipos(f'{e["codigo"]} no lleva verificación del día.')
    if e['estatus'] == 'BAJA':
        raise ErrorEquipos(f'{e["codigo"]} está dado de baja.')
    if not si(nivelada_limpia):
        raise ErrorEquipos('Primero confirma que el equipo está nivelado y limpio.')
    previas = de_hoy(conn, equipo_id, hoy)
    if any(v['conforme'] for v in previas):
        raise ErrorEquipos(f'{e["codigo"]} ya tiene su verificación conforme de hoy.')
    puntos = catalogo.puntos(conn, equipo_id, 'VERIFICACION')
    if not puntos:
        raise ErrorEquipos(f'{e["codigo"]} no tiene puntos de verificación; pide a mantenimiento que los capture.')
    pesas_ok = {p['id'] for p in pesas_aptas(conn, hoy)}
    resultados = []
    for p in puntos:
        dato = (lecturas or {}).get(p['id']) or (lecturas or {}).get(str(p['id'])) or {}
        nombre = p['nombre'] or f'punto {p["orden"]}'
        lectura = numero(dato.get('lectura'), f'la lectura de {nombre}')
        pesa_id = None
        if p['requiere_pesa']:
            try:
                pesa_id = int(dato.get('pesa_id') or 0)
            except (TypeError, ValueError):
                pesa_id = 0
            if not pesa_id:
                raise ErrorEquipos(f'Elige la pesa patrón de {nombre}.')
            if pesa_id not in pesas_ok:
                raise ErrorEquipos(f'La pesa elegida para {nombre} no está apta (calibración vencida o fuera de servicio).')
        conforme, error = cumple(p, lectura)
        resultados.append({'punto_id': p['id'], 'nombre': nombre, 'pesa_id': pesa_id, 'lectura': lectura,
                           'error': error, 'conforme': conforme, 'unidad': p['unidad']})
    conforme = int(all(r['conforme'] for r in resultados))
    intento = len(previas) + 1
    cur = conn.execute('INSERT INTO eq_verificaciones (equipo_id, fecha, intento, nivelada_limpia, conforme, '
                       'observaciones, realizo_firma_id) VALUES (?, ?, ?, 1, ?, ?, ?)',
                       (equipo_id, hoy, intento, conforme, texto(observaciones), firma_id))
    ver_id = cur.lastrowid
    for r in resultados:
        conn.execute('INSERT INTO eq_verificacion_lecturas (verificacion_id, punto_id, pesa_id, lectura, error, conforme) '
                     'VALUES (?, ?, ?, ?, ?, ?)', (ver_id, r['punto_id'], r['pesa_id'], r['lectura'], r['error'],
                                                   r['conforme']))
    res = {'id': ver_id, 'conforme': conforme, 'intento': intento, 'repetir': False, 'fuera': False,
           'ot_id': None, 'impacto_id': None, 'lecturas': resultados}
    if conforme:
        fijar_vencimientos(conn, equipo_id, ultima_verificacion=hoy, usuario=usuario,
                           motivo='Verificación del día conforme')
        return res
    if e['estatus'] != 'EN_SERVICIO':
        return res                       # fuera de servicio: solo queda registrada (p. ej. tras una reparación)
    if intento < max(1, parametro_int('equipos.intentos_verificacion', conn)):
        res['repetir'] = True
        return res
    fallas = ', '.join(f'{r["nombre"]}: {r["lectura"]:g} {r["unidad"]}' for r in resultados if not r['conforme'])
    res['ot_id'] = mantenimiento.abrir_ot(conn, equipo_id, tipo='CORRECTIVO', usuario=usuario,
                                          descripcion=f'Verificación del día no conforme {intento} veces ({fallas})',
                                          reporto_firma_id=firma_id, se_puede_usar=0, fecha_programada=hoy)
    folio = conn.execute('SELECT folio FROM eq_ordenes_trabajo WHERE id = ?', (res['ot_id'],)).fetchone()['folio']
    catalogo.poner_fuera(conn, equipo_id, motivo=f'Verificación del día no conforme {intento} veces',
                         firma_id=firma_id, usuario=usuario, causa='VERIFICACION', folio=folio)
    res['fuera'] = True
    res['impacto_id'] = impactos.abrir(conn, equipo_id, origen='VERIFICACION', origen_id=ver_id, hasta=hoy)
    return res


def historial(conn, equipo_id, limite=50):
    vers = conn.execute('SELECT v.*, f.nombre AS firmante FROM eq_verificaciones v '
                        'LEFT JOIN firmas f ON f.id = v.realizo_firma_id WHERE v.equipo_id = ? '
                        'ORDER BY v.fecha DESC, v.id DESC LIMIT ?', (equipo_id, limite)).fetchall()
    salida = []
    for v in vers:
        lect = conn.execute('SELECT l.*, p.nombre, p.unidad, p.orden, pe.codigo AS pesa FROM eq_verificacion_lecturas l '
                            'JOIN eq_puntos p ON p.id = l.punto_id LEFT JOIN equipos pe ON pe.id = l.pesa_id '
                            'WHERE l.verificacion_id = ? ORDER BY p.orden', (v['id'],)).fetchall()
        salida.append({'v': v, 'lecturas': lect})
    return salida
