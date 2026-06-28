/* === SELECT PRO — Portal approach: panel va al body para no ser cortado por overflow:hidden === */
(function () {
    'use strict';

    let counter = 0;
    const openMap = new Map(); // wrapId → { panel, trigger, wrap }

    function init() {
        document.querySelectorAll('select.select-pro:not([data-pro-init])').forEach(initOne);
    }

    function initOne(sel) {
        sel.setAttribute('data-pro-init', 'true');
        const wid = 'spw' + (++counter);

        /* Wrapper */
        const wrap = document.createElement('div');
        wrap.className = 'select-pro-wrap';
        wrap.dataset.wid = wid;
        sel.parentNode.insertBefore(wrap, sel);
        wrap.appendChild(sel);

        /* Trigger */
        const trigger = document.createElement('button');
        trigger.type = 'button';
        trigger.className = 'select-pro-trigger';
        trigger.dataset.wid = wid;
        if (sel.classList.contains('compact')) trigger.classList.add('compact');
        const lbl = document.createElement('span');
        lbl.className = 'select-pro-label';
        const chev = document.createElement('i');
        chev.className = 'ri-arrow-down-s-line select-pro-chevron';
        trigger.appendChild(lbl);
        trigger.appendChild(chev);
        wrap.appendChild(trigger);

        /* Panel — en el BODY para escapar overflow:hidden del padre */
        const panel = document.createElement('div');
        panel.className = 'select-pro-panel';
        panel.dataset.wid = wid;
        document.body.appendChild(panel);

        /* ── Posicionamiento ── */
        function position() {
            const r = trigger.getBoundingClientRect();
            panel.style.width  = r.width + 'px';
            panel.style.left   = (r.left + window.scrollX) + 'px';
            const spaceBelow   = window.innerHeight - r.bottom - 10;
            const panelH       = Math.min(320, panel.scrollHeight || 200);
            if (spaceBelow >= panelH || spaceBelow >= r.top) {
                panel.style.top    = (r.bottom + window.scrollY + 6) + 'px';
                panel.style.bottom = '';
            } else {
                panel.style.top    = '';
                panel.style.bottom = (window.innerHeight - r.top - window.scrollY + 6) + 'px';
            }
        }

        /* ── Opciones ── */
        function renderOptions() {
            panel.innerHTML = '';
            const kids = Array.from(sel.children);
            if (!kids.length) {
                const e = document.createElement('div');
                e.className = 'select-pro-empty';
                e.textContent = 'Sin opciones disponibles';
                panel.appendChild(e);
                return;
            }
            kids.forEach(child => {
                if (child.tagName === 'OPTGROUP') {
                    const g = document.createElement('div');
                    g.className = 'select-pro-group-label';
                    g.textContent = child.label || '';
                    panel.appendChild(g);
                    Array.from(child.children).forEach(addOption);
                } else if (child.tagName === 'OPTION') {
                    addOption(child);
                }
            });
        }

        function addOption(opt) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'select-pro-option';
            btn.dataset.value = opt.value;
            btn.textContent = opt.text;
            if (opt.value === '' && (opt.disabled || opt === sel.options[0])) btn.dataset.placeholder = 'true';
            if (opt.value === sel.value && opt.value !== '') btn.classList.add('selected');
            btn.addEventListener('click', e => {
                e.stopPropagation();
                if (opt.value === '' && opt.disabled) return;
                pickValue(opt.value);
                close();
            });
            panel.appendChild(btn);
        }

        function updateLabel() {
            const cur = Array.from(sel.options).find(o => o.value === sel.value);
            if (cur && cur.value !== '') {
                lbl.textContent = cur.text;
                trigger.setAttribute('data-empty', 'false');
            } else {
                lbl.textContent = sel.options[0] ? sel.options[0].text : 'Seleccionar...';
                trigger.setAttribute('data-empty', 'true');
            }
        }

        function pickValue(val) {
            sel.value = val;
            updateLabel();
            renderOptions();
            sel.dispatchEvent(new Event('change', { bubbles: true }));
            sel.dispatchEvent(new Event('input',  { bubbles: true }));
        }

        /* ── Abrir / Cerrar ── */
        function open() {
            openMap.forEach((obj, id) => {
                if (id !== wid) {
                    obj.panel.classList.remove('is-open');
                    obj.wrap.classList.remove('open');
                    openMap.delete(id);
                }
            });
            openMap.set(wid, { panel, trigger, wrap });
            wrap.classList.add('open');
            panel.classList.add('is-open');
            position();
        }

        function close() {
            openMap.delete(wid);
            wrap.classList.remove('open');
            panel.classList.remove('is-open');
        }

        /* ── Eventos ── */
        trigger.addEventListener('click', e => {
            e.stopPropagation();
            if (sel.disabled) return;
            wrap.classList.contains('open') ? close() : open();
        });

        trigger.addEventListener('keydown', e => {
            if (['Enter', ' ', 'ArrowDown'].includes(e.key)) {
                e.preventDefault();
                open();
                const first = panel.querySelector('.select-pro-option:not([data-placeholder])');
                if (first) first.focus();
            }
        });

        panel.addEventListener('keydown', e => {
            const opts = Array.from(panel.querySelectorAll('.select-pro-option:not([data-placeholder])'));
            const idx  = opts.indexOf(document.activeElement);
            if (e.key === 'ArrowDown')  { e.preventDefault(); const n = opts[Math.min(idx + 1, opts.length - 1)]; if (n) n.focus(); }
            else if (e.key === 'ArrowUp')   { e.preventDefault(); const p = opts[Math.max(idx - 1, 0)]; if (p) p.focus(); }
            else if (e.key === 'Escape')    { close(); trigger.focus(); }
        });

        /* Sincronizar si el valor cambia externamente */
        sel.addEventListener('change', () => { updateLabel(); renderOptions(); });

        /* Observar cambios en opciones (útil para selects dinámicos) */
        new MutationObserver(() => { renderOptions(); updateLabel(); }).observe(sel, { childList: true, subtree: true });

        updateLabel();
        renderOptions();
    }

    /* ── Cerrar al hacer click fuera ── */
    document.addEventListener('click', e => {
        if (e.target.closest('.select-pro-wrap') || e.target.closest('.select-pro-panel')) return;
        openMap.forEach(obj => {
            obj.panel.classList.remove('is-open');
            obj.wrap.classList.remove('open');
        });
        openMap.clear();
    });

    document.addEventListener('keydown', e => {
        if (e.key !== 'Escape') return;
        openMap.forEach(obj => {
            obj.panel.classList.remove('is-open');
            obj.wrap.classList.remove('open');
        });
        openMap.clear();
    });

    /* Reposicionar al hacer scroll o resize */
    function reposition() {
        openMap.forEach(obj => {
            if (!obj.panel.classList.contains('is-open')) return;
            const r = obj.trigger.getBoundingClientRect();
            obj.panel.style.width  = r.width + 'px';
            obj.panel.style.left   = (r.left + window.scrollX) + 'px';
            obj.panel.style.top    = (r.bottom + window.scrollY + 6) + 'px';
            obj.panel.style.bottom = '';
        });
    }

    document.addEventListener('scroll', reposition, true);
    window.addEventListener('resize', () => {
        openMap.forEach(obj => {
            obj.panel.classList.remove('is-open');
            obj.wrap.classList.remove('open');
        });
        openMap.clear();
    });

    /* ── Inicializar ── */
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    /* Observar nuevos selects (modales, etc.) */
    const startObs = () => {
        new MutationObserver(ms => {
            for (const m of ms) { if (m.addedNodes.length) { init(); return; } }
        }).observe(document.body, { childList: true, subtree: true });
    };
    document.body ? startObs() : document.addEventListener('DOMContentLoaded', startObs);
})();
