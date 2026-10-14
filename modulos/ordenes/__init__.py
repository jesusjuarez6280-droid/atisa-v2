"""Módulo 2 · Órdenes de producción y expediente de lote (02-ordenes-produccion.md).

Fórmulas maestras con versiones y aprobación; órdenes que copian la fórmula escalada y
reciben su número de lote; ejecución en la tablet (despeje, pesadas, pasos, granel,
envasado y conciliación) con firmas de número de empleado + PIN; expediente de lote
imprimible para Calidad y la auditoría.

El número de lote usa el parámetro de la base `nucleo.formato_lote_produccion`
(por defecto {AA}{MM}{###}, p. ej. 2610015).
"""
from nucleo.registro import Modulo

from .blueprint import bp
from . import rutas_oficina, rutas_planta  # noqa: F401  (registran sus rutas en bp)
from .esquema import MIGRACIONES
from . import ganchos

MODULO = Modulo(
    clave='ordenes',
    nombre='Órdenes de producción',
    orden=10,
    blueprint=bp,
    permisos={
        'ordenes':         ['admin', 'produccion', 'calidad'],     # ver órdenes, expedientes y fórmulas
        'ordenes_planear': ['admin', 'produccion'],                # crear órdenes y fórmulas, revisar
        'ordenes_planta':  ['admin', 'produccion', 'operador'],    # pantallas de la tablet
    },
    menu=[
        ('produccion', '/ordenes', 'Órdenes de producción', 'ri-file-list-3-line', '', 'ordenes'),
        ('produccion', '/ordenes/formulas', 'Fórmulas maestras', 'ri-flask-line', '/ordenes/formulas', 'ordenes'),
    ],
    inicio={'produccion': 'ordenes.lista'},
    migraciones=MIGRACIONES,
    parametros=[
        ('ordenes.verificar_pesadas', 'todas',
         'Qué pesadas verifica una segunda persona: todas o activos'),
        ('ordenes.expertise_minimo', '60', 'Nivel mínimo (%) en la Matriz de Expertise para firmar un paso'),
        ('ordenes.bloquear_sin_calificacion', '0',
         '1 = no deja firmar un paso a quien no tiene el nivel mínimo; 0 = solo avisa'),
        ('ordenes.dias_alerta_en_proceso', '5', 'Días en proceso para avisar que una orden se atrasó'),
        ('ordenes.codigo_formato_expediente', '', 'Código del formato impreso del expediente de lote (p. ej. F-PRO-001)'),
    ],
    alertas=ganchos.alertas,
    kpis=ganchos.kpis,
    pendientes=ganchos.pendientes,
    accesos_planta=ganchos.accesos_planta,
    verificaciones_liberacion=ganchos.verificaciones_liberacion,
    secciones_lote=ganchos.seccion_lote,
    enlaces={'orden': ganchos.url_orden, 'expediente': ganchos.url_expediente},
    consultas={'lotes_por_equipo': ganchos.lotes_por_equipo, 'demanda_material': ganchos.demanda_material},
    datos_humo=ganchos.datos_humo,
)
