#!/usr/bin/env python3
"""Consulta servicios por ids/claves. No lee bases ajenas ni imprime datos personales."""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'matriculas-api'))
from app.grpc_stubs import cupos_pb2 as pb, cupos_pb2_grpc
import grpc


def get(base,path,key):
    req=urllib.request.Request(base.rstrip('/')+path,headers={'X-API-Key':key})
    with urllib.request.urlopen(req,timeout=5) as response:
        return json.load(response)


def verificar(saga,reservation,payment,enrollments):
    active=any(e['estado']=='CONFIRMADA' for e in enrollments)
    reserved=reservation.estado_ocupar==pb.CUPO_OCUPADO and reservation.estado_liberar!=pb.CUPO_LIBERADO
    if saga['estado']=='COMPLETADA':
        return reserved and payment['estado']=='COBRADO' and active
    if saga['estado']=='COMPENSADA':
        return not reserved and payment['estado'] in ('REEMBOLSADO','INEXISTENTE') and not active
    raise ValueError('La saga aún no es terminal')


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('saga_ids',nargs='+')
    ap.add_argument('--gateway',default=os.getenv('BASE_URL','http://localhost:8000'))
    ap.add_argument('--pagos',default=os.getenv('PAGOS_URL','http://localhost:8002'))
    ap.add_argument('--cupos',default=os.getenv('CUPOS_GRPC_ADDR','localhost:50051'))
    args=ap.parse_args()
    key=os.environ['ORCHESTRATOR_API_KEY']; service=os.environ['SERVICE_API_KEY']
    failures=0
    with grpc.insecure_channel(args.cupos) as channel:
        stub=cupos_pb2_grpc.CuposStub(channel)
        for sid in args.saga_ids:
            try:
                saga=get(args.gateway,'/v1/sagas/'+urllib.parse.quote(sid,safe=''),key)
                reservation=stub.consultarOperacion(pb.ConsultarOperacionRequest(idempotency_key=f'{sid}:RESERVAR'),timeout=5)
                payment=get(args.pagos,'/v1/payments/by-key/'+urllib.parse.quote(f'{sid}:COBRAR',safe=''),service)
                # Filtra por la clave propia, para no confundir una reinscripción posterior.
                enrollments=get(args.gateway,'/v1/enrollments?'+urllib.parse.urlencode(
                    {'student_id':saga['alumno_id'],'curso_id':saga['curso_id'],'client_key':f'{sid}:INSCRIBIR','limit':100}),service)['items']
                # Solo se inspecciona la inscripción de esta saga, incluso si hubo otra posterior.
                ok=verificar(saga,reservation,payment,enrollments)
            except Exception as exc:
                print(json.dumps({'saga_id':sid,'ok':False,'error':type(exc).__name__}))
                failures+=1
                continue
            print(json.dumps({'saga_id':sid,'ok':ok}))
            failures+=not ok
    return int(failures>0)


if __name__=='__main__':
    sys.exit(main())
