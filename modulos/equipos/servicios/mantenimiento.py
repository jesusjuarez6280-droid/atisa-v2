"""Mantenimiento: preventivos por hacer, órdenes de trabajo (OT-2026-0001), fallas reportadas
(correctivo) y cierre de órdenes."""
import json
from datetime import date, timedelta

from nucleo import folios

from . import catalogo
from .comun import (ErrorEquipos, equipo, exigir_firma, fecha_iso, fijar_vencimientos, hoy_iso, numero, programa,
                    si, sumar_meses, texto)


def ot(conn, ot_id):
    fila = conn.execute('SELECT o.*, e.codigo, e.nombre AS equipo_nombre, e.estatus AS equipo_estatus, '
                        'p.nombre AS proveedor_nombre FROM eq_ordenes_trabajo o JOIN equipos e ON e.id = o.equipo_id '
                        'LEFT JOIN proveedores p ON p.id = o.proveedor_id WHERE o.id = ?', (ot_id,)).fetchone()
    if not fila:
        raise ErrorEquipos('La orden de trabajo no existe.')
    return fila


def tareas_de(texto_tareas):
    return [t.strip(' -•\t') for t in (texto_tareas or '').splitlines() if t.strip(' -•\t')]


def tareas_hechas(o):
    try:
        return json.loads(o['tareas_hechas'] or '[]')
    except (TypeError, ValueError):
        return []


def por_hacer(conn, hoy=None):
    """Preventivos dentro de sus días de aviso (o vencidos) que aún no tienen orden abierta."""
    hoy = hoy_iso(hoy)
    filas = conn.execute(
        "SELECT p.*, e.codigo, e.nombre, e.area, e.vence_mantenimiento FROM eq_programas p "
        "JOIN equipos e ON e.id = p.equipo_id WHERE p.actividad = 'MANTENIMIENTO' AND p.activo = 1 "
        "AND e.estatus <> 'BAJA' AND e.vence_mantenimiento IS NOT NULL AND NOT EXISTS ("
        "  SELECT 1 FROM eq_ordenes_trabajo o WHERE o.equipo_id = p.equipo_id AND o.tipo = 'PREVENTIVO' "
        "  AND o.estatus = 'ABIERTA') ORDER BY e.vence_mantenimiento, e.codigo").fetchall()
    limite = lambda dias: (date.fromisoformat(hoy) + timedelta(days=int(dias))).isoformat()  # noqa: E731
    return [f for f in filas if f['vence_mantenimiento'] <= limite(f['dias_aviso'])]


def abrir_ot(conn, equipo_id, *, tipo, usuario, descripcion=None, fecha_programada=None, proveedor_id=None,
             reporto_firma_id=None, se_puede_usar=None) -> int:
    e = equipo(conn, equipo_id)
    if e['estatus'] == 'BAJA':
        raise ErrorEquipos('El equipo está dado de baja.')
    if tipo not in ('PREVENTIVO', 'CORRECTIVO'):
        raise ErrorEquipos('Tipo de orden no válido.')
    if tipo == 'PREVENTIVO':
        prog = programa(conn, equipo_id, 'MANTENIMIENTO')
        if not prog:
            raise ErrorEquipos(f'{e["codigo"]} no tiene programa de mantenimiento preventivo.')
        if conn.execute("SELECT 1 FROM eq_ordenes_trabajo WHERE equipo_id = ? AND tipo = 'PREVENTIVO' "
                        "AND estatus = 'ABIERTA'", (equipo_id,)).fetchone():
            raise ErrorEquipos(f'{e["codigo"]} ya tiene una orden preventiva abierta.')
        descripcion = texto(descripcion) or prog['tareas']
        fecha_programada = fecha_programada or e['vence_mantenimiento']
        proveedor_id = proveedor_id or prog['proveedor_id']
    descripcion = texto(descripcion)
    if not descripcion:
        raise ErrorEquipos('Describe la falla.' if tipo == 'CORRECTIVO' else 'Faltan las tareas de la orden.')
    proveedor_id = int(proveedor_id) if str(proveedor_id or '').strip() not in ('', '0', 'None') else None
    folio = folios.siguiente_folio(conn, 'OT')
    cur = conn.execute(
        'INSERT INTO eq_ordenes_trabajo (folio, equipo_id, tipo, descripcion, se_puede_usar, fecha_programada, '
        'proveedor_id, reporto_firma_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (folio, equipo_id, tipo, descripcion, se_puede_usar, fecha_iso(fecha_programada, 'fecha programada', False),
         proveedor_id, reporto_firma_id))
    return cur.lastrowid


