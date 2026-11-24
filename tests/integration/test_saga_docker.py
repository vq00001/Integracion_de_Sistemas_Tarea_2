"""Prueba destructiva de proceso, opt-in: requiere Docker accesible y Pagos apuntando al simulador de
Transbank (TBK_BASE_URL → tests/support/fake_transbank.py): el cobro con Webpay exige un comprador, que
esta prueba reemplaza pagando en el formulario del simulador. Con Webpay real se omite."""
import os
import re
import threading
import time
import uuid

import pytest
import requests
from tests.conftest import compose

pytestmark=pytest.mark.falla


def pagar_en_simulador(pago_url):
    """Hace de comprador: abre la redirección, aprueba en el formulario del simulador y vuelve a Pagos."""
    html=requests.get(pago_url,timeout=10).text
    action=re.search(r"action='([^']+)'",html)
    token=re.search(r"name='token_ws' value='(\w+)'",html)
    if not (action and token) or 'transbank.cl' in action.group(1):
        pytest.skip('El cobro usa Webpay real: apuntar TBK_BASE_URL al simulador (tests/support/fake_transbank.py)')
    base=action.group(1).rsplit('/webpayserver/',1)[0]
    result=requests.post(base+'/fake/resolver',data={'token':token.group(1),'resultado':'aprobar'},timeout=15)
    assert result.status_code==200 and 'Pago aprobado' in result.text


def test_docker_kill_recupera_saga_en_medio_de_inscripcion(estudiante):
    base=os.getenv('SAGA_BASE_URL')
    key=os.getenv('ORCHESTRATOR_API_KEY')
    internal=os.getenv('INTERNAL_KEY')
    if not (base and key and internal):
        pytest.skip('Definir SAGA_BASE_URL, ORCHESTRATOR_API_KEY, INTERNAL_KEY y conectar Pagos')
    headers={'X-API-Key':key,'Idempotency-Key':str(uuid.uuid4())}
    body={'alumno_id':estudiante['id'],'curso_id':1,'monto':100}
    def fault(delay):
        # Ejecutado en el contenedor, el endpoint nunca se expone por el gateway.
        compose('exec','-T','-e',f'FAULT_KEY={internal}','matriculas-api','python','-c',
            "import os,json,urllib.request; "
            "req=urllib.request.Request('http://localhost:8000/internal/fault',"
            f"data=json.dumps({{'lentitud_seconds':{delay}}}).encode(),"
            "headers={'Content-Type':'application/json','X-Internal-Key':os.environ['FAULT_KEY']},method='PUT'); "
            "urllib.request.urlopen(req)")
    responses=[]
    def request():
        try:
            responses.append(requests.post(base+'/v1/matriculas',json=body,headers=headers,timeout=30))
        except requests.RequestException:
            pass
    fault(8)
    thread=threading.Thread(target=request)
    try:
        thread.start()
        # Replay no repite efectos y expone el id de la saga mientras está en vuelo.
        deadline=time.monotonic()+10
        saga=None
        while time.monotonic()<deadline:
            time.sleep(.1)
            replay=requests.post(base+'/v1/matriculas',json=body,headers=headers,timeout=15)
            if replay.status_code==202:
                saga=replay.json(); break
        assert saga is not None, 'No se observó una saga en curso'
        if saga.get('estado')=='ESPERANDO_PAGO':
            pagar_en_simulador(saga['pago_url'])  # la saga se reanuda e inscribe (lento por la falla inducida)
        # Esperar intención INSCRIBIR en logs, sin asumir cuánto tarda Pagos.
        while time.monotonic()<deadline:
            logs=compose('logs','--no-log-prefix','orquestador').stdout
            if saga['id'] in logs and any(saga['id'] in line and 'INSCRIBIR' in line and 'PENDIENTE' in line for line in logs.splitlines()):
                break
            time.sleep(.1)
        else:
            pytest.fail('No se alcanzó INSCRIBIR; verificar Pagos')
        compose('kill','-s','SIGKILL','orquestador')
        compose('start','orquestador')
        fault(0)
        deadline=time.monotonic()+120
        state=None
        while time.monotonic()<deadline:
            try:
                result=requests.get(base+'/v1/sagas/'+saga['id'],headers=headers,timeout=3)
                state=result.json().get('estado')
                if state=='COMPENSADA': break
            except (requests.RequestException, ValueError):
                pass
            time.sleep(1)
        assert state=='COMPENSADA'
    finally:
        fault(0)
        thread.join(timeout=30)
