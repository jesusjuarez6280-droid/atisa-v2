"""Desviaciones y CAPA: pruebas del diseño 04 (sección 9) a nivel de servicios."""
import itertools
from datetime import date, timedelta

import pytest

from fabricas import conexion, crear_lote, crear_material, crear_usuario, firma
from modulos.desviaciones import servicios as sv
from nucleo import inventario, registro

PIN = '2580'
_n = itertools.count(1)


def _hoy(dias=0):
    return (date.today() + timedelta(days=dias)).isoformat()


@pytest.fixture
def gente(db):
    """Un jefe de calidad (gestiona), otro de calidad (gestiona), un analista de calidad,
    un jefe de producción que investiga y un operador."""
    n = next(_n)
    return {
        'area': f'Área prueba {n}',      # cada prueba en su área: las "parecidas" no se cruzan
        'jefe': crear_usuario(db, f'dv_jefe{n}', rol='calidad', facultades='gestionar_desviaciones,liberar_lotes', pin=PIN),
        'jefe2': crear_usuario(db, f'dv_jefe2_{n}', rol='calidad', facultades='gestionar_desviaciones', pin=PIN),
        'analista': crear_usuario(db, f'dv_ana{n}', rol='calidad', pin=PIN),
        'prod': crear_usuario(db, f'dv_prod{n}', rol='produccion', pin=PIN),
        'oper': crear_usuario(db, f'dv_oper{n}', rol='operador', pin=PIN),
    }


def _f(db, numero, significado='Realizó', facultad=None):
    return firma(db, numero, PIN, significado, facultad=facultad, modulo='desviaciones', entidad='dev_desviaciones')


def _reportar(db, gente, tipo='DESVIACION', lotes=None, **datos):
    base = {'descripcion': 'El pH salió en 8.1', 'area': gente['area'], 'fecha_detecto': _hoy()}
    base.update(datos)
    did = sv.reportar(db, tipo=tipo, datos=base, firma_id=_f(db, gente['prod']), usuario='pruebas', lotes=lotes)
    db.commit()
    return did


def _clasificar(db, gente, did, clasificacion='MAYOR', lotes=None):
    sv.clasificar(db, did, clasificacion=clasificacion, responsable=gente['prod'],
                  firma_id=_f(db, gente['jefe'], 'Aprobó', 'gestionar_desviaciones'), usuario='pruebas', lotes=lotes)
    db.commit()


def _lote_aprobado(db, gente, material_id=None, **campos):
    lid = crear_lote(db, material_id or crear_material(db), cantidad=50, origen='INICIAL', estatus='APROBADO',
                     firma_id=_f(db, gente['jefe'], 'Liberó', 'liberar_lotes'), **campos)
    return lid


def _investigar(db, gente, did, causa='Método', acciones=1, preventiva=False):
    sv.guardar_investigacion(db, did, {'causa_raiz': 'El procedimiento no indica calibrar el potenciómetro',
                                       'categoria_causa': causa}, 'pruebas')
    for l in sv.lotes(db, did):
        sv.evaluar_lote(db, did, l['lote_id'], impacto='SIN_IMPACTO', justificacion='Se reanalizó y cumple',
                        usuario='pruebas')
    ids = []
    for i in range(acciones):
        ids.append(sv.agregar_accion(db, did, tipo='PREVENTIVA' if preventiva and i == 0 else 'CORRECTIVA',
                                     descripcion=f'Acción {i}', responsable=gente['prod'],
                                     fecha_compromiso=_hoy(10), usuario='pruebas'))
    db.commit()
    return ids


def _hasta_acciones(db, gente, did, **kw):
    _clasificar(db, gente, did)
    ids = _investigar(db, gente, did, **kw)
    sv.enviar(db, did, firma_id=_f(db, gente['prod']), usuario='pruebas')
    sv.aprobar(db, did, firma_id=_f(db, gente['jefe'], 'Aprobó', 'gestionar_desviaciones'), usuario='pruebas')
    db.commit()
    return ids


def _error(db, fn, *args, **kwargs):
    with pytest.raises(sv.ErrorDesviacion) as e:
        fn(*args, **kwargs)
    db.rollback()
    return str(e.value)


