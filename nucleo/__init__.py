"""Base común del ERP ATISA.

Lo que comparten los módulos: conexión a la base, permisos y facultades, vistas,
auditoría, menú, registro de módulos, tablas compartidas y sus servicios
(inventario por lote, equipos, firmas con PIN, folios, archivos y parámetros).

Regla: nada de nucleo/ importa de erp.py (evita importaciones circulares y el
problema de que `python erp.py` corre como __main__).
"""
