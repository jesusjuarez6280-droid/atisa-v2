"""Almacén con lotes y caducidades (diseño 01-almacen).

Recepciones con inspección y cuarentena, existencias por lote con semáforo, salidas de
producto terminado (primero el que caduca antes), devoluciones de cliente, destrucciones,
conteos ciegos, etiquetas de estatus, ficha del lote con trazabilidad y carga inicial.
"""
from nucleo.registro import Modulo

from . import ganchos
from .esquema import MIGRACIONES
from .rutas import bp

MODULO = Modulo(
    clave='almacen',
    nombre='Almacén',
    orden=10,
    blueprint=bp,
    permisos={
        'almacen':             ['admin', 'almacen', 'calidad', 'produccion'],  # consultar
        'almacen_operar':      ['admin', 'almacen'],     # recibir, salidas, devoluciones, conteos, etiquetas, catálogos
        'almacen_proveedores': ['admin', 'calidad'],     # cambiar el estatus de un proveedor
    },
    menu=[
        ('logistica', '/almacen', 'Existencias', 'ri-archive-2-line', '/almacen/lote', 'almacen'),
        ('logistica', '/almacen/recepciones', 'Recepciones', 'ri-inbox-archive-line', '/almacen/recepciones',
         'almacen'),
        ('logistica', '/almacen/salidas', 'Salidas', 'ri-truck-line', '/almacen/salidas', 'almacen'),
        ('logistica', '/almacen/conteos', 'Conteos', 'ri-list-check-2', '/almacen/conteos', 'almacen'),
        ('logistica', '/almacen/catalogos', 'Catálogos', 'ri-book-2-line', '/almacen/catalogos', 'almacen'),
    ],
    inicio={'almacen': 'almacen.existencias'},
    migraciones=MIGRACIONES,
    parametros=[
        ('almacen.dias_alerta_caducidad', '90', 'Días antes de caducar para avisar en el Tablero'),
        ('almacen.dias_alerta_cuarentena', '15', 'Días en cuarentena a partir de los cuales se avisa'),
        ('almacen.vida_util_minima_dias', '180', 'Vida útil mínima al recibir (si caduca antes, avisa)'),
        ('almacen.codigo_formato_etiqueta', '', 'Código del formato impreso en las etiquetas (F-ALM-LOG…)'),
        ('almacen.codigo_formato_recepcion', '', 'Código del formato del comprobante de recepción'),
        ('almacen.codigo_formato_salida', '', 'Código del formato de remisión y actas de salida'),
    ],
    alertas=ganchos.alertas,
    kpis=ganchos.kpis,
    pendientes=ganchos.pendientes,
    enlaces={
        'ficha_lote': ganchos.url_ficha_lote,
        'material': ganchos.url_material,
        'recepcion': ganchos.url_recepcion,
    },
    datos_humo=ganchos.datos_humo,
)
