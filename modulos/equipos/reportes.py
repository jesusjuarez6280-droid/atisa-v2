"""Programa anual de calibración y mantenimiento, y exportaciones a Excel."""
import io

import pandas as pd

from .servicios.comun import sumar_meses

MESES = ('Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun', 'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic')
ESTATUS_TEXTO = {'EN_SERVICIO': 'En servicio', 'FUERA_DE_SERVICIO': 'Fuera de servicio', 'BAJA': 'Baja'}


def _meses_programados(vence, frecuencia, anio):
    """Meses (1-12) del año en que toca, contando hacia atrás y hacia adelante desde el vencimiento."""
    if not vence or not frecuencia:
        return set()
    meses, inicio, fin = set(), f'{anio}-01-01', f'{anio}-12-31'
    f = vence
    while f >= inicio:
        if f <= fin:
            meses.add(int(f[5:7]))
        f = sumar_meses(f, -frecuencia)
    f = sumar_meses(vence, frecuencia)
    while f <= fin:
        if f >= inicio:
            meses.add(int(f[5:7]))
        f = sumar_meses(f, frecuencia)
    return meses


def programa_anual(conn, anio):
    """[{codigo, nombre, area, tipo, cal: {mes: {'toca', 'hecho'}}, man: {...}, frec_cal, frec_man}]"""
    anio = int(anio)
    progs = {}
    for p in conn.execute('SELECT * FROM eq_programas WHERE activo = 1').fetchall():
        progs.setdefault(p['equipo_id'], {})[p['actividad']] = p
    hechas_cal, hechas_man = {}, {}
    for r in conn.execute("SELECT equipo_id, fecha FROM eq_calibraciones WHERE estatus = 'APTO' AND substr(fecha, 1, 4) = ?",
                          (str(anio),)).fetchall():
        hechas_cal.setdefault(r['equipo_id'], set()).add(int(r['fecha'][5:7]))
    for r in conn.execute("SELECT equipo_id, fin FROM eq_ordenes_trabajo WHERE tipo = 'PREVENTIVO' AND estatus = 'TERMINADA' "
                          'AND substr(fin, 1, 4) = ?', (str(anio),)).fetchall():
        hechas_man.setdefault(r['equipo_id'], set()).add(int(r['fin'][5:7]))
    filas = []
    for e in conn.execute("SELECT * FROM equipos WHERE estatus <> 'BAJA' ORDER BY area, codigo").fetchall():
        p = progs.get(e['id'], {})
        if not p and e['id'] not in hechas_cal and e['id'] not in hechas_man:
            continue
        pc, pm = p.get('CALIBRACION'), p.get('MANTENIMIENTO')
        toca_c = _meses_programados(e['vence_calibracion'], pc['frecuencia_meses'], anio) if pc else set()
        toca_m = _meses_programados(e['vence_mantenimiento'], pm['frecuencia_meses'], anio) if pm else set()
        hc, hm = hechas_cal.get(e['id'], set()), hechas_man.get(e['id'], set())
        filas.append({
            'id': e['id'], 'codigo': e['codigo'], 'nombre': e['nombre'], 'area': e['area'], 'tipo': e['tipo'],
            'frec_cal': pc['frecuencia_meses'] if pc else None, 'frec_man': pm['frecuencia_meses'] if pm else None,
            'cal': {m: {'toca': m in toca_c, 'hecho': m in hc} for m in range(1, 13)},
            'man': {m: {'toca': m in toca_m, 'hecho': m in hm} for m in range(1, 13)},
        })
    return filas


def _celda(c):
    if c['hecho']:
        return '✔'
    return '○' if c['toca'] else ''


def excel_programa(filas, anio):
    renglones = []
    for f in filas:
        for actividad, clave, frec in (('Calibración', 'cal', f['frec_cal']), ('Mantenimiento', 'man', f['frec_man'])):
            if not frec and not any(c['hecho'] for c in f[clave].values()):
                continue
            r = {'Código': f['codigo'], 'Equipo': f['nombre'], 'Área': f['area'] or '', 'Actividad': actividad,
                 'Cada (meses)': frec or ''}
            r.update({MESES[m - 1]: _celda(f[clave][m]) for m in range(1, 13)})
            renglones.append(r)
    return _libro({f'Programa {anio}': pd.DataFrame(renglones)})


def excel_equipos(filas):
    df = pd.DataFrame([{
        'Código': e['codigo'], 'Nombre': e['nombre'], 'Tipo': e['tipo'], 'Área': e['area'] or '',
        'Marca': e['marca'] or '', 'Modelo': e['modelo'] or '', 'Serie': e['serie'] or '',
        'Estatus': ESTATUS_TEXTO.get(e['estatus'], e['estatus']), 'Instrumento': 'Sí' if e['es_instrumento'] else 'No',
        'Verificación diaria': 'Sí' if e['verificacion_diaria'] else 'No',
        'Vence calibración': e['vence_calibracion'] or '', 'Vence mantenimiento': e['vence_mantenimiento'] or '',
        'Última verificación': e['ultima_verificacion'] or '',
    } for e in filas])
    return _libro({'Equipos': df})


def _libro(hojas):
    salida = io.BytesIO()
    with pd.ExcelWriter(salida, engine='openpyxl') as writer:
        for nombre, df in hojas.items():
            df.to_excel(writer, sheet_name=nombre[:31], index=False)
    salida.seek(0)
    return salida
