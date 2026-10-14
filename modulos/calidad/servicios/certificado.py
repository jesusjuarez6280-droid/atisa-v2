"""Certificado de análisis (diseño 03, 4.5 y regla 13), con el formato por defecto del diseño.

Solo de lotes aprobados; solo resultados válidos de pruebas marcadas para el certificado; con los nombres
de quien analizó, revisó y liberó. Para una presentación toma el análisis de su granel e imprime los
datos de la presentación.
"""
from nucleo.parametros import parametro

from ._comun import ErrorCalidad, nombre_firmante, lote_fila, sigue_al_granel
from . import especificaciones as esp


def armar(conn, lote_id) -> dict:
    lote = lote_fila(conn, lote_id)
    if lote['estatus'] != 'APROBADO':
        raise ErrorCalidad(f'El certificado solo sale de lotes aprobados; {lote["numero_lote"]} está '
                           f'{lote["estatus"]}.')
    base_id = lote['lote_padre_id'] if sigue_al_granel(lote) else lote['id']
    base = lote_fila(conn, base_id) if base_id != lote['id'] else lote
    a = conn.execute("SELECT * FROM cc_analisis WHERE lote_id = ? AND tipo = 'LIBERACION' AND estatus = 'CONFORME' "
                     'ORDER BY id DESC LIMIT 1', (base_id,)).fetchone()
    if not a:
        raise ErrorCalidad(f'El lote {base["numero_lote"]} no tiene análisis de liberación conforme.')
    e = esp.especificacion(conn, a['especificacion_id'])
    filas = conn.execute(
        'SELECT p.prueba, p.tipo, p.texto_especificacion, p.metodo, p.unidad, p.decimales, r.valor_redondeado, '
        'r.texto, r.cumple, r.fuente, r.fecha_analisis, f.nombre AS realizo FROM cc_especificacion_pruebas p '
        'JOIN cc_resultados r ON r.prueba_id = p.id AND r.analisis_id = ? AND r.valido = 1 '
        'JOIN firmas f ON f.id = r.realizo_firma_id '
        'WHERE p.especificacion_id = ? AND p.en_certificado = 1 ORDER BY p.orden', (a['id'], e['id'])).fetchall()
    pruebas = []
    for f in filas:
        d = dict(f)
        if d['tipo'] == 'NUMERICA' and d['valor_redondeado'] is not None:
            d['resultado'] = f'{d["valor_redondeado"]:.{int(d["decimales"])}f}' + (f' {d["unidad"]}' if d['unidad'] else '')
        else:
            d['resultado'] = d['texto'] or ''
        pruebas.append(d)
    dictamen = conn.execute("SELECT d.*, f.nombre AS libero FROM cc_dictamenes d JOIN firmas f ON f.id = d.firma_id "
                            "WHERE d.lote_id IN (?, ?) AND d.decision = 'APROBADO' ORDER BY d.id DESC LIMIT 1",
                            (lote['id'], base_id)).fetchone()
    analistas = list(dict.fromkeys(p['realizo'] for p in pruebas))
    return {
        'lote': dict(lote), 'granel': dict(base) if base_id != lote['id'] else None, 'analisis': dict(a),
        'especificacion': dict(e), 'pruebas': pruebas, 'analizo': ', '.join(analistas),
        'reviso': nombre_firmante(conn, a['reviso_firma_id']),
        'libero': dictamen['libero'] if dictamen else None,
        'fecha_dictamen': (dictamen['ts'] or '')[:10] if dictamen else None,
        'codigo_formato': parametro('calidad.codigo_formato_certificado', conn),
    }
