"""Helpers de vistas HTMX (movidos de erp.py)."""
from flask import request, render_template, make_response, redirect


def render_page(template, **context):
    """Renderiza la página completa, o solo el #content si la petición viene de HTMX."""
    layout = '_partial.html' if request.headers.get('HX-Request') else 'base.html'
    return render_template(template, _layout=layout, **context)


def hx_redirect(url):
    """Redirección compatible con HTMX (HX-Redirect) y con carga normal (302)."""
    if request.headers.get('HX-Request'):
        resp = make_response('')
        resp.headers['HX-Redirect'] = url
        return resp
    return redirect(url)


def full_page(template, **context):
    """Páginas STANDALONE (documento HTML completo: login, responsiva, impresión,
    ejecutar receta, planta). Si llegan vía HTMX (un enlace boosteado por error), se
    fuerza una navegación real para NO inyectar un <html> completo dentro de #content."""
    if request.headers.get('HX-Request'):
        destino = request.full_path
        if destino.endswith('?'):
            destino = destino[:-1]
        resp = make_response('')
        resp.headers['HX-Redirect'] = destino
        return resp
    return render_template(template, **context)