# --- 1. Reportar -------------------------------------------------------------
def test_folio_segun_el_tipo_con_su_firma(db, gente):
    anio = date.today().year
    d1 = sv.desviacion(db, _reportar(db, gente))
    q = sv.desviacion(db, _reportar(db, gente, 'QUEJA', cliente='Granja El Sol'))
    h = sv.desviacion(db, _reportar(db, gente, 'HALLAZGO', auditoria='5S Almacén 2026-10-02'))
    assert d1['folio'].startswith(f'DES-{anio}-') and q['folio'].startswith(f'QJA-{anio}-')
    assert h['folio'].startswith(f'HAL-{anio}-')
    assert d1['estatus'] == 'REPORTADA' and d1['reporto_firma_id']
    assert db.execute('SELECT significado FROM firmas WHERE id = ?', (d1['reporto_firma_id'],)).fetchone()[0] == 'Realizó'
    assert d1['titulo'] == 'El pH salió en 8.1'


def test_reportar_pide_lo_obligatorio_y_queja_con_reaccion_es_critica(db, gente):
    fid = _f(db, gente['prod'])
    assert 'qué pasó' in _error(db, sv.reportar, db, tipo='DESVIACION', firma_id=fid, usuario='x',
                                datos={'area': 'Producción', 'fecha_detecto': _hoy()})
    assert 'área' in _error(db, sv.reportar, db, tipo='DESVIACION', firma_id=fid, usuario='x',
                            datos={'descripcion': 'x', 'fecha_detecto': _hoy()})
    did = _reportar(db, gente, 'QUEJA', cliente='Rancho', posible_reaccion_adversa=1)
    assert sv.desviacion(db, did)['clasificacion'] == 'CRITICA'
    assert 'crítica' in _error(db, sv.clasificar, db, did, clasificacion='MAYOR', responsable=gente['prod'],
                               firma_id=_f(db, gente['jefe'], 'Aprobó', 'gestionar_desviaciones'), usuario='x')


def test_desde_otro_modulo_llega_con_origen_documento_y_lotes(db, gente):
    lid = crear_lote(db, cantidad=5, folio='OP-T-0001')
    did = _reportar(db, gente, lotes=[lid], origen_tipo='orden_paso', origen_id=31,
                    origen_texto='OP-T-0001 · paso 5', documento_folio='OP-T-0001', origen_url='/ordenes/31')
    d = sv.desviacion(db, did)
    assert (d['origen_modulo'], d['origen_tipo'], d['origen_id']) == ('ordenes', 'orden_paso', 31)
    assert [l['lote_id'] for l in sv.lotes(db, did)] == [lid]
    assert sv.desviaciones_de(db, 'orden_paso', 31)[0]['folio'] == d['folio']
    assert registro.consulta('desviaciones_de', db, 'orden_paso', 31)[0]['url'] == f'/desviaciones/{did}'
    assert sv.desviaciones_de(db, 'orden_paso', 32) == []


# --- 2. Clasificar -----------------------------------------------------------
def test_solo_quien_gestiona_clasifica_y_la_fecha_sale_de_la_clasificacion(db, gente):
    did = _reportar(db, gente)
    assert 'facultad' in _error(db, sv.clasificar, db, did, clasificacion='MAYOR', responsable=gente['prod'],
                                firma_id=_f(db, gente['analista']), usuario='x')
    _clasificar(db, gente, did, 'CRITICA')
    d = sv.desviacion(db, did)
    assert d['estatus'] == 'EN_INVESTIGACION' and d['responsable'] == gente['prod']
    assert d['vence_investigacion'] == _hoy(15)
    did2 = _reportar(db, gente)
    _clasificar(db, gente, did2, 'MENOR')
    assert sv.desviacion(db, did2)['vence_investigacion'] == _hoy(30)


def test_responsable_debe_tener_acceso_al_modulo(db, gente):
    did = _reportar(db, gente)
    assert 'acceso' in _error(db, sv.clasificar, db, did, clasificacion='MAYOR', responsable=gente['oper'],
                              firma_id=_f(db, gente['jefe'], 'Aprobó', 'gestionar_desviaciones'), usuario='x')


