"""Ganchos del módulo Equipos (sección 7 del diseño 05)."""
from datetime import date, timedelta

from flask import url_for

from nucleo.auth import PERMISOS
from nucleo.parametros import parametro_int

from .servicios import calibraciones, catalogo, impactos, mantenimiento, verificaciones
from .servicios.comun import FACULTAD

URL = '/equipos'


def _hoy():
    return date.today().isoformat()


def _en(dias):
    return (date.today() + timedelta(days=dias)).isoformat()


def _plural(n, uno, varios):
    return f'{n} {uno if n == 1 else varios}'


def _cuenta(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0] or 0


def _cal_vencidas(conn):
    return _cuenta(conn, "SELECT COUNT(*) FROM equipos WHERE estatus <> 'BAJA' AND vence_calibracion < ?", _hoy())


def _cal_por_vencer(conn):
    return _cuenta(conn, "SELECT COUNT(*) FROM equipos WHERE estatus <> 'BAJA' AND vence_calibracion BETWEEN ? AND ?",
                   _hoy(), _en(parametro_int('equipos.dias_aviso_calibracion', conn)))


def _man_vencidos(conn):
    return _cuenta(conn, "SELECT COUNT(*) FROM equipos WHERE estatus <> 'BAJA' AND vence_mantenimiento < ?", _hoy())


def _fuera(conn):
    return _cuenta(conn, "SELECT COUNT(*) FROM equipos WHERE estatus = 'FUERA_DE_SERVICIO'")


def _sin_programa(conn):
    return _cuenta(conn, "SELECT COUNT(*) FROM equipos e WHERE e.es_instrumento = 1 AND e.estatus <> 'BAJA' "
                         "AND NOT EXISTS (SELECT 1 FROM eq_programas p WHERE p.equipo_id = e.id "
                         "AND p.actividad = 'CALIBRACION' AND p.activo = 1)")


def alertas(conn):
    res = []

    def alerta(nivel, tipo, detalle, url, icono='ri-tools-line', nombre='Equipos', area='Mantenimiento'):
        res.append({'tipo': tipo, 'icono': icono, 'nivel': nivel, 'nombre': nombre, 'area': area,
                    'detalle': detalle, 'url': url})

    n = _cal_vencidas(conn)
    if n:
        alerta('rojo', 'Calibración vencida', f'{_plural(n, "equipo", "equipos")} con calibración vencida',
               f'{URL}?filtro=cal_vencida', 'ri-scales-3-line')
    for i in impactos.pendientes(conn):
        alerta('rojo', 'Equipo fuera de tolerancia',
               f'{i["codigo"]} se encontró fuera de tolerancia y falta evaluar el impacto',
               f'{URL}/impacto/{i["id"]}', 'ri-error-warning-line', nombre=i['codigo'], area='Calidad')
    n = _cal_por_vencer(conn)
    if n:
        alerta('amarillo', 'Calibración por vencer', f'{_plural(n, "calibración vence", "calibraciones vencen")} '
               f'en {parametro_int("equipos.dias_aviso_calibracion", conn)} días', f'{URL}?filtro=cal_por_vencer',
               'ri-scales-3-line')
    n = _man_vencidos(conn)
    if n:
        alerta('amarillo', 'Mantenimiento vencido', f'{_plural(n, "mantenimiento vencido", "mantenimientos vencidos")}',
               f'{URL}?filtro=man_vencido')
    n = _fuera(conn)
    if n:
        alerta('amarillo', 'Fuera de servicio', f'{_plural(n, "equipo fuera", "equipos fuera")} de servicio',
               f'{URL}?filtro=fuera', 'ri-forbid-line')
    n = _sin_programa(conn)
    if n:
        alerta('amarillo', 'Sin programa de calibración',
               f'{_plural(n, "instrumento", "instrumentos")} sin programa de calibración', f'{URL}?filtro=sin_programa',
               'ri-calendar-close-line')
    return res


