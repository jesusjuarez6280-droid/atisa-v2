"""Reglas del módulo de calidad, sin Flask: cada función recibe la conexión abierta y NO hace
commit (quien llama confirma o revierte todo, firma incluida). Los errores se lanzan como
ErrorCalidad (o los de la base: FirmaInvalida, ErrorInventario, ErrorEquipo, ErrorArchivo)
con un mensaje listo para mostrarse.

La firma llega como dict {'numero_empleado', 'pin'} (y 'sesion' cuando se llama fuera de
una petición, como en las pruebas)."""
from ._comun import ErrorCalidad  # noqa: F401
