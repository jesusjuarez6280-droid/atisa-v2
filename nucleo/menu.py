"""Menú lateral por departamentos y pantalla de arranque (movido de erp.py).

Los módulos de modulos/ agregan sus entradas al registrarse (nucleo.registro):
la entrada se guarda como tupla de 4 (ruta, texto, icono, match) para que
base.html no cambie, y su permiso va aparte en PERMISO_DE_RUTA.
"""
from nucleo.auth import PERMISOS, roles_sesion

# --- Navegación por departamentos (organigrama ATISA) ---
# Cada módulo apunta a una ruta que YA existe; 'match' resalta el módulo activo
# también en sus subrutas (ej. /asistencias sigue resaltado dentro de /reporte_individual).
# Los departamentos con 'modulos' vacío son el plan a futuro (hoja de ruta): solo
# el admin los ve, marcados como "Próximamente", hasta que se construyan.
# Para mover un módulo de departamento, basta con cambiarlo de lista aquí.
NAV_DEPARTAMENTOS = [
    {'key': 'general', 'nombre': 'General', 'icono': 'ri-home-5-line', 'modulos': [
        ('/dashboard',   'Dashboard',        'ri-dashboard-3-line',    ''),
        ('/tablero',     'Tablero Gerencial','ri-line-chart-line',     ''),
        ('/organigrama', 'Organigrama',      'ri-organization-chart',  ''),
        ('/avisos',      'Avisos',           'ri-megaphone-line',      ''),
    ]},
    {'key': 'rh', 'nombre': 'Recursos Humanos', 'icono': 'ri-group-line', 'modulos': [
        ('/empleados',    'Empleados',        'ri-id-card-line',        ''),
        ('/incidencias',  'Incidencias',      'ri-calendar-close-line', ''),
        ('/asistencias',  'Asistencias',      'ri-time-line',           '/reporte_individual'),
        ('/capacitacion', 'Capacitación',     'ri-graduation-cap-line', ''),
        ('/expertise',    'Matriz Expertise', 'ri-medal-line',          ''),
        ('/perfiles',     'Perfiles de Puesto','ri-profile-line',       ''),
        ('/reclutamiento','Reclutamiento',    'ri-user-search-line',    ''),
        ('/cardex',       'Cardex & Madurez', 'ri-profile-line',        ''),
        ('/bpd',          'Desempeño BPD',    'ri-speed-up-line',       ''),
        ('/reportes',     'Reportes',         'ri-bar-chart-2-line',    ''),
    ]},
    {'key': 'produccion', 'nombre': 'Producción', 'icono': 'ri-flask-line', 'modulos': [
        ('/produccion', 'Producción', 'ri-medicine-bottle-line', ''),
        ('/recetas',    'Procesos',   'ri-list-check-3',         ''),
    ]},
    {'key': 'calidad', 'nombre': 'Calidad', 'icono': 'ri-shield-check-line', 'modulos': [
        ('/cinco_s',    'Auditorías 5S',      'ri-sparkling-2-line',  ''),
        ('/limpiezas',  'Rol Limpiezas',      'ri-brush-line',        ''),
        ('/documentos', 'Documentos BPM',     'ri-folder-open-line',  ''),
        ('/normativa',  'Documentos Legales', 'ri-scales-3-line',     '/normativa/bpm'),
        ('/activos',    'Activos y EPP',      'ri-shield-check-line', ''),
    ]},
    {'key': 'logistica', 'nombre': 'Almacén y Logística', 'icono': 'ri-truck-line', 'modulos': [
        ('/gasolina', 'Tarjeta Gasolina', 'ri-gas-station-fill', ''),
    ]},
    {'key': 'administracion', 'nombre': 'Administración', 'icono': 'ri-building-line', 'modulos': [
        ('/auditoria',     'Auditoría y Catálogos', 'ri-file-shield-2-line', ''),
        ('/configuracion', 'Configuración',         'ri-settings-4-line',    ''),
    ]},
    {'key': 'comercial',         'nombre': 'Comercial',                  'icono': 'ri-line-chart-line',   'modulos': []},
    {'key': 'id',                'nombre': 'Investigación y Desarrollo', 'icono': 'ri-microscope-line',   'modulos': []},
    {'key': 'farmacovigilancia', 'nombre': 'Farmacovigilancia',         'icono': 'ri-heart-pulse-line',  'modulos': []},
]

# Permiso de cada ruta que agregó un módulo (las rutas viejas usan la ruta sin '/').
PERMISO_DE_RUTA = {}

# Pantalla de arranque por rol que registran los módulos: {'almacen': 'almacen.existencias'}.
# El operador arranca en la pantalla de planta.
INICIOS = {'operador': 'nucleo.planta'}


def permiso_de_ruta(ruta):
    return PERMISO_DE_RUTA.get(ruta, ruta[1:])


def construir_nav(roles):
    """Arma el árbol de navegación visible para los roles en sesión (acepta un rol
    suelto o la lista). Filtra cada módulo por PERMISOS y oculta departamentos sin
    módulos accesibles (los departamentos 'a futuro' solo los ve el admin)."""
    if isinstance(roles, str) or roles is None:
        roles = [roles] if roles else []
    roles = set(roles)
    arbol = []
    for d in NAV_DEPARTAMENTOS:
        mods = [m for m in d['modulos'] if roles & set(PERMISOS.get(permiso_de_ruta(m[0]), []))]
        es_roadmap = len(d['modulos']) == 0
        if mods or (es_roadmap and 'admin' in roles):
            arbol.append({**d, 'modulos_visibles': mods, 'es_roadmap': es_roadmap})
    return arbol


def landing_endpoint():
    """Primer módulo al que la sesión SÍ tiene acceso.
    Evita el bucle de redirección: nunca devuelve un destino que luego deniegue."""
    roles = roles_sesion()
    # 1. La pantalla de arranque que registró un módulo para el rol principal o un área.
    for rol in roles:
        endpoint = INICIOS.get(rol)
        if endpoint:
            return endpoint
    # 2. El primer módulo de siempre al que tenga acceso.
    prioridad = ['dashboard', 'recetas', 'empleados', 'documentos', 'reportes',
                 'asistencias', 'activos', 'gasolina', 'cinco_s', 'produccion', 'configuracion']
    for modulo in prioridad:
        if set(roles) & set(PERMISOS.get(modulo, [])):
            return modulo
    # 3. Mis pendientes (todos los roles la ven).
    if roles:
        return 'nucleo.pendientes'
    return 'sin_acceso'   # rol sin ningún módulo: pantalla estática (no se atora)