def kpis(conn):
    hoy = _hoy()
    total = _cuenta(conn, "SELECT COUNT(*) FROM equipos e JOIN eq_programas p ON p.equipo_id = e.id "
                          "AND p.actividad = 'CALIBRACION' AND p.activo = 1 WHERE e.estatus <> 'BAJA'")
    vigentes = _cuenta(conn, "SELECT COUNT(*) FROM equipos e JOIN eq_programas p ON p.equipo_id = e.id "
                             "AND p.actividad = 'CALIBRACION' AND p.activo = 1 WHERE e.estatus <> 'BAJA' "
                             'AND e.vence_calibracion >= ?', hoy)
    pct_cal = round(vigentes * 100 / total) if total else None
    mes = hoy[:7]
    hechas = conn.execute("SELECT fin, fecha_programada FROM eq_ordenes_trabajo WHERE tipo = 'PREVENTIVO' "
                          "AND estatus = 'TERMINADA' AND substr(fin, 1, 7) = ?", (mes,)).fetchall()
    a_tiempo = sum(1 for h in hechas if not h['fecha_programada'] or h['fin'] <= h['fecha_programada'])
    pct_man = round(a_tiempo * 100 / len(hechas)) if hechas else None
    fuera = _fuera(conn)
    ver_hechas, ver_tocan = verificaciones.conteo_hoy(conn)

    def nivel(pct):
        if pct is None:
            return 'verde'
        return 'verde' if pct >= 95 else 'amarillo' if pct >= 80 else 'rojo'

    return [
        {'titulo': 'Instrumentos con calibración vigente', 'valor': f'{pct_cal} %' if pct_cal is not None else '—',
         'detalle': f'{vigentes} de {total}', 'nivel': nivel(pct_cal), 'url': f'{URL}?filtro=cal_vencida'},
        {'titulo': 'Mantenimientos preventivos a tiempo', 'valor': f'{pct_man} %' if pct_man is not None else '—',
         'detalle': f'{a_tiempo} de {len(hechas)} este mes', 'nivel': nivel(pct_man), 'url': f'{URL}/mantenimiento'},
        {'titulo': 'Equipos fuera de servicio', 'valor': fuera, 'detalle': 'bloqueados para usarse',
         'nivel': 'amarillo' if fuera else 'verde', 'url': f'{URL}?filtro=fuera'},
        {'titulo': 'Verificaciones de hoy', 'valor': f'{ver_hechas}/{ver_tocan}', 'detalle': 'hechas de las que tocan',
         'nivel': 'verde' if ver_hechas >= ver_tocan else 'amarillo', 'url': f'{URL}?filtro=verif_pendiente'},
    ]


def pendientes(conn, usuario):
    roles, facultades = set(usuario.get('roles') or []), set(usuario.get('facultades') or [])
    res = []
    if roles & {'mantenimiento', 'admin'}:
        for o in conn.execute("SELECT o.id, o.folio, o.fecha_programada, e.codigo FROM eq_ordenes_trabajo o "
                              "JOIN equipos e ON e.id = o.equipo_id WHERE o.estatus = 'ABIERTA' AND o.tipo = 'PREVENTIVO' "
                              'ORDER BY o.fecha_programada LIMIT 10').fetchall():
            res.append({'texto': f'{o["folio"]} ({o["codigo"]}) programada para el {o["fecha_programada"] or "—"}',
                        'url': f'{URL}/ot/{o["id"]}',
                        'nivel': 'rojo' if (o['fecha_programada'] or '9') < _hoy() else 'azul',
                        'fecha': o['fecha_programada']})
        n = len(mantenimiento.por_hacer(conn))
        if n:
            res.append({'texto': f'{_plural(n, "mantenimiento", "mantenimientos")} por vencer sin orden',
                        'url': f'{URL}/mantenimiento', 'nivel': 'amarillo', 'fecha': None})
        n = _cuenta(conn, "SELECT COUNT(DISTINCT o.equipo_id) FROM eq_ordenes_trabajo o JOIN equipos e ON e.id = o.equipo_id "
                          "WHERE o.estatus = 'ABIERTA' AND o.tipo = 'CORRECTIVO' AND e.estatus = 'FUERA_DE_SERVICIO'")
        if n:
            res.append({'texto': f'Reparar {_plural(n, "equipo", "equipos")} fuera de servicio',
                        'url': f'{URL}/mantenimiento?tab=abiertas', 'nivel': 'rojo', 'fecha': None})
        n = _cal_por_vencer(conn) + _cal_vencidas(conn)
        if n:
            res.append({'texto': f'Calibrar {_plural(n, "equipo", "equipos")} vencidos o que vencen en '
                                 f'{parametro_int("equipos.dias_aviso_calibracion", conn)} días',
                        'url': f'{URL}/calibraciones', 'nivel': 'amarillo', 'fecha': None})
    if FACULTAD in facultades:
        n = _cuenta(conn, "SELECT COUNT(*) FROM eq_calibraciones WHERE estatus = 'POR_DICTAMINAR'")
        if n:
            res.append({'texto': f'Dictaminar {_plural(n, "calibración", "calibraciones")}',
                        'url': f'{URL}/calibraciones', 'nivel': 'amarillo', 'fecha': None})
        for i in impactos.pendientes(conn):
            res.append({'texto': f'Evaluar el impacto de {i["codigo"]} fuera de tolerancia',
                        'url': f'{URL}/impacto/{i["id"]}', 'nivel': 'rojo', 'fecha': i['hasta']})
        listos = [e for e in conn.execute("SELECT id FROM equipos WHERE estatus = 'FUERA_DE_SERVICIO'").fetchall()
                  if not catalogo.motivos_para_no_regresar(conn, e['id'])]
        if listos:
            res.append({'texto': f'Regresar a servicio {_plural(len(listos), "equipo reparado", "equipos reparados")}',
                        'url': f'{URL}?filtro=fuera', 'nivel': 'amarillo', 'fecha': None})
    if roles & {'produccion', 'admin'}:
        n = len(verificaciones.pendientes_de_hoy(conn))
        if n:
            res.append({'texto': f'{_plural(n, "báscula o equipo", "básculas o equipos")} sin verificar hoy',
                        'url': f'{URL}?filtro=verif_pendiente', 'nivel': 'amarillo', 'fecha': None})
    return res


