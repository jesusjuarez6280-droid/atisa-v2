# Cómo se agrega un módulo al ERP

Cada módulo nuevo (almacén, órdenes, calidad, desviaciones, equipos…) vive en su
carpeta `modulos/<clave>/` y **no toca `erp.py` ni ningún archivo existente**. La base
común (`nucleo/`) lo descubre sola al arrancar. Diseño completo:
`00-base-comun.md` (secciones 3.2, 3.3, 7 y 8).

## 1. Carpeta

```
modulos/<clave>/
├── __init__.py      # MODULO = Modulo(...)   ← lo único que la base lee
├── esquema.py       # MIGRACIONES = [(1, crear_tablas), ...]
├── rutas.py         # bp = Blueprint('<clave>', __name__, url_prefix='/<clave>', template_folder='templates')
├── servicios.py     # reglas de negocio sin Flask (se prueban solas)
├── ganchos.py       # alertas, pendientes, datos_humo…
└── templates/<clave>/*.html
tests/test_<clave>_*.py
```

## 2. `__init__.py`

```python
from nucleo.registro import Modulo
from .rutas import bp
from .esquema import MIGRACIONES
from . import ganchos

MODULO = Modulo(
    clave='almacen',                     # = nombre de la carpeta
    nombre='Almacén',
    orden=10,                            # orden en el menú dentro del departamento
    blueprint=bp,
    permisos={'almacen': ['admin', 'almacen', 'calidad', 'produccion'],
              'almacen_operar': ['admin', 'almacen']},
    menu=[('logistica', '/almacen', 'Existencias', 'ri-archive-2-line', '/almacen/', 'almacen')],
    inicio={'almacen': 'almacen.existencias'},   # pantalla de arranque del rol
    migraciones=MIGRACIONES,
    parametros=[('almacen.dias_alerta_caducidad', '90', 'Días antes de caducar para avisar')],
    alertas=ganchos.alertas, kpis=ganchos.kpis, pendientes=ganchos.pendientes,
    accesos_planta=None, verificaciones_liberacion=None, secciones_lote=ganchos.seccion_lote,
    enlaces={'ficha_lote': ganchos.url_ficha_lote}, consultas={},
    datos_humo=ganchos.datos_humo,
)
```

- Departamentos de menú: `general`, `rh`, `produccion`, `calidad`, `logistica` (Almacén y Logística), `administracion`, `comercial`, `id`, `farmacovigilancia`.
- Roles: `admin`, `rh`, `operador`, `almacen`, `produccion`, `calidad`, `mantenimiento` (`nucleo.auth.ROLES`).
- Facultades de firma: `emitir_ordenes`, `aprobar_maestros`, `liberar_lotes`, `autorizar_ajustes`, `gestionar_desviaciones`, `dictaminar_equipos`.
- Un permiso, enlace, consulta, ruta de menú o pantalla de arranque repetidos son **error al arrancar**.
- `ATISA_MODULOS_APAGADOS=calidad,equipos` apaga módulos sin borrar código.

## 3. Rutas

```python
from nucleo.auth import login_required, requiere_modulo, requiere_facultad
from nucleo.db import get_db_connection
from nucleo.vistas import render_page, full_page, hx_redirect

@bp.route('/liberar/<int:lote_id>', methods=['POST'])
@login_required
@requiere_modulo('calidad')
@requiere_facultad('liberar_lotes')     # solo en firmas críticas
def liberar(lote_id): ...
```

Toda ruta lleva `@login_required` + `@requiere_modulo(...)`. Si el id no existe: 404 o
regreso con aviso, nunca 500. Listas paginadas (50). Errores con
`current_app.logger.exception(...)` y aviso claro; nunca `except: pass`.

## 4. Servicios de la base (tablas compartidas)

En `materiales`, `proveedores`, `lotes`, `lote_movimientos`, `lote_historial`, `equipos`,
`firmas`, `folios`, `archivos` y `parametros` se escribe **solo** con estos servicios
(leer con SQL directo sí se vale). Reciben `conn`, no hacen commit y lanzan una excepción
con mensaje listo para `flash()`:

| Módulo | Funciones |
|---|---|
| `nucleo.inventario` | `crear_material`, `editar_material`, `crear_proveedor`, `editar_proveedor`, `crear_lote`, `mover`, `lote_disponible`, `sugerir_lotes`, `cambiar_estatus`, `corregir_lote`, `lote`, `existencia_material`, `trazabilidad` · `ErrorInventario` |
| `nucleo.equipos` | `crear_equipo`, `editar_equipo`, `equipo_apto`, `actualizar_vencimientos` · `ErrorEquipo` |
| `nucleo.firmas` | `firmar`, `fijar_pin`, `borrar_pin`, `firma`, `firma_tiene_facultad` · `FirmaInvalida`, `SIGNIFICADOS` |
| `nucleo.folios` | `siguiente_folio(conn, 'OP')` → `OP-2026-0001`, `numero_lote(conn, formato, TIPO=, COD=)` |
| `nucleo.archivos` | `guardar_archivo`, `archivos_de` (se sirven en `/archivo/<id>`) |
| `nucleo.parametros` | `parametro`, `parametro_int` |

