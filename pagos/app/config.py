"""Configuración de Pagos desde variables de entorno.

Las credenciales de Transbank que vienen por defecto son las PÚBLICAS del ambiente de integración
(publicadas en transbankdevelopers.cl para probar Webpay Plus sin costo ni contrato). No sirven
para cobrar dinero real. Las claves propias del servicio (PAGOS_API_KEY, INTERNAL_KEY,
PAGOS_CALLBACK_KEY) NO tienen valor por defecto: sin ellas el servicio se niega a arrancar.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

TBK_INTEGRACION_URL = "https://webpay3gint.transbank.cl"
TBK_INTEGRACION_CODIGO = "597055555532"
TBK_INTEGRACION_API_KEY = "579B532A7440BB0C9079DED94D31EA1615BACEB56610332264630D42D0A36B1C"
TBK_PRODUCCION_URL = "https://webpay3g.transbank.cl"


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./pagos.db"
    # Claves propias del sistema (sin valor por defecto).
    api_key: str = ""            # la usa el orquestador para llamar a Pagos
    internal_key: str = ""       # rutas /internal (inyección de fallas)
    callback_key: str = ""       # la usa Pagos para avisar al orquestador
    orquestador_url: str = "http://orquestador:8000"
    # URL pública (la del gateway) a la que Webpay devuelve al comprador.
    public_base_url: str = "http://localhost:8000"
    result_url: str = ""         # opcional: portal al que se redirige tras el pago
    # Transbank
    tbk_base_url: str = TBK_INTEGRACION_URL
    tbk_commerce_code: str = TBK_INTEGRACION_CODIGO
    tbk_api_key: str = TBK_INTEGRACION_API_KEY
    tbk_timeout: float = 1.5            # create y refund: caben dentro del timeout del orquestador (2 s)
    tbk_commit_timeout: float = 5.0     # commit/status ocurren fuera de la saga síncrona
    # Ciclo de vida del pago
    ttl_seconds: int = 600
    procesando_seconds: float = 20.0    # tras este tiempo un commit incierto se resuelve consultando el estado
    reconcile_seconds: float = 10.0
    # Resiliencia hacia Transbank
    cb_threshold: int = 5
    cb_recovery_seconds: float = 15.0
    bulkhead: int = 8
    callback_timeout: float = 2.0

    @classmethod
    def from_env(cls, env=None) -> "Settings":
        e = os.environ if env is None else env

        def f(name, default, cast=str):
            raw = e.get(name)
            return default if raw in (None, "") else cast(raw)

        d = cls()
        return cls(
            database_url=f("DATABASE_URL", d.database_url),
            api_key=f("PAGOS_API_KEY", ""),
            internal_key=f("INTERNAL_KEY", ""),
            callback_key=f("PAGOS_CALLBACK_KEY", ""),
            orquestador_url=f("ORQUESTADOR_URL", d.orquestador_url).rstrip("/"),
            public_base_url=f("PAGOS_PUBLIC_BASE_URL", d.public_base_url).rstrip("/"),
            result_url=f("PAGOS_RESULT_URL", ""),
            tbk_base_url=f("TBK_BASE_URL", d.tbk_base_url).rstrip("/"),
            tbk_commerce_code=f("TBK_COMMERCE_CODE", d.tbk_commerce_code),
            tbk_api_key=f("TBK_API_KEY", d.tbk_api_key),
            tbk_timeout=f("TBK_TIMEOUT_SECONDS", d.tbk_timeout, float),
            tbk_commit_timeout=f("TBK_COMMIT_TIMEOUT_SECONDS", d.tbk_commit_timeout, float),
            ttl_seconds=f("PAGOS_TTL_SECONDS", d.ttl_seconds, int),
            procesando_seconds=f("PAGOS_PROCESANDO_SECONDS", d.procesando_seconds, float),
            reconcile_seconds=f("PAGOS_RECONCILE_SECONDS", d.reconcile_seconds, float),
            cb_threshold=f("PAGOS_CB_THRESHOLD", d.cb_threshold, int),
            cb_recovery_seconds=f("PAGOS_CB_RECOVERY_SECONDS", d.cb_recovery_seconds, float),
            bulkhead=f("PAGOS_BULKHEAD", d.bulkhead, int),
            callback_timeout=f("PAGOS_CALLBACK_TIMEOUT_SECONDS", d.callback_timeout, float),
        )

    def validar(self) -> None:
        for nombre, valor in (("PAGOS_API_KEY", self.api_key), ("INTERNAL_KEY", self.internal_key),
                              ("PAGOS_CALLBACK_KEY", self.callback_key)):
            if len(valor) < 24:
                raise RuntimeError(f"{nombre} debe tener al menos 24 caracteres (python scripts/generar_env.py)")
        if self.tbk_commerce_code == TBK_INTEGRACION_CODIGO and self.tbk_base_url.startswith(TBK_PRODUCCION_URL):
            raise RuntimeError("Credenciales de integración apuntando a producción: configura TBK_COMMERCE_CODE y TBK_API_KEY propios")

    @property
    def ambiente(self) -> str:
        return "produccion" if self.tbk_base_url.startswith(TBK_PRODUCCION_URL) else "integracion"
