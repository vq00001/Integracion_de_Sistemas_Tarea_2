"""H6 RED: la evidencia debe demostrar qué circuito contiene la cascada.

Los CSV de estas pruebas son SINTÉTICOS y viven en tmp_path; no constituyen una
nueva medición ni sustituyen las tres repeticiones reales. Se prueban el plan,
el contrato de métricas y la interpretación, sin modificar resultados históricos.
"""
import importlib.util
import json
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from tests.conftest import ROOT
from tests.unit.test_api_unidad import m  # noqa: F401
from tests.unit.test_saga import setup_saga  # noqa: F401


def _load(name, path, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def exp(monkeypatch):
    return _load('h6_experimento', 'experiment/scripts/saga_fallas.py', monkeypatch)


@pytest.fixture
def graf(monkeypatch, tmp_path):
    monkeypatch.setenv('MPLCONFIGDIR', str(tmp_path / 'matplotlib'))
    module = _load('h6_graficos', 'experiment/graficar_saga_fallas.py', monkeypatch)
    out = tmp_path / 'resultados'
    out.mkdir()
    monkeypatch.setattr(module, 'OUT', out)
    return module


@pytest.fixture
def launches(exp, monkeypatch):
    recorded, stacks = {}, []
    monkeypatch.setenv('INSCRIPCION_WRITER_KEY', 'host-writer-key-que-no-debe-heredarse')
    monkeypatch.setattr(exp.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0))
    monkeypatch.setattr(exp.Stack, '_esperar', lambda *a: None)
    monkeypatch.setattr(exp.Stack, '_ampliar_cupos', lambda *a: None)
    def start(variant):
        stack = exp.Stack('h6-config', variant)
        stacks.append(stack)
        monkeypatch.setattr(stack.fake, 'start', lambda: None)
        envs = {}
        monkeypatch.setattr(stack, '_lanzar', lambda name, command, **env: envs.__setitem__(name, env))
        stack.start()
        recorded[variant] = envs
        return envs
    yield start
    for stack in stacks:
        shutil.rmtree(stack.W)


@pytest.mark.parametrize('rapido', [True, False], ids=['smoke', 'completo'])
def test_h6_plan_aisla_pagos_lento_del_breaker_de_transbank(exp, launches, rapido):
    envs = launches('con_patron')
    phases = exp.plan(rapido)
    targeted = [(i, phase) for i, phase in enumerate(phases)
                if getattr(phase, 'falla_en', None) == 'pagos' and phase.latencia_ms > 0]
    assert targeted, 'Falta una fase lenta sobre Pagos; la actual solo inyecta lentitud dentro del guard de Transbank'
    for index, phase in targeted:
        assert phase.destino == 'matriculas', 'Debe medirse la Saga completa'
        assert phase.latencia_ms / 1000 > float(envs['orquestador']['SAGA_TIMEOUT_SECONDS'])
        assert 0 < phase.workers <= int(envs['orquestador']['SAGA_BULKHEAD']), 'La admisión no debe enmascarar el breaker'
        assert any(p.destino == 'matriculas' and p.latencia_ms == 0 and not p.falla for p in phases[index+1:]), (
            'Falta recuperación sana después del fallo lento de Pagos')


@pytest.mark.parametrize('variant', ['con_patron', 'sin_patron'])
def test_h6_stack_declara_su_clave_escritora_sin_depender_del_host(launches, variant):
    envs = launches(variant)
    writer = envs['inscripcion'].get('INSCRIPCION_WRITER_KEY')
    assert writer and len(writer) >= 24, 'El experimento no prepara la credencial de escritura exigida por H1'
    assert writer == envs['orquestador'].get('INSCRIPCION_WRITER_KEY')
    assert writer != 'host-writer-key-que-no-debe-heredarse'


def test_h6_control_variantes_conservan_los_mismos_timeouts(launches):
    with_pattern, without = launches('con_patron'), launches('sin_patron')
    for service, name in [('orquestador', 'SAGA_TIMEOUT_SECONDS'), ('pagos', 'TBK_TIMEOUT_SECONDS')]:
        assert with_pattern[service][name] == without[service][name]


