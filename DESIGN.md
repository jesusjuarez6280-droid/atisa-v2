# Design

Sistema de diseño del ERP/Intranet ATISA. Registro: **product**. Estética: **clínico y confiable** — sobrio, mucho aire, azul de marca, color solo cuando significa. Stack: Flask + Jinja + HTMX + Tailwind (precompilado en `static/css/`, con runtime de respaldo). Reglas personalizadas viven en el bloque `<style>` de `templates/base.html` (carga al final; gana sobre el Tailwind compilado). **No editar `estilos.css` / `tailwind.css`: son autogenerados.**

> ⚠️ **MODO ESTÁTICO — regla crítica.** Como `tailwind.css` existe, el runtime de Tailwind está APAGADO: **solo funcionan las clases que ya estaban compiladas**. Si usas una clase arbitraria nueva (`w-[420px]`, `text-[#0e6ba8]`, `bg-[#0e6ba8]/10`, `space-y-0.5`, etc.) que no esté en el precompilado, **no hace nada** (esto causó que el sidebar se encimara). Antes de usar una clase arbitraria nueva, agrégala al bloque "Utilidades de marca" del `<style>` de `base.html`. Prefiere clases estándar ya compiladas (`slate/emerald/amber/rose`, `text-xl`, `w-11`…) cuando se pueda.

## Color

Paleta canónica (la que usa `base.html`; ignorar restos viejos `#1e6fd9`/`#0f2744` que quedaron en el CSS compilado):

| Rol | Valor | Uso |
|---|---|---|
| Marca / acento | `#0e6ba8` | Acción primaria, selección actual, estado activo, foco. **Solo eso, no decoración.** |
| Acento suave | `rgba(14,107,168,.10)` | Fondo de estado activo (nav, chips, hover de acción). |
| Tinta principal | `#1d1d1f` | Títulos y números destacados. |
| Texto secundario | `#6e6e73` | Texto de cuerpo/etiquetas. |
| Texto terciario | `#86868b` | Pistas, metadatos. Contraste AA mínimo; no bajar más. |
| Fondo página | `#f5f5f7` | Lienzo. |
| Superficie | `#ffffff` | Tarjetas, paneles, sidebar. |
| Bordes | `#ececef` / `#f0f0f3` | Divisores y bordes de tarjeta (1px, sutiles). |

Semánticos (estado, NO decoración): éxito `emerald` (activo, asistencia, ok) · atención `amber` (retardo, por vencer) · error/alerta `rose` (falta, 3ª falta, vencido). Roles como categoría: admin `violet`, rh `emerald`, operador `cyan` (se permite porque codifica categoría, no adorno).

