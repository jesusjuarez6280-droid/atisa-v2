"""Ganchos que el módulo ofrece a la base y a los otros módulos (diseño 04, sección 7)."""
from flask import url_for

from nucleo import inventario
from nucleo.auth import PERMISOS
from nucleo.parametros import parametro_int

from . import servicios as sv


# --- enlaces -----------------------------------------------------------------

def url_reportar_desviacion(origen, origen_id, texto=None, folio=None, lotes=None, equipo_id=None,
                            material_id=None, volver=None, planta=False):
    """URL del formulario ya lleno. `origen` es uno de los nombres de origen (sección 7);
    `lotes` es una lista de ids; con planta=True, la versión de tablet."""
    datos = {'origen': origen, 'origen_id': origen_id, 'texto': texto, 'folio': folio,
             'lotes': ','.join(str(x) for x in lotes) if lotes else None, 'equipo_id': equipo_id,
             'material_id': material_id, 'volver': volver}
    datos = {k: v for k, v in datos.items() if v not in (None, '')}
    return url_for('desviaciones.planta_nueva' if planta else 'desviaciones.nueva', **datos)


def url_desviacion(desviacion_id):
    return url_for('desviaciones.detalle', desviacion_id=desviacion_id)


# --- consultas ---------------------------------------------------------------

def desviaciones_de(conn, origen, origen_id):
    return sv.desviaciones_de(conn, origen, origen_id)


# --- liberación y ficha del lote --------------------------------------------

def verificaciones_liberacion(conn, lote_id):
    return sv.bloqueos_de_lote(conn, lote_id)


def seccion_lote(conn, lote_id):
    return {'titulo': 'Desviaciones', 'plantilla': 'desviaciones/seccion_lote.html',
            'contexto': {'lote_id': lote_id, 'desviaciones': sv.desviaciones_de_lote(conn, lote_id),
                         'nombre_estatus': sv.NOMBRE_ESTATUS, 'nombre_impacto': sv.NOMBRE_IMPACTO}}


# --- planta, pendientes, alertas y kpis -------------------------------------

def accesos_planta(conn, usuario):
    if not set(usuario.get('roles') or []) & set(PERMISOS.get('desviaciones_planta', [])):
        return []
    return [{'titulo': 'Reportar desviación', 'icono': 'ri-error-warning-line',
             'url': url_for('desviaciones.planta_nueva', volver='/planta'), 'contador': None}]


