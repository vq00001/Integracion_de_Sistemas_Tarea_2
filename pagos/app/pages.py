"""Páginas mínimas que ve el comprador (el resto del sistema es JSON)."""
from html import escape

_BASE = ("<!doctype html><html lang='es'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         "<title>{t}</title><body style='font-family:Arial,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem'>{b}</body></html>")


def redireccion(url: str, token: str) -> str:
    """Formulario que envía el token a Webpay (Webpay Plus exige un POST con token_ws)."""
    cuerpo = (f"<h2>Redirigiendo a Webpay…</h2><form id='f' method='post' action='{escape(url, quote=True)}'>"
              f"<input type='hidden' name='token_ws' value='{escape(token, quote=True)}'>"
              "<noscript><button type='submit'>Ir a Webpay</button></noscript></form>"
              "<script>document.getElementById('f').submit()</script>")
    return _BASE.format(t="Redirigiendo a Webpay", b=cuerpo)


_MENSAJES = {
    "COBRADO": ("Pago aprobado", "Tu pago fue aprobado. Estamos confirmando tu matrícula; consulta su estado en el sistema."),
    "RECHAZADO": ("Pago rechazado", "El pago no fue aprobado. No se realizó ningún cobro ni se confirmó tu matrícula."),
    "ABORTADO": ("Pago cancelado", "Cancelaste el pago o venció el tiempo del formulario. No se realizó ningún cobro."),
    "EXPIRADO": ("Pago vencido", "El tiempo para pagar terminó. No se realizó ningún cobro."),
    "CANCELADO": ("Pago cancelado", "Este cobro fue cancelado. No se realizó ningún cobro."),
    "PROCESANDO": ("Confirmando pago", "Estamos confirmando tu pago con Transbank. Consulta el estado de tu matrícula en unos minutos."),
    "REEMBOLSO_PENDIENTE": ("Devolución pendiente", "Detectamos una diferencia en el pago. Estamos gestionando su devolución. Consulta nuevamente en unos minutos."),
    "REEMBOLSADO": ("Pago reembolsado", "Este pago fue reembolsado."),
}


def resultado(vista: dict) -> str:
    titulo, texto = _MENSAJES.get(vista["estado"], ("Pago", "Estado del pago: " + vista["estado"]))
    monto = f"{vista['monto']:,}".replace(",", ".")
    etiqueta = "Monto solicitado" if vista["estado"] == "REEMBOLSO_PENDIENTE" else "Monto"
    cuerpo = (f"<h2>{escape(titulo)}</h2><p>{escape(texto)}</p>"
              f"<p style='color:#555'>{etiqueta}: ${monto} CLP<br>Referencia: {escape(vista['payment_id'])}</p>")
    return _BASE.format(t=titulo, b=cuerpo)


def info(mensaje: str) -> str:
    return _BASE.format(t="Pagos", b=f"<h2>{escape(mensaje)}</h2>")