def _response(text, status=200):
    response = requests.Response()
    response.status_code = status
    response._content = text.encode()
    response.url = 'http://metricas.test/metrics'
    return response


ORQ_METRICS = '''circuito_estado{dependencia="COBRAR",estado="OPEN"} 1
saga_circuito_operacion_estado{dependencia="COBRAR",operacion="forward",estado="OPEN"} 1
saga_circuito_operacion_estado{dependencia="COBRAR",operacion="compensacion",estado="CLOSED"} 1
saga_circuito_operacion_estado{dependencia="COBRAR",operacion="consulta",estado="HALF_OPEN"} 1
saga_dependencia_intentos_total{dependencia="COBRAR",operacion="forward"} 7
saga_dependencia_intentos_total{dependencia="COBRAR",operacion="compensacion"} 5
saga_dependencia_intentos_total{dependencia="COBRAR",operacion="consulta"} 3
saga_dependencia_en_vuelo{dependencia="COBRAR",operacion="forward"} 2
saga_admision_rechazos_total 11
compensaciones_total 5
'''
PAY_METRICS = '''circuito_estado{dependencia="TRANSBANK",estado="CLOSED"} 1
transbank_intentos_total 19
bulkhead_rechazos_total{dependencia="TRANSBANK"} 0
circuito_rechazos_total{dependencia="TRANSBANK"} 0
'''


def test_h6_muestreo_distingue_operaciones_intentos_y_carga(exp, monkeypatch):
    stack = SimpleNamespace(orq='http://orq.test', pagos='http://pagos.test')
    monkeypatch.setattr(exp.requests, 'get', lambda url, **kw: _response(ORQ_METRICS if url.startswith(stack.orq) else PAY_METRICS))
    row = exp.muestra_metricas(stack)
    expected = {'orq_cb_COBRAR': 'OPEN', 'orq_cb_COBRAR_compensacion': 'CLOSED',
                'orq_cb_COBRAR_consulta': 'HALF_OPEN', 'orq_intentos_COBRAR_forward': 7,
                'orq_intentos_COBRAR_compensacion': 5, 'orq_intentos_COBRAR_consulta': 3,
                'orq_en_vuelo_COBRAR_forward': 2, 'orq_admision_rech': 11, 'pagos_tbk_intentos': 19}
    for key, value in expected.items():
        assert row.get(key) == value, f'No se conserva la medición {key}'


@pytest.mark.parametrize('service', ['orq', 'pagos'])
def test_h6_error_http_de_metricas_no_se_marca_como_muestra_valida(exp, monkeypatch, service):
    stack = SimpleNamespace(orq='http://orq.test', pagos='http://pagos.test')
    def get(url, **kwargs):
        failed = url.startswith(getattr(stack, service))
        return _response('servicio ocupado' if failed else ORQ_METRICS + PAY_METRICS, 503 if failed else 200)
    monkeypatch.setattr(exp.requests, 'get', get)
    row = exp.muestra_metricas(stack)
    assert row[service + '_ok'] == 0, 'HTTP 503 no es una observación válida del estado del circuito'


def test_h6_contador_ausente_no_se_inventa_como_cero_intentos(exp, monkeypatch):
    monkeypatch.setattr(exp.requests, 'get', lambda *a, **kw: _response(''))
    row = exp.muestra_metricas(SimpleNamespace(orq='http://orq.test', pagos='http://pagos.test'))
    value = row.get('pagos_tbk_intentos')
    assert value is None or pd.isna(value), 'Ausencia de telemetría no demuestra cero llamadas'


