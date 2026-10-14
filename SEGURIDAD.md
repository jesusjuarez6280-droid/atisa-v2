# 🛡️ Seguridad y Calidad — Sistema ATISA

Documento de seguridad, blindaje y aseguramiento de calidad (QA) del ERP/Intranet de ATISA.
Describe cómo se protege la aplicación, cómo se probó, qué defectos se corrigieron y qué
pasos seguir para un despliegue en producción.

> ⚠️ **Datos de demostración:** la base incluida (`base_planta.db`) contiene únicamente datos
> ficticios de prueba. No hay información real de personas.

---

## 1. Modelo de seguridad

La aplicación aplica defensa en capas. Cada petición pasa por varios controles:

| Control | Cómo funciona |
|---|---|
| **Autenticación** | Login con usuario/contraseña; contraseñas guardadas con *hash* (Werkzeug, `pbkdf2`), nunca en texto plano. |
| **RBAC (roles y permisos)** | Matriz única `PERMISOS` + decoradores `@login_required`, `@requiere_modulo`, `@requiere_rol`. Cada ruta valida el rol antes de ejecutar. |
| **Re-validación de sesión** | En **cada** request se revalida el usuario contra la BD: si lo dieron de baja o le cambiaron el rol, se aplica al instante (no hay rol "congelado"). |
| **Anti-escalada de privilegios** | Un no-admin no puede crear/editar/eliminar cuentas admin ni auto-asignarse el rol admin; no se puede dejar al sistema sin ningún administrador activo. |
| **CSRF** | Flask-WTF protege todos los POST; el token se inyecta automáticamente vía HTMX y explícitamente en formularios de subida de archivos. |
| **XSS** | Autoescape de Jinja2 en todas las plantillas; los archivos servidos llevan `X-Content-Type-Options: nosniff`. |
| **Inyección SQL** | 100% de las consultas usan parámetros (`?`); los nombres de columna en updates dinámicos salen de listas **fijas en código**, no del usuario. |
| **Redirecciones seguras** | Los parámetros de "regreso" solo aceptan rutas internas de la app (se bloquean `//host`, `/\host` y URLs externas → sin *open-redirect*). |
| **Integridad referencial** | SQLite con `PRAGMA foreign_keys = ON`: no se pueden crear registros huérfanos (candidato sin vacante, KPI sin puesto, evaluación sin empleado, etc.). |
| **Subida de archivos** | Solo `pdf/jpg/jpeg/png`; nombres saneados con `secure_filename` (neutraliza *path traversal* tipo `../../`); se rechazan `.exe/.html/.svg/.js`. |
| **Auditoría** | Se registran automáticamente los inicios/cierres de sesión y toda modificación de datos (quién, qué ruta, cuándo). |

---

## 2. Metodología de QA

Todas las pruebas se corrieron de forma **estrictamente controlada**:

1. **Respaldo** de la base antes de cada batería (`base_planta.db` + archivos WAL/SHM, con *checkpoint*).
2. **Pruebas adversariales** ("historias retorcidas"): entradas basura, valores fuera de rango,
   IDs inexistentes, intentos de escalada de privilegios, XSS, *path traversal*, doble envío.
3. **Restauración** de la base al terminar → **cero contaminación** (se verifica que no quede
   ningún dato de prueba).

**Cobertura:** 5 rondas, ~100 pruebas automatizadas, sobre **todos** los módulos (nuevos y viejos).
Cada corrida termina en verde y con la base idéntica a como empezó.

---

## 3. Defectos encontrados y corregidos

Durante el QA se encontraron y corrigieron **10 defectos reales** (probados antes y después del arreglo):

### Módulos nuevos
1. **Doble registro en la bitácora de auditoría al iniciar sesión** — el login se guardaba dos veces (como "acceso" y como "cambio"). Corregido.
2. **Nivel de madurez del Cardex incorrecto** — los colaboradores fuera de Producción salían siempre como "Nuevo (1/5)" aunque fueran veteranos. Ahora, si no hay competencias evaluadas, el nivel se calcula por **antigüedad** (medible).

### Módulos operativos (activos, gasolina)
3. **🔴 Doble asignación de un activo a dos personas** — `editar_activo` permitía marcar "Disponible" un activo que seguía asignado, abriendo una puerta trasera para prestarlo dos veces. Corregido: no se puede liberar un activo con asignación vigente.
4. **`editar_activo` sin validación** — aceptaba estatus inventados y borraba categoría/descripción con campos vacíos. Corregido.
5. **`devolver_activo` desincronizaba el inventario** — re-devolver una asignación ya cerrada liberaba un activo que estaba re-asignado a otra persona. Corregido.
6. **`editar_tarjeta` sin validación** — aceptaba estatus inventados y saldo negativo. Corregido.
7. **`registrar_carga` a tarjeta inexistente** — creaba una bitácora huérfana y decía "registrado correctamente" sin descontar nada. Corregido: valida que la tarjeta exista.

### Seguridad web
8. **Open-redirect en la subida de documentos** — el parámetro de regreso se validaba con `startswith('/')`, lo que **no** bloqueaba URLs "protocolo-relativas" (`//sitio-externo.com`) ni el truco del backslash (`/\sitio-externo.com`): el navegador terminaba en un sitio externo. Corregido con un validador que normaliza los backslashes y solo permite rutas internas de la propia app.

*(Los defectos 9–11 corresponden a sub-casos verificados de los anteriores —estatus basura, campos vacíos y desincronización de inventario—, cada uno con su prueba dedicada.)*

