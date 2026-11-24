"""H5: un único Compose up desde una copia limpia, sin credenciales heredadas.

Se copian fuentes versionadas y nuevas, excluyendo .env y archivos ignorados.
La prueba de arranque usa Docker real, proyecto/volúmenes propios y puerto
efímero. Su limpieza solo alcanza ese proyecto; no toca la instalación habitual.
Los demás casos validan el modelo Compose, la documentación y los secretos.
"""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import uuid

import pytest

from tests.conftest import ROOT


SECRET_NAMES = {
    'MATRICULAS_DB_PASSWORD', 'MATRICULAS_API_KEYS', 'MATRICULAS_JWT_SECRET',
    'ORCHESTRATOR_API_KEY', 'INTERNAL_KEY', 'PAGOS_API_KEY', 'PAGOS_CALLBACK_KEY',
    'INSCRIPCION_WRITER_KEY',
}
SERVICES = {'cupos', 'matriculas-db', 'matriculas-api', 'orquestador', 'pagos', 'bff', 'gateway'}


@pytest.fixture
def clon(tmp_path):
    # Incluir archivos nuevos permite probar la solución antes de git add.
    tracked = subprocess.run(['git', '-c', f'safe.directory={ROOT}', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                             cwd=ROOT, capture_output=True, check=True).stdout.decode().split('\0')
    folder = tmp_path / 'clon limpio'
    folder.mkdir()
    for relative in tracked:
        if not relative or Path(relative).name == '.env':
            continue
        source, dest = ROOT / relative, folder / relative
        if source.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
    env = dict(os.environ)
    names = set(re.findall(r'\$\{([A-Za-z_][A-Za-z_0-9]*)',
                           (folder / 'docker-compose.yml').read_text(encoding='utf-8')))
    names |= SECRET_NAMES | {'API_KEY', 'API_KEYS', 'JWT_SECRET'}
    for name in list(env):
        if name in names or name.startswith('COMPOSE_'):
            env.pop(name)
    # El test no escribe metadatos de Buildx en el perfil Docker del usuario.
    env['BUILDX_CONFIG'] = str(tmp_path / 'buildx')
    assert not (folder / '.env').exists()
    return folder, env


def _compose(clon, *args, project=None, override=None, timeout=30):
    folder, env = clon
    command = ['docker', 'compose', '--project-name', project or 'h5config-' + uuid.uuid4().hex,
               '--project-directory', str(folder), '-f', str(folder / 'docker-compose.yml')]
    if override:
        command += ['-f', str(override)]
    return subprocess.run([*command, *args], cwd=folder, env=env,
                          capture_output=True, text=True, timeout=timeout)


def _generate(clon):
    folder, env = clon
    result = subprocess.run([sys.executable, 'scripts/generar_env.py'], cwd=folder,
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    values = dict(line.split('=', 1) for line in (folder / '.env').read_text(encoding='utf-8').splitlines()
                  if line and not line.startswith('#') and '=' in line)
    return {name: values[name] for name in SECRET_NAMES}, result


def test_h5_compose_resuelve_desde_clon_limpio_sin_generar_dotenv(clon):
    result = _compose(clon, 'config', '--format', 'json')
    assert result.returncode == 0, (
        'Compose exige preparación antes del único up solicitado:\n' + result.stderr)
    assert SERVICES <= set(json.loads(result.stdout)['services'])
    assert not (clon[0] / '.env').exists(), 'Resolver Compose no debe escribir credenciales'


def test_h5_plan_de_unico_up_no_necesita_paso_previo(clon):
    result = _compose(clon, '--dry-run', 'up', '--build', '-d', timeout=60)
    assert result.returncode == 0, (
        'El plan de docker compose up no funciona desde un clon limpio:\n' + result.stderr)


def test_h5_readme_documenta_un_solo_compose_up_para_el_arranque():
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    section = readme.split('### Levantar la integración', 1)[1].split('\n### ', 1)[0]
    block = re.search(r'```bash\s*\n(.*?)```', section, re.S).group(1)
    commands = [line.strip() for line in block.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    assert len(commands) == 1, f'El procedimiento exige {len(commands)} comandos: {commands}'
    tokens = shlex.split(commands[0])
    assert tokens[:3] == ['docker', 'compose', 'up']
    assert set(tokens[3:]) <= {'--build', '-d', '--detach', '--wait'}


def test_h5_control_configurado_solo_gateway_publica_puerto(clon):
    _generate(clon)
    result = _compose(clon, 'config', '--format', 'json')
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)['services']
    published = {name for name, service in services.items() if service.get('ports')}
    assert published == {'gateway'}
    port = services['gateway']['ports'][0]
    assert str(port['published']) == '8000' and port['target'] == 8080
    assert port['host_ip'] == '127.0.0.1'


def test_h5_control_generacion_segura_no_rota_ni_publica_secretos(clon):
    first, output1 = _generate(clon)
    again, output2 = _generate(clon)
    independent = clon[0].parent / 'otra instalacion'
    shutil.copytree(clon[0] / 'scripts', independent / 'scripts')
    shutil.copy2(clon[0] / '.env.example', independent / '.env.example')
    other, output3 = _generate((independent, clon[1]))
    assert all(first[name] == again[name] for name in SECRET_NAMES), 'Se rotaron credenciales existentes'
    assert all(first[name] != other[name] for name in SECRET_NAMES), 'Dos instalaciones comparten secretos'
    assert all(len(secret) >= 24 for secret in first.values())
    assert len(set(first.values())) == len(SECRET_NAMES)
    output = output1.stdout + output1.stderr + output2.stdout + output2.stderr + output3.stdout + output3.stderr
    assert all(secret not in output for secret in [*first.values(), *other.values()])
    if os.name == 'posix':
        assert (clon[0] / '.env').stat().st_mode & 0o777 == 0o600


def test_h5_up_real_desde_clon_limpio_con_volumenes_aislados(clon):
    project = 'h5test-' + uuid.uuid4().hex
    override = clon[0] / 'h5-puerto-efimero.yml'
    override.write_text('services:\n  gateway:\n    ports: !override\n'
                        '      - "127.0.0.1::8080"\n', encoding='utf-8')
    # Incluso en RED, down solo puede apuntar al namespace aleatorio del test.
    assert project.startswith('h5test-') and project != 'integracion-sistemas-t1'
    try:
        result = _compose(clon, 'up', '--build', '-d', '--wait', '--wait-timeout', '120',
                          project=project, override=override, timeout=600)
        assert result.returncode == 0, (
            'El único up desde el clon sin .env no levantó la integración:\n' + result.stderr)
        running = _compose(clon, 'ps', '--format', 'json', project=project, override=override)
        assert running.returncode == 0, running.stderr
        rows = [json.loads(line) for line in running.stdout.splitlines() if line.strip()]
        states = {row['Service']: row['State'] for row in rows}
        assert all(states.get(service) == 'running' for service in SERVICES), states
        publishers = [(row['Service'], port) for row in rows for port in row.get('Publishers', [])
                      if port.get('PublishedPort', 0)]
        assert len(publishers) == 1 and publishers[0][0] == 'gateway'
        port = publishers[0][1]
        assert port['URL'] == '127.0.0.1'
        # La misma prueba funciona desde el host y desde el runner: la interfaz
        # publicada en 127.0.0.1 del daemon no es el loopback del runner. La sonda
        # HTTP se ejecuta dentro de Gateway y nunca extrae su secreto al stdout.
        probe_code = '''import hashlib,json,os,urllib.request
key=os.environ['MATRICULAS_API_KEYS']
with urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3) as response:
    assert response.status == 200
request=urllib.request.Request('http://127.0.0.1:8080/v1/bff/dashboard',headers={'X-API-Key':key.split(',')[0]})
with urllib.request.urlopen(request,timeout=10) as response:
    assert response.status == 200
    assert {'cursos','matriculas'} <= json.load(response).keys()
print(hashlib.sha256(key.encode()).hexdigest())
'''
        probe_command = ('exec', '-T', 'gateway', 'python', '/usr/local/bin/service_env.py',
                         'python', '-c', probe_code)
        probe = _compose(clon, *probe_command, project=project, override=override)
        assert probe.returncode == 0, probe.stderr
        digest = probe.stdout.strip()
        restarted = _compose(clon, 'up', '-d', '--wait', '--wait-timeout', '120',
                             project=project, override=override, timeout=180)
        assert restarted.returncode == 0, restarted.stderr
        probe = _compose(clon, *probe_command, project=project, override=override)
        assert probe.returncode == 0, probe.stderr
        assert probe.stdout.strip() == digest, 'El segundo up rotó las credenciales'
    finally:
        cleanup = _compose(clon, 'down', '--volumes', '--remove-orphans',
                           project=project, override=override, timeout=60)
        if cleanup.returncode:
            # En RED incluso down falla al interpolar sin .env. Verificar que
            # ese fallo ocurrió antes de crear recursos del proyecto aislado.
            label = f'label=com.docker.compose.project={project}'
            for command in (['docker', 'ps', '-aq', '--filter', label],
                            ['docker', 'volume', 'ls', '-q', '--filter', label],
                            ['docker', 'network', 'ls', '-q', '--filter', label]):
                resources = subprocess.run(command, capture_output=True, text=True, check=True, timeout=10)
                assert not resources.stdout.strip(), f'Quedaron recursos del proyecto de prueba {project}'
