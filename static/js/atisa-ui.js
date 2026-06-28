/* ============================================================
   ATISA UI — Toasts premium + intercepción de validación nativa
   Reemplaza el feo globo "Completa este campo" del navegador por
   nuestro sistema de notificaciones (estética lujo silencioso).
   ============================================================ */
(function () {
  'use strict';

  var STYLES = {
    success: { label: 'Listo',    icon: 'ri-check-line',          chip: 'bg-emerald-50 text-emerald-600', lbl: 'text-emerald-600' },
    error:   { label: 'Atención', icon: 'ri-error-warning-line',  chip: 'bg-rose-50 text-rose-500',       lbl: 'text-rose-500' },
    info:    { label: 'Aviso',    icon: 'ri-information-line',     chip: 'bg-[#0e6ba8]/10 text-[#0e6ba8]', lbl: 'text-[#0e6ba8]' }
  };

  function zone() {
    var z = document.getElementById('toast-zone');
    if (!z) {
      z = document.createElement('div');
      z.id = 'toast-zone';
      z.className = 'fixed top-5 right-5 z-[10000] flex flex-col gap-3 w-[360px] max-w-[calc(100vw-2.5rem)]';
      document.body.appendChild(z);
    }
    return z;
  }

  function toast(message, type) {
    var s = STYLES[type] || STYLES.info;
    var el = document.createElement('div');
    el.className = 'flash-toast toast-enter flex items-start gap-3 bg-white rounded-2xl border border-[#ececef] shadow-[0_16px_48px_-8px_rgba(16,24,40,0.18)] pl-3.5 pr-2.5 py-3';

    var chip = document.createElement('div');
    chip.className = 'mt-0.5 w-9 h-9 rounded-xl flex items-center justify-center shrink-0 ' + s.chip;
    chip.innerHTML = '<i class="text-[19px] ' + s.icon + '"></i>';

    var body = document.createElement('div');
    body.className = 'flex-1 min-w-0 py-0.5';
    var lbl = document.createElement('p');
    lbl.className = 'text-[11px] font-semibold uppercase tracking-wide ' + s.lbl;
    lbl.textContent = s.label;
    var msg = document.createElement('p');
    msg.className = 'text-[#1d1d1f] text-sm font-medium leading-snug mt-0.5';
    msg.textContent = message;                 // textContent => sin riesgo de inyección
    body.appendChild(lbl);
    body.appendChild(msg);

    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'shrink-0 w-7 h-7 rounded-lg flex items-center justify-center text-[#c7c7cc] hover:text-[#6e6e73] hover:bg-[#f3f3f5] transition-colors';
    close.innerHTML = '<i class="ri-close-line text-base"></i>';
    close.addEventListener('click', function () { remove(el); });

    el.appendChild(chip);
    el.appendChild(body);
    el.appendChild(close);
    zone().appendChild(el);

    setTimeout(function () { remove(el); }, 4500);
    return el;
  }

  function remove(el) {
    if (!el || el.dataset.closing) return;
    el.dataset.closing = '1';
    el.style.transition = 'opacity .45s ease, transform .45s ease';
    el.style.opacity = '0';
    el.style.transform = 'translateX(18px)';
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 450);
  }

  window.atisaToast = toast;

  /* ---- Marcado de campo inválido (rosa) ---- */
  function marcarInvalido(field) {
    if (field.classList && field.classList.contains('select-pro')) {
      var wrap = field.closest('.select-pro-wrap');
      var trig = wrap && wrap.querySelector('.select-pro-trigger');
      if (trig) {
        trig.style.borderColor = '#f43f5e';
        trig.style.boxShadow = '0 0 0 4px rgba(244,63,94,.12)';
      }
    } else if (field.style) {
      field.style.borderColor = '#f43f5e';
      field.style.boxShadow = '0 0 0 4px rgba(244,63,94,.12)';
    }
  }

  function limpiarInvalido(field) {
    if (field.classList && field.classList.contains('select-pro')) {
      var wrap = field.closest('.select-pro-wrap');
      var trig = wrap && wrap.querySelector('.select-pro-trigger');
      if (trig) { trig.style.borderColor = ''; trig.style.boxShadow = ''; }
    } else if (field.style) {
      field.style.borderColor = '';
      field.style.boxShadow = '';
    }
  }

  /* ---- Intercepción de la validación nativa ---- */
  var toastEnRafaga = false;
  document.addEventListener('invalid', function (e) {
    e.preventDefault();                          // suprime el globo nativo
    var field = e.target;
    marcarInvalido(field);
    if (!toastEnRafaga) {
      toastEnRafaga = true;
      toast(field.validationMessage || 'Completa este campo', 'error');
      try { field.focus({ preventScroll: false }); } catch (_) {}
      setTimeout(function () { toastEnRafaga = false; }, 150);
    }
  }, true);

  // Limpia el resaltado al corregir el campo
  document.addEventListener('input',  function (e) { limpiarInvalido(e.target); }, true);
  document.addEventListener('change', function (e) { limpiarInvalido(e.target); }, true);
})();
