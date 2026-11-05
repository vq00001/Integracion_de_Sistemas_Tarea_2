"""Escenarios aislados con HTTP/gRPC reales; Pagos es un doble de pruebas explícito."""
import os, subprocess, socket, threading, time, json, uuid, sys, tempfile, shutil
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import requests
r=Path(__file__).resolve().parents[2]
py=sys.executable
w=Path(os.environ['SAGA_E2E_WORKDIR'])
w.mkdir(exist_ok=True)
# No importar stubs del host: generar con el intérprete de este runner.
shutil.copytree(r/'matriculas-api/app', w/'app',
               ignore=shutil.ignore_patterns('grpc_stubs', '__pycache__'))
subprocess.run([py, str(r/'matriculas-api/generar_stubs.py'), '--destino',
                str(w/'app/grpc_stubs')], check=True, capture_output=True)
def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));return s.getsockname()[1]
a,g,o=port(),port(),port()
admin='local-testing-key-32-characters-admin'
internal='local-internal-testing-key-32-characters'
orchkey='local-orchestrator-testing-key-32-characters'
ledger={}; refund_down=False
class Payments(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        global refund_down
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        key=self.headers['Idempotency-Key']
        if self.path.endswith('/refund'):
            if refund_down:
                self.send_response(503); self.end_headers();return
            ledger[body['original_key']]='REEMBOLSADO'
            result='REEMBOLSADO'
        else:
            result=ledger.setdefault(key,'COBRADO')
        self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
        self.wfile.write(json.dumps({'estado':result}).encode())
pay=ThreadingHTTPServer(('127.0.0.1',0),Payments)
threading.Thread(target=pay.serve_forever,daemon=True).start()
env={**os.environ,'API_KEYS':admin,'JWT_SECRET':'local-test-secret-at-least-32-characters',
    'DATABASE_URL':f'sqlite:///{w}/inscripcion.db','DB_PATH':str(w/'cupos.db'),'GRPC_PORT':str(g),
    'CUPOS_GRPC_TARGET':f'127.0.0.1:{g}','PYTHONPATH':str(w)+os.pathsep+os.environ.get('PYTHONPATH',''),
    'SAGA_DATABASE_URL':f'sqlite:///{w}/sagas.db','ORCHESTRATOR_API_KEY':orchkey,
    'INTERNAL_KEY':internal,'SERVICE_API_KEY':admin,'INSCRIPCION_URL':f'http://127.0.0.1:{a}',
    'PAGOS_URL':f'http://127.0.0.1:{pay.server_port}','SAGA_STALE_SECONDS':'.1',
    'SAGA_RECONCILE_SECONDS':'.1','SAGA_CB_RECOVERY_SECONDS':'.1','SAGA_CB_THRESHOLD':'10'}
subprocess.run([py,'-m','grpc_tools.protoc',f'-I{r}/proto',f'--python_out={w}',f'--grpc_python_out={w}',str(r/'proto/cupos.proto')],check=True)
sys.path.insert(0,str(w))
import grpc
import cupos_pb2 as pb
import cupos_pb2_grpc
channel=grpc.insecure_channel(f'127.0.0.1:{g}')
stub=cupos_pb2_grpc.CuposStub(channel)
def invariant(saga):
    sid=saga['id']
    reservation=stub.consultarOperacion(pb.ConsultarOperacionRequest(idempotency_key=sid+':RESERVAR'),timeout=3)
    active=reservation.estado_ocupar==pb.CUPO_OCUPADO and reservation.estado_liberar!=pb.CUPO_LIBERADO
    rows=requests.get(f'http://127.0.0.1:{a}/v1/enrollments',params={'client_key':sid+':INSCRIBIR'},headers={'X-API-Key':admin}).json()['items']
    enrolled=any(row['estado']=='CONFIRMADA' for row in rows)
    if saga['estado']=='COMPLETADA':
        assert active and enrolled and ledger[sid+':COBRAR']=='COBRADO'
    else:
        assert not active and not enrolled and ledger.get(sid+':COBRAR','INEXISTENTE') in ('REEMBOLSADO','INEXISTENTE')
processes=[]
def launch(name,cmd):
    log=(w/f'{name}.log').open('a')
    proc=subprocess.Popen(cmd,cwd=r,env=env,stdout=log,stderr=log);processes.append((name,proc));return proc
def ready(url):
    for _ in range(100):
        for name, proc in dict(processes).items():
            if proc.poll() is not None:
                raise RuntimeError(f'{name} terminó antes de estar listo (exit={proc.returncode})')
        try:
            if requests.get(url,timeout=.2).status_code==200:return
        except requests.RequestException:pass
        time.sleep(.1)
    raise RuntimeError('service not ready '+url)
def fault(**kwargs):
    result=requests.put(f'http://127.0.0.1:{a}/internal/fault',json=kwargs,headers={'X-Internal-Key':internal},timeout=2)
    assert result.status_code==200,result.text
counter=20_000_000
def alumno():
    global counter
    counter+=1
    digits=str(counter);s=sum(int(d)*((i%6)+2) for i,d in enumerate(reversed(digits)))
    dv=11-s%11;dv='0' if dv==11 else 'K' if dv==10 else str(dv)
    response=requests.post(f'http://127.0.0.1:{a}/v1/students',headers={'X-API-Key':admin},
        json={'rut':f'{digits}-{dv}','nombre':'Test','apellido':'Saga','email':f'{uuid.uuid4().hex}@example.com'})
    assert response.status_code==201,response.text
    return response.json()['id']
def matricula(student=None,key=None):
    return requests.post(f'http://127.0.0.1:{o}/v1/matriculas',json={'alumno_id':student or alumno(),'curso_id':1,'monto':100},
        headers={'X-API-Key':orchkey,'Idempotency-Key':key or str(uuid.uuid4())},timeout=15)
def state(sid):
    result=requests.get(f'http://127.0.0.1:{o}/v1/sagas/{sid}',headers={'X-API-Key':orchkey},timeout=2)
    return result.json()['estado']
try:
    launch('cupos',[py,str(r/'cupos/src/server.py')])
    launch('api',[py,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(a)])
    ready(f'http://127.0.0.1:{a}/v1/health')
    cmd=[py,'-m','uvicorn','app.orchestrator_main:app','--host','127.0.0.1','--port',str(o)]
    proc=launch('orquestador',cmd);ready(f'http://127.0.0.1:{o}/metrics')
    student,key=alumno(),str(uuid.uuid4())
    first=matricula(student,key); assert first.status_code==201,first.text
    replay=matricula(student,key);assert replay.status_code==200 and replay.json()==first.json()
    invariant(first.json())
    print('OK: camino feliz y replay HTTP',flush=True)
    fault(falla=True)
    failed=matricula();assert failed.status_code==503 and failed.json()['estado']=='COMPENSADA',failed.text
    assert ledger[failed.json()['id']+':COBRAR']=='REEMBOLSADO'
    fault()
    invariant(failed.json())
    transitions=[json.loads(line) for line in (w/'orquestador.log').read_text().splitlines() if line.startswith('{')]
    steps=[line['paso'] for line in transitions if line['saga_id']==failed.json()['id'] and line['estado']=='OK']
    assert steps==['RESERVAR','COBRAR','INSCRIBIR','COBRAR','RESERVAR'],steps
    print('OK: último paso falla, compensación inversa',flush=True)
    fault(lentitud_seconds=3)
    slow=matricula();assert slow.json()['estado']=='COMPENSADA',slow.text
    time.sleep(1.2)
    fault()
    enrollments=requests.get(f'http://127.0.0.1:{a}/v1/enrollments',params={'client_key':slow.json()['id']+':INSCRIBIR'},headers={'X-API-Key':admin}).json()['items']
    assert len(enrollments)==1 and enrollments[0]['estado']=='ANULADA'
    invariant(slow.json())
    print('OK: timeout y alta tardía no resucita inscripción',flush=True)
    refund_down=True;fault(falla=True)
    pending=matricula();assert pending.json()['estado']=='COMPENSACION_PENDIENTE',pending.text
    refund_down=False;fault()
    for _ in range(80):
        if state(pending.json()['id'])=='COMPENSADA':break
        time.sleep(.1)
    assert state(pending.json()['id'])=='COMPENSADA'
    invariant({**pending.json(),'estado':'COMPENSADA'})
    print('OK: reembolso caído y reconciliación al recuperar Pagos',flush=True)
    fault(lentitud_seconds=1.5)
    student=alumno(); key=str(uuid.uuid4())
    result=[]
    def send():
        try:result.append(matricula(student,key))
        except requests.RequestException:pass
    sender=threading.Thread(target=send);sender.start()
    import sqlite3
    sid=None
    for _ in range(100):
        with sqlite3.connect(w/'sagas.db') as db:
            row=db.execute("SELECT s.id FROM sagas s JOIN saga_steps p ON p.saga_id=s.id WHERE s.idempotency_key=? AND p.paso='INSCRIBIR' AND p.estado='PENDIENTE'",(key,)).fetchone()
        if row:sid=row[0];break
        time.sleep(.02)
    assert sid
    proc.kill();proc.wait(); fault()
    launch('orquestador',cmd);ready(f'http://127.0.0.1:{o}/metrics')
    for _ in range(100):
        if state(sid)=='COMPENSADA':break
        time.sleep(.1)
    assert state(sid)=='COMPENSADA'
    sender.join(timeout=10)
    time.sleep(1.6)  # dejar llegar el alta tardía tras matar el coordinador
    invariant({'id':sid,'estado':'COMPENSADA'})
    print('OK: SIGKILL real y recuperación desde BD persistida',flush=True)
finally:
    for name, proc in processes:
        if proc.poll() is None:proc.terminate()
    for name, proc in processes:
        proc.wait(timeout=10)
    channel.close()
    pay.shutdown()