def test_h6_exportador_cuenta_solo_intentos_reales_por_operacion(setup_saga, exp, monkeypatch):
    sg, obj, sessions, _, _ = setup_saga
    sc = importlib.import_module('app.saga_clients')
    api = importlib.import_module('app.orchestrator_main')
    observed = []
    def charge(*args, **kwargs):
        observed.append('forward')
        raise TimeoutError()
    client = sc.Client(charge, lambda *a, **k: observed.append('compensacion'), threshold=1,
                       inquiry=lambda *a, **k: observed.append('consulta'))
    for _ in range(2):
        with pytest.raises(sc.DependencyFailure):
            client.forward({}, 'cobro')
    client.compensate({}, 'reembolso', 'cobro')
    client.query({}, 'consulta')
    assert observed == ['forward', 'compensacion', 'consulta']
    monkeypatch.setattr(api, 'orchestrator', obj)
    monkeypatch.setattr(api, 'sessions', sessions)
    obj.clients = {'COBRAR': client}
    metrics = exp.parsear_metricas(api.metrics())
    for operation in ('forward', 'compensacion', 'consulta'):
        assert exp._valor(metrics, 'saga_dependencia_intentos_total', dependencia='COBRAR', operacion=operation) == 1, (
            'Faltan intentos reales separados; el contador agregado incluye llamadas no enviadas')
    assert any(name == 'saga_admision_rechazos_total' for name, _, _ in metrics)


def test_h6_exportador_mide_ocupacion_real_y_la_libera(setup_saga, exp, monkeypatch):
    _, obj, sessions, _, _ = setup_saga
    sc = importlib.import_module('app.saga_clients')
    api = importlib.import_module('app.orchestrator_main')
    started, release = threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        started.set()
        assert release.wait(3)
    obj.clients = {'COBRAR': sc.Client(blocked, lambda *a, **k: None)}
    monkeypatch.setattr(api, 'orchestrator', obj)
    monkeypatch.setattr(api, 'sessions', sessions)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(obj.clients['COBRAR'].forward, {}, 'cobro')
        try:
            assert started.wait(3)
            metrics = exp.parsear_metricas(api.metrics())
            assert exp._valor(metrics, 'saga_dependencia_en_vuelo', dependencia='COBRAR', operacion='forward') == 1
        finally:
            release.set()
        future.result(timeout=3)
    metrics = exp.parsear_metricas(api.metrics())
    assert exp._valor(metrics, 'saga_dependencia_en_vuelo', dependencia='COBRAR', operacion='forward') == 0


def test_h6_control_h2_abre_tras_cinco_fallas_y_la_siguiente_saga_no_llama_dependencias(setup_saga):
    sg, obj, _, _, _ = setup_saga
    sc = importlib.import_module('app.saga_clients')
    events = []
    def callback(step, kind):
        def apply(*args, **kwargs):
            events.append((step, kind))
            if step == 'COBRAR' and kind == 'forward':
                raise TimeoutError()
        return apply
    obj.clients = {step: sc.Client(callback(step, 'forward'), callback(step, 'compensacion'), threshold=5)
                   for step in sg.STEPS}
    for number in range(5):
        result, _ = obj.matricular(f'h6-{number}', f'alumno-{number}', 1, 100)
        assert result['estado'] == 'COMPENSADA'
    assert events.count(('COBRAR', 'forward')) == events.count(('COBRAR', 'compensacion')) == 5
    assert obj.clients['COBRAR'].breaker.state.value == 'OPEN'
    before = list(events)
    result, _ = obj.matricular('h6-rechazada', 'alumno-rechazado', 1, 100)
    assert result['motivo_fallo'] == 'PAGOS_CIRCUITO_ABIERTO'
    assert events == before, 'Con el breaker abierto no deben reservarse cupos ni intentarse cobros'