def pendientes(conn, usuario):
    numero = usuario.get('numero_empleado')
    roles = set(usuario.get('roles') or [])
    if not roles & set(PERMISOS.get('desviaciones', [])):
        return []
    lista = []
    hoy = sv.hoy()
    if sv.FACULTAD in (usuario.get('facultades') or []):
        n = conn.execute("SELECT COUNT(*) FROM dev_desviaciones WHERE estatus = 'REPORTADA'").fetchone()[0]
        if n:
            lista.append({'texto': f'Clasificar {n} desviaci{"ón" if n == 1 else "ones"}',
                          'url': '/desviaciones?pestana=clasificar', 'nivel': 'amarillo', 'fecha': None})
        for r in conn.execute("SELECT d.id, d.folio, f.numero_empleado FROM dev_desviaciones d "
                              "LEFT JOIN firmas f ON f.id = d.investigo_firma_id "
                              "WHERE d.estatus = 'POR_APROBAR' ORDER BY d.id").fetchall():
            if r['numero_empleado'] != numero:
                lista.append({'texto': f"Aprobar la investigación de {r['folio']}", 'url': f"/desviaciones/{r['id']}",
                              'nivel': 'amarillo', 'fecha': None})
        listas = [r for r in conn.execute("SELECT id FROM dev_desviaciones WHERE estatus = 'EN_ACCIONES'").fetchall()
                  if not sv.faltantes_cierre(conn, r['id'])]
        if listas:
            n = len(listas)
            lista.append({'texto': f'Cerrar {n} desviaci{"ón lista" if n == 1 else "ones listas"}',
                          'url': '/desviaciones?pestana=acciones', 'nivel': 'azul', 'fecha': None})
    if 'calidad' in roles:
        n = len(sv.eficacias_por_revisar(conn, hoy))
        if n:
            lista.append({'texto': f'Revisar la eficacia de {n} acci{"ón" if n == 1 else "ones"}',
                          'url': '/desviaciones/acciones', 'nivel': 'amarillo', 'fecha': None})
        n = len(sv.acciones_por_verificar(conn, excepto=numero))
        if n:
            lista.append({'texto': f'Verificar {n} acci{"ón hecha" if n == 1 else "ones hechas"}',
                          'url': '/desviaciones/acciones', 'nivel': 'amarillo', 'fecha': None})
    for r in conn.execute("SELECT id, folio, vence_investigacion FROM dev_desviaciones "
                          "WHERE estatus = 'EN_INVESTIGACION' AND responsable = ? ORDER BY vence_investigacion",
                          (numero,)).fetchall():
        lista.append({'texto': f"Investigar {r['folio']} (vence el {r['vence_investigacion']})",
                      'url': f"/desviaciones/{r['id']}",
                      'nivel': 'rojo' if (r['vence_investigacion'] or '9') < hoy else 'amarillo',
                      'fecha': r['vence_investigacion']})
    for a in sv.mis_acciones(conn, numero):
        lista.append({'texto': f"Acción {a['numero']} de {a['folio']}: {a['descripcion']} (vence el {a['fecha_compromiso']})",
                      'url': '/desviaciones/acciones',
                      'nivel': 'rojo' if a['fecha_compromiso'] < hoy else 'amarillo', 'fecha': a['fecha_compromiso']})
    return lista


def alertas(conn):
    hoy = sv.hoy()
    lista = []
    for r in conn.execute(f"SELECT id, folio, titulo, area FROM dev_desviaciones WHERE clasificacion = 'CRITICA' "
                          f"AND estatus IN {sv.ABIERTAS} ORDER BY id").fetchall():
        lista.append({'tipo': 'Desviación crítica', 'icono': 'ri-error-warning-line', 'nivel': 'rojo',
                      'nombre': f"{r['folio']} crítica abierta", 'area': r['area'], 'detalle': r['titulo'],
                      'url': f"/desviaciones/{r['id']}"})
    n = sv.acciones_vencidas(conn, hoy)
    if n:
        lista.append({'tipo': 'CAPA', 'icono': 'ri-calendar-close-line', 'nivel': 'amarillo',
                      'nombre': f'{n} acci{"ón vencida" if n == 1 else "ones vencidas"}', 'area': 'Calidad',
                      'detalle': 'Acciones correctivas o preventivas después de su fecha compromiso',
                      'url': '/desviaciones?pestana=acciones'})
    n = sv.investigaciones_vencidas(conn, hoy)
    if n:
        lista.append({'tipo': 'Desviaciones', 'icono': 'ri-search-eye-line', 'nivel': 'amarillo',
                      'nombre': f'{n} investigaci{"ón vencida" if n == 1 else "ones vencidas"}', 'area': 'Calidad',
                      'detalle': 'Pasó su fecha límite sin enviarse a aprobación',
                      'url': '/desviaciones?pestana=investigacion'})
    dias = parametro_int('desviaciones.dias_alerta_sin_clasificar', conn)
    n = conn.execute("SELECT COUNT(*) FROM dev_desviaciones WHERE estatus = 'REPORTADA' "
                     "AND julianday('now', 'localtime') - julianday(creado_en) > ?", (dias,)).fetchone()[0]
    if n:
        lista.append({'tipo': 'Desviaciones', 'icono': 'ri-inbox-line', 'nivel': 'amarillo',
                      'nombre': f'{n} reporte{"" if n == 1 else "s"} sin clasificar hace más de {dias} días',
                      'area': 'Calidad', 'detalle': 'Esperan clasificación', 'url': '/desviaciones?pestana=clasificar'})
    return lista


