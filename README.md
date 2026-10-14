# Sistema ATISA — ERP / Intranet

Sistema interno (ERP/Intranet) para **ATISA**, laboratorio fabricante de medicamentos veterinarios. Centraliza la operación de Recursos Humanos, Producción y Calidad en una sola aplicación web, pensada para funcionar **100% en red local, sin internet**, y cumplir con los requisitos de una auditoría **BPM (SENASICA)**.

> ⚠️ **Datos de demostración:** la base incluida (`base_planta.db`) contiene únicamente datos ficticios de prueba (usuarios "Pepe", "Pruebas", etc.). No hay información real.

---

## ✨ Lo que hace distinto a este proyecto

- **Sensación de app (SPA) sin frameworks pesados:** navegación sin recargas con **HTMX** (swaps parciales, barra de progreso, preload).
- **Funciona sin internet:** Tailwind, HTMX, iconos y fuentes servidos en local. Ideal para una planta sin conexión confiable.
- **Gráficas hechas a mano en SVG** (sin librerías externas): radar de competencias, sparklines de tendencia y barras de producción.
- **Autonomía / cálculo automático:**
  - Importa las checadas del reloj **ZKTeco** (Excel) y calcula retardos y horas extra.
  - Marca asistencias, faltas y descansos automáticamente en el calendario de incidencias.
  - Calcula **antigüedad y días de vacaciones** según la LFT (reforma 2023).
  - Genera el calendario de **días festivos oficiales de México** (LFT Art. 74).
- **Seguridad:** control de acceso por roles (RBAC), protección **CSRF** (Flask-WTF), re-validación de sesión contra BD en cada request, blindaje anti-escalada de privilegios, headers anti-XSS y guard anti doble-clic.
- **Documentos imprimibles** en HTML listo para imprimir o guardar como PDF desde el navegador (responsivas, constancias, acuses).

## 🧩 Módulos

Recursos Humanos · Empleados · Asistencias (ZKTeco) · Incidencias · Capacitación · Matriz de Expertise · Desempeño · Reportes · Auditorías 5S · Rol de Limpiezas · Producción · Activos y EPP (responsivas) · Tarjeta de Gasolina · Documentos · Avisos · Configuración.

## 🛠️ Stack técnico

| Capa | Tecnología |
|------|-----------|
| Backend | Python · **Flask** |
| Base de datos | **SQLite** (modo WAL, PRAGMAs de rendimiento, FK) |
| Frontend | **HTMX** · **Tailwind CSS** (precompilado) · Remix Icons |
| Seguridad | Flask-WTF (CSRF) · Werkzeug (hash de contraseñas) |
| Datos / archivos | pandas · openpyxl |
| Despliegue | Gunicorn (ver `requirements.txt`) |

## 🎨 Diseño

Identidad visual *"clínico y confiable"* documentada en [`DESIGN.md`](DESIGN.md) y [`PRODUCT.md`](PRODUCT.md): paleta, tipografía y componentes consistentes en todas las pantallas.

## 🛡️ Seguridad y QA

Modelo de seguridad, metodología de pruebas, defectos corregidos y checklist de producción en [`SEGURIDAD.md`](SEGURIDAD.md): RBAC, CSRF, anti-XSS/SQLi, blindaje de sesión y contraseñas, anti-fuerza-bruta y auditoría. Auditado con ~100 pruebas adversariales, con respaldo/restauración de la base.

## 🚀 Cómo correrlo

```bash
pip install -r requirements.txt
python erp.py
```

Abre **http://localhost:5001**. La base de demostración ya trae usuarios de prueba para explorar los módulos.

## 🧪 Pruebas

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests
```

Las pruebas usan una base temporal (nunca tocan `base_planta.db`) y corren solas en GitHub Actions en cada PR, en Windows y Linux. Las de humo (`tests/test_humo.py`) abren **todas las pantallas con cada rol**, así que al agregar un módulo nuevo:

- protege cada ruta con `@login_required` y `@requiere_modulo('<modulo>')`, y agrega el módulo a `PERMISOS`;
- si la ruta lleva un parámetro nuevo (ej. `<int:lote_id>`), agrega un registro de ejemplo en el fixture `datos` de `tests/test_humo.py`;
- en plantillas usa solo clases que ya existan en `static/css/tailwind.css` (está precompilado).

---

*Proyecto desarrollado por Jesús Armando Juárez Cabrera.*