### Robustez (no es de seguridad)
- **Recetas:** escribir texto en el campo "minutos" de un paso tumbaba **toda** la receta. Ahora un valor inválido se convierte a 0 y la receta se guarda normal.

---

## 4. Blindaje de despliegue

Se aplicaron **6 medidas** de endurecimiento pensando en un despliegue real:

| # | Medida | Detalle |
|---|---|---|
| 1 | **Llave secreta fuera del código** | Antes estaba escrita en el código (y el repo es público → cualquiera podía falsificar cookies). Ahora se lee de la variable de entorno `ATISA_SECRET_KEY`. |
| 2 | **Cookies de sesión endurecidas** | `HttpOnly` (no legibles por JavaScript), `SameSite=Lax` (no viajan a sitios externos), `Secure` activable por HTTPS. |
| 3 | **Expiración de sesión** | La sesión caduca a las **8 horas**. |
| 4 | **Política de contraseña** | Mínimo **8 caracteres** al crear o cambiar contraseñas (no afecta cuentas existentes). |
| 5 | **Anti-fuerza-bruta en login** | Tras **5 intentos fallidos** se bloquea 10 minutos esa combinación IP+usuario. |
| 6 | **Modo debug configurable** | `debug=True` ya no está fijo (el depurador de Werkzeug permite ejecución de código). Se controla con `ATISA_DEBUG`. |

---

## 5. Checklist de producción

**Seguro por defecto (oct 2026):** arrancar con `INICIAR_ERP.bat` / `python erp.py` ya es seguro
sin configurar nada:

- **Llave de sesión:** si no defines `ATISA_SECRET_KEY`, el ERP genera una llave aleatoria en el
  primer arranque y la guarda en `.secret_key` junto a la base (archivo ignorado por git). Ya no
  existe una llave fija en el código que cualquiera pueda leer en el repo público.
- **Modo debug apagado:** el depurador de Werkzeug solo se enciende con `ATISA_DEBUG=true`
  (úsalo solo en tu PC de desarrollo, nunca en el servidor de planta).
- **Contraseña inicial:** quien entre con la contraseña de fábrica del admin (`admin123`) queda
  obligado a cambiarla antes de usar el sistema (`/cambiar_password`).
- **Archivos privados:** expedientes, contratos y documentos legales ya no se pueden abrir por
  `/static/...`; solo por las rutas que piden sesión y permiso.
- **Excel de Reportes:** exporta solo datos laborales (nunca contraseñas, CURP, RFC, NSS, salario
  ni CLABE).
- **Respaldo automático diario:** la primera petición de cada día copia la base a `respaldos/`
  (se conservan 30 días). Para tener copia fuera del sitio, sincroniza esa carpeta a USB/NAS/nube.

Variables opcionales:

```bash
export ATISA_SECRET_KEY="..."   # llave propia (si no, se usa la de .secret_key)
export ATISA_HTTPS=true         # cookie 'Secure' (solo si sirves por HTTPS)
export ATISA_DEBUG=true         # SOLO en desarrollo
export ATISA_DB=ruta/base.db    # otra ubicación de la base
export ATISA_RESPALDOS=ruta     # otra carpeta de respaldos
```

En Windows (PowerShell) sería `$env:ATISA_SECRET_KEY = "..."`, etc.

> 💡 La primera vez que corras esta versión **se cierran las sesiones anteriores** (cambia la
> llave): hay que volver a iniciar sesión una vez. Es normal.

**Con Gunicorn:** `gunicorn -w 1 --threads 8 -b 0.0.0.0:5001 wsgi:app` (`wsgi.py` crea las tablas
al arrancar). Un solo worker con hilos, porque el anti-fuerza-bruta y la caché viven en memoria.

**Pruebas automáticas:** `pip install pytest` y luego `python -m pytest tests` (usan una base
temporal, nunca tocan `base_planta.db`).

**Recomendaciones adicionales para producción** (fuera del código de la app):
- Servir detrás de un proxy con **HTTPS** (Nginx/Caddy).
- Restringir el acceso a la red local o VPN de la empresa.
- No subir nunca a GitHub una `base_planta.db` con datos reales (el repo es público).

---

## 6. Limitaciones conocidas (transparencia)

Cosas que **no** son defectos, pero conviene tener claras:

- **Bitácora de auditoría:** registra todo POST exitoso como "modificación"; si una validación
  falla y no cambia nada, igual queda como intento. Sirve como registro de actividad.
- **Anti-fuerza-bruta:** el conteo vive en memoria (por proceso) y usa la IP del cliente; detrás
  de un proxy inverso podría compartir IP entre usuarios. Suficiente para un despliegue local.
- **Integraciones externas** del pliego original (IMSS Digital, SAT/CFDI, reloj biométrico en vivo,
  2FA/SSO, respaldo automático fuera del sitio) **no son código de esta app**: requieren cuentas
  y servicios de terceros. Quedan documentadas como hoja de ruta.

---

## 7. Resumen

| Métrica | Valor |
|---|---|
| Rondas de QA | 6 |
| Pruebas automatizadas | ~120 (todas en verde) |
| Defectos reales corregidos | 11 |
| Blindajes de despliegue | 6 |
| Fixes de robustez | 1 |
| Contaminación de datos tras las pruebas | 0 (respaldo/restauración) |

El sistema fue auditado de punta a punta: cada módulo —nuevo y viejo— se sometió a pruebas
adversariales, se corrigieron los defectos encontrados y se endureció la configuración para un
despliegue seguro.
