"""Punto de entrada para Gunicorn (producción):

    gunicorn -w 1 --threads 8 -b 0.0.0.0:5001 wsgi:app

Crea/actualiza las tablas antes de atender peticiones (con `python erp.py` esto ya
ocurre en el arranque; con Gunicorn no, porque no se ejecuta el bloque __main__).
Se recomienda 1 worker con varios hilos: el anti-fuerza-bruta del login y la caché
viven en la memoria del proceso.
"""
from erp import app, inicializar_bd

inicializar_bd()