def test_h6_exportador_cuenta_rechazos_de_admision_antes_de_las_dependencias(setup_saga, exp, monkeypatch):
    sg, _, sessions, _, _ = setup_saga
    sc = importlib.import_module('app.saga_clients')
    api = importlib.import_module('app.orchestrator_main')
    from app.errors import TooManyRequestsError
    started, release = threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        started.set()
        assert release.wait(3)
    clients = {step: sc.Client(blocked if step == 'COBRAR' else lambda *a, **k: None,
                               lambda *a, **k: None) for step in sg.STEPS}
    with monkeypatch.context() as config:
        config.setenv('SAGA_BULKHEAD', '1')
        obj = sg.Orchestrator(sessions, clients)
    monkeypatch.setattr(api, 'orchestrator', obj)
    monkeypatch.setattr(api, 'sessions', sessions)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(obj.matricular, 'h6-admitida', 'alumno-uno', 1, 100)
        try:
            assert started.wait(3)
            with pytest.raises(TooManyRequestsError):
                obj.matricular('h6-sobrecarga', 'alumno-dos', 1, 100)
            metrics = exp.parsear_metricas(api.metrics())
            assert exp._valor(metrics, 'saga_admision_rechazos_total') == 1, (
                'La sobrecarga rechazada antes de reservar debe quedar visible en la evidencia de H3/H6')
        finally:
            release.set()
        assert first.result(timeout=3)[0]['estado'] == 'COMPLETADA'


PHASES = ('antes', 'lenta', 'alivio', 'bulkhead', 'pico', 'caida', 'recuperacion')


def _data(open_reps=3, pending=0):
    requests_rows, probes, metrics, phases, states = [], [], [], [], []
    for variant in ('con_patron', 'sin_patron'):
        for rep in (1, 2, 3):
            counters = dict(pagos_tbk_intentos=0, pagos_bulkhead_rech=0, pagos_circuito_rech=0,
                            orq_intentos_COBRAR_forward=0, orq_intentos_COBRAR_compensacion=0,
                            orq_intentos_COBRAR_consulta=0, orq_admision_rech=0, orq_compensaciones=0)
            for index, phase in enumerate(PHASES):
                start, end = index * 2, index * 2 + 2
                phases.append(dict(variante=variant, rep=rep, fase=phase, t_ini=start, t_fin=end,
                                   workers=4, think_s=0.1, tbk_latencia_ms=0 if phase == 'lenta' else 1200,
                                   tbk_caido=0, destino='pagos' if phase == 'bulkhead' else 'matriculas',
                                   falla_en='pagos' if phase == 'lenta' else 'transbank',
                                   pagos_latencia_ms=4700 if phase == 'lenta' else 0,
                                   saga_timeout_s=0.4, tbk_timeout_s=0.2, comprador='aprobar'))
                cases = [
                        (202, 0.01, ''), (429, 0.01, ''),
                        (503, 0.01, 'PAGOS_CIRCUITO_ABIERTO'), (503, 0.6, 'COBRAR:DependencyFailure')]
                if variant == 'sin_patron':
                    cases = [(202, 0.01, '')] + [(503, 0.6, 'COBRAR:DependencyFailure')] * 3
                for i, (code, latency, reason) in enumerate(cases):
                    requests_rows.append(dict(variante=variant, rep=rep, fase=phase, t=start + 0.1 + i * 0.2,
                                              codigo=code, estado='COMPENSADA' if code == 503 else 'ESPERANDO_PAGO' if code == 202 else None,
                                              motivo_fallo=reason, error_code='TOO_MANY_REQUESTS' if code == 429 else '',
                                              saga_id=f'{variant}-{rep}-{index}-{i}' if code != 429 else '', latencia_s=latency))
                for t in (start, start + 1, end):
                    if t == end:
                        for name, delta in [('pagos_tbk_intentos', 3), ('orq_intentos_COBRAR_forward', 2 if variant == 'con_patron' else 4),
                                            ('orq_intentos_COBRAR_compensacion', 1), ('orq_intentos_COBRAR_consulta', 1),
                                            ('orq_admision_rech', 1 if variant == 'con_patron' else 0), ('orq_compensaciones', 2)]:
                            counters[name] += delta
                    row = dict(variante=variant, rep=rep, fase=phase, t=t, orq_ok=1, pagos_ok=1,
                               pagos_cb='CLOSED', orq_cb_COBRAR='OPEN' if variant == 'con_patron' and rep <= open_reps and phase == 'lenta' and t > start else 'CLOSED',
                               orq_cb_COBRAR_compensacion='CLOSED', orq_cb_COBRAR_consulta='CLOSED',
                               orq_en_vuelo_COBRAR_forward=2 if t == start+1 else 0, **counters)
                    for dep in ('RESERVAR', 'COBRAR', 'INSCRIBIR'):
                        row['orq_bulkhead_rech_' + dep] = 0
                    for state in ('EN_CURSO', 'ESPERANDO_PAGO', 'COMPLETADA', 'COMPENSANDO', 'COMPENSADA', 'COMPENSACION_PENDIENTE'):
                        row['orq_sagas_' + state] = 0
                    metrics.append(row)
                for name in ('orq_sagas', 'ins_enrollments', 'pagos_health'):
                    probes.append(dict(variante=variant, rep=rep, fase=phase, t=start+1, sonda=name, codigo=200, latencia_s=0.02))
            states.append(dict(variante=variant, rep=rep, momento='final', t=14, convergencia_s=-1 if pending else 0.1,
                               sagas_en_vuelo=0, sagas_COMPLETADA=0 if pending else 3, sagas_ESPERANDO_PAGO=pending,
                               sagas_COMPENSADA=5, sagas_COMPENSACION_PENDIENTE=0, cupos_huerfanos=0, cobros_huerfanos=0))
    return {name: pd.DataFrame(rows) for name, rows in [('requests', requests_rows), ('sondas', probes),
            ('metricas', metrics), ('fases', phases), ('estados', states)]}


