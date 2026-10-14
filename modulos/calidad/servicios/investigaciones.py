"""Investigación de un resultado fuera de especificación (OOS) — diseño 03, 4.3 y regla 7.

Se abre sola al capturar un resultado que no cumple. Quien investiga palomea la revisión del
laboratorio, escribe los hallazgos y concluye (firma Realizó); quien tiene gestionar_desviaciones la
aprueba (firma Aprobó) y entonces se aplica:
  · ERROR_LABORATORIO: el resultado se anula con el folio de la investigación y la prueba se captura de nuevo;
  · CONFIRMADO: el resultado queda válido y el análisis NO_CONFORME.
"""
from nucleo import folios

from ._comun import (ErrorCalidad, ROLES_CALIDAD, texto, ahora, firmar, nombre_firmante, lote_fila)

REVISION = (
    ('calculos_ok', 'Cálculos correctos'),
    ('equipo_ok', 'Equipo apto y bien usado'),
    ('metodo_ok', 'Método seguido'),
    ('reactivos_ok', 'Reactivos y estándares vigentes'),
    ('muestra_ok', 'Muestra correcta y bien preparada'),
    ('analista_ok', 'Analista calificado'),
)
CONCLUSIONES = {'ERROR_LABORATORIO': 'Error del laboratorio', 'CONFIRMADO': 'Confirmado'}


def investigacion(conn, investigacion_id):
    fila = conn.execute('SELECT * FROM cc_investigaciones WHERE id = ?', (investigacion_id,)).fetchone()
    if not fila:
        raise ErrorCalidad('La investigación no existe.')
    return fila


def abrir(conn, *, analisis_id, resultado_id) -> int:
    folio = folios.siguiente_folio(conn, 'OOS')
    cur = conn.execute('INSERT INTO cc_investigaciones (folio, analisis_id, resultado_id) VALUES (?, ?, ?)',
                       (folio, analisis_id, resultado_id))
    return cur.lastrowid


def _checks(revision):
    salida = {}
    for campo, _ in REVISION:
        v = (revision or {}).get(campo)
        salida[campo] = None if v in (None, '') else (1 if str(v) in ('1', 'True', 'true', 'si', 'sí') else 0)
    return salida


def guardar(conn, investigacion_id, *, revision=None, hallazgos=None):
    """Avance sin firma (se puede guardar a medias)."""
    i = investigacion(conn, investigacion_id)
    if i['estatus'] != 'ABIERTA' or i['realizo_firma_id']:
        raise ErrorCalidad(f'{i["folio"]} ya está concluida: no se edita.')
    c = _checks(revision)
    conn.execute(f'UPDATE cc_investigaciones SET {", ".join(f"{k} = ?" for k in c)}, hallazgos = ? WHERE id = ?',
                 (*c.values(), texto(hallazgos), investigacion_id))


def concluir(conn, investigacion_id, *, conclusion, firma, revision=None, hallazgos=None) -> int:
    i = investigacion(conn, investigacion_id)
    if i['estatus'] != 'ABIERTA':
        raise ErrorCalidad(f'{i["folio"]} ya está cerrada.')
    if i['realizo_firma_id']:
        raise ErrorCalidad(f'{i["folio"]} ya está concluida; falta que la aprueben.')
    if conclusion not in CONCLUSIONES:
        raise ErrorCalidad('Elige la conclusión: error del laboratorio o confirmado.')
    c = _checks(revision)
    if any(v is None for v in c.values()):
        raise ErrorCalidad('Contesta toda la revisión del laboratorio.')
    hallazgos = texto(hallazgos)
    if not hallazgos:
        raise ErrorCalidad('Escribe los hallazgos de la investigación.')
    errores = [nombre for campo, nombre in REVISION if c[campo] == 0]
    if conclusion == 'ERROR_LABORATORIO' and not errores:
        raise ErrorCalidad('Para concluir error del laboratorio marca en la revisión qué se encontró mal.')
    if conclusion == 'CONFIRMADO' and errores:
        raise ErrorCalidad('La revisión encontró un error del laboratorio (' + ', '.join(errores) +
                           '): no se puede confirmar el resultado.')
    fid = firmar(conn, firma, 'Realizó', 'cc_investigaciones', investigacion_id,
                 f'{i["folio"]}: concluyó {CONCLUSIONES[conclusion].lower()}', roles=ROLES_CALIDAD)
    conn.execute(f'UPDATE cc_investigaciones SET {", ".join(f"{k} = ?" for k in c)}, hallazgos = ?, conclusion = ?, '
                 'realizo_firma_id = ? WHERE id = ?', (*c.values(), hallazgos, conclusion, fid, investigacion_id))
    return fid


