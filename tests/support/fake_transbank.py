"""Simulador de Transbank Webpay Plus para pruebas SIN red (mismo contrato REST v1.2).

No es el servicio real: sirve para probar el adaptador, los casos límite (commit perdido, 5xx,
latencia) y demos sin internet. Para el ambiente de integración REAL de Transbank no se usa.

  create   POST /rswebpaytransaction/api/webpay/v1.2/transactions
  commit   PUT  .../transactions/{token}
  status   GET  .../transactions/{token}
  refund   POST .../transactions/{token}/refunds
  navegador  POST /webpayserver/initTransaction (token_ws) → página con Aprobar / Rechazar → 303 al return_url

Control para pruebas: `FakeTransbank.pagar(token, aprobar)`, `.config(latencia_ms=, falla=, commit_perdido=)`.
Uso manual: python tests/support/fake_transbank.py --port 9100
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

API = "/rswebpaytransaction/api/webpay/v1.2/transactions"


class FakeTransbank:
    def __init__(self, commerce_code="597055555532", api_key="SECRET", host="127.0.0.1", port=0, public_url=None):
        self.commerce_code, self.api_key = commerce_code, api_key
        self.tx: dict[str, dict] = {}
        self.calls = {"create": 0, "commit": 0, "status": 0, "refund": 0}
        self.latencia_ms, self.falla, self.commit_perdido = 0, False, False
        self.lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _html(self, code, body, location=None):
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                if location:
                    self.send_header("Location", location)
                self.end_headers()
                self.wfile.write(body.encode())

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n).decode() if n else ""

            def _auth(self):
                ok = (self.headers.get("Tbk-Api-Key-Id") == fake.commerce_code
                      and self.headers.get("Tbk-Api-Key-Secret") == fake.api_key)
                if not ok:
                    self._json(401, {"error_message": "Not Authorized"})
                return ok

            def _preludio(self):
                if fake.latencia_ms:
                    time.sleep(fake.latencia_ms / 1000)
                if fake.falla:
                    self._json(503, {"error_message": "Service Unavailable"})
                    return False
                return True

            def do_POST(self):
                path = urlparse(self.path).path
                if path == "/webpayserver/initTransaction":       # navegador: formulario de pago
                    form = parse_qs(self._body())
                    token = (form.get("token_ws") or [""])[0]
                    if token not in fake.tx:
                        return self._html(404, "Transacción inexistente")
                    return self._html(200, (
                        f"<h2>Webpay (simulador)</h2><p>Monto ${fake.tx[token]['amount']}</p>"
                        f"<form method='post' action='/fake/resolver'><input type='hidden' name='token' value='{token}'>"
                        "<button name='resultado' value='aprobar'>Aprobar</button> "
                        "<button name='resultado' value='rechazar'>Rechazar</button> "
                        "<button name='resultado' value='abortar'>Anular compra</button></form>"))
                if path == "/fake/resolver":
                    form = parse_qs(self._body())
                    token, res = form["token"][0], form["resultado"][0]
                    tx = fake.tx[token]
                    if res == "abortar":
                        q = urlencode({"TBK_TOKEN": token, "TBK_ORDEN_COMPRA": tx["buy_order"], "TBK_ID_SESION": tx["session_id"]})
                    else:
                        fake.pagar(token, res == "aprobar")
                        q = urlencode({"token_ws": token})
                    return self._html(303, "", location=f"{tx['return_url']}?{q}")
                if not self._auth() or not self._preludio():
                    return
                fake.calls_inc("create" if path == API else "refund")
                body = json.loads(self._body() or "{}")
                if path == API:
                    if not (0 < len(body.get("buy_order", "")) <= 26) or not isinstance(body.get("amount"), int) or body["amount"] <= 0:
                        return self._json(422, {"error_message": "Invalid request"})
                    token = uuid.uuid4().hex + uuid.uuid4().hex
                    with fake.lock:
                        fake.tx[token] = {"buy_order": body["buy_order"], "session_id": body["session_id"], "amount": body["amount"],
                                          "return_url": body["return_url"], "status": "INITIALIZED", "paid": None, "refund": None}
                    return self._json(200, {"token": token, "url": fake.url + "/webpayserver/initTransaction"})
                m = re.fullmatch(re.escape(API) + r"/(\w+)/refunds", path)
                if m:
                    tx = fake.tx.get(m.group(1))
                    if tx is None or tx["status"] != "AUTHORIZED" or body.get("amount") != tx["amount"]:
                        return self._json(422, {"error_message": "Cannot refund"})
                    tx["status"], tx["refund"] = "REVERSED", "REVERSED"
                    return self._json(200, {"type": "REVERSED", "authorization_code": "123456", "response_code": 0,
                                            "nullified_amount": tx["amount"], "balance": 0})
                self._json(404, {"error_message": "Not found"})

            def do_PUT(self):
                path = urlparse(self.path).path
                if not self._auth() or not self._preludio():
                    return
                fake.calls_inc("commit")
                m = re.fullmatch(re.escape(API) + r"/(\w+)", path)
                tx = fake.tx.get(m.group(1)) if m else None
                if tx is None or tx["paid"] is None or tx["status"] != "INITIALIZED":
                    return self._json(422, {"error_message": "Transaction not paid, unknown or already committed"})
                tx["status"] = "AUTHORIZED" if tx["paid"] else "FAILED"
                if fake.commit_perdido:                       # se procesó, pero la respuesta no llega
                    return self._json(500, {"error_message": "Internal error"})
                self._json(200, fake.vista(tx))

            def do_GET(self):
                path = urlparse(self.path).path
                if not self._auth() or not self._preludio():
                    return
                fake.calls_inc("status")
                m = re.fullmatch(re.escape(API) + r"/(\w+)", path)
                tx = fake.tx.get(m.group(1)) if m else None
                if tx is None:
                    return self._json(422, {"error_message": "Transaction not found"})
                self._json(200, fake.vista(tx))

        self.server = ThreadingHTTPServer((host, port), H)
        self.url = public_url or f"http://{host}:{self.server.server_port}"  # la URL que ve el navegador
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # ------------------------------------------------------------------ utilidades
    def calls_inc(self, name):
        with self.lock:
            self.calls[name] += 1

    @staticmethod
    def vista(tx):
        ok = tx["status"] in ("AUTHORIZED", "REVERSED")
        return {"vci": "TSY", "amount": tx["amount"], "status": tx["status"], "buy_order": tx["buy_order"],
                "session_id": tx["session_id"], "card_detail": {"card_number": "6623"},
                "authorization_code": "1213" if ok else None, "payment_type_code": "VN",
                "response_code": 0 if ok else -1, "installments_number": 0}

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def pagar(self, token: str, aprobar: bool = True):
        """Lo que haría el comprador en el formulario de Webpay."""
        self.tx[token]["paid"] = bool(aprobar)

    def config(self, latencia_ms=None, falla=None, commit_perdido=None):
        if latencia_ms is not None:
            self.latencia_ms = latencia_ms
        if falla is not None:
            self.falla = falla
        if commit_perdido is not None:
            self.commit_perdido = commit_perdido


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--commerce-code", default="597055555532")
    ap.add_argument("--api-key", default="579B532A7440BB0C9079DED94D31EA1615BACEB56610332264630D42D0A36B1C")
    ap.add_argument("--public-url", default=None, help="URL que usará el navegador (p. ej. http://localhost:9100 si se escucha en 0.0.0.0)")
    a = ap.parse_args()
    f = FakeTransbank(a.commerce_code, a.api_key, a.host, a.port, a.public_url)
    print(f"Simulador de Transbank en {f.url}  (TBK_BASE_URL={f.url})", flush=True)
    f.server.serve_forever()