Patrón de una ruta que firma:

```python
conn = get_db_connection()
try:
    fid = firmas.firmar(conn, numero_empleado=request.form['firma_numero'], pin=request.form['firma_pin'],
                        significado='Liberó', modulo='calidad', entidad='lotes', entidad_id=lote_id,
                        detalle=f'Liberó lote {n}', facultad='liberar_lotes', critica=True)
    inventario.cambiar_estatus(conn, lote_id=lote_id, nuevo='APROBADO', usuario=session['nombre'],
                               motivo='Análisis conforme', firma_id=fid, folio=folio)
    conn.commit()
except (firmas.FirmaInvalida, inventario.ErrorInventario) as e:
    conn.rollback()
    flash(str(e), 'error')
```

Firma primero y después las demás escrituras. El PIN nunca se escribe en la bitácora.

## 5. Ganchos (sección 8 del diseño)

| Gancho | Firma | Dónde aparece |
|---|---|---|
| `alertas` | `fn(conn) -> [{tipo, icono, nivel, nombre, area, detalle, url}]` | Tablero → Alertas |
| `kpis` | `fn(conn) -> [{titulo, valor, detalle, nivel, url}]` | Tablero → Operación |
| `pendientes` | `fn(conn, usuario) -> [{texto, url, nivel, fecha}]` | `/pendientes` y Dashboard |
| `accesos_planta` | `fn(conn, usuario) -> [{titulo, icono, url, contador}]` | `/planta` |
| `verificaciones_liberacion` | `fn(conn, lote_id) -> ['motivo', ...]` | Calidad, antes de liberar |
| `secciones_lote` | `fn(conn, lote_id) -> {titulo, plantilla, contexto} \| None` | Ficha del lote |
| `enlaces` | `{'nombre': fn(**datos) -> url}` | Botones entre módulos |
| `consultas` | `{'nombre': fn(conn, ...) -> datos}` | Datos de otro módulo |
| `datos_humo` | `fn(conn) -> {'param_url': valor}` | Solo pruebas de humo |

`usuario = {numero_empleado, nombre, username, roles, facultades}`. Para usar lo de otro
módulo: `nucleo.registro.enlace('ficha_lote', lote_id=3)` (o `enlace(...)` en plantillas)
y `registro.consulta('lotes_por_equipo', conn, equipo_id, desde, default=[])`; si ese
módulo no está instalado devuelven `None` / el default y el botón simplemente no se pinta.
Para el resto: `registro.verificaciones_liberacion(conn, lote_id)`, `registro.secciones_lote(conn, lote_id)`.

## 6. Plantillas

- Extienden `{% extends _layout|default("base.html") %}` y se pintan con `render_page`.
- Pantallas de tablet: documento completo con `full_page`, su propio `<meta name="csrf-token">`
  y `{% include 'nucleo/_utilidades.html' %}` (ver `nucleo/templates/nucleo/planta.html`).
- Componentes: `{% from 'nucleo/_firma.html' import campo_firma, firmado %}`,
  `{% from 'nucleo/_lote.html' import chip_estatus, caducidad %}` y
  `{% extends 'nucleo/_impresion.html' %}` para hojas carta.
- En plantillas hay `puede('permiso')`, `tiene_facultad('liberar_lotes')`, `enlace(...)` y `dias_hasta(fecha)`.
- Tailwind está precompilado: solo clases que existan en `static/css/tailwind.css` o en
  `nucleo/_utilidades.html`. Si falta una, va en un `<style>` de la plantilla del módulo.

## 7. Pruebas

- `tests/test_<clave>_*.py`, con el fixture `db` y `client` de `conftest.py` (base temporal).
- `tests/fabricas.py`: `crear_usuario(db, 'x', rol='calidad', facultades='liberar_lotes', pin='2580')`,
  `crear_material`, `crear_lote`, `crear_equipo`, `firma(db, numero, pin, 'Liberó', facultad=...)`, `login`.
- `tests/test_humo.py` recorre solo todas las rutas del módulo con cada rol usando `datos_humo`.
- Ejemplo mínimo completo de un módulo registrado: `tests/modulo_prueba/`.
- Corre `python -m pytest -q tests` antes de subir.
