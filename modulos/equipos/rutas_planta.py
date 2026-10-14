"""Pantallas de la tablet (pantalla completa): verificaciones del día y reportar falla.
Viven en el mismo blueprint 'equipos', bajo /equipos/planta/..."""
from datetime import date

from flask import current_app, flash, request, url_for

from nucleo import archivos, firmas
from nucleo.auth import login_required, requiere_modulo
from nucleo.db import get_db_connection
from nucleo.parametros import parametro
from nucleo.vistas import full_page, hx_redirect

from .rutas_oficina import ERRORES, _equipo_o_404, _fallo, _usuario, bp
from .servicios import catalogo, mantenimiento, verificaciones


@bp.route('/planta/verificar')
@login_required
@requiere_modulo('equipos_planta')
def planta_verificar_lista():
    conn = get_db_connection()
    pendientes = verificaciones.pendientes_de_hoy(conn)
    hechas, tocan = verificaciones.conteo_hoy(conn)
    conn.close()
    return full_page('equipos/planta_verificar_lista.html', pendientes=pendientes, hechas=hechas, tocan=tocan)


@bp.route('/planta/verificar/<int:equipo_id>', methods=['GET', 'POST'])
@login_required
@requiere_modulo('equipos_planta')
def planta_verificar(equipo_id):
    conn = get_db_connection()
    e = _equipo_o_404(conn, equipo_id)
    if request.method == 'POST':
        lecturas = {}
        for p in catalogo.puntos(conn, equipo_id, 'VERIFICACION'):
            lecturas[p['id']] = {'lectura': request.form.get(f'lectura_{p["id"]}'),
                                 'pesa_id': request.form.get(f'pesa_{p["id"]}')}
        try:
            fid = firmas.firmar(conn, numero_empleado=request.form.get('firma_numero'),
                                pin=request.form.get('firma_pin'), significado='Realizó', modulo='equipos',
                                entidad='equipos', entidad_id=equipo_id, detalle=f'Verificación del día de {e["codigo"]}')
            res = verificaciones.verificar(conn, equipo_id, lecturas=lecturas, firma_id=fid, usuario=_usuario(),
                                           nivelada_limpia=request.form.get('nivelada_limpia'),
                                           observaciones=request.form.get('observaciones'))
            conn.commit()
            if res['conforme']:
                flash(f'{e["codigo"]} verificada: apta por hoy.', 'success')
                return hx_redirect(url_for('equipos.planta_verificar_lista'))
            if res['repetir']:
                flash(f'{e["codigo"]} no cumple. Limpia, nivela y repite la verificación.', 'warning')
                return hx_redirect(url_for('equipos.planta_verificar', equipo_id=equipo_id))
            if res['fuera']:
                flash(f'{e["codigo"]} volvió a fallar: quedó fuera de servicio y se avisó a mantenimiento. '
                      'No la uses.', 'error')
            else:
                flash(f'{e["codigo"]} no cumple.', 'error')
            return hx_redirect(url_for('equipos.planta_verificar_lista'))
        except ERRORES as ex:
            _fallo(conn, ex)
        except Exception:
            conn.rollback()
            current_app.logger.exception('Equipos: falló la verificación del día de %s', equipo_id)
            flash('Algo salió mal y no se guardó. El error quedó registrado.', 'error')
        finally:
            conn.close()
        conn = get_db_connection()
    ctx = {'e': e, 'puntos': catalogo.puntos(conn, equipo_id, 'VERIFICACION'),
           'pesas': verificaciones.pesas_aptas(conn), 'previas': verificaciones.de_hoy(conn, equipo_id),
           'estado': verificaciones.estado_hoy(conn, e), 'form': request.form,
           'codigo_formato': parametro('equipos.codigo_formato_verificacion', conn)}
    conn.close()
    return full_page('equipos/planta_verificar.html', **ctx)


def procesar_falla(conn):
    """POST de reportar falla (tablet y oficina). Devuelve el resultado o None si no se guardó."""
    equipo_id = request.form.get('equipo_id', type=int)
    try:
        if not equipo_id:
            raise catalogo.ErrorEquipos('Elige el equipo que falla.')
        e = catalogo.equipo(conn, equipo_id)
        fid = firmas.firmar(conn, numero_empleado=request.form.get('firma_numero'), pin=request.form.get('firma_pin'),
                            significado='Realizó', modulo='equipos', entidad='equipos', entidad_id=equipo_id,
                            detalle=f'Reportó falla de {e["codigo"]}')
        res = mantenimiento.reportar_falla(conn, equipo_id, descripcion=request.form.get('descripcion'),
                                           se_puede_usar=request.form.get('se_puede_usar'), firma_id=fid,
                                           usuario=_usuario())
        f = request.files.get('foto')
        if f and f.filename:
            archivos.guardar_archivo(conn, f, modulo='equipos', entidad='eq_ordenes_trabajo', entidad_id=res['ot_id'],
                                     descripcion='Foto de la falla', usuario=_usuario())
        conn.commit()
        flash(f'Falla reportada ({res["folio"]}).' + (f' {e["codigo"]} quedó fuera de servicio: no lo uses.'
                                                     if res['fuera'] else ''), 'success')
        return res
    except ERRORES as ex:
        _fallo(conn, ex)
    except Exception:
        conn.rollback()
        current_app.logger.exception('Equipos: falló el reporte de falla')
        flash('Algo salió mal y no se guardó. El error quedó registrado.', 'error')
    return None


@bp.route('/planta/falla', methods=['GET', 'POST'])
@login_required
@requiere_modulo('equipos_planta')
def planta_falla():
    conn = get_db_connection()
    if request.method == 'POST':
        res = procesar_falla(conn)
        if res:
            conn.close()
            return hx_redirect(url_for('nucleo.planta'))
    equipos = conn.execute("SELECT id, codigo, nombre, area, estatus FROM equipos WHERE estatus <> 'BAJA' "
                           'ORDER BY area, codigo').fetchall()
    conn.close()
    areas = {}
    for e in equipos:
        areas.setdefault(e['area'] or 'Sin área', []).append(e)
    return full_page('equipos/planta_falla.html', areas=areas, elegido=request.values.get('equipo_id', type=int),
                     form=request.form, hoy=date.today().isoformat())
