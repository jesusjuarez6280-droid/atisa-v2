"""Exportaciones a Excel del módulo de desviaciones."""
from io import BytesIO

import pandas as pd

from . import servicios as sv


def _libro(hojas):
    """hojas: [(nombre, DataFrame)] → BytesIO listo para send_file."""
    salida = BytesIO()
    with pd.ExcelWriter(salida, engine='openpyxl') as writer:
        for nombre, df in hojas:
            df.to_excel(writer, sheet_name=nombre[:31], index=False)
    salida.seek(0)
    return salida


def lista(conn, filtros):
    filas, _ = sv.buscar(conn, **filtros, pagina=1, por_pagina=100000)
    df = pd.DataFrame([{
        'Folio': r['folio'], 'Tipo': sv.NOMBRE_TIPO.get(r['tipo']), 'Fecha de detección': r['fecha_detecto'],
        'Título': r['titulo'], 'Área': r['area'], 'Clasificación': sv.NOMBRE_CLASIFICACION.get(r['clasificacion'], ''),
        'Lotes afectados': r['lotes_texto'] or '', 'Responsable': r['responsable_nombre'] or '',
        'Fecha límite': r['fecha_limite'] or '', 'Estatus': sv.NOMBRE_ESTATUS.get(r['estatus']),
        'Categoría de causa': r['categoria_causa'] or '', 'Causa raíz': r['causa_raiz'] or '',
        'Cerrada': (r['cerrada_en'] or '')[:10],
    } for r in filas], columns=['Folio', 'Tipo', 'Fecha de detección', 'Título', 'Área', 'Clasificación',
                                'Lotes afectados', 'Responsable', 'Fecha límite', 'Estatus', 'Categoría de causa',
                                'Causa raíz', 'Cerrada'])
    return _libro([('Desviaciones', df)])


def tendencias(conn):
    t = sv.tendencias(conn)

    def tabla(lista, titulo):
        return pd.DataFrame([{titulo: x['etiqueta'], 'Desviaciones': x['n']} for x in lista],
                            columns=[titulo, 'Desviaciones'])

    clasif = [{'etiqueta': sv.NOMBRE_CLASIFICACION.get(x['etiqueta'], x['etiqueta']), 'n': x['n']}
              for x in t['por_clasificacion']]
    repetidas = pd.DataFrame([{'Área': r['area'], 'Categoría de causa': r['categoria_causa'], 'Veces': r['n'],
                               'Folios': r['folios']} for r in t['repetidas']],
                             columns=['Área', 'Categoría de causa', 'Veces', 'Folios'])
    acc = t['acciones']
    acciones = pd.DataFrame([{'Acciones': 'Hechas a tiempo', 'Número': acc['a_tiempo']},
                             {'Acciones': 'Hechas tarde', 'Número': acc['tarde']},
                             {'Acciones': 'Vencidas sin hacer', 'Número': acc['vencidas']},
                             {'Acciones': 'Pendientes en plazo', 'Número': acc['en_plazo']}])
    return _libro([('Por mes', tabla(t['por_mes'], 'Mes')), ('Por área', tabla(t['por_area'], 'Área')),
                   ('Por causa', tabla(t['por_categoria'], 'Categoría de causa')),
                   ('Por clasificación', tabla(clasif, 'Clasificación')),
                   ('Causas que se repiten', repetidas), ('Acciones', acciones)])


RELACIONES = {'PRESENTACION': 'Presentación envasada', 'PRODUCIDO': 'Lote producido (orden que lo usó)',
              'VENTA': 'Venta', 'DEVOLUCION': 'Devolución de cliente'}


def alcance(conn, desviacion_id):
    df = pd.DataFrame([{
        'Desde el lote': r['desde_numero_lote'], 'Relación': RELACIONES.get(r['relacion'], r['relacion']),
        'Lote': r['numero_lote'] or '', 'Material': f"{r['material_codigo'] or ''} {r['material_nombre'] or ''}".strip(),
        'Estatus': r['estatus'] or '', 'Folio / orden': r['folio'] or '', 'Cliente': r['cliente'] or '',
        'Cantidad': r['cantidad'] if r['cantidad'] is not None else '', 'Fecha': r['fecha'] or '',
    } for r in sv.alcance(conn, desviacion_id)], columns=['Desde el lote', 'Relación', 'Lote', 'Material', 'Estatus',
                                                         'Folio / orden', 'Cliente', 'Cantidad', 'Fecha'])
    return _libro([('Alcance', df)])
