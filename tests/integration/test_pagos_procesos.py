"""Matrícula con cobro Webpay (Pagos real + simulador de Transbank) con procesos reales, sin Docker."""
import os
import subprocess
import sys

from tests.conftest import ROOT


def test_matricula_con_pago_webpay_de_punta_a_punta(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "tests/support/pagos_scenario.py")],
                            env={**os.environ, "SAGA_E2E_WORKDIR": str(tmp_path)}, cwd=ROOT,
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr + "\n" + "\n".join(
        f"--- {p.name}\n{p.read_text()[-3000:]}" for p in sorted(tmp_path.glob("*.log")))
    assert result.stdout.count("OK:") == 6, result.stdout
