"""Folios automáticos (OP-2026-0001) y números de lote con formato configurable."""
import re
from datetime import datetime


def _siguiente(conn, serie, anio):
    fila = conn.execute(
        'INSERT INTO folios (serie, anio, ultimo) VALUES (?, ?, 1) '
        'ON CONFLICT(serie, anio) DO UPDATE SET ultimo = ultimo + 1 RETURNING ultimo',
        (serie, anio)).fetchone()
    return fila[0]


def siguiente_folio(conn, serie, fecha=None):
    """'OP' → 'OP-2026-0001'. Consecutivo por serie y año, dentro de la transacción
    del que llama (si la transacción se revierte, el número no se gasta)."""
    serie = (serie or '').strip().upper()
    if not serie:
        raise ValueError('Falta la serie del folio.')
    anio = (fecha or datetime.now()).year
    return f'{serie}-{anio}-{_siguiente(conn, serie, anio):04d}'


_TOKEN_CONSECUTIVO = re.compile(r'\{(#+)\}')


def numero_lote(conn, formato, fecha=None, **datos):
    """Arma un número de lote con tokens {AA} {AAAA} {MM} {DD} {COD} {TIPO} y un
    consecutivo {###} (tantos # como dígitos). El consecutivo corre por cada prefijo
    distinto: '{TIPO}-{AA}-{####}' numera aparte MP y EM y reinicia cada año;
    '{AA}{MM}{###}' reinicia cada mes.
    datos: COD (código del material) y TIPO (MP, EM, GR…)."""
    fecha = fecha or datetime.now()
    texto = (formato.replace('{AAAA}', f'{fecha:%Y}').replace('{AA}', f'{fecha:%y}')
             .replace('{MM}', f'{fecha:%m}').replace('{DD}', f'{fecha:%d}')
             .replace('{COD}', str(datos.get('COD', datos.get('cod', '')) or ''))
             .replace('{TIPO}', str(datos.get('TIPO', datos.get('tipo', '')) or '')))
    m = _TOKEN_CONSECUTIVO.search(texto)
    if not m:
        return texto
    serie = 'LOTE:' + _TOKEN_CONSECUTIVO.sub('#', texto)
    n = _siguiente(conn, serie, fecha.year)
    return texto[:m.start()] + f'{n:0{len(m.group(1))}d}' + texto[m.end():]
