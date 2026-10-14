"""Registro de módulos: el "enchufe" para que cada módulo viva en su carpeta.

Cada paquete de modulos/ expone en su __init__.py un objeto MODULO = Modulo(...).
cargar_modulos(app) (lo llama erp.py al final, ya con todas sus rutas definidas)
descubre los paquetes, registra el blueprint, suma permisos, agrega las entradas
al menú y guarda los ganchos. ATISA_MODULOS_APAGADOS=calidad,equipos apaga módulos
sin borrar código.

Ganchos (todos opcionales; ver 00-base-comun.md sección 8):
  alertas(conn)                    -> [{tipo, icono, nivel, nombre, area, detalle, url}]
  kpis(conn)                       -> [{titulo, valor, detalle, nivel, url}]
  pendientes(conn, usuario)        -> [{texto, url, nivel, fecha}]
  accesos_planta(conn, usuario)    -> [{titulo, icono, url, contador}]
  verificaciones_liberacion(conn, lote_id) -> ['motivo que impide liberar', ...]
  secciones_lote(conn, lote_id)    -> {titulo, plantilla, contexto} | None
  enlaces   = {'nombre': fn(**datos) -> url}
  consultas = {'nombre': fn(conn, ...) -> datos}
  datos_humo(conn) -> {'param_url': valor}   (solo pruebas de humo)
usuario = {numero_empleado, nombre, username, roles, facultades}
"""
import importlib
import os
import pkgutil
from dataclasses import dataclass, field
from typing import Callable, Optional

from flask import current_app, has_app_context

from nucleo.auth import PERMISOS, ROLES
from nucleo.menu import NAV_DEPARTAMENTOS, PERMISO_DE_RUTA, INICIOS
from nucleo.parametros import registrar_parametros


class ErrorRegistro(Exception):
    """Un módulo está mal declarado: se avisa al arrancar, no en planta."""


@dataclass
class Modulo:
    clave: str                       # = nombre de la carpeta; prefijo de sus parámetros
    nombre: str
    blueprint: object = None         # Blueprint('<clave>', __name__, url_prefix='/<clave>', template_folder='templates')
    orden: int = 50                  # orden en el menú dentro del departamento
    permisos: dict = field(default_factory=dict)   # {'almacen': ['admin', 'almacen'], ...}
    menu: list = field(default_factory=list)       # [(departamento, ruta, texto, icono, match, permiso)]
    inicio: dict = field(default_factory=dict)     # {'rol': 'endpoint'} pantalla de arranque
    migraciones: list = field(default_factory=list)  # [(1, fn(conn)), (2, fn(conn)), ...]
    parametros: list = field(default_factory=list)   # [('clave.nombre', 'default', 'descripción')]
    alertas: Optional[Callable] = None
    kpis: Optional[Callable] = None
    pendientes: Optional[Callable] = None
    accesos_planta: Optional[Callable] = None
    verificaciones_liberacion: Optional[Callable] = None
    secciones_lote: Optional[Callable] = None
    enlaces: dict = field(default_factory=dict)
    consultas: dict = field(default_factory=dict)
    datos_humo: Optional[Callable] = None


# clave -> Modulo, en orden de registro
REGISTRO = {}
# nombre de enlace/consulta -> (clave del módulo, función)
ENLACES = {}
CONSULTAS = {}


def modulos_activos():
    return list(REGISTRO.values())


def modulo_instalado(clave):
    return clave in REGISTRO


def _apagados():
    return {x.strip() for x in os.environ.get('ATISA_MODULOS_APAGADOS', '').split(',') if x.strip()}


def registrar_modulo(app, m):
    """Valida y registra UN módulo. Cualquier choque es error al arrancar."""
    if not isinstance(m, Modulo):
        raise ErrorRegistro('MODULO debe ser un nucleo.registro.Modulo')
    if m.clave in REGISTRO or m.clave == 'nucleo':
        raise ErrorRegistro(f"El módulo '{m.clave}' ya está registrado")
    for permiso, roles in m.permisos.items():
        if permiso in PERMISOS:
            raise ErrorRegistro(f"{m.clave}: el permiso '{permiso}' ya existe")
        desconocidos = set(roles) - set(ROLES)
        if desconocidos:
            raise ErrorRegistro(f"{m.clave}: roles desconocidos en '{permiso}': {sorted(desconocidos)}")
    deptos = {d['key']: d for d in NAV_DEPARTAMENTOS}
    for entrada in m.menu:
        if len(entrada) != 6:
            raise ErrorRegistro(f'{m.clave}: cada entrada de menú lleva 6 datos '
                                '(departamento, ruta, texto, icono, match, permiso)')
        depto, ruta, _, _, _, permiso = entrada
        if depto not in deptos:
            raise ErrorRegistro(f"{m.clave}: departamento de menú desconocido '{depto}'")
        if permiso not in m.permisos and permiso not in PERMISOS:
            raise ErrorRegistro(f"{m.clave}: el menú usa el permiso '{permiso}' que nadie declaró")
        if ruta in PERMISO_DE_RUTA or any(x[0] == ruta for d in NAV_DEPARTAMENTOS for x in d['modulos']):
            raise ErrorRegistro(f"{m.clave}: la ruta de menú '{ruta}' ya existe")
    for nombre in m.enlaces:
        if nombre in ENLACES:
            raise ErrorRegistro(f"{m.clave}: el enlace '{nombre}' ya lo ofrece '{ENLACES[nombre][0]}'")
    for nombre in m.consultas:
        if nombre in CONSULTAS:
            raise ErrorRegistro(f"{m.clave}: la consulta '{nombre}' ya la ofrece '{CONSULTAS[nombre][0]}'")
    for rol in m.inicio:
        if rol in INICIOS and not (rol == 'operador' and INICIOS[rol] == 'nucleo.planta'):
            raise ErrorRegistro(f"{m.clave}: la pantalla de arranque del rol '{rol}' ya la puso otro módulo")
    registrar_parametros(m.clave, m.parametros)

    # Todo validado: se aplica.
    if m.blueprint is not None:
        app.register_blueprint(m.blueprint)
    PERMISOS.update({k: list(v) for k, v in m.permisos.items()})
    for depto, ruta, texto, icono, match, permiso in m.menu:
        d = deptos[depto]
        d.setdefault('_orden', {})
        d['_orden'][ruta] = m.orden
        d['modulos'].append((ruta, texto, icono, match or ''))
        # Las entradas de módulos van después de las de siempre, por 'orden'.
        d['modulos'].sort(key=lambda x: d['_orden'].get(x[0], -1))
        PERMISO_DE_RUTA[ruta] = permiso
    for rol, endpoint in m.inicio.items():
        if rol != 'operador':          # el operador siempre arranca en /planta
            INICIOS[rol] = endpoint
    for nombre, fn in m.enlaces.items():
        ENLACES[nombre] = (m.clave, fn)
    for nombre, fn in m.consultas.items():
        CONSULTAS[nombre] = (m.clave, fn)
    REGISTRO[m.clave] = m
    return m


