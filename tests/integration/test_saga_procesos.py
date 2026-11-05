"""Integración aislada, sin Docker ni servicios externos; incluye muerte real del proceso."""
import os
import subprocess
import sys
from tests.conftest import ROOT


def test_saga_http_grpc_compensacion_y_recuperacion_con_sigkill(tmp_path):
    result=subprocess.run([sys.executable,str(ROOT/'tests/support/saga_scenario.py')],
        env={**os.environ,'SAGA_E2E_WORKDIR':str(tmp_path)},cwd=ROOT,
        capture_output=True,text=True,timeout=90)
    assert result.returncode==0, result.stdout+result.stderr+"\n"+"\n".join(
        path.read_text()[-6000:] for path in tmp_path.glob('*.log'))
    assert result.stdout.count('OK:')==5
