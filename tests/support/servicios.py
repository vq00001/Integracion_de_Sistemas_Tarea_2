"""Preparación explícita de dependencias: un servicio caído no es un test omitido."""
import time

import grpc
import requests


def esperar_cupos(address, timeout=30):
    with grpc.insecure_channel(address) as channel:
        try:
            grpc.channel_ready_future(channel).result(timeout=timeout)
        except grpc.FutureTimeoutError:
            raise RuntimeError(
                f'Cupos gRPC no está disponible en {address}. '
                'En Docker, revisa docker compose logs --tail=100 cupos. '
                'Desde el host, levanta también docker-compose.test.yml '
                'y configura CUPOS_GRPC_ADDR si usas otro puerto.'
            ) from None


def esperar_catalogo(base_url, api_key, timeout=40):
    deadline = time.monotonic() + timeout
    last_status = 'sin respuesta'
    while time.monotonic() < deadline:
        try:
            response = requests.get(base_url.rstrip('/')+'/v1/courses',
                                    headers={'X-API-Key': api_key}, timeout=3)
            last_status = response.status_code
            if response.status_code == 200:
                return
            if response.status_code in (401, 403):
                raise RuntimeError('La clave del runner no permite consultar cursos; '
                                   'revisa MATRICULAS_API_KEYS/API_KEY.')
        except requests.RequestException:
            last_status = 'sin conexión HTTP'
        time.sleep(.5)
    raise RuntimeError(f'El catálogo no pudo comunicarse con Cupos (último estado: {last_status}). '
                       'Revisa los logs de cupos y matriculas-api y CUPOS_GRPC_TARGET. '
                       'La suite no se ejecutó contra una dependencia caída.')