def _report(graf, data, ts='h6_sintetico'):
    per_rep = graf.tabla_por_rep(data)
    table = graf.tabla_agregada(per_rep)
    return graf.resumen(data, table, per_rep, None, ts).read_text(encoding='utf-8')


def test_h6_tabla_separa_fail_fast_admision_intentos_y_compensaciones(graf):
    table = graf.tabla_por_rep(_data())
    expected = {'pct_fail_fast_orq', 'pct_429', 'intentos_a_pagos', 'rechazos_admision_orq',
                'compensaciones_orq', 'orq_cobros_en_vuelo_max'}
    assert expected <= set(table.columns), f'Faltan mediciones de la frontera Orquestador→Pagos: {sorted(expected - set(table.columns))}'
    row = table[(table.variante == 'con_patron') & (table.rep == 1) & (table.fase == 'lenta')].iloc[0]
    assert row['pct_rapidas'] == 75 and row['pct_fail_fast_orq'] == 25
    assert row['pct_429'] == 25 and row['rechazos_admision_orq'] == 1
    assert row['intentos_a_pagos'] == 2 and row['compensaciones_orq'] == 2
    assert row['orq_cobros_en_vuelo_max'] == 2


def test_h6_reinicio_de_contador_no_produce_intentos_negativos_validos(graf):
    data = _data()
    mask = (data['metricas'].variante == 'con_patron') & (data['metricas'].rep == 1)
    ids = data['metricas'][mask & (data['metricas'].fase == 'lenta')].index
    data['metricas'].loc[ids, 'pagos_tbk_intentos'] = 0
    with pytest.raises(ValueError, match='(?i)(contador|reset|reinici|métrica)'):
        graf.tabla_por_rep(data)


@pytest.mark.parametrize('opened', [0, 2, 3], ids=['0-de-3', '2-de-3', '3-de-3'])
def test_h6_resumen_cuantifica_apertura_del_orquestador_por_repeticion(graf, opened):
    text = _report(graf, _data(open_reps=opened))
    import re
    assert re.search(r'Orquestador→Pagos.{0,180}abrió en ' + str(opened) + r' de 3', text), (
        'El informe debe contar la apertura del orquestador incluso cuando Transbank no abre')


def test_h6_resumen_no_infiere_un_bug_de_codigo_solo_desde_metricas(graf):
    data = _data(open_reps=0)
    data['metricas'].loc[data['metricas'].fase == 'lenta', 'pagos_cb'] = 'OPEN'
    data['metricas']['orq_bulkhead_rech_COBRAR'] = data['metricas'].groupby(['variante', 'rep']).cumcount()
    text = _report(graf, data)
    assert 'comparten un mismo breaker' not in text, 'La corrida no demuestra una causa de código; H2 ya separó los circuitos'
    assert 'xfail estricto' not in text
    assert 'cada rechazo se trata como falla incierta' not in text, 'El diagnóstico fijo contradice H3'