### Contraste y legibilidad (OBLIGATORIO — AA ≥4.5:1)
- **Texto chico o de énfasis sobre fondos tintados:** usa `.text-azul` (#0b557f, ~8:1), NO `text-[#0e6ba8]`. El azul de marca puro a tamaño chico sobre un tinte se lee mal (fue una queja real del usuario).
- `text-[#0e6ba8]` se reserva para **iconos y texto grande** (≥18px); como botón es `bg-[#0e6ba8]` con texto blanco.
- **Nunca** texto de cuerpo en `slate-300`/`slate-400` (no llegan a 4.5:1). Cuerpo mínimo `slate-500`; secundario legible `slate-600`; metadatos `#86868b`. Pistas muy tenues solo en texto no esencial.
- Banners informativos: fondo `bg-blue-50` (existe) o `bg-[#0e6ba8]/10`; cuerpo en `slate-700`/tinta; énfasis en `.text-azul`. Tamaño mínimo cómodo `text-sm` (no `text-xs`) para párrafos.

### Clases que NO existen en el precompilado (modo estático) — PROHIBIDO usarlas
Salen invisibles/rotas. Reemplazos seguros:
- `bg-blue-700` / `hover:bg-blue-700` → `bg-[#0e6ba8]` / `hover:bg-[#0e6ba8]` (o hover a `#1d1d1f`).
- `text-blue-600` / `text-blue-700` → `.text-azul`.
- Cualquier `indigo-*` → usar `violet-*` (sí existe) o el azul de marca.
- Sí existen y son seguras: `blue-50`, `blue-100`, `border-blue-100`, y las escalas `slate/gray/emerald/amber/rose/cyan/violet/teal/orange`.

Estrategia: **Restrained** (neutros + un acento). Prohibido: degradados en iconos/botones, tarjetas de color distinto "para variar", arcoíris.

## Typography

- **Una sola familia**: sans del sistema (`ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif`). Sin pares display/body.
- Escala **fija en px/rem** (no fluida): número métrica 42px · h1 ~19-24px · h2 18px (`text-lg`) · cuerpo 15px · `sm` 14px · `xs`/`[11px]` etiquetas.
- Pesos: 500 (medium) y 600 (semibold) para jerarquía; 700 (bold) solo en números/título principal. Evitar 800/900.
- `tabular-nums` en cifras de tablas y métricas.

## Mayúsculas (decisión de dirección)
Todo el texto **mostrado** va en MAYÚSCULAS vía `text-transform: uppercase` global en `base.html`. Se EXCLUYEN: `input, textarea, select` (no estorbar al escribir), `code/pre/.font-mono` (datos técnicos) y la clase de escape `.normal-case`. El valor guardado en la base de datos **no cambia** (text-transform es solo visual).

## Layout
- Página: `p-10`, secciones separadas con `space-y-8`.
- Sidebar fijo de 300px = riel de departamentos (68px) + panel de módulos. `main` con `ml-[300px]`.
- Tarjetas: `bg-white`, borde 1px `#ececef`/`slate-100`, radio `rounded-2xl` (tarjetas) / `rounded-xl` (controles, chips, botones). **Sin tarjetas anidadas.**
- Rejillas responsivas: `grid` con columnas por breakpoint (`grid-cols-2 xl:grid-cols-4`). Flex para 1D.

## Components
Vocabulario único en todas las pantallas:
- **Botón primario**: `bg-[#0e6ba8] text-white rounded-xl`, hover `bg-blue-700`/`#1d1d1f`, `font-bold`.
- **Botón secundario**: `border border-slate-200 text-slate-600 rounded-xl`, hover `border-[#0e6ba8] text-[#0e6ba8]`.
- **Botón ícono**: cuadro `w-10/11 h-10/11 rounded-xl` mismo patrón secundario.
- **Chip de estado**: `px-3 py-1.5 rounded-lg` con par fondo-50/texto-700 del color semántico + punto.
- **Tile de ícono** (tarjetas): **plano**, `rounded-xl` con fondo tintado del acento (`bg-[#0e6ba8]/10`, icono `#0e6ba8`); color semántico solo si el dato lo exige. **Nunca degradado.**
- **Avatar**: círculo `bg-[#0e6ba8] text-white` con iniciales (consistente con sidebar).
- **Tabla**: encabezado `bg-slate-50` texto `xs` `#86868b`; filas con `hover:bg-slate-50/70`, divisores `slate-50/100`.
- **Modal**: centrado, `bg-black/40` backdrop, panel `rounded-2xl`. Usar con moderación (preferir inline).
- **Toast**: `atisa-ui.js` (no usar `alert()`/`confirm()` nativos).
- **Anti doble-clic**: guard global en `base.html` (no re-implementar por formulario).

## Motion
150-250ms, `transition-colors`/`transition-all`. La animación comunica estado (hover, foco, carga), no decora. Sin secuencias de entrada en carga de página. Barra de progreso superior HTMX = feedback de navegación. Respetar `prefers-reduced-motion`.

## States
Cada componente: default, hover, focus, disabled. Estados vacíos que enseñan (icono + qué hacer), no "no hay nada". Cargas con atenuación de `#content` (ya implementado), no spinners sueltos.
