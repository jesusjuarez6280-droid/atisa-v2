"""Exportaciones a Excel (pandas + openpyxl, como los reportes actuales) y plantillas
de la carga inicial."""
import io

import pandas as pd

from . import servicios


def _excel(hojas):
    """hojas: [(nombre, DataFrame)] → BytesIO listo para send_file."""
    salida = io.BytesIO()
    with pd.ExcelWriter(salida, engine='openpyxl') as writer:
        for nombre, df in hojas:
            df.to_excel(writer, sheet_name=nombre[:31], index=False)
    salida.seek(0)
    return salida


def _df(filas, columnas):
    """columnas: [(llave, título)]."""
    return pd.DataFrame([{t: f.get(k) for k, t in columnas} for f in filas], columns=[t for _, t in columnas])


_COLS_LOTE = [('material_codigo', 'Código'), ('material_nombre', 'Material'), ('numero_lote', 'Lote'),
              ('lote_proveedor', 'Lote proveedor'), ('proveedor_nombre', 'Proveedor'), ('existencia', 'Existencia'),
              ('unidad', 'Unidad'), ('estatus', 'Estatus'), ('fecha_entrada', 'Entrada'),
              ('fecha_caducidad', 'Caducidad'), ('fecha_reanalisis', 'Reanálisis'), ('ubicacion', 'Ubicación')]


def existencias(conn):
    lotes, _ = servicios.existencias_por_lote(conn, {}, limite=-1)
    materiales = servicios.existencias_por_material(conn, {})
    return _excel([
        (f'Por lote {servicios.hoy()}', _df(lotes, _COLS_LOTE)),
        ('Por material', _df(materiales, [('codigo', 'Código'), ('nombre', 'Material'), ('tipo', 'Tipo'),
                                          ('unidad', 'Unidad'), ('disponible', 'Disponible'),
                                          ('cuarentena', 'Cuarentena'), ('retenido', 'Retenido'),
                                          ('rechazado', 'Rechazado o caducado'), ('total', 'Total'),
                                          ('stock_minimo', 'Stock mínimo')])),
    ])


def lotes_filtrados(conn, filtro, titulo):
    lotes, _ = servicios.existencias_por_lote(conn, {'f': filtro}, limite=-1)
    return _excel([(titulo, _df(lotes, _COLS_LOTE))])


def kardex(conn, material):
    lotes, movimientos, _ = servicios.kardex_material(conn, material['id'])
    return _excel([
        ('Lotes', _df(lotes, [('numero_lote', 'Lote'), ('lote_proveedor', 'Lote proveedor'),
                              ('proveedor_nombre', 'Proveedor'), ('estatus', 'Estatus'),
                              ('existencia', 'Existencia'), ('fecha_caducidad', 'Caducidad'),
                              ('ubicacion', 'Ubicación')])),
        ('Movimientos', _df(movimientos, [('ts', 'Fecha'), ('numero_lote', 'Lote'), ('tipo', 'Movimiento'),
                                          ('cantidad', 'Cantidad'), ('saldo', 'Saldo del lote'),
                                          ('folio', 'Folio'), ('motivo', 'Motivo'), ('usuario', 'Usuario')])),
    ])


def plantilla_materiales():
    ejemplo = [{'codigo': 'MP-0001', 'nombre': 'Ácido ascórbico', 'tipo': 'MP', 'unidad': 'kg',
                'stock_minimo': 25, 'requiere_analisis': 'si', 'vida_util_meses': 24,
                'condiciones_almacen': 'Lugar seco'}]
    return _excel([('Materiales', pd.DataFrame(ejemplo, columns=servicios.COLUMNAS_MATERIALES))])


def plantilla_existencias():
    ejemplo = [{'codigo_material': 'MP-0001', 'unidad': 'kg', 'lote': 'MP-26-0001', 'lote_proveedor': 'A12345',
                'proveedor': '', 'cantidad': 120.5, 'fecha_caducidad': '2027-06-30', 'fecha_reanalisis': '',
                'estatus': 'CUARENTENA', 'ubicacion': 'CUA-01'}]
    return _excel([('Existencias', pd.DataFrame(ejemplo, columns=servicios.COLUMNAS_EXISTENCIAS))])


def leer_hoja(archivo, columnas):
    """FileStorage de Excel → renglones normalizados (dict por renglón, todo texto)."""
    if not archivo or not archivo.filename:
        return []
    if not archivo.filename.lower().endswith(('.xlsx', '.xlsm')):
        raise servicios.ErrorAlmacen(f'{archivo.filename}: sube la plantilla en Excel (.xlsx).')
    try:
        df = pd.read_excel(archivo, dtype=object)
    except Exception:
        raise servicios.ErrorAlmacen(f'No se pudo leer {archivo.filename}; usa la plantilla descargada.')
    df.columns = [str(c).strip().lower() for c in df.columns]
    requeridas = servicios.COLUMNAS_REQUERIDAS
    faltan = [c for c in columnas if c in requeridas and c not in df.columns]
    if faltan:
        raise servicios.ErrorAlmacen(f'{archivo.filename}: faltan las columnas {", ".join(faltan)}.')
    return servicios.normalizar_renglones(df.to_dict('records'), columnas)
