# Rediseño ATISA — control de avance

Dirección: **clínico y confiable** (ver `DESIGN.md` y `PRODUCT.md`). Skills de diseño: `impeccable`, `frontend-design`.
Se trabaja **pantalla por pantalla**. Al terminar cada turno se marca aquí dónde quedó "el puntito".

## Base / fundación
- [x] Navegación por departamentos (riel + panel) — `base.html` + `erp.py`
- [x] `PRODUCT.md` y `DESIGN.md` (dirección y tokens)
- [x] Regla global de MAYÚSCULAS + letter-spacing — `base.html`
- [x] Isotipo real de ATISA en el riel (`isotipo.png` recortado del logo)
- [x] `.text-azul` (azul de texto legible, contraste 8:1) + utilidades de marca en `base.html`
- [x] **Barrido sistémico en TODAS las plantillas** (script): `hover:bg-blue-700`→tinta, `bg-blue-50/40,50`→sólido, `bg-sky-50`→`bg-blue-50`, degradados-slop aplanados (se conservó el velo de foto en ejecutar_receta). 0 clases rotas restantes; las 27 plantillas compilan.
- [ ] (pend.) Actualizar impeccable a v3.7.1 (`npx impeccable skills update`, aplica la próxima sesión)

> ⚠️ El workflow multi-agente (ultracode) **falló por límite de uso de la cuenta** (27 agentes toparon "session limit"), gastó presupuesto sin editar nada. En este plan conviene trabajar **inline** (no relanzar workflows masivos a la ligera).

## Pantallas
Orden por impacto. Estado: ⬜ pendiente · 🔄 en curso · ✅ hecho

Nota: TODAS recibieron el barrido sistémico (clases rotas/degradados). ✅ = además con pase de pulido completo (contraste, overflow, layout).

- ✅ `dashboard.html` — degradados fuera, color por significado, avatares azul, tiles planos
- ✅ `capacitacion.html` — encabezados rotados ya no se cortan (h-48), badges legibles, sky→blue
- ✅ `incidencias.html` — banner legible (text-sm, .text-azul, bg sólido)
- ✅ `empleados.html` — encabezados +contraste, input normalizado, botón "Crear" consistente
- ✅ **Contraste global (2 barridos)**: encabezados uppercase + texto chico descriptivo `slate-400`→`slate-500` en ~22 archivos; quitados `focus:ring` rotos; guiones tenues legibles
- ✅ `asistencias.html` — subtítulos +contraste, rings rotos fuera, horas-extra en `.text-azul`
- ✅ `reportes.html` — revisado; ya limpio con los barridos (color con significado, botones ok)
- ✅ `activos.html` — botón "Dar de Alta" consistente, icono `text-blue-500` roto → azul de marca
- ✅ `gasolina.html` — adornos de esquina rotos fuera, botones unificados (azul→tinta)
- ✅ **Sweep de hovers azules rotos** (`hover:bg-blue-50/100`, `hover:border-blue-200`) → utilidades de marca, en 8 archivos. 0 clases blue rotas en todo el proyecto.
- ✅ `produccion.html` — barras de gráfica ya visibles (`bg-[#0e6ba8]/80` + hovers definidos en base)
- ✅ `expertise.html` — encabezados rotados sin corte (h-48), `min-w-[..]` → `w-64/w-11`
- ✅ `bpd.html` — revisado; limpio (semáforo y sparklines con color con significado)
- ✅ **Foco azul global**: `focus:border-[#0e6ba8]` (82 inputs) + opacidades de marca definidas en `base.html`
- ✅ **Bump global slate-300→400** (valores cero/vacíos y placeholders) sin tocar iconos grandes
- ✅ `cinco_s.html` · `limpiezas.html` · `avisos.html` — revisados; limpios (color con significado)
- ✅ `documentos.html` — botón consistente, glassmorphism (`backdrop-blur`) fuera
- ✅ `configuracion.html` — arcoíris fuera (stats + accesos rápidos → azul; verde/rojo semánticos conservados), avatar y botón consistentes
- ✅ `login.html` — subtítulo +contraste (página independiente; no hereda mayúsculas globales)
- ✅ `recetas.html` — revisado; limpio (rojo solo en borrar = destructivo)
- ✅ `crear_receta` / `ejecutar_receta` / `reporte_individual` / `sin_acceso` / `_flashes` / `_partial` / impresión — barridos globales aplicados; botones de imprimir/regresar neutrales y verde/rojo semánticos (correctos)

## ✅ PASE DE DISEÑO COMPLETO
Verificación final: 28 plantillas compilan · app arranca · **0 clases rotas en todo el proyecto** · solo queda el degradado legítimo (velo sobre fotos).
Pendiente fino opcional a futuro: ¿login en mayúsculas como el resto? · revisar en navegador real con Ctrl+Shift+R.

## Puntito actual
**Vamos en:** fundación + barrido sistémico + bump global de encabezados + pulido fino de dashboard, capacitación, incidencias y empleados.
**Sigue:** pulido por pantalla `asistencias.html` → reportes → activos → gasolina → resto.
**Pendiente de pulido fino en cada pantalla:** texto chico `text-[#0e6ba8]`→`.text-azul`, cuerpo en `slate-300/400`→`slate-500/600`, color arcoíris→significado.
