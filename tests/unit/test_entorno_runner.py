"""Regresiones de ejecución desde un runner con el repo montado desde otro entorno."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import grpc
import pytest
import requests

from tests.conftest import ROOT
from tests.support import servicios
from tests.unit.test_api_unidad import m


def test_api_en_proceso_usa_stubs_aislados_del_host(m):
    app_dir = Path(m.main.__file__).parent
    assert app_dir != ROOT/'matriculas-api/app'
    generated = app_dir/'grpc_stubs/cupos_pb2_grpc.py'
    assert generated.exists()
    # Importar el stub ya dispara la comprobación del runtime gRPC. La prueba
    # anterior fallaba aquí si el repo contenía código generado en un host nuevo.
    module = __import__('app.grpc_stubs.cupos_pb2_grpc', fromlist=['CuposStub'])
    assert Path(module.__file__) == generated
    assert module.GRPC_VERSION == grpc.__version__


def test_cupos_inaccesible_da_diagnostico_y_no_skip(monkeypatch):
    channel = Mock()
    channel.__enter__ = Mock(return_value=channel)
    channel.__exit__ = Mock(return_value=None)
    monkeypatch.setattr(servicios.grpc, 'insecure_channel', Mock(return_value=channel))
    future = Mock()
    future.result.side_effect = grpc.FutureTimeoutError()
    monkeypatch.setattr(servicios.grpc, 'channel_ready_future', Mock(return_value=future))
    with pytest.raises(RuntimeError, match='docker compose logs'):
        servicios.esperar_cupos('cupos:50051', timeout=.01)


def test_catalogo_espera_recuperacion_del_circuito(monkeypatch):
    get = Mock(side_effect=[Mock(status_code=503), Mock(status_code=200)])
    monkeypatch.setattr(servicios.requests, 'get', get)
    sleep = Mock()
    monkeypatch.setattr(servicios.time, 'sleep', sleep)
    servicios.esperar_catalogo('http://api:8000', 'clave-de-prueba')
    assert get.call_count == 2
    sleep.assert_called_once_with(.5)


def test_catalogo_rechaza_credenciales_invalidas_sin_reintentar(monkeypatch):
    get = Mock(return_value=Mock(status_code=401))
    monkeypatch.setattr(servicios.requests, 'get', get)
    with pytest.raises(RuntimeError, match='clave del runner'):
        servicios.esperar_catalogo('http://api:8000', 'clave-invalida')
    assert get.call_count == 1


def test_runner_no_arranca_pytest_si_cupos_esta_caido(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location('runner', ROOT/'scripts/ejecutar_tests.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setattr(runner, 'esperar_api', lambda: None)
    monkeypatch.setattr(runner.sys, 'argv', ['ejecutar_tests.py', '--rapido'])
    monkeypatch.setattr(servicios, 'esperar_cupos', Mock(side_effect=RuntimeError('Cupos caído')))
    run = Mock()
    monkeypatch.setattr(runner.subprocess, 'run', run)
    assert runner.main() == 1
    run.assert_not_called()
    assert 'Preparación fallida: Cupos caído' in capsys.readouterr().err