def reportar_falla(conn, equipo_id, *, descripcion, se_puede_usar, firma_id, usuario) -> dict:
    """Cualquiera la reporta. Crea la OT correctiva; con "no se puede usar" el equipo queda fuera de
    servicio en ese momento. Devuelve {'ot_id', 'folio', 'fuera'}."""
    e = equipo(conn, equipo_id)
    exigir_firma(conn, firma_id)
    if se_puede_usar in (None, ''):
        raise ErrorEquipos('Indica si el equipo se puede seguir usando.')
    usable = si(se_puede_usar)
    ot_id = abrir_ot(conn, equipo_id, tipo='CORRECTIVO', usuario=usuario, descripcion=descripcion,
                     reporto_firma_id=firma_id, se_puede_usar=usable, fecha_programada=hoy_iso())
    folio = conn.execute('SELECT folio FROM eq_ordenes_trabajo WHERE id = ?', (ot_id,)).fetchone()['folio']
    fuera = False
    if not usable and e['estatus'] == 'EN_SERVICIO':
        catalogo.poner_fuera(conn, equipo_id, motivo=f'Falla reportada ({folio}): {texto(descripcion)}',
                             firma_id=firma_id, usuario=usuario, causa='FALLA', folio=folio)
        fuera = True
    return {'ot_id': ot_id, 'folio': folio, 'fuera': fuera}


def terminar_ot(conn, ot_id, *, firma_id, usuario, tareas=None, trabajo_realizado=None, refacciones=None,
                horas=None, afecta_medicion=False, inicio=None, fin=None, hoy=None) -> dict:
    """tareas: [{'tarea', 'hecha': bool, 'nota'}] — en el preventivo cada tarea del plan va palomeada
    o con nota de por qué no se hizo. Termina el preventivo: próximo = fin + frecuencia."""
    o = ot(conn, ot_id)
    exigir_firma(conn, firma_id)
    if o['estatus'] != 'ABIERTA':
        raise ErrorEquipos('La orden ya está terminada o cancelada.')
    fin = fecha_iso(fin, 'fecha de término', obligatoria=False) or hoy_iso(hoy)
    inicio = fecha_iso(inicio, 'fecha de inicio', obligatoria=False) or fin
    if inicio > fin:
        raise ErrorEquipos('El inicio no puede ser después del término.')
    if fin > hoy_iso(hoy):
        raise ErrorEquipos('La fecha de término no puede ser futura.')
    horas = numero(horas, 'las horas', obligatorio=False)
    if horas is not None and horas < 0:
        raise ErrorEquipos('Las horas no pueden ser negativas.')
    hechas = []
    if o['tipo'] == 'PREVENTIVO':
        plan = tareas_de(o['descripcion'])
        dadas = {t.get('tarea'): t for t in (tareas or [])}
        for t in plan:
            d = dadas.get(t, {})
            hecha, nota = bool(d.get('hecha')), texto(d.get('nota'))
            if not hecha and not nota:
                raise ErrorEquipos(f'Palomea la tarea «{t}» o escribe por qué no se hizo.')
            hechas.append({'tarea': t, 'hecha': hecha, 'nota': nota})
    trabajo_realizado = texto(trabajo_realizado)
    if o['tipo'] == 'CORRECTIVO' and not trabajo_realizado:
        raise ErrorEquipos('Describe la reparación que se hizo.')
    conn.execute("UPDATE eq_ordenes_trabajo SET estatus = 'TERMINADA', tareas_hechas = ?, trabajo_realizado = ?, "
                 'refacciones = ?, horas = ?, afecta_medicion = ?, inicio = ?, fin = ?, realizo_firma_id = ? WHERE id = ?',
                 (json.dumps(hechas, ensure_ascii=False) if hechas else None, trabajo_realizado, texto(refacciones),
                  horas, si(afecta_medicion), inicio, fin, firma_id, ot_id))
    proximo = None
    if o['tipo'] == 'PREVENTIVO':
        prog = programa(conn, o['equipo_id'], 'MANTENIMIENTO')
        if prog:
            proximo = sumar_meses(fin, prog['frecuencia_meses'])
            fijar_vencimientos(conn, o['equipo_id'], vence_mantenimiento=proximo, usuario=usuario,
                               motivo=f'Mantenimiento {o["folio"]} terminado')
    return {'proximo': proximo}


def cancelar_ot(conn, ot_id, *, motivo, usuario):
    o = ot(conn, ot_id)
    if o['estatus'] != 'ABIERTA':
        raise ErrorEquipos('Solo se cancela una orden abierta.')
    motivo = texto(motivo)
    if not motivo:
        raise ErrorEquipos('Escribe el motivo de la cancelación.')
    conn.execute("UPDATE eq_ordenes_trabajo SET estatus = 'CANCELADA', motivo_cancelacion = ? WHERE id = ?",
                 (motivo, ot_id))


def ordenes(conn, estatus, limite=50, desplazamiento=0):
    return conn.execute('SELECT o.*, e.codigo, e.nombre AS equipo_nombre FROM eq_ordenes_trabajo o '
                        'JOIN equipos e ON e.id = o.equipo_id WHERE o.estatus = ? '
                        'ORDER BY COALESCE(o.fecha_programada, o.creado_en) DESC, o.id DESC LIMIT ? OFFSET ?',
                        (estatus, limite, desplazamiento)).fetchall()
