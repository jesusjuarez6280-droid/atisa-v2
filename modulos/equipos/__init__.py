"""Módulo 5 · Equipos: calibración, verificación del día y mantenimiento.

Diseño: 05-equipos-calibracion.md. El catálogo `equipos` y el candado `equipo_apto` son de la
base; este módulo lleva la ficha, los programas, las calibraciones, las verificaciones, las
órdenes de trabajo, las evaluaciones de impacto y mueve el estatus y los vencimientos con
`actualizar_vencimientos`.
"""
from nucleo.registro import Modulo

from .rutas_oficina import bp
from . import rutas_planta  # noqa: F401  (registra /equipos/planta/... en el mismo blueprint)
from .esquema import MIGRACIONES
from . import ganchos

MODULO = Modulo(
    clave='equipos',
    nombre='Equipos',
    orden=30,
    blueprint=bp,
    permisos={
        'equipos':        ['admin', 'mantenimiento', 'calidad', 'produccion'],
        'equipos_operar': ['admin', 'mantenimiento', 'calidad'],
        'equipos_planta': ['admin', 'operador', 'produccion', 'calidad', 'mantenimiento'],
    },
    menu=[('produccion', '/equipos', 'Equipos', 'ri-tools-line', '/equipos/', 'equipos')],
    inicio={'mantenimiento': 'equipos.lista'},
    migraciones=MIGRACIONES,
    parametros=[
        ('equipos.dias_aviso_calibracion', '30', 'Días de aviso antes de que venza una calibración (el que se propone '
                                                 'al crear un programa)'),
        ('equipos.dias_aviso_mantenimiento', '15', 'Días de aviso antes de que venza un mantenimiento preventivo'),
        ('equipos.intentos_verificacion', '2', 'Verificaciones del día no conformes seguidas que dejan el equipo '
                                               'fuera de servicio'),
        ('equipos.codigo_formato_verificacion', '', 'Código del formato de verificación del día (p. ej. F-MAN-002)'),
        ('equipos.codigo_formato_etiqueta', '', 'Código del formato de la etiqueta de estado del equipo'),
        ('equipos.codigo_formato_ot', '', 'Código del formato de la orden de trabajo'),
    ],
    alertas=ganchos.alertas,
    kpis=ganchos.kpis,
    pendientes=ganchos.pendientes,
    accesos_planta=ganchos.accesos_planta,
    verificaciones_liberacion=ganchos.verificaciones_liberacion,
    enlaces={'equipo': ganchos.url_equipo, 'verificar_equipo': ganchos.url_verificar,
             'reportar_falla': ganchos.url_reportar_falla},
    consultas={'calibracion_vigente': ganchos.calibracion_vigente},
    datos_humo=ganchos.datos_humo,
)