def test_menor_sin_lotes_se_cierra_al_clasificar(db, gente):
    sin_correccion = _reportar(db, gente)
    fid = _f(db, gente['jefe'], 'Cerró', 'gestionar_desviaciones')
    assert 'corrección' in _error(db, sv.cerrar_al_clasificar, db, sin_correccion, justificacion='x',
                                  firma_id=fid, usuario='x')
    did = _reportar(db, gente, correccion_inmediata='Se reetiquetó la caja')
    sv.cerrar_al_clasificar(db, did, justificacion='Error de etiqueta ya corregido, sin causa que investigar',
                            firma_id=_f(db, gente['jefe'], 'Cerró', 'gestionar_desviaciones'), usuario='x')
    db.commit()
    d = sv.desviacion(db, did)
    assert (d['estatus'], d['clasificacion']) == ('CERRADA', 'MENOR') and d['cerro_firma_id']
    # con un lote sin evaluar no se puede
    con_lote = _reportar(db, gente, lotes=[crear_lote(db, cantidad=1)], correccion_inmediata='x')
    assert 'impacto' in _error(db, sv.cerrar_al_clasificar, db, con_lote, justificacion='x',
                               firma_id=_f(db, gente['jefe'], 'Cerró', 'gestionar_desviaciones'), usuario='x')


# --- 3. Retener --------------------------------------------------------------
def test_retener_lote_aprobado_y_sus_presentaciones(db, gente):
    gr = crear_material(db, tipo='GR')
    pt = crear_material(db, tipo='PT', material_padre_id=gr, unidad='pza')
    granel = _lote_aprobado(db, gente, gr)
    presentacion = crear_lote(db, pt, cantidad=10, origen='PRODUCCION', lote_padre_id=granel)
    assert inventario.lote(db, presentacion)['estatus'] == 'APROBADO'
    did = _reportar(db, gente, lotes=[granel])
    folio = sv.desviacion(db, did)['folio']
    retenidos = sv.retener(db, did, granel, firma_id=_f(db, gente['jefe'], 'Retuvo', 'gestionar_desviaciones'),
                           usuario='pruebas')
    db.commit()
    assert set(retenidos) == {granel, presentacion}
    for lid in (granel, presentacion):
        assert inventario.lote(db, lid)['estatus'] == 'RETENIDO'
        assert db.execute("SELECT folio FROM lote_historial WHERE lote_id = ? AND valor_nuevo = 'RETENIDO'",
                          (lid,)).fetchone()[0] == folio
    assert all(l['retenido'] for l in sv.lotes(db, did))


def test_no_se_retiene_un_lote_en_cuarentena(db, gente):
    lid = crear_lote(db, cantidad=3)
    did = _reportar(db, gente, lotes=[lid])
    assert 'aprobado' in _error(db, sv.retener, db, did, lid,
                                firma_id=_f(db, gente['jefe'], 'Retuvo', 'gestionar_desviaciones'), usuario='x')
    assert 'facultad' in _error(db, sv.retener, db, did, _lote_aprobado(db, gente),
                                firma_id=_f(db, gente['analista'], 'Retuvo'), usuario='x')


# --- 4. Bloqueo de liberación ---------------------------------------------------
def test_bloqueo_de_liberacion(db, gente):
    lid = crear_lote(db, cantidad=3, folio='REC-T-BLOQ')
    otro = crear_lote(db, cantidad=3, folio='REC-T-BLOQ')        # nació del mismo documento, nadie lo agregó
    did = _reportar(db, gente, lotes=[lid], documento_folio='REC-T-BLOQ')
    folio = sv.desviacion(db, did)['folio']
    assert sv.bloqueos_de_lote(db, lid) == [f'{folio}: falta evaluar el impacto en este lote.']
    assert sv.bloqueos_de_lote(db, otro) == [f'{folio}: falta evaluar el impacto en este lote (viene de REC-T-BLOQ).']
    _clasificar(db, gente, did)
    sv.evaluar_lote(db, did, lid, impacto='CON_IMPACTO', justificacion='pH fuera', usuario='x')
    sv.evaluar_lote(db, did, otro, impacto='SIN_IMPACTO', justificacion='Otro tanque', usuario='x')
    db.commit()
    assert 'solo se puede rechazar' in sv.bloqueos_de_lote(db, lid)[0]
    assert sv.bloqueos_de_lote(db, otro) == []
    # La base junta los motivos de todos los módulos
    assert any(folio in m for m in registro.verificaciones_liberacion(db, lid))
    sv.anular(db, did, motivo='Duplicada', firma_id=_f(db, gente['jefe'], 'Anuló', 'gestionar_desviaciones'),
              usuario='x')
    db.commit()
    assert sv.bloqueos_de_lote(db, lid) == []


