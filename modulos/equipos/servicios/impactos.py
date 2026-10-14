"""Evaluación de impacto: un equipo se encontró fuera de tolerancia; ¿qué lotes y análisis
pudo afectar desde la última vez que se supo que estaba bien? (regla 5)

Los lotes y análisis los dan otros módulos por sus consultas (si están instalados):
  lotes_por_equipo(conn, equipo_id, desde)     (Órdenes)
  analisis_por_equipo(conn, equipo_id, desde)  (Calidad)
Cada una devuelve una lista de dicts; aquí se lee de cada uno `lote_id` (para el candado de
liberación), `fecha` (para recortar la ventana) y, para mostrar, `numero_lote`, `folio`,
`material`, `descripcion` y `url` si vienen.
"""
from flask import current_app, has_app_context

from nucleo import registro

from .comun import ErrorEquipos, equipo, exigir_facultad, texto

CONSULTAS = (('lotes_por_equipo', 'Órdenes y lotes'), ('analisis_por_equipo', 'Análisis'))


def ultima_vez_bien(conn, equipo_id, *, hasta, excluir_verificacion=None, excluir_calibracion=None):
    """Fecha de la última vez que se supo que el equipo estaba bien antes de `hasta`: la última
    verificación del día conforme o, si no se verifica a diario, la última calibración que lo
    dejó conforme. Si nunca hubo ninguna, la fecha de alta."""
    e = equipo(conn, equipo_id)
    if e['verificacion_diaria']:
        fila = conn.execute('SELECT fecha FROM eq_verificaciones WHERE equipo_id = ? AND conforme = 1 AND fecha <= ? '
                            'AND id <> ? ORDER BY fecha DESC, id DESC LIMIT 1',
                            (equipo_id, hasta, excluir_verificacion or 0)).fetchone()
        if fila:
            return fila['fecha']
    fila = conn.execute("SELECT fecha FROM eq_calibraciones WHERE equipo_id = ? AND estatus = 'APTO' "
                        'AND conforme_dejado = 1 AND fecha <= ? AND id <> ? ORDER BY fecha DESC, id DESC LIMIT 1',
                        (equipo_id, hasta, excluir_calibracion or 0)).fetchone()
    if fila:
        return fila['fecha']
    alta = conn.execute('SELECT fecha_alta FROM eq_fichas WHERE equipo_id = ?', (equipo_id,)).fetchone()
    return (alta and alta['fecha_alta']) or (e['creado_en'] or hasta)[:10]


def abrir(conn, equipo_id, *, origen, origen_id, hasta):
    """Se abre sola (calibración fuera de tolerancia o dos verificaciones fallidas). Devuelve su id."""
    previa = conn.execute('SELECT id FROM eq_impactos WHERE origen = ? AND origen_id = ?', (origen, origen_id)).fetchone()
    if previa:
        return previa['id']
    desde = ultima_vez_bien(conn, equipo_id, hasta=hasta,
                            excluir_verificacion=origen_id if origen == 'VERIFICACION' else None,
                            excluir_calibracion=origen_id if origen == 'CALIBRACION' else None)
    cur = conn.execute('INSERT INTO eq_impactos (equipo_id, origen, origen_id, desde, hasta) VALUES (?, ?, ?, ?, ?)',
                       (equipo_id, origen, origen_id, min(desde, hasta), hasta))
    return cur.lastrowid


def impacto(conn, impacto_id):
    fila = conn.execute('SELECT i.*, e.codigo, e.nombre AS equipo_nombre FROM eq_impactos i '
                        'JOIN equipos e ON e.id = i.equipo_id WHERE i.id = ?', (impacto_id,)).fetchone()
    if not fila:
        raise ErrorEquipos('La evaluación de impacto no existe.')
    return fila


def _en_ventana(item, desde, hasta):
    fecha = str(item.get('fecha') or '')[:10]
    return not fecha or desde <= fecha <= hasta


def usos_en_ventana(conn, equipo_id, desde, hasta=None):
    """{'lotes_por_equipo': [...], 'analisis_por_equipo': [...], 'faltan': [...], 'errores': [...]}.
    `faltan` = consultas de módulos no instalados; `errores` = las que tronaron."""
    res = {'faltan': [], 'errores': []}
    for nombre, titulo in CONSULTAS:
        if not registro.hay_consulta(nombre):
            res[nombre] = []
            res['faltan'].append(titulo)
            continue
        try:
            items = registro.consulta(nombre, conn, equipo_id, desde, default=[]) or []
            items = [dict(i) for i in items]
        except Exception:
            if has_app_context():
                current_app.logger.exception('Falló la consulta %s del equipo %s', nombre, equipo_id)
            res[nombre] = []
            res['errores'].append(titulo)
            continue
        res[nombre] = [i for i in items if hasta is None or _en_ventana(i, desde, hasta)]
    return res


def lote_ids(usos):
    ids = []
    for nombre, _ in CONSULTAS:
        for i in usos.get(nombre, []):
            try:
                lote = int(i.get('lote_id') or 0)
            except (TypeError, ValueError):
                continue
            if lote and lote not in ids:
                ids.append(lote)
    return ids


def evaluar(conn, impacto_id, *, conclusion, firma_id, usuario):
    """Conclusión firmada por dictaminar_equipos; puede remitir a la desviación."""
    i = impacto(conn, impacto_id)
    exigir_facultad(conn, firma_id)
    if i['estatus'] != 'PENDIENTE':
        raise ErrorEquipos('Esta evaluación ya está firmada.')
    conclusion = texto(conclusion)
    if not conclusion:
        raise ErrorEquipos('Escribe la conclusión de la evaluación (puede remitir a la desviación).')
    conn.execute("UPDATE eq_impactos SET estatus = 'EVALUADO', conclusion = ?, firma_id = ? WHERE id = ?",
                 (conclusion, firma_id, impacto_id))


def pendientes(conn):
    return conn.execute("SELECT i.*, e.codigo, e.nombre AS equipo_nombre FROM eq_impactos i "
                        "JOIN equipos e ON e.id = i.equipo_id WHERE i.estatus = 'PENDIENTE' ORDER BY i.id").fetchall()


def bloqueos_de_lote(conn, lote_id):
    """Gancho verificaciones_liberacion: motivos por los que Calidad no puede liberar el lote."""
    motivos = []
    for i in pendientes(conn):
        usos = usos_en_ventana(conn, i['equipo_id'], i['desde'], i['hasta'])
        if usos['errores']:
            motivos.append(f'No se pudo revisar si {i["codigo"]} (fuera de tolerancia el {i["hasta"]}) '
                           'se usó en este lote; avisa a sistemas.')
        elif int(lote_id) in lote_ids(usos):
            motivos.append(f'{i["codigo"]} se encontró fuera de tolerancia el {i["hasta"]}: '
                           'falta evaluar su impacto en este lote.')
    return motivos
