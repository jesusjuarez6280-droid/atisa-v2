"""Ganchos que Calidad ofrece a la base y a los otros módulos (diseño 03, sección 7)."""
from flask import url_for

from nucleo import firmas, inventario
from nucleo.parametros import parametro_int

from .servicios import analisis as an, especificaciones as esp, retencion as ret
from .servicios._comun import lote_fila, sigue_al_granel, ErrorCalidad


def _contar(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def _de_calidad(usuario):
    roles = set(usuario.get('roles') or [])
    return bool(roles & {'calidad', 'admin'})


def pendientes(conn, usuario):
    salida = []
    facultades = set(usuario.get('facultades') or [])
    if _de_calidad(usuario):
        muestrear = an.por_muestrear(conn)
        if muestrear:
            viejo = max((m['dias'] or 0) for m in muestrear)
            salida.append({'texto': f'Muestrear {len(muestrear)} lote{"s" if len(muestrear) != 1 else ""} '
                                    f'(el más antiguo hace {viejo} día{"s" if viejo != 1 else ""})',
                           'url': '/calidad?tab=muestrear', 'nivel': 'amarillo' if viejo > 3 else 'azul',
                           'fecha': None})
        n = _contar(conn, "SELECT COUNT(*) FROM cc_analisis WHERE estatus = 'POR_REVISAR'")
        if n:
            salida.append({'texto': f'Revisar {n} análisis', 'url': '/calidad?tab=revisar', 'nivel': 'azul',
                           'fecha': None})
        n = _contar(conn, "SELECT COUNT(*) FROM cc_investigaciones WHERE estatus = 'ABIERTA'")
        if n:
            salida.append({'texto': f'Cerrar {n} investigaci{"ones" if n != 1 else "ón"} fuera de especificación',
                           'url': '/calidad?tab=analisis', 'nivel': 'rojo', 'fecha': None})
        n = len(an.reanalisis_proximos(conn))
        if n:
            salida.append({'texto': f'{n} reanálisis por vencer', 'url': '/calidad?tab=reanalisis',
                           'nivel': 'amarillo', 'fecha': None})
        n = len(ret.lotes_sin_retencion(conn))
        if n:
            salida.append({'texto': f'Guardar muestra de retención de {n} lote{"s" if n != 1 else ""}',
                           'url': '/calidad/retencion', 'nivel': 'azul', 'fecha': None})
        n = _contar(conn, "SELECT COUNT(*) FROM cc_muestras_retencion WHERE estatus <> 'DESTRUIDA' "
                          "AND destruir_desde <= date('now','localtime')")
        if n:
            salida.append({'texto': f'Destruir {n} muestra{"s" if n != 1 else ""} de retención',
                           'url': '/calidad/retencion?filtro=por_destruir', 'nivel': 'azul', 'fecha': None})
    if 'liberar_lotes' in facultades:
        n = len(an.lotes_por_dictaminar(conn))
        n += sum(1 for r in conn.execute("SELECT id FROM lotes WHERE estatus = 'RETENIDO'").fetchall()
                 if _retenido_listo(conn, r['id']))
        if n:
            salida.append({'texto': f'Dictaminar {n} lote{"s" if n != 1 else ""}', 'url': '/calidad?tab=dictaminar',
                           'nivel': 'amarillo', 'fecha': None})
    if 'aprobar_maestros' in facultades:
        for e in conn.execute("SELECT e.id, e.version, m.codigo FROM cc_especificaciones e JOIN materiales m "
                              "ON m.id = e.material_id WHERE e.estatus = 'EN_REVISION' AND e.reviso_firma_id IS NOT NULL "
                              'ORDER BY e.id LIMIT 10').fetchall():
            salida.append({'texto': f'Aprobar especificación {e["codigo"]} v{e["version"]}',
                           'url': f'/calidad/especificaciones/{e["id"]}', 'nivel': 'azul', 'fecha': None})
        n = sum(1 for m in esp.sin_especificacion(conn) if m['lotes_esperando'])
        if n:
            salida.append({'texto': f'{n} material{"es" if n != 1 else ""} con lotes esperando y sin especificación',
                           'url': '/calidad/especificaciones?tab=sin', 'nivel': 'amarillo', 'fecha': None})
    return salida


def _retenido_listo(conn, lote_id):
    """Retenido que ya no tiene motivos pendientes en su lista de verificación."""
    from .servicios import dictamen
    try:
        return dictamen.verificaciones(conn, lote_id)['completa']
    except ErrorCalidad:
        return False


def alertas(conn):
    salida = []
    for i in conn.execute(
            "SELECT i.id, i.folio, l.numero_lote, m.nombre FROM cc_investigaciones i "
            "JOIN cc_analisis a ON a.id = i.analisis_id JOIN lotes l ON l.id = a.lote_id "
            "JOIN materiales m ON m.id = l.material_id WHERE l.estatus = 'APROBADO' "
            "AND (i.estatus = 'ABIERTA' OR i.conclusion = 'CONFIRMADO')").fetchall():
        salida.append({'tipo': 'Lote aprobado fuera de especificación', 'icono': 'ri-error-warning-line',
                       'nivel': 'rojo', 'nombre': f'Lote {i["numero_lote"]}', 'area': 'Calidad',
                       'detalle': f'{i["folio"]} · {i["nombre"]}: retén el lote', 'url': f'/calidad/oos/{i["id"]}'})
    dias = parametro_int('calidad.dias_alerta_oos', conn)
    for i in conn.execute("SELECT id, folio, CAST(julianday('now','localtime') - julianday(abierta_en) AS INTEGER) d "
                          "FROM cc_investigaciones WHERE estatus = 'ABIERTA' "
                          "AND abierta_en < datetime('now','localtime', ?)", (f'-{dias} days',)).fetchall():
        salida.append({'tipo': 'Investigación OOS atrasada', 'icono': 'ri-time-line', 'nivel': 'rojo',
                       'nombre': i['folio'], 'area': 'Calidad', 'detalle': f'Abierta hace {i["d"]} días',
                       'url': f'/calidad/oos/{i["id"]}'})
    n = sum(1 for m in an.por_muestrear(conn) if not m['especificacion_id'])
    if n:
        salida.append({'tipo': 'Lotes sin especificación', 'icono': 'ri-file-warning-line', 'nivel': 'amarillo',
                       'nombre': f'{n} lote{"s" if n != 1 else ""} en cuarentena', 'area': 'Calidad',
                       'detalle': 'No hay especificación vigente para analizarlos',
                       'url': '/calidad/especificaciones?tab=sin'})
    dias = parametro_int('calidad.dias_alerta_dictamen', conn)
    n = _contar(conn, "SELECT COUNT(DISTINCT a.lote_id) FROM cc_analisis a JOIN lotes l ON l.id = a.lote_id "
                      "WHERE l.estatus = 'CUARENTENA' AND a.tipo = 'LIBERACION' AND a.estatus = 'CONFORME' "
                      "AND a.terminado_en < datetime('now','localtime', ?)", (f'-{dias} days',))
    if n:
        salida.append({'tipo': 'Lotes esperando dictamen', 'icono': 'ri-hourglass-line', 'nivel': 'amarillo',
                       'nombre': f'{n} lote{"s" if n != 1 else ""} listos', 'area': 'Calidad',
                       'detalle': f'Con análisis conforme hace más de {dias} días', 'url': '/calidad?tab=dictaminar'})
    return salida


def kpis(conn):
    por_dictaminar = len(an.lotes_por_dictaminar(conn))
    prom = conn.execute(
        "SELECT AVG(julianday(d.ts) - julianday(l.fecha_entrada)) FROM cc_dictamenes d JOIN lotes l ON l.id = d.lote_id "
        "WHERE d.decision = 'APROBADO' AND strftime('%Y-%m', d.ts) = strftime('%Y-%m', 'now', 'localtime')").fetchone()[0]
    rechazados = _contar(conn, "SELECT COUNT(DISTINCT lote_id) FROM cc_dictamenes WHERE decision = 'RECHAZADO' "
                               "AND strftime('%Y-%m', ts) = strftime('%Y-%m', 'now', 'localtime')")
    inv = _contar(conn, "SELECT COUNT(*) FROM cc_investigaciones WHERE estatus = 'ABIERTA'")
    return [
        {'titulo': 'Lotes por dictaminar', 'valor': por_dictaminar, 'detalle': 'Con análisis terminado',
         'nivel': 'amarillo' if por_dictaminar else 'verde', 'url': '/calidad?tab=dictaminar'},
        {'titulo': 'Días promedio de liberación', 'valor': f'{prom:.1f}' if prom is not None else '—',
         'detalle': 'Lotes aprobados este mes', 'nivel': None, 'url': '/calidad'},
        {'titulo': 'Lotes rechazados', 'valor': rechazados, 'detalle': 'Este mes',
         'nivel': 'rojo' if rechazados else 'verde', 'url': '/calidad?tab=terminados'},
        {'titulo': 'Investigaciones abiertas', 'valor': inv, 'detalle': 'Resultados fuera de especificación',
         'nivel': 'rojo' if inv else 'verde', 'url': '/calidad?tab=analisis'},
    ]


def seccion_lote(conn, lote_id):
    """Sección "Calidad" en la ficha del lote de Almacén."""
    try:
        lote = lote_fila(conn, lote_id)
    except ErrorCalidad:
        return None
    from .servicios import dictamen
    granel = None
    base_id = lote['id']
    if sigue_al_granel(lote):
        base_id = lote['lote_padre_id']
        granel = conn.execute('SELECT id, numero_lote FROM lotes WHERE id = ?', (base_id,)).fetchone()
    analisis = conn.execute('SELECT id, folio, tipo, estatus, creado_en FROM cc_analisis WHERE lote_id = ? '
                            'ORDER BY id DESC', (base_id,)).fetchall()
    muestras = conn.execute('SELECT id, envases_actuales, ubicacion, estatus, destruir_desde FROM cc_muestras_retencion '
                            'WHERE lote_id = ? ORDER BY id', (lote_id,)).fetchall()
    return {'titulo': 'Calidad', 'plantilla': 'calidad/_seccion_lote.html',
            'contexto': {'cc_lote': dict(lote), 'cc_granel': dict(granel) if granel else None,
                         'cc_analisis': [dict(a) for a in analisis], 'cc_dictamenes': dictamen.historial(conn, lote_id),
                         'cc_muestras': [dict(m) for m in muestras]}}


def url_analisis(analisis_id):
    return url_for('calidad.analisis_ver', analisis_id=analisis_id)


def url_dictamen(lote_id):
    return url_for('calidad.dictamen', lote_id=lote_id)


def url_certificado(lote_id):
    return url_for('calidad.certificado', lote_id=lote_id)


def url_investigacion(investigacion_id):
    return url_for('calidad.oos', investigacion_id=investigacion_id)


def analisis_por_equipo(conn, equipo_id, desde):
    return an.analisis_por_equipo(conn, equipo_id, desde)


def datos_humo(conn):
    """Material con especificación vigente, un lote en cuarentena con su análisis y un resultado que abre una
    investigación, y una muestra de retención. Solo para las pruebas de humo."""
    u = conn.execute("SELECT numero_empleado FROM usuarios WHERE estatus = 1 AND rol IN ('calidad','admin') "
                     "ORDER BY rol = 'calidad' DESC, id LIMIT 1").fetchone()
    numero = u['numero_empleado']
    firmas.fijar_pin(conn, numero, '2580', 'humo')
    sesion = {'username': 'humo', 'numero_empleado': numero, 'ip': None}

    def f(significado, entidad, entidad_id):
        return firmas.firmar(conn, numero_empleado=numero, pin='2580', significado=significado, modulo='calidad',
                             entidad=entidad, entidad_id=entidad_id, detalle='humo', sesion=sesion)

    mat = inventario.crear_material(conn, codigo='CC-HUMO-MP', nombre='Materia prima humo calidad', tipo='MP',
                                    unidad='kg', usuario='humo')
    eid = esp.crear(conn, material_id=mat, usuario='humo',
                    datos={'codigo_documento': 'ESP-HUMO', 'pide_retencion': 1, 'cantidad_retencion': 0.1,
                           'meses_reanalisis': 12, 'cantidad_muestra': 0.2},
                    pruebas_lista=[{'prueba': 'Valoración', 'tipo': 'NUMERICA', 'limite_min': 98, 'limite_max': 102,
                                    'unidad': '%', 'decimales': 1, 'metodo': 'MA-HUMO'},
                                   {'prueba': 'Aspecto', 'tipo': 'CUALITATIVA', 'texto_especificacion': 'Polvo blanco'}])
    conn.execute("UPDATE cc_especificaciones SET estatus = 'VIGENTE', reviso_firma_id = ?, aprobo_firma_id = ?, "
                 "vigente_desde = date('now','localtime') WHERE id = ?",
                 (f('Revisó', 'cc_especificaciones', eid), f('Aprobó', 'cc_especificaciones', eid), eid))
    lote = inventario.crear_lote(conn, material_id=mat, cantidad=50, origen='RECEPCION', usuario='humo',
                                 modulo='calidad', lote_proveedor='LP-HUMO', fecha_caducidad='2030-12-31')
    aid = an.muestrear(conn, lote_id=lote, usuario='humo', firma={'numero_empleado': numero, 'pin': '2580',
                                                                  'sesion': sesion}, cantidad=0.2)
    prueba = esp.pruebas(conn, eid)[0]
    res = an.capturar(conn, analisis_id=aid, prueba_id=prueba['id'], usuario='humo', valor='97.0', fuente='Interno',
                      firma={'numero_empleado': numero, 'pin': '2580', 'sesion': sesion})
    mid = ret.guardar(conn, lote_id=lote, envases=2, cantidad=0.1, ubicacion='Anaquel humo', usuario='humo',
                      firma={'numero_empleado': numero, 'pin': '2580', 'sesion': sesion})
    return {'lote_id': lote, 'analisis_id': aid, 'especificacion_id': eid,
            'investigacion_id': res['investigacion_id'], 'muestra_id': mid, 'prueba_id': prueba['id'],
            'resultado_id': res['resultado_id']}