def test_presentacion_detenida_por_desviacion_de_su_granel(db, gente):
    gr = crear_material(db, tipo='GR')
    pt = crear_material(db, tipo='PT', material_padre_id=gr, unidad='pza')
    granel = crear_lote(db, gr, cantidad=10)
    presentacion = crear_lote(db, pt, cantidad=10, origen='PRODUCCION', lote_padre_id=granel)
    did = _reportar(db, gente, lotes=[granel])
    motivos = sv.bloqueos_de_lote(db, presentacion)
    assert len(motivos) == 1 and 'granel' in motivos[0] and sv.desviacion(db, did)['folio'] in motivos[0]


# --- 5. Enviar la investigación -------------------------------------------------
def test_investigacion_no_se_envia_incompleta(db, gente):
    lid = crear_lote(db, cantidad=2)
    did = _reportar(db, gente, lotes=[lid])
    _clasificar(db, gente, did, 'CRITICA')
    msg = _error(db, sv.enviar, db, did, firma_id=_f(db, gente['prod']), usuario='x')
    assert 'causa raíz' in msg and 'categoría' in msg and 'impacto' in msg and 'crítica' in msg
    sv.guardar_investigacion(db, did, {'causa_raiz': 'x', 'categoria_causa': 'Método',
                                       'sin_acciones_motivo': 'No hace falta'}, 'x')
    sv.evaluar_lote(db, did, lid, impacto='SIN_IMPACTO', justificacion='ok', usuario='x')
    db.commit()
    assert 'crítica necesita' in _error(db, sv.enviar, db, did, firma_id=_f(db, gente['prod']), usuario='x')


def test_mayor_puede_justificar_que_no_hacen_falta_acciones(db, gente):
    did = _reportar(db, gente)
    _clasificar(db, gente, did, 'MAYOR')
    sv.guardar_investigacion(db, did, {'causa_raiz': 'x', 'categoria_causa': 'Mano de obra'}, 'x')
    db.commit()
    assert 'mano de obra' in _error(db, sv.enviar, db, did, firma_id=_f(db, gente['prod']), usuario='x')
    sv.guardar_investigacion(db, did, {'sin_acciones_motivo': 'Fue un caso aislado ya corregido'}, 'x')
    sv.enviar(db, did, firma_id=_f(db, gente['prod']), usuario='x')
    db.commit()
    assert sv.desviacion(db, did)['estatus'] == 'POR_APROBAR'


def test_con_dos_parecidas_pide_una_preventiva(db, gente):
    eq = db.execute("INSERT INTO equipos (codigo, nombre, tipo) VALUES (?, 'Báscula', 'Báscula')",
                    (f'EQ-DV-{next(_n)}',)).lastrowid
    db.commit()
    for _ in range(2):
        _reportar(db, gente, equipo_id=eq)
    did = _reportar(db, gente, equipo_id=eq)
    _clasificar(db, gente, did)
    _investigar(db, gente, did)
    assert len(sv.parecidas(db, sv.desviacion(db, did))) == 2
    assert 'preventiva' in _error(db, sv.enviar, db, did, firma_id=_f(db, gente['prod']), usuario='x')
    sv.agregar_accion(db, did, tipo='PREVENTIVA', descripcion='Verificar la báscula diario', responsable=gente['prod'],
                      fecha_compromiso=_hoy(5), usuario='x')
    sv.enviar(db, did, firma_id=_f(db, gente['prod']), usuario='x')
    db.commit()