def regresar(conn, investigacion_id):
    """Quien aprueba no está de acuerdo: la conclusión se quita y se vuelve a investigar."""
    i = investigacion(conn, investigacion_id)
    if i['estatus'] != 'ABIERTA' or not i['realizo_firma_id']:
        raise ErrorCalidad('Solo se regresa una investigación concluida y sin aprobar.')
    conn.execute('UPDATE cc_investigaciones SET conclusion = NULL, realizo_firma_id = NULL WHERE id = ?',
                 (investigacion_id,))


def aprobar(conn, investigacion_id, *, firma) -> int:
    from . import analisis as an
    i = investigacion(conn, investigacion_id)
    if i['estatus'] != 'ABIERTA':
        raise ErrorCalidad(f'{i["folio"]} ya está cerrada.')
    if not i['realizo_firma_id'] or not i['conclusion']:
        raise ErrorCalidad('Primero quien investigó concluye y firma.')
    fid = firmar(conn, firma, 'Aprobó', 'cc_investigaciones', investigacion_id,
                 f'Aprobó {i["folio"]}: {CONCLUSIONES[i["conclusion"]].lower()}', facultad='gestionar_desviaciones')
    conn.execute("UPDATE cc_investigaciones SET estatus = 'CERRADA', aprobo_firma_id = ?, cerrada_en = ? WHERE id = ?",
                 (fid, ahora(), investigacion_id))
    if i['conclusion'] == 'ERROR_LABORATORIO':
        conn.execute('UPDATE cc_resultados SET valido = 0, motivo_anulacion = ?, anulo_firma_id = ? '
                     'WHERE id = ? AND valido = 1', (f'{i["folio"]}: error de laboratorio', fid, i['resultado_id']))
        an.actualizar_estatus(conn, i['analisis_id'])
    else:
        a = an.analisis(conn, i['analisis_id'])
        if a['estatus'] in an.ABIERTOS:
            conn.execute("UPDATE cc_analisis SET estatus = 'NO_CONFORME', terminado_en = ? WHERE id = ?",
                         (ahora(), a['id']))
    return fid


def lote_aprobado_sin_retener(conn, inv):
    """True si el lote del análisis está APROBADO y el OOS no se ha descartado por error de laboratorio."""
    if inv['conclusion'] == 'ERROR_LABORATORIO' and inv['estatus'] == 'CERRADA':
        return False
    fila = conn.execute('SELECT l.estatus FROM cc_analisis a JOIN lotes l ON l.id = a.lote_id WHERE a.id = ?',
                        (inv['analisis_id'],)).fetchone()
    return bool(fila) and fila['estatus'] == 'APROBADO'


def detalle(conn, investigacion_id) -> dict:
    from . import analisis as an
    i = dict(investigacion(conn, investigacion_id))
    i['realizo'] = nombre_firmante(conn, i['realizo_firma_id'])
    i['aprobo'] = nombre_firmante(conn, i['aprobo_firma_id'])
    a = dict(an.analisis(conn, i['analisis_id']))
    r = conn.execute('SELECT r.*, p.prueba, p.texto_especificacion, p.unidad, p.decimales, p.tipo AS prueba_tipo, '
                     'f.nombre AS realizo, eq.codigo AS equipo_codigo FROM cc_resultados r '
                     'JOIN cc_especificacion_pruebas p ON p.id = r.prueba_id JOIN firmas f ON f.id = r.realizo_firma_id '
                     'LEFT JOIN equipos eq ON eq.id = r.equipo_id WHERE r.id = ?', (i['resultado_id'],)).fetchone()
    lote = dict(lote_fila(conn, a['lote_id']))
    return {'i': i, 'a': a, 'r': dict(r), 'lote': lote, 'revision': REVISION, 'conclusiones': CONCLUSIONES,
            'aprobado_sin_retener': lote_aprobado_sin_retener(conn, i)}


def abiertas(conn):
    return conn.execute(
        "SELECT i.*, a.folio AS analisis_folio, l.numero_lote, m.codigo AS material_codigo, "
        "CAST(julianday('now','localtime') - julianday(i.abierta_en) AS INTEGER) AS dias "
        'FROM cc_investigaciones i JOIN cc_analisis a ON a.id = i.analisis_id JOIN lotes l ON l.id = a.lote_id '
        "JOIN materiales m ON m.id = l.material_id WHERE i.estatus = 'ABIERTA' ORDER BY i.id").fetchall()
