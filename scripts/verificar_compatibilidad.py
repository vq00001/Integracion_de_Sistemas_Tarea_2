#!/usr/bin/env python3
"""Comprobación AUTOMÁTICA de compatibilidad de los contratos (.proto y OpenAPI) contra la línea base.

D3 definía qué es un cambio "compatible" pero no había nada que lo verificara: dependía de que alguien
leyera el diff. Este script (autocontenido, sin instalar buf ni oasdiff) compara el contrato actual con
el último contrato PUBLICADO, guardado en `contrato-base/`, y falla si hay un cambio que rompe a un
consumidor existente.

  python scripts/verificar_compatibilidad.py                    # compara; código de salida 1 si rompe
  python scripts/verificar_compatibilidad.py --actualizar-base  # al PUBLICAR una versión: la actual pasa a ser la base

ROMPE (código 1):
  .proto   mensaje/campo/enum/valor/RPC eliminado; número de campo reutilizado con otro tipo o etiqueta;
           tipo de entrada o salida de un RPC cambiado; paquete o servicio renombrado.
  OpenAPI  ruta u operación eliminada; código de respuesta eliminado; parámetro eliminado o que pasa a ser
           obligatorio; propiedad obligatoria nueva en un cuerpo de petición; propiedad eliminada de una
           respuesta; cambio de tipo; valor de enum eliminado.
ADVIERTE (no falla): restricciones que se estrechan (`maximum` nuevo o menor, `minLength` mayor, `pattern`
nuevo): pueden rechazar peticiones que antes pasaban; deben justificarse en el historial del contrato (D3).
LO QUE NO VE: cambios de comportamiento que no alteran el esquema (p. ej. validaciones nuevas, o que
`liberarCupo` ahora exija que la clave haya ocupado). Esos se documentan en D3 y se cubren con pruebas.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "contrato-base"
PROTO = ROOT / "proto" / "cupos.proto"
OPENAPI = ROOT / "matriculas-api" / "docs" / "openapi.json"


# ------------------------------------------------------------------ protobuf
def descriptor_set(proto: Path):
    from google.protobuf import descriptor_pb2
    from grpc_tools import protoc
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "d.desc"
        rc = protoc.main(["protoc", f"-I{proto.parent}", f"--descriptor_set_out={out}", str(proto)])
        if rc != 0:
            raise SystemExit(f"protoc falló al compilar {proto}")
        ds = descriptor_pb2.FileDescriptorSet()
        ds.ParseFromString(out.read_bytes())
        return ds


def comparar_proto(base, actual) -> list[str]:
    """`base` y `actual` son FileDescriptorSet. Devuelve la lista de cambios que rompen."""
    errores: list[str] = []
    fb, fa = base.file[0], actual.file[0]
    if fb.package != fa.package:
        errores.append(f"paquete renombrado: {fb.package} → {fa.package}")
    msgs_a = {m.name: m for m in fa.message_type}
    for mb in fb.message_type:
        ma = msgs_a.get(mb.name)
        if ma is None:
            errores.append(f"mensaje eliminado: {mb.name}")
            continue
        campos_a = {c.number: c for c in ma.field}
        for cb in mb.field:
            ca = campos_a.get(cb.number)
            if ca is None:
                errores.append(f"campo eliminado: {mb.name}.{cb.name} (#{cb.number})")
            elif (ca.type, ca.label, ca.type_name) != (cb.type, cb.label, cb.type_name):
                errores.append(f"campo {mb.name}.{cb.name} (#{cb.number}) cambió de tipo o etiqueta")
            elif ca.name != cb.name:
                errores.append(f"campo {mb.name}.#{cb.number} renombrado {cb.name} → {ca.name} (rompe JSON)")
    enums_a = {e.name: e for e in fa.enum_type}
    for eb in fb.enum_type:
        ea = enums_a.get(eb.name)
        if ea is None:
            errores.append(f"enum eliminado: {eb.name}")
            continue
        valores_a = {v.name: v.number for v in ea.value}
        for vb in eb.value:
            if vb.name not in valores_a:
                errores.append(f"valor de enum eliminado: {eb.name}.{vb.name}")
            elif valores_a[vb.name] != vb.number:
                errores.append(f"valor de enum con otro número: {eb.name}.{vb.name}")
    servicios_a = {s.name: s for s in fa.service}
    for sb in fb.service:
        sa = servicios_a.get(sb.name)
        if sa is None:
            errores.append(f"servicio eliminado/renombrado: {sb.name}")
            continue
        rpcs_a = {m.name: m for m in sa.method}
        for rb in sb.method:
            ra = rpcs_a.get(rb.name)
            if ra is None:
                errores.append(f"RPC eliminado: {sb.name}.{rb.name}")
            elif (ra.input_type, ra.output_type, ra.client_streaming, ra.server_streaming) != \
                 (rb.input_type, rb.output_type, rb.client_streaming, rb.server_streaming):
                errores.append(f"RPC {sb.name}.{rb.name} cambió de tipos o de streaming")
    return errores


# ------------------------------------------------------------------ OpenAPI
def _res(spec: dict, nodo):
    """Sigue $ref."""
    visto = 0
    while isinstance(nodo, dict) and "$ref" in nodo and visto < 20:
        partes = nodo["$ref"].lstrip("#/").split("/")
        nodo = spec
        for p in partes:
            nodo = nodo[p]
        visto += 1
    return nodo


def _tipo(spec, esq) -> str:
    esq = _res(spec, esq) or {}
    if "anyOf" in esq or "oneOf" in esq:
        alts = esq.get("anyOf") or esq.get("oneOf")
        return "|".join(sorted(_tipo(spec, a) for a in alts))
    return str(esq.get("type", "objeto" if "properties" in esq else "?"))


def _comparar_esquema(spec_b, spec_a, b, a, ruta, rol, errores, avisos, nivel=0):
    b, a = _res(spec_b, b) or {}, _res(spec_a, a) or {}
    if nivel > 6:
        return
    if _tipo(spec_b, b) != _tipo(spec_a, a):
        errores.append(f"{ruta}: el tipo cambió {_tipo(spec_b, b)} → {_tipo(spec_a, a)}")
        return
    if b.get("enum") and a.get("enum"):
        for v in set(b["enum"]) - set(a["enum"]):
            errores.append(f"{ruta}: valor de enum eliminado {v!r}")
    for k, est in (("maximum", "menor"), ("minLength", "mayor")):
        if k in a and (k not in b or (a[k] < b[k] if k == "maximum" else a[k] > b[k])):
            avisos.append(f"{ruta}: restricción más estricta {k}={a[k]} (antes {b.get(k, 'sin límite')})")
    if "pattern" in a and a.get("pattern") != b.get("pattern"):
        avisos.append(f"{ruta}: patrón nuevo o modificado")
    pb, pa = b.get("properties", {}), a.get("properties", {})
    req_b, req_a = set(b.get("required", [])), set(a.get("required", []))
    if rol == "response":
        for p in pb:
            if p not in pa:
                errores.append(f"{ruta}: propiedad eliminada de la respuesta: {p}")
    else:  # petición
        for p in req_a - req_b:
            errores.append(f"{ruta}: propiedad OBLIGATORIA nueva en la petición: {p}")
    for p in pb:
        if p in pa:
            _comparar_esquema(spec_b, spec_a, pb[p], pa[p], f"{ruta}.{p}", rol, errores, avisos, nivel + 1)
    if "items" in b and "items" in a:
        _comparar_esquema(spec_b, spec_a, b["items"], a["items"], f"{ruta}[]", rol, errores, avisos, nivel + 1)


def comparar_openapi(base: dict, actual: dict) -> tuple[list[str], list[str]]:
    errores: list[str] = []
    avisos: list[str] = []
    for ruta, ops_b in base.get("paths", {}).items():
        ops_a = actual.get("paths", {}).get(ruta)
        if ops_a is None:
            errores.append(f"ruta eliminada: {ruta}")
            continue
        for metodo, op_b in ops_b.items():
            if metodo.startswith("x-") or not isinstance(op_b, dict):
                continue
            op_a = ops_a.get(metodo)
            if op_a is None:
                errores.append(f"operación eliminada: {metodo.upper()} {ruta}")
                continue
            etiqueta = f"{metodo.upper()} {ruta}"
            # parámetros
            par_a = {(p["name"], p["in"]): p for p in map(lambda x: _res(actual, x), op_a.get("parameters", []))}
            for p in map(lambda x: _res(base, x), op_b.get("parameters", [])):
                q = par_a.get((p["name"], p["in"]))
                if q is None:
                    errores.append(f"{etiqueta}: parámetro eliminado {p['name']}")
                    continue
                if q.get("required") and not p.get("required"):
                    errores.append(f"{etiqueta}: el parámetro {p['name']} pasó a ser obligatorio")
                _comparar_esquema(base, actual, p.get("schema", {}), q.get("schema", {}),
                                  f"{etiqueta} param {p['name']}", "request", errores, avisos)
            for nuevo in set(par_a) - {(p["name"], p["in"]) for p in map(lambda x: _res(base, x), op_b.get("parameters", []))}:
                if par_a[nuevo].get("required"):
                    errores.append(f"{etiqueta}: parámetro obligatorio NUEVO {nuevo[0]}")
            # cuerpo de petición
            cb = op_b.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
            ca = op_a.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
            if cb and ca:
                _comparar_esquema(base, actual, cb, ca, f"{etiqueta} cuerpo", "request", errores, avisos)
            elif ca and not cb and op_a.get("requestBody", {}).get("required"):
                errores.append(f"{etiqueta}: ahora exige un cuerpo que antes no tenía")
            # respuestas
            for codigo, r_b in op_b.get("responses", {}).items():
                r_a = op_a.get("responses", {}).get(codigo)
                if r_a is None:
                    errores.append(f"{etiqueta}: código de respuesta eliminado {codigo}")
                    continue
                sb = r_b.get("content", {}).get("application/json", {}).get("schema")
                sa = r_a.get("content", {}).get("application/json", {}).get("schema")
                if sb and sa:
                    _comparar_esquema(base, actual, sb, sa, f"{etiqueta} → {codigo}", "response", errores, avisos)
    return errores, avisos


# Migración U1 → saga solicitada: Inscripción ya no propaga fallas de Cupos.
# La base histórica se conserva. Solo se aceptan estas eliminaciones concretas;
# cualquier otra ruptura sigue fallando. Ver docs/saga.md.
CAMBIOS_SAGA = {
    f"POST {route}: código de respuesta eliminado {code}"
    for route, codes in {
        "/v1/enrollments": (502, 504),
        "/v1/enrollments/{enrollment_id}/revert": (409, 500, 502, 503, 504),
    }.items() for code in codes
}


def errores_no_autorizados(errores):
    return [error for error in errores if error not in CAMBIOS_SAGA]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--actualizar-base", action="store_true", help="la versión actual pasa a ser la base (al publicar)")
    a = ap.parse_args()

    if a.actualizar_base:
        BASE.mkdir(exist_ok=True)
        from google.protobuf import descriptor_pb2  # noqa: F401
        (BASE / "cupo.v1.desc").write_bytes(descriptor_set(PROTO).SerializeToString())
        shutil.copy(PROTO, BASE / "cupos.base.proto")
        shutil.copy(OPENAPI, BASE / "openapi.base.json")
        print("Línea base actualizada en contrato-base/.")
        return 0

    from google.protobuf import descriptor_pb2
    base_ds = descriptor_pb2.FileDescriptorSet()
    base_ds.ParseFromString((BASE / "cupo.v1.desc").read_bytes())
    err_proto = comparar_proto(base_ds, descriptor_set(PROTO))
    err_api, avisos = comparar_openapi(json.loads((BASE / "openapi.base.json").read_text(encoding="utf-8")),
                                       json.loads(OPENAPI.read_text(encoding="utf-8")))
    for e in set(err_api) & CAMBIOS_SAGA:
        print("MIGRACIÓN [OpenAPI] ", e)
    err_api = errores_no_autorizados(err_api)
    for e in err_proto:
        print("ROMPE  [.proto]  ", e)
    for e in err_api:
        print("ROMPE  [OpenAPI] ", e)
    for w in avisos:
        print("AVISO  [OpenAPI] ", w)
    if err_proto or err_api:
        print("\nIncompatible con la línea base: sube la versión mayor (cupo.v2 / /v2) o revierte el cambio.")
        return 1
    print("Compatible con la línea base (contrato-base/)." + (f" {len(avisos)} aviso(s)." if avisos else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
