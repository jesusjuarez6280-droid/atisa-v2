"""Desviaciones y CAPA (diseño 04-desviaciones-capa.md).

Reportar (también desde la tablet), clasificar, retener lotes, investigar la causa raíz,
acciones correctivas y preventivas con responsable y fecha, cierre y revisión de eficacia;
también quejas de clientes y hallazgos de auditoría. Detiene la liberación de un lote en
Calidad mientras no se evalúe si la desviación lo afectó.
"""
from nucleo.registro import Modulo

from .rutas import bp
from .esquema import MIGRACIONES
from .servicios import PARAMETROS
from . import ganchos

MODULO = Modulo(
    clave='desviaciones',
    nombre='Desviaciones y CAPA',
    orden=30,
    blueprint=bp,
    permisos={
        # ver, reportar y atender sus acciones
        'desviaciones':        ['admin', 'calidad', 'produccion', 'almacen', 'mantenimiento', 'rh'],
        # reportar en la tablet
        'desviaciones_planta': ['admin', 'operador', 'produccion', 'calidad', 'almacen', 'mantenimiento'],
    },
    menu=[('calidad', '/desviaciones', 'Desviaciones y CAPA', 'ri-error-warning-line', '/desviaciones/',
           'desviaciones')],
    migraciones=MIGRACIONES,
    parametros=PARAMETROS,
    alertas=ganchos.alertas,
    kpis=ganchos.kpis,
    pendientes=ganchos.pendientes,
    accesos_planta=ganchos.accesos_planta,
    verificaciones_liberacion=ganchos.verificaciones_liberacion,
    secciones_lote=ganchos.seccion_lote,
    enlaces={'reportar_desviacion': ganchos.url_reportar_desviacion, 'desviacion': ganchos.url_desviacion},
    consultas={'desviaciones_de': ganchos.desviaciones_de},
    datos_humo=ganchos.datos_humo,
)
