"""Cliente REST de Transbank Webpay Plus (API v1.2).

Se usa directamente la API REST (sin el SDK) para controlar los timeouts, que son parte del diseño
resiliente. Documentación: https://www.transbankdevelopers.cl/referencia/webpay

  crear      POST /transactions               → {token, url}
  confirmar  PUT  /transactions/{token}       → resultado de la autorización (commit)
  estado     GET  /transactions/{token}
  reembolsar POST /transactions/{token}/refunds {amount} → type REVERSED | NULLIFIED
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .resilience import Guard, TransbankRejected, TransbankUnavailable

API_PATH = "/rswebpaytransaction/api/webpay/v1.2/transactions"


class TransbankClient:
    def __init__(self, base_url: str, commerce_code: str, api_key: str, timeout: float = 1.5,
                 commit_timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Tbk-Api-Key-Id": commerce_code, "Tbk-Api-Key-Secret": api_key,
                        "Content-Type": "application/json", "Accept": "application/json"}
        self.timeout, self.commit_timeout = timeout, commit_timeout

    def _request(self, method: str, path: str, body: dict | None, timeout: float) -> dict:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base_url + API_PATH + path, data=data, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detalle = ""
            try:
                detalle = json.loads(exc.read()).get("error_message", "")[:120]
            except Exception:
                pass
            if exc.code >= 500 or exc.code in (408, 429):
                raise TransbankUnavailable(f"HTTP {exc.code}") from exc
            raise TransbankRejected(exc.code, detalle) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise TransbankUnavailable(type(exc).__name__) from exc
        except json.JSONDecodeError as exc:
            raise TransbankUnavailable("respuesta inválida") from exc

    # --- operaciones sin protección (las usa Guard) ---
    def crear(self, buy_order: str, session_id: str, amount: int, return_url: str) -> dict:
        r = self._request("POST", "", {"buy_order": buy_order, "session_id": session_id,
                                       "amount": amount, "return_url": return_url}, self.timeout)
        if not r.get("token") or not r.get("url"):
            raise TransbankUnavailable("respuesta sin token")
        return r

    def confirmar(self, token: str) -> dict:
        return self._request("PUT", f"/{token}", {}, self.commit_timeout)

    def estado(self, token: str) -> dict:
        return self._request("GET", f"/{token}", None, self.commit_timeout)

    def reembolsar(self, token: str, amount: int) -> dict:
        return self._request("POST", f"/{token}/refunds", {"amount": amount}, self.timeout)


class Webpay:
    """Fachada protegida: cada operación pasa por bulkhead + circuit breaker + inyección de fallas."""

    def __init__(self, client: TransbankClient, guard: Guard):
        self.client, self.guard = client, guard

    def crear(self, *a):
        return self.guard.call("crear", self.client.timeout, self.client.crear, *a)

    def confirmar(self, token):
        return self.guard.call("confirmar", self.client.commit_timeout, self.client.confirmar, token)

    def estado(self, token):
        return self.guard.call("estado", self.client.commit_timeout, self.client.estado, token)

    def reembolsar(self, token, amount):
        return self.guard.call("reembolsar", self.client.timeout, self.client.reembolsar, token, amount)
