from flask import Blueprint, abort, request, flash, url_for

from nucleo.auth import login_required, requiere_modulo, requiere_facultad
from nucleo.db import get_db_connection
from nucleo.vistas import render_page, hx_redirect

bp = Blueprint('prueba', __name__, url_prefix='/prueba', template_folder='templates')


@bp.route('')
@login_required
@requiere_modulo('prueba')
def inicio():
    conn = get_db_connection()
    cosas = conn.execute('SELECT * FROM prb_cosas ORDER BY id').fetchall()
    conn.close()
    return render_page('prueba/inicio.html', cosas=cosas)


@bp.route('/<int:cosa_id>')
@login_required
@requiere_modulo('prueba')
def cosa(cosa_id):
    conn = get_db_connection()
    c = conn.execute('SELECT * FROM prb_cosas WHERE id = ?', (cosa_id,)).fetchone()
    conn.close()
    if not c:
        abort(404)
    return render_page('prueba/inicio.html', cosas=[c])


@bp.route('/liberar', methods=['POST'])
@login_required
@requiere_modulo('prueba')
@requiere_facultad('liberar_lotes')
def liberar():
    flash('Liberado.', 'success')
    return hx_redirect(url_for('prueba.inicio'))