def kpis(conn):
    i = sv.indicadores(conn)
    return [
        {'titulo': 'Desviaciones abiertas', 'valor': i['abiertas'],
         'detalle': f"{i['criticas']} crítica{'' if i['criticas'] == 1 else 's'}",
         'nivel': 'rojo' if i['criticas'] else ('amarillo' if i['abiertas'] else 'verde'), 'url': '/desviaciones'},
        {'titulo': 'Acciones CAPA vencidas', 'valor': i['acciones_vencidas'], 'detalle': 'pasaron su fecha compromiso',
         'nivel': 'amarillo' if i['acciones_vencidas'] else 'verde', 'url': '/desviaciones?pestana=acciones'},
        {'titulo': 'Cerradas a tiempo (mes)',
         'valor': f"{i['a_tiempo_pct']}%" if i['a_tiempo_pct'] is not None else '—',
         'detalle': f"{i['cerradas_mes']} cerrada{'' if i['cerradas_mes'] == 1 else 's'} en el mes",
         'nivel': None if i['a_tiempo_pct'] is None else ('verde' if i['a_tiempo_pct'] >= 80 else 'amarillo'),
         'url': '/desviaciones?pestana=cerradas'},
        {'titulo': 'Días promedio para cerrar', 'valor': i['dias_promedio'] if i['dias_promedio'] is not None else '—',
         'detalle': 'últimos 12 meses', 'nivel': None, 'url': '/desviaciones/tendencias'},
    ]


# --- pruebas de humo ---------------------------------------------------------

def datos_humo(conn):
    """Una desviación con un lote afectado y una acción; devuelve desviacion_id y accion_id."""
    roles = PERMISOS['desviaciones']
    numero = conn.execute(f"SELECT numero_empleado FROM usuarios WHERE estatus = 1 AND rol IN ({','.join('?' * len(roles))}) "
                          "ORDER BY id LIMIT 1", roles).fetchone()[0]
    did = sv.siguiente_id_desviacion(conn)
    fid = conn.execute("INSERT INTO firmas (numero_empleado, nombre, significado, modulo, entidad, entidad_id, detalle) "
                       "VALUES (?, 'Humo', 'Realizó', 'desviaciones', 'dev_desviaciones', ?, 'humo')",
                       (numero, did)).lastrowid
    fila = conn.execute("SELECT id FROM materiales WHERE codigo = 'DEV-HUMO-MP'").fetchone()
    mid = fila[0] if fila else inventario.crear_material(conn, codigo='DEV-HUMO-MP', nombre='Material humo desviaciones',
                                                         tipo='MP', unidad='kg', usuario='humo')
    lid = inventario.crear_lote(conn, material_id=mid, cantidad=10, origen='RECEPCION', usuario='humo',
                                modulo='desviaciones', folio='REC-HUMO-DEV')
    sv.reportar(conn, tipo='DESVIACION', firma_id=fid, usuario='humo', lotes=[lid], desviacion_id=did, datos={
        'titulo': 'Desviación humo', 'descripcion': 'Prueba de humo', 'fecha_detecto': sv.hoy(),
        'area': 'Producción', 'documento_folio': 'REC-HUMO-DEV', 'material_id': mid})
    conn.execute("UPDATE dev_desviaciones SET estatus = 'EN_INVESTIGACION', clasificacion = 'MAYOR', responsable = ?, "
                 "vence_investigacion = ? WHERE id = ?", (numero, sv.sumar_dias(sv.hoy(), 30), did))
    # Evaluado sin impacto: el lote de humo no le estorba a quien pruebe liberaciones.
    conn.execute("UPDATE dev_lotes SET impacto = 'SIN_IMPACTO', justificacion = 'Humo' WHERE desviacion_id = ?", (did,))
    aid = sv.agregar_accion(conn, did, tipo='CORRECTIVA', descripcion='Acción humo', responsable=numero,
                            fecha_compromiso=sv.sumar_dias(sv.hoy(), 10), usuario='humo')
    return {'desviacion_id': did, 'accion_id': aid}
