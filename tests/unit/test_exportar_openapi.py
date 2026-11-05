"""Regresiones de contratos entre Python 3.12 (Docker) y el entorno local."""
import builtins
import importlib
import importlib.util
import json
import sys
from http import HTTPStatus

from tests.conftest import ROOT
from tests.unit.test_api_unidad import m


def test_descripcion_422_no_depende_de_la_version_de_python(m, monkeypatch):
    # Python cambió la frase de 422; no debe cambiar nuestro contrato por ello.
    monkeypatch.setattr(HTTPStatus.UNPROCESSABLE_ENTITY, 'phrase', 'Etiqueta de otra version')
    monkeypatch.setattr(m.main.app, 'openapi_schema', None)
    spec = m.main.app.openapi()
    assert spec['paths']['/v1/enrollments']['post']['responses']['422']['description'] == 'Unprocessable Entity'
    orchestrator = importlib.import_module('app.orchestrator_main')
    monkeypatch.setattr(orchestrator.app, 'openapi_schema', None)
    assert orchestrator.app.openapi()['paths']['/v1/matriculas']['post']['responses']['422']['description'] == 'Unprocessable Entity'


def test_exportar_desde_url_no_importa_app_ni_modifica_orquestador(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('exportar', ROOT/'matriculas-api/exportar_openapi.py')
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    schema = {'openapi': '3.1.0', 'info': {'title': 'API remota', 'version': '2.0.0'}, 'paths': {}}
    monkeypatch.setattr(exporter, 'AQUI', tmp_path)
    monkeypatch.setattr(exporter, 'cargar_esquema', lambda url: schema)
    monkeypatch.setattr(sys, 'argv', ['exportar', '--desde-url', 'http://localhost:8000/openapi.json'])
    docs = tmp_path/'docs'
    docs.mkdir()
    orchestrator_file = docs/'orchestrator.openapi.json'
    orchestrator_file.write_text('contrato independiente\n')
    real_import = builtins.__import__
    def guard(name, *args, **kwargs):
        if name == 'app' or name.startswith('app.'):
            raise AssertionError('La exportación remota importó dependencias locales')
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guard)
    exporter.main()
    assert json.loads((docs/'openapi.json').read_text()) == schema
    assert exporter.yaml.safe_load((docs/'openapi.yaml').read_text()) == schema
    assert orchestrator_file.read_text() == 'contrato independiente\n'
