@echo off
REM ============================================================
REM  Recompila el CSS de Tailwind escaneando las plantillas.
REM  Ejecuta este .bat cada vez que agregues clases nuevas en
REM  los .html (colores, tamanos, etc.) para que aparezcan.
REM
REM  Tip: agrega  --watch  al final para recompilar en vivo
REM       mientras editas (deja la ventana abierta).
REM ============================================================
cd /d "%~dp0"
tailwindcss.exe -c tailwind.config.js -i ./static/css/input.css -o ./static/css/tailwind.css --minify
echo.
echo  Listo. CSS regenerado en static/css/tailwind.css
pause
