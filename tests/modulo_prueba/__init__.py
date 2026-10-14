"""Módulo de PRUEBA: solo existe en las pruebas (conftest lo carga con ATISA_MODULOS_EXTRA).

Sirve de ejemplo mínimo de cómo se arma un módulo real en modulos/<clave>/ y comprueba
que la base lo registra: menú, permisos, migraciones, parámetros, ganchos y humo.
"""
from nucleo.registro import Modulo

from .rutas import bp
from .esquema import MIGRACIONES
from . import ganchos

MODULO = Modulo(
    clave='prueba',
    nombre='Módulo de prueba',
    orden=90,
    blueprint=bp,
    permisos={
        'prueba':         ['admin', 'almacen'],
        'prueba_operar':  ['admin'],
    },
    menu=[('logistica', '/prueba', 'Prueba', 'ri-flask-line', '/prueba/', 'prueba')],
    migraciones=MIGRACIONES,
    parametros=[('prueba.dias_aviso', '30', 'Días de aviso del módulo de prueba')],
    alertas=ganchos.alertas,
    kpis=ganchos.kpis,
    pendientes=ganchos.pendientes,
    accesos_planta=ganchos.accesos_planta,
    verificaciones_liberacion=ganchos.verificaciones_liberacion,
    secciones_lote=ganchos.seccion_lote,
    enlaces={'prueba_cosa': ganchos.url_cosa},
    consultas={'prueba_contar': ganchos.contar},
    datos_humo=ganchos.datos_humo,
)