def accesos_planta(conn, usuario):
    if not set(usuario.get('roles') or []) & set(PERMISOS.get('equipos_planta', [])):
        return []
    return [
        {'titulo': 'Verificaciones del día', 'icono': 'ri-scales-3-line', 'url': f'{URL}/planta/verificar',
         'contador': len(verificaciones.pendientes_de_hoy(conn))},
        {'titulo': 'Reportar falla de equipo', 'icono': 'ri-alarm-warning-line', 'url': f'{URL}/planta/falla',
         'contador': None},
    ]


def verificaciones_liberacion(conn, lote_id):
    return impactos.bloqueos_de_lote(conn, lote_id)


# --- Enlaces y consultas para otros módulos ------------------------------------

def url_equipo(equipo_id):
    return url_for('equipos.ficha', equipo_id=equipo_id)


def url_verificar(equipo_id):
    return url_for('equipos.planta_verificar', equipo_id=equipo_id)


def url_reportar_falla(equipo_id=None):
    return url_for('equipos.planta_falla', equipo_id=equipo_id) if equipo_id else url_for('equipos.planta_falla')


def calibracion_vigente(conn, equipo_id, fecha):
    return calibraciones.calibracion_vigente(conn, equipo_id, fecha)


# --- Pruebas de humo -----------------------------------------------------------