def test_h6_resumen_usa_parametros_del_csv_en_vez_de_valores_fijos(graf):
    text = _report(graf, _data())
    assert any(value in text for value in ('4700', '4.7', '4,7')), 'Se omitió la latencia de Pagos registrada en esta corrida'
    assert '2 s la saga, 1,5 s hacia Transbank' not in text, 'Los timeouts del informe no corresponden al CSV'


def test_h6_resumen_de_recuperacion_no_afirma_que_el_comprador_nunca_paga(graf):
    text = _report(graf, _data(pending=0))
    assert 'el comprador no paga' not in text
    assert 'quedan en `ESPERANDO_PAGO`' not in text
    assert 'COMPLETADA 3' in text


def test_h6_control_tabla_y_limites_conservan_los_datos_sinteticos(graf):
    data = _data()
    table = graf.tabla_por_rep(data)
    assert len(table) == 2 * 3 * len(PHASES)
    assert set(table['rep']) == {1, 2, 3}
    text = _report(graf, data)
    assert 'SQLite' in text and 'simulad' in text and 'cupos huérfanos 0' in text


class PendingStack:
    variante = 'con_patron'
    orq, ins, pagos = 'http://orq.test', 'http://ins.test', 'http://pagos.test'
    def __init__(self, settle):
        self.settle, self.paid, self.buyers = settle, False, 0
    def alumno(self, session=None):
        return 'sintetico'
    def matricular(self, *args, **kwargs):
        return _response(json.dumps(self.saga('sonda')), 202)
    def saga(self, sid):
        return {'id': sid, 'estado': 'COMPLETADA' if self.paid else 'ESPERANDO_PAGO',
                'pago_url': self.pagos + '/ir/sonda'}
    def comprador(self, *args, **kwargs):
        self.buyers += 1
        self.paid = self.settle
    def falla_pagos(self, *args, **kwargs):
        pass
    def snapshot(self):
        states = {f'sagas_{state}': 0 for state in ('EN_CURSO', 'ESPERANDO_PAGO', 'COMPLETADA', 'COMPENSANDO', 'COMPENSADA', 'COMPENSACION_PENDIENTE')}
        states['sagas_COMPLETADA' if self.paid else 'sagas_ESPERANDO_PAGO'] = 1
        return {**states, 'cupos_ocupados': 1, 'pagos_COBRADO': int(self.paid),
                'pagos_PENDIENTE': int(not self.paid), 'inscripciones_CONFIRMADA': int(self.paid)}


def _minimal_cascade(exp, monkeypatch, settle):
    stack = PendingStack(settle)
    class Session:
        def get(self, *args, **kwargs):
            return _response('{}')
        def close(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(exp.requests, 'Session', Session)
    monkeypatch.setattr(exp, 'muestra_metricas', lambda stack: {})
    rows = exp.cascada(stack, [exp.Fase('recuperacion', 0, 0, 0.1)],
                       'h6_sintetico', drenaje_s=0.01)
    return stack, next(row for row in rows['estados'] if row['momento'] == 'final')


def test_h6_cascada_ejercita_al_comprador_y_completa_la_recuperacion(exp, monkeypatch):
    stack, final = _minimal_cascade(exp, monkeypatch, settle=True)
    assert stack.buyers > 0, 'E3 no ejerce el pago ni la inscripción tras recuperar la dependencia'
    assert final['sagas_COMPLETADA'] == 1 and final['sagas_ESPERANDO_PAGO'] == 0
    assert final['cupos_huerfanos'] == final['cobros_huerfanos'] == 0


def test_h6_pagos_pendientes_no_se_presentan_como_negocio_cerrado(exp, monkeypatch):
    _, final = _minimal_cascade(exp, monkeypatch, settle=False)
    assert final['sagas_ESPERANDO_PAGO'] == 1
    assert final.get('negocio_cerrado') is False or final['convergencia_s'] < 0, (
        'El drenaje operativo no demuestra cierre de negocio cuando aún se espera al comprador')
