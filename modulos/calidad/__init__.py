"""Módulo 3 · Control de calidad y liberación (diseño 03-calidad-liberacion.md).

Especificaciones con versiones, bandeja de lotes en cuarentena, muestreo y resultados, investigación de
resultados fuera de especificación, revisión, dictamen con lista de verificación, certificado de análisis
y muestras de retención. Tablas con prefijo cc_; lo compartido (lotes, firmas, equipos…) se escribe solo
con los servicios de la base.
"""
from nucleo.registro import Modulo

from .rutas import bp
from .esquema import MIGRACIONES
from . import ganchos

MODULO = Modulo(
    clave='calidad',
    nombre='Calidad y liberación',
    orden=1,
    blueprint=bp,
    permisos={
        'calidad':        ['admin', 'calidad', 'produccion', 'almacen'],   # consultar bandeja, análisis y certificados
        'calidad_operar': ['admin', 'calidad'],   # muestrear, capturar, revisar, investigar, especificaciones, retención
    },
    menu=[
        ('calidad', '/calidad', 'Análisis y liberación', 'ri-test-tube-line', '/calidad/analisis', 'calidad'),
        ('calidad', '/calidad/especificaciones', 'Especificaciones', 'ri-file-list-3-line',
         '/calidad/especificaciones', 'calidad'),
        ('calidad', '/calidad/retencion', 'Muestras de retención', 'ri-archive-drawer-line', '/calidad/retencion',
         'calidad'),
    ],
    inicio={'calidad': 'calidad.bandeja'},
    migraciones=MIGRACIONES,
    parametros=[
        ('calidad.dias_aviso_reanalisis', '30', 'Días antes de la fecha de reanálisis para avisar'),
        ('calidad.meses_retencion', '12', 'Meses después de la caducidad para poder destruir una muestra de retención'),
        ('calidad.retencion_presentaciones', '1', 'En un granel, pedir muestra de retención de cada presentación (1 = sí)'),
        ('calidad.libera_distinto_de_analista', '1', 'Quien dictamina no puede haber capturado resultados (1 = sí)'),
        ('calidad.dias_alerta_oos', '30', 'Días con una investigación OOS abierta para alertar'),
        ('calidad.dias_alerta_dictamen', '2', 'Días con un lote listo para dictaminar para alertar'),
        ('calidad.codigo_formato_certificado', '', 'Código del formato del certificado de análisis (se imprime)'),
        ('calidad.codigo_formato_analisis', '', 'Código del formato de la hoja de trabajo de análisis (se imprime)'),
    ],
    alertas=ganchos.alertas,
    kpis=ganchos.kpis,
    pendientes=ganchos.pendientes,
    secciones_lote=ganchos.seccion_lote,
    enlaces={'analisis': ganchos.url_analisis, 'dictamen': ganchos.url_dictamen,
             'certificado': ganchos.url_certificado, 'investigacion': ganchos.url_investigacion},
    consultas={'analisis_por_equipo': ganchos.analisis_por_equipo},
    datos_humo=ganchos.datos_humo,
)
