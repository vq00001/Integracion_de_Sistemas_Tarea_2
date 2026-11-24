"""Frontera HTTP experimental: demora SOLO crear pagos; registra envíos tardíos.

Se usa en ambas variantes. No altera el servicio Pagos ni simula su respuesta:
reenvía la petición real, incluso si el orquestador ya agotó su timeout.
"""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests


class PagosProxy:
    def __init__(self, target):
        self.target = target
        self.lock = threading.Lock()
        self.delay = 0
        self.phase = 'inicio'
        self.events = []
        self.active = 0
        self.server = None
        self.url = target

    def configure(self, delay_ms=0, phase='inicio'):
        with self.lock:
            self.delay, self.phase = delay_ms / 1000, phase

    def start(self):
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_request(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                operation = ('forward' if self.command == 'POST' and self.path == '/v1/payments'
                             else 'compensacion' if self.path == '/v1/payments/refund' else 'consulta')
                with owner.lock:
                    delay, phase = owner.delay, owner.phase
                    owner.active += 1
                event = dict(fase=phase, operacion=operation, clave=self.headers.get('Idempotency-Key', ''),
                             recibido=time.monotonic(), enviado=None, terminado=None, codigo=0)
                headers = {k: v for k, v in self.headers.items() if k.lower() not in
                           ('host', 'connection', 'content-length', 'transfer-encoding')}
                try:
                    if operation == 'forward':
                        time.sleep(delay)
                    event['enviado'] = time.monotonic()
                    response = requests.request(self.command, owner.target + self.path, data=body or None,
                                                headers=headers, timeout=20, allow_redirects=False)
                    event['codigo'] = response.status_code
                    self.send_response(response.status_code)
                    self.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
                    self.send_header('Content-Length', str(len(response.content)))
                    self.end_headers()
                    self.wfile.write(response.content)
                except requests.RequestException:
                    try:
                        self.send_error(502)
                    except OSError:
                        pass
                except OSError:
                    pass  # El cliente ya agotó su timeout; el envío tardío queda registrado.
                finally:
                    event['terminado'] = time.monotonic()
                    with owner.lock:
                        owner.events.append(event)
                        owner.active -= 1

            do_GET = do_POST = handle_request
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=3)