# --- 6. Quién firma qué ------------------------------------------------------------
def test_quien_investigo_no_aprueba(db, gente):
    did = _reportar(db, gente)
    sv.clasificar(db, did, clasificacion='MAYOR', responsable=gente['jefe2'],
                  firma_id=_f(db, gente['jefe'], 'Aprobó', 'gestionar_desviaciones'), usuario='x')
    _investigar(db, gente, did)
    sv.enviar(db, did, firma_id=_f(db, gente['jefe2']), usuario='x')
    db.commit()
    assert 'propia' in _error(db, sv.aprobar, db, did,
                              firma_id=_f(db, gente['jefe2'], 'Aprobó', 'gestionar_desviaciones'), usuario='x')
    sv.regresar(db, did, comentarios='Falta el 5 porqués', usuario='x')
    db.commit()
    d = sv.desviacion(db, did)
    assert d['estatus'] == 'EN_INVESTIGACION' and d['comentarios_aprobacion'] == 'Falta el 5 porqués'


def test_reprogramar_pide_motivo_y_firma_y_queda_en_historial(db, gente):
    did = _reportar(db, gente)
    (aid,) = _hasta_acciones(db, gente, did)
    fid = _f(db, gente['jefe'], 'Autorizó', 'gestionar_desviaciones')
    assert 'motivo' in _error(db, sv.reprogramar, db, aid, motivo='', firma_id=fid, usuario='x',
                              fecha_compromiso=_hoy(20))
    assert 'facultad' in _error(db, sv.reprogramar, db, aid, motivo='Falta refacción',
                                firma_id=_f(db, gente['analista'], 'Autorizó'), usuario='x', fecha_compromiso=_hoy(20))
    sv.reprogramar(db, aid, motivo='Falta refacción', firma_id=_f(db, gente['jefe'], 'Autorizó', 'gestionar_desviaciones'),
                   usuario='x', fecha_compromiso=_hoy(20))
    db.commit()
    assert sv.accion(db, aid)['fecha_compromiso'] == _hoy(20)
    h = [x for x in sv.historial(db, did) if x['campo'] == 'fecha_compromiso']
    assert h and h[0]['valor_anterior'] == _hoy(10) and h[0]['motivo'] == 'Falta refacción' and h[0]['firma_id']


def test_quien_hizo_la_accion_no_la_verifica(db, gente):
    did = _reportar(db, gente)
    (aid,) = _hasta_acciones(db, gente, did)
    assert 'responsable' in _error(db, sv.marcar_hecha, db, aid, como_se_hizo='x', firma_id=_f(db, gente['analista']),
                                   usuario='x')
    sv.marcar_hecha(db, aid, como_se_hizo='Se actualizó el PNO', firma_id=_f(db, gente['prod']), usuario='x')
    db.commit()
    assert 'no la puede verificar' in _error(db, sv.verificar, db, aid, firma_id=_f(db, gente['prod'], 'Verificó'),
                                             usuario='x')
    sv.verificar(db, aid, firma_id=_f(db, gente['analista'], 'Verificó'), usuario='x')
    db.commit()
    assert sv.accion(db, aid)['estatus'] == 'VERIFICADA'


# --- 7. Capacitación -----------------------------------------------------------------
def test_accion_de_capacitacion_se_completa_con_lo_que_registra_rh(db, gente):
    did = _reportar(db, gente)
    _clasificar(db, gente, did)
    curso = db.execute("INSERT INTO cursos (nombre, tipo) VALUES ('BPM pesadas', 'BPM')").lastrowid
    aid = sv.agregar_accion(db, did, tipo='CORRECTIVA', descripcion='Capacitar en pesadas', responsable=gente['prod'],
                            fecha_compromiso=_hoy(10), curso_id=curso, personas=[gente['oper'], gente['prod']],
                            usuario='x')
    db.execute("INSERT INTO capacitacion_registro (numero_empleado, curso_id, estatus, fecha) VALUES (?, ?, 'Tomado', ?)",
               (gente['oper'], curso, _hoy()))
    db.execute("INSERT INTO capacitacion_registro (numero_empleado, curso_id, estatus, fecha) VALUES (?, ?, 'Tomado', ?)",
               (gente['prod'], curso, _hoy(-400)))      # tomado ANTES de la acción: no cuenta
    db.commit()
    avance = sv.avance_capacitacion(db, aid)
    assert (avance['tomadas'], avance['total'], avance['completa']) == (1, 2, False)
    db.execute('UPDATE capacitacion_registro SET fecha = ? WHERE numero_empleado = ? AND curso_id = ?',
               (_hoy(), gente['prod'], curso))
    db.commit()
    assert sv.avance_capacitacion(db, aid)['completa']
    # Calidad la verifica sin que nadie la marque hecha
    sv.verificar(db, aid, firma_id=_f(db, gente['analista'], 'Verificó'), usuario='x')
    db.commit()
    assert sv.accion(db, aid)['estatus'] == 'VERIFICADA'


