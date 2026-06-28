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
- **Generación de documentos** con ReportLab (responsivas, cartas, acuses imprimibles).

## 🧩 Módulos

Recursos Humanos · Empleados · Asistencias (ZKTeco) · Incidencias · Capacitación · Matriz de Expertise · Desempeño · Reportes · Auditorías 5S · Rol de Limpiezas · Producción · Activos y EPP (responsivas) · Tarjeta de Gasolina · Documentos · Avisos · Configuración.

## 🛠️ Stack técnico

| Capa | Tecnología |
|------|-----------|
| Backend | Python · **Flask** |
| Base de datos | **SQLite** (modo WAL, PRAGMAs de rendimiento, FK) |
| Frontend | **HTMX** · **Tailwind CSS** (precompilado) · Remix Icons |
| Seguridad | Flask-WTF (CSRF) · Werkzeug (hash de contraseñas) |
| Datos / archivos | pandas · openpyxl · ReportLab |
| Despliegue | Gunicorn (ver `requirements.txt`) |

## 🎨 Diseño

Identidad visual *"clínico y confiable"* documentada en [`DESIGN.md`](DESIGN.md) y [`PRODUCT.md`](PRODUCT.md): paleta, tipografía y componentes consistentes en todas las pantallas.

## 🚀 Cómo correrlo

```bash
pip install -r requirements.txt
python erp.py
```

Abre **http://localhost:5001**. La base de demostración ya trae usuarios de prueba para explorar los módulos.

---

*Proyecto desarrollado por Jesús Armando Juárez Cabrera.*