def cargar_modulos(app, paquete='modulos'):
    """Descubre e importa los paquetes de `paquete`, y los extra de ATISA_MODULOS_EXTRA
    (nombres de paquete separados por coma; lo usan las pruebas)."""
    apagados = _apagados()
    nombres = []
    try:
        base = importlib.import_module(paquete)
        nombres = [f'{paquete}.{n}' for _, n, es_paquete in pkgutil.iter_modules(base.__path__) if es_paquete]
    except ImportError:
        nombres = []
    nombres += [x.strip() for x in os.environ.get('ATISA_MODULOS_EXTRA', '').split(',') if x.strip()]
    for nombre in sorted(nombres, key=lambda n: n.rsplit('.', 1)[-1]):
        if nombre.rsplit('.', 1)[-1] in apagados:
            continue
        mod = importlib.import_module(nombre)
        m = getattr(mod, 'MODULO', None)
        if m is None:
            raise ErrorRegistro(f'{nombre}: falta MODULO en su __init__.py')
        if m.clave in apagados:
            continue
        registrar_modulo(app, m)
    return modulos_activos()


# === Uso de los ganchos =====================================================

def _log_fallo(m, gancho):
    if has_app_context():
        current_app.logger.exception('Falló el gancho %s del módulo %s', gancho, m.clave)


def recolectar(gancho, *args):
    """Llama el gancho en cada módulo que lo tenga y junta las listas. Si un módulo
    truena, se registra en el log y los demás siguen (una alerta rota no tumba el Tablero)."""
    juntos = []
    for m in modulos_activos():
        fn = getattr(m, gancho)
        if not fn:
            continue
        try:
            juntos.extend(fn(*args) or [])
        except Exception:
            _log_fallo(m, gancho)
    return juntos


def recolectar_por_modulo(gancho, *args):
    """[(modulo, resultado)] para pantallas que agrupan por módulo (Mis pendientes)."""
    grupos = []
    for m in modulos_activos():
        fn = getattr(m, gancho)
        if not fn:
            continue
        try:
            res = fn(*args) or []
        except Exception:
            _log_fallo(m, gancho)
            continue
        if res:
            grupos.append((m, res))
    return grupos


def verificaciones_liberacion(conn, lote_id):
    """Motivos que impiden liberar un lote. A diferencia de los demás ganchos, si un
    módulo truena se considera que IMPIDE liberar (más vale detener que liberar a ciegas)."""
    motivos = []
    for m in modulos_activos():
        if not m.verificaciones_liberacion:
            continue
        try:
            motivos.extend(m.verificaciones_liberacion(conn, lote_id) or [])
        except Exception:
            _log_fallo(m, 'verificaciones_liberacion')
            motivos.append(f'No se pudo revisar {m.nombre}; avisa a sistemas.')
    return motivos


def secciones_lote(conn, lote_id):
    secciones = []
    for m in modulos_activos():
        if not m.secciones_lote:
            continue
        try:
            s = m.secciones_lote(conn, lote_id)
        except Exception:
            _log_fallo(m, 'secciones_lote')
            continue
        if s:
            secciones.append({**s, 'modulo': m.clave})
    return secciones


def enlace(nombre, **datos):
    """URL que ofrece otro módulo, o None si no está instalado (el botón no se pinta)."""
    par = ENLACES.get(nombre)
    if not par:
        return None
    try:
        return par[1](**datos)
    except Exception:
        if has_app_context():
            current_app.logger.exception('Falló el enlace %s', nombre)
        return None


def hay_consulta(nombre):
    return nombre in CONSULTAS


def consulta(nombre, conn, *args, default=None, **kwargs):
    """Datos que ofrece otro módulo; `default` si no está instalado."""
    par = CONSULTAS.get(nombre)
    if not par:
        return default
    return par[1](conn, *args, **kwargs)
