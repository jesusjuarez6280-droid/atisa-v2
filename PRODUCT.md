# Product

## Register

product

## Users
Personal interno de ATISA (Alta Tecnología Industrial para la Salud Animal, S.A. de C.V.), laboratorio de productos veterinarios regulado. Tres roles: **admin** (dirección/sistemas), **rh** (recursos humanos y calidad), **operador** (producción en planta). Perfil mayoritariamente **no técnico**: usan el sistema en planta y oficina para tareas concretas, no para "explorar". El sistema debe ser obvio, no requerir capacitación.

## Product Purpose
Intranet/ERP que centraliza la operación de la empresa por departamentos (organigrama: RH, Producción, Calidad, Logística, Comercial, I&D, Administración, Farmacovigilancia). Hoy cubre: empleados, asistencias (checador ZKTeco), incidencias, capacitación, expertise, desempeño BPD, 5S, limpiezas, producción/recetas, activos y EPP, gasolina, documentos, avisos. Meta cercana: cumplir la **auditoría BPM de SENASICA (24-25 ago 2026)** con expedientes y documentación completos. Éxito = un solo lugar confiable donde cada área registra y consulta lo suyo sin Excel suelto.

## Brand Personality
Clínico, confiable, preciso. Voz directa y en español llano, sin tecnicismos ni "humo". Tres palabras: **preciso, confiable, ordenado**. Debe sentirse como instrumental de laboratorio: limpio, sobrio, sin adornos. La confianza se gana con claridad y consistencia, no con efectos.

## Anti-references
- Dashboards SaaS "bonitos" llenos de degradados, tarjetas-métrica gigantes con número enorme + flechita de tendencia, y acentos arcoíris. (El dashboard actual cae en esto: se corrige.)
- Paletas tipo "startup" multicolor donde cada tarjeta es de un color distinto sin significado.
- Animaciones decorativas, glassmorphism, sombras dramáticas.
- Interfaces que parecen plantilla genérica de Bootstrap/AdminLTE.

## Design Principles
1. **El color significa algo.** Azul = acción/marca. Rojo/ámbar = atención. Verde = ok. Nunca color por decoración ni por variar.
2. **La herramienta desaparece en la tarea.** Familiaridad ganada: botones, formularios e iconos idénticos en todas las pantallas. Cero sorpresas.
3. **Claridad sobre densidad decorativa.** Mucho aire, jerarquía clara, texto legible. Si algo no ayuda a la tarea, sobra.
4. **Autonomía.** El sistema llena solo lo que puede deducir (asistencias, vacaciones LFT, festivos) y solo pide al humano lo que el dato no sabe.
5. **Honestidad de estado.** Estados vacíos que enseñan, errores claros, nada de "todo bien" cuando no lo está.

## Accessibility & Inclusion
Usuarios de planta de edades variadas: priorizar **legibilidad** (contraste AA ≥4.5:1 en texto, nada de gris claro "elegante" sobre blanco), tipografía cómoda y objetivos de clic amplios. Funciona sin internet (assets locales). `prefers-reduced-motion` respetado. Texto en mayúsculas por decisión de dirección, aplicado solo a lo que se muestra (no a campos de captura, para no estorbar al escribir).
