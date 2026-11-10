"""BFF web: respuestas de portal compuestas desde cursos e inscripciones."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
import os

import requests
import grpc
from flask import Flask, jsonify, request
from grpc_stubs import cupos_pb2, cupos_pb2_grpc

app = Flask(__name__)
MATRICULAS_API_URL = os.getenv("INSCRIPCION_SERVICE_URL", "http://matriculas-api:8000").rstrip("/")
BFF_TIMEOUT = max(0.1, float(os.getenv("BFF_UPSTREAM_TIMEOUT_SECONDS", "4")))
CUPOS_CHANNEL = grpc.insecure_channel(os.getenv("CUPOS_GRPC_TARGET", "cupos:50051"))
CUPOS_STUB = cupos_pb2_grpc.CuposStub(CUPOS_CHANNEL)
_pool = ThreadPoolExecutor(max_workers=max(2, int(os.getenv("BFF_MAX_WORKERS", "16"))))


def _get(path: str, headers: dict[str, str]):
    return requests.get(f"{MATRICULAS_API_URL}{path}", headers=headers, timeout=BFF_TIMEOUT)


def _cursos_disponibles():
    response = CUPOS_STUB.listarCursosDisponibles(cupos_pb2.ListarRequest(), timeout=BFF_TIMEOUT)
    return {"items": [
        {"curso_id": course.curso_id, "nombre": course.nombre,
         "cupos_totales": course.cupos_totales, "cupos_libres": course.cupos_libres}
        for course in response.cursos
    ]}


@app.get("/health")
def health():
    return jsonify(status="ok", service="bff-web")


@app.get("/v1/bff/dashboard")
def dashboard():
    """Una llamada del cliente reúne el catálogo de cursos y sus matrículas."""
    headers = {name: request.headers[name] for name in ("Authorization", "X-API-Key")
               if name in request.headers}
    courses = _pool.submit(_cursos_disponibles)
    enrollments = _pool.submit(_get, "/v1/enrollments?limit=100", headers)
    try:
        available_courses = courses.result(timeout=BFF_TIMEOUT + 0.25)
        enrollment_response = enrollments.result(timeout=BFF_TIMEOUT + 0.25)
    except FuturesTimeout:
        courses.cancel()
        enrollments.cancel()
        return jsonify(error={
            "code": "BFF_DEPENDENCY_TIMEOUT",
            "message": "No fue posible completar el panel; reintenta en unos segundos",
        }), 504
    except grpc.RpcError as exc:
        courses.cancel()
        enrollments.cancel()
        status = 504 if exc.code() == grpc.StatusCode.DEADLINE_EXCEEDED else 503
        code = "BFF_DEPENDENCY_TIMEOUT" if status == 504 else "CUPOS_UNAVAILABLE"
        return jsonify(error={"code": code, "message": "No fue posible consultar Cupos"}), status
    except requests.Timeout:
        courses.cancel()
        enrollments.cancel()
        return jsonify(error={"code": "BFF_DEPENDENCY_TIMEOUT",
                              "message": "Inscripción excedió el tiempo límite"}), 504
    except requests.RequestException:
        courses.cancel()
        enrollments.cancel()
        return jsonify(error={"code": "INSCRIPCION_UNAVAILABLE",
                              "message": "No fue posible consultar Inscripción"}), 502

    if not enrollment_response.ok:
        failed = enrollment_response
        try:
            detail = failed.json()
        except ValueError:
            detail = {"message": "El servicio de matrículas devolvió una respuesta no válida"}
        return jsonify(error={"code": "BFF_UPSTREAM_ERROR", "upstream_status": failed.status_code,
                              "details": detail}), failed.status_code

    return jsonify({
        "cursos": available_courses,
        "matriculas": enrollment_response.json(),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090)