def datos_humo(conn):
    """Una báscula con programa, puntos, calibración dictaminada, verificación y orden de trabajo, una pesa
    patrón y una balanza con su evaluación de impacto (ya firmada, para no bloquear lotes de otras pruebas)."""
    from werkzeug.security import generate_password_hash
    numero = 'EQ-HUMO'
    if not conn.execute('SELECT 1 FROM usuarios WHERE numero_empleado = ?', (numero,)).fetchone():
        conn.execute("INSERT INTO usuarios (numero_empleado, nombre_completo, username, password_hash, rol, estatus, "
                     "facultades) VALUES (?, 'Metrología Humo', 'eq_humo', ?, 'calidad', 0, ?)",
                     (numero, generate_password_hash('no-entra-eq-humo'), FACULTAD))

    def firma(significado):
        return conn.execute("INSERT INTO firmas (numero_empleado, nombre, significado, modulo, entidad, entidad_id) "
                            "VALUES (?, 'Metrología Humo', ?, 'equipos', 'humo', 0)", (numero, significado)).lastrowid

    u = 'humo'
    lab = calibraciones.crear_laboratorio(conn, nombre='Laboratorio Humo de Metrología', usuario=u)
    pesa = catalogo.alta(conn, datos={'codigo': 'PES-HUMO', 'nombre': 'Pesa patrón 20 kg', 'tipo': 'Pesa patrón',
                                      'area': 'Producción'},
                         ficha={'valor_nominal': 20, 'unidad_nominal': 'kg', 'clase': 'M1'}, usuario=u)
    catalogo.guardar_programa(conn, pesa, 'CALIBRACION', frecuencia_meses=12, proveedor_id=lab, usuario=u)
    catalogo.guardar_puntos(conn, pesa, 'CALIBRACION', [{'nominal': 20, 'tolerancia': 0.001, 'unidad': 'kg'}], usuario=u)
    c = calibraciones.registrar(conn, pesa, fecha=_hoy(), modalidad='Externa', proveedor_id=lab, certificado='HUMO-1',
                                con_certificado_pdf=True, firma_id=firma('Realizó'), usuario=u,
                                puntos=[{'nominal': 20, 'tolerancia': 0.001, 'unidad': 'kg', 'encontrado': 20.0002}])
    calibraciones.dictaminar(conn, c['id'], es_apto=True, firma_id=firma('Aprobó'), usuario=u)

    bascula = catalogo.alta(conn, datos={'codigo': 'BAS-HUMO', 'nombre': 'Báscula de piso humo', 'tipo': 'Báscula',
                                         'area': 'Producción', 'es_instrumento': 1, 'verificacion_diaria': 1,
                                         'capacidad': '150 kg', 'resolucion': '0.01 kg'},
                            ficha={'criticidad': 'Alta', 'rango_uso': '1 a 100 kg', 'tolerancia_proceso': '± 0.05 kg'},
                            usuario=u)
    catalogo.guardar_programa(conn, bascula, 'CALIBRACION', frecuencia_meses=12, modalidad='Interna', usuario=u)
    catalogo.guardar_programa(conn, bascula, 'MANTENIMIENTO', frecuencia_meses=6,
                              tareas='Limpiar plataforma\nRevisar cable y celda de carga', usuario=u)
    catalogo.guardar_puntos(conn, bascula, 'CALIBRACION', [{'nominal': 20, 'tolerancia': 0.05, 'unidad': 'kg'}], usuario=u)
    catalogo.guardar_puntos(conn, bascula, 'VERIFICACION',
                            [{'nombre': 'Carga alta', 'nominal': 20, 'tolerancia': 0.05, 'unidad': 'kg',
                              'requiere_pesa': 1}], usuario=u)
    c = calibraciones.registrar(conn, bascula, fecha=_hoy(), modalidad='Interna', patrones=[pesa],
                                firma_id=firma('Realizó'), usuario=u,
                                puntos=[{'nominal': 20, 'tolerancia': 0.05, 'unidad': 'kg', 'encontrado': 20.01}])
    calibraciones.dictaminar(conn, c['id'], es_apto=True, firma_id=firma('Aprobó'), usuario=u)
    punto = catalogo.puntos(conn, bascula, 'VERIFICACION')[0]['id']
    v = verificaciones.verificar(conn, bascula, lecturas={punto: {'lectura': 20.02, 'pesa_id': pesa}},
                                 firma_id=firma('Realizó'), usuario=u)
    ot_id = mantenimiento.abrir_ot(conn, bascula, tipo='PREVENTIVO', usuario=u)

    balanza = catalogo.alta(conn, datos={'codigo': 'BAL-HUMO', 'nombre': 'Balanza analítica humo',
                                         'tipo': 'Balanza analítica', 'area': 'Laboratorio', 'es_instrumento': 1},
                            usuario=u)
    catalogo.guardar_programa(conn, balanza, 'CALIBRACION', frecuencia_meses=12, proveedor_id=lab, usuario=u)
    c = calibraciones.registrar(conn, balanza, fecha=_hoy(), modalidad='Externa', proveedor_id=lab,
                                certificado='HUMO-2', con_certificado_pdf=True, ajustado=True, firma_id=firma('Realizó'),
                                usuario=u, puntos=[{'nominal': 100, 'tolerancia': 0.001, 'unidad': 'g',
                                                    'encontrado': 100.004, 'dejado': 100.0002}])
    impacto_id = calibraciones.dictaminar(conn, c['id'], es_apto=True, firma_id=firma('Aprobó'), usuario=u)['impacto_id']
    impactos.evaluar(conn, impacto_id, conclusion='Prueba de humo: sin lotes en la ventana.', firma_id=firma('Aprobó'),
                     usuario=u)
    return {'equipo_id': bascula, 'calibracion_id': c['id'], 'verificacion_id': v['id'], 'ot_id': ot_id,
            'impacto_id': impacto_id}