# --- 8. Cerrar -----------------------------------------------------------------------
def test_cerrar_exige_acciones_verificadas_lotes_evaluados_y_respuesta(db, gente):
    did = _reportar(db, gente, 'QUEJA', cliente='Granja')
    (aid,) = _hasta_acciones(db, gente, did)
    lid = crear_lote(db, cantidad=1)
    sv.ligar_lote(db, did, lid, 'x')
    db.commit()
    cerrar = lambda: sv.cerrar(db, did, conclusion='Listo', usuario='x',  # noqa: E731
                               firma_id=_f(db, gente['jefe'], 'Cerró', 'gestionar_desviaciones'))
    msg = _error(db, cerrar)
    assert 'Acción 1' in msg and 'impacto' in msg and 'respuesta al cliente' in msg
    sv.marcar_hecha(db, aid, como_se_hizo='Hecho', firma_id=_f(db, gente['prod']), usuario='x')
    sv.verificar(db, aid, firma_id=_f(db, gente['analista'], 'Verificó'), usuario='x')
    sv.evaluar_lote(db, did, lid, impacto='SIN_IMPACTO', justificacion='Otro proceso', usuario='x')
    sv.registrar_respuesta(db, did, respuesta='Se repuso el producto', fecha=_hoy(), usuario='x')
    db.commit()
    assert sv.faltantes_cierre(db, did) == []
    cerrar()
    db.commit()
    assert sv.desviacion(db, did)['estatus'] == 'CERRADA'


# --- 9. Eficacia -----------------------------------------------------------------------
def test_accion_no_eficaz_abre_otra_desviacion_ligada(db, gente):
    did = _reportar(db, gente, area='Laboratorio')
    (aid,) = _hasta_acciones(db, gente, did)
    db.execute("UPDATE dev_acciones SET eficacia_criterio = 'No se repite en 3 meses', eficacia_fecha = ? WHERE id = ?",
               (_hoy(30), aid))
    sv.marcar_hecha(db, aid, como_se_hizo='Hecho', firma_id=_f(db, gente['prod']), usuario='x')
    sv.verificar(db, aid, firma_id=_f(db, gente['analista'], 'Verificó'), usuario='x')
    db.commit()
    assert 'toca' in _error(db, sv.revisar_eficacia, db, aid, eficaz=0, comentario='x',
                            firma_id=_f(db, gente['analista'], 'Verificó'), usuario='x')
    db.execute('UPDATE dev_acciones SET eficacia_fecha = ? WHERE id = ?', (_hoy(-1), aid))
    db.commit()
    from modulos.desviaciones import ganchos
    usuario = {'numero_empleado': gente['analista'], 'roles': ['calidad'], 'facultades': []}
    assert any('eficacia' in p['texto'] for p in ganchos.pendientes(db, usuario))
    nueva = sv.revisar_eficacia(db, aid, eficaz=0, comentario='Se repitió en noviembre',
                                firma_id=_f(db, gente['analista'], 'Verificó'), usuario='x')
    db.commit()
    n = sv.desviacion(db, nueva)
    assert n['origen_tipo'] == 'accion_no_eficaz' and n['origen_id'] == aid and n['area'] == 'Laboratorio'
    assert [r['id'] for r in sv.relacionadas(db, did)] == [nueva]
    assert sv.accion(db, aid)['eficaz'] == 0


# --- 10. Enlaces -----------------------------------------------------------------------
def test_enlace_reportar_desviacion_oficina_y_tablet(app):
    with app.test_request_context():
        url = registro.enlace('reportar_desviacion', origen='orden_paso', origen_id=31, texto='OP-1 · paso 5',
                              folio='OP-1', lotes=[3, 4], volver='/ordenes/31')
        assert url.startswith('/desviaciones/nueva?') and 'lotes=3,4' in url and 'folio=OP-1' in url
        assert registro.enlace('reportar_desviacion', origen='calibracion', origen_id=2,
                               planta=True).startswith('/desviaciones/planta/nueva?')
        assert registro.enlace('desviacion', desviacion_id=7) == '/desviaciones/7'


# --- 11. Alcance ---------------------------------------------------------------------------
def test_alcance_muestra_ordenes_lotes_presentaciones_y_clientes(db, gente):
    n = next(_n)
    mp = _lote_aprobado(db, gente)
    inventario.mover(db, lote_id=mp, tipo='CONSUMO_PRODUCCION', cantidad=5, usuario='x', modulo='pruebas',
                     folio=f'OP-ALC-{n}')
    gr = crear_material(db, tipo='GR')
    pt = crear_material(db, tipo='PT', material_padre_id=gr, unidad='pza')
    granel = crear_lote(db, gr, cantidad=20, origen='PRODUCCION', folio=f'OP-ALC-{n}')
    inventario.cambiar_estatus(db, lote_id=granel, nuevo='APROBADO', usuario='x', motivo='ok',
                               firma_id=_f(db, gente['jefe'], 'Liberó', 'liberar_lotes'))
    presentacion = crear_lote(db, pt, cantidad=10, origen='PRODUCCION', lote_padre_id=granel)
    inventario.mover(db, lote_id=presentacion, tipo='VENTA', cantidad=4, usuario='x', modulo='pruebas',
                     motivo='Granja La Esperanza')
    db.commit()
    did = _reportar(db, gente, lotes=[mp])
    alcance = sv.alcance(db, did)
    relaciones = {(r['relacion'], r['lote_id']) for r in alcance}
    assert ('PRODUCIDO', granel) in relaciones and ('PRESENTACION', presentacion) in relaciones
    venta = [r for r in alcance if r['relacion'] == 'VENTA'][0]
    assert venta['cliente'] == 'Granja La Esperanza' and venta['cantidad'] == 4
    assert sv.agregar_alcance(db, did, 'x') == 2
    db.commit()
    assert {l['lote_id'] for l in sv.lotes(db, did)} == {mp, granel, presentacion}


# --- 12. Indicadores, pendientes y alertas ------------------------------------------------
def test_alertas_kpis_y_pendientes(db, gente):
    from modulos.desviaciones import ganchos
    did = _reportar(db, gente)
    _clasificar(db, gente, did, 'CRITICA')
    folio = sv.desviacion(db, did)['folio']
    assert any(a['nombre'] == f'{folio} crítica abierta' and a['nivel'] == 'rojo' for a in ganchos.alertas(db))
    assert ganchos.kpis(db)[0]['titulo'] == 'Desviaciones abiertas'
    pend = ganchos.pendientes(db, {'numero_empleado': gente['prod'], 'roles': ['produccion'], 'facultades': []})
    assert any(p['texto'].startswith(f'Investigar {folio}') for p in pend)
    jefe = {'numero_empleado': gente['jefe'], 'roles': ['calidad'], 'facultades': ['gestionar_desviaciones']}
    _reportar(db, gente)
    assert any(p['texto'].startswith('Clasificar') for p in ganchos.pendientes(db, jefe))
    assert ganchos.pendientes(db, {'numero_empleado': gente['oper'], 'roles': ['operador'], 'facultades': []}) == []


def test_tendencias_y_lista(db, gente):
    did = _reportar(db, gente, area='Almacén')
    t = sv.tendencias(db)
    assert len(t['por_mes']) == 12 and t['por_mes'][-1]['n'] >= 1
    assert any(x['etiqueta'] == 'Almacén' for x in t['por_area'])
    filas, total = sv.buscar(db, pestana='clasificar', area='Almacén')
    assert did in [f['id'] for f in filas] and total >= 1


def test_migracion_es_idempotente():
    from nucleo.esquema import aplicar_migraciones_de, version_actual
    from modulos.desviaciones.esquema import MIGRACIONES
    conn = conexion()
    try:
        assert aplicar_migraciones_de(conn, 'desviaciones', MIGRACIONES) == 0
        assert version_actual(conn, 'desviaciones') == 1
    finally:
        conn.close()
