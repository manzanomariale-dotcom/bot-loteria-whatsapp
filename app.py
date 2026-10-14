import os
import re
import tempfile
import logging

import cv2
import pytesseract
import requests
from flask import Flask, request, jsonify, Response
from supabase import create_client

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Configure these values in Render > your service > Environment.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "").strip()
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "").strip()
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "").strip()

# Agencia FyD payment details. Verify these are correct before deploying.
MIS_DATOS = {
    "banco": "0102",            # Banco de Venezuela
    "telefono": "04249249777",  # Pago Móvil
    "cedula": "28701248",       # Cédula/RIF
}

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def preprocesar_imagen(ruta_imagen):
    """Mejora la imagen para que Tesseract pueda leer el comprobante."""
    imagen = cv2.imread(ruta_imagen)
    if imagen is None:
        return None
    gris = cv2.cvtColor(imagen, cv2.COLOR_BGR2GRAY)
    _, umbral = cv2.threshold(gris, 150, 255, cv2.THRESH_BINARY)
    return umbral


def verificar_referencia_en_bd(referencia):
    """Consulta Supabase para comprobar si la referencia ya fue registrada."""
    if supabase is None:
        raise RuntimeError("Faltan SUPABASE_URL o SUPABASE_KEY en las variables de entorno.")
    respuesta = (
        supabase.table("pagos_agencia")
        .select("referencia")
        .eq("referencia", referencia)
        .limit(1)
        .execute()
    )
    return bool(respuesta.data)


def guardar_referencia_en_bd(referencia):
    """Guarda una referencia para evitar reutilizarla."""
    if supabase is None:
        raise RuntimeError("Faltan SUPABASE_URL o SUPABASE_KEY en las variables de entorno.")
    supabase.table("pagos_agencia").insert({"referencia": referencia}).execute()


def enviar_mensaje_whatsapp(numero_destino, texto_respuesta):
    """Envía un mensaje de texto mediante Meta WhatsApp Cloud API."""
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        app.logger.error("Faltan WHATSAPP_TOKEN o PHONE_NUMBER_ID en Render.")
        return False

    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": numero_destino,
        "type": "text",
        "text": {"body": texto_respuesta},
    }
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=20)
        if not response.ok:
            app.logger.error("Meta no pudo enviar el mensaje: %s %s",
                             response.status_code, response.text[:500])
        return response.ok
    except requests.RequestException:
        app.logger.exception("Error conectando con WhatsApp Cloud API.")
        return False


def descargar_imagen_meta(media_id):
    """Descarga una imagen adjunta al mensaje de WhatsApp."""
    if not WHATSAPP_TOKEN or not media_id:
        return None

    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    try:
        respuesta = requests.get(
            f"https://graph.facebook.com/v21.0/{media_id}",
            headers=headers,
            timeout=20,
        )
        respuesta.raise_for_status()
        media_url = respuesta.json().get("url")
        if not media_url:
            app.logger.error("Meta no devolvió URL para el archivo multimedia.")
            return None

        descarga = requests.get(media_url, headers=headers, timeout=30)
        descarga.raise_for_status()

        # Render has a temporary filesystem; save only for this request.
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as archivo:
            archivo.write(descarga.content)
            return archivo.name
    except (requests.RequestException, ValueError):
        app.logger.exception("No se pudo descargar el comprobante desde Meta.")
        return None


@app.route("/", methods=["GET"])
def inicio():
    return jsonify({"status": "activo", "servicio": "Agencia FyD WhatsApp"}), 200


# Meta uses GET to verify the webhook and POST to deliver events.
# Keeping both methods on the same route avoids route/method mismatches.
@app.route("/webhook-whatsapp", methods=["GET", "POST"])
def webhook_whatsapp():
    if request.method == "GET":
        mode = request.args.get("hub.mode", "")
        token = request.args.get("hub.verify_token", "")
        challenge = request.args.get("hub.challenge", "")

        if mode == "subscribe" and VERIFY_TOKEN and token == VERIFY_TOKEN and challenge:
            app.logger.info("Webhook de Meta verificado correctamente.")
            return Response(challenge, status=200, content_type="text/plain")

        app.logger.warning("Falló la verificación del webhook de Meta.")
        return Response("Token de verificación incorrecto o parámetros incompletos.", status=403)

    # POST: Meta may send status updates that do not contain a customer message.
    data = request.get_json(silent=True) or {}
    try:
        entries = data.get("entry") or []
        for entry in entries:
            for change in (entry.get("changes") or []):
                value = change.get("value") or {}
                messages = value.get("messages") or []

                for mensaje in messages:
                    numero_cliente = mensaje.get("from")
                    tipo_mensaje = mensaje.get("type")

                    if not numero_cliente:
                        continue

                    if tipo_mensaje != "image":
                        enviar_mensaje_whatsapp(
                            numero_cliente,
                            "Hola, Agencia FyD recibió tu mensaje. Para revisar un pago, envía una foto clara del comprobante.",
                        )
                        continue

                    media_id = (mensaje.get("image") or {}).get("id")
                    ruta_temporal = descargar_imagen_meta(media_id)
                    if not ruta_temporal:
                        enviar_mensaje_whatsapp(
                            numero_cliente,
                            "No se pudo descargar el comprobante. Intenta enviarlo nuevamente.",
                        )
                        continue

                    try:
                        imagen_limpia = preprocesar_imagen(ruta_temporal)
                        if imagen_limpia is None:
                            enviar_mensaje_whatsapp(
                                numero_cliente,
                                "No se pudo leer la imagen. Envía una foto más clara.",
                            )
                            continue

                        try:
                            texto_extraido = pytesseract.image_to_string(
                                imagen_limpia, lang="spa"
                            ).upper()
                        except pytesseract.TesseractError:
                            app.logger.exception("Error de Tesseract al leer el comprobante.")
                            enviar_mensaje_whatsapp(
                                numero_cliente,
                                "No se pudo leer el comprobante automáticamente. Será necesaria una revisión manual.",
                            )
                            continue

                        # This is only a preliminary OCR check. OCR finding a bank code,
                        # phone, or ID anywhere in an image does NOT prove the payment
                        # was sent to the agency. Treat uncertain cases as manual review.
                        coincide_dato = any(
                            dato in texto_extraido
                            for dato in (
                                MIS_DATOS["telefono"],
                                MIS_DATOS["cedula"],
                            )
                        )
                        if not coincide_dato:
                            enviar_mensaje_whatsapp(
                                numero_cliente,
                                "Comprobante pendiente de revisión: no pude confirmar con seguridad los datos de destino de Agencia FyD.",
                            )
                            continue

                        coincidencias = re.findall(
                            r"(?:REF(?:ERENCIA)?|COMPROBANTE|NRO\.?)[:\s#-]*(\d{6,12})",
                            texto_extraido,
                        )
                        if not coincidencias:
                            coincidencias = re.findall(r"\b\d{6,12}\b", texto_extraido)

                        if not coincidencias:
                            enviar_mensaje_whatsapp(
                                numero_cliente,
                                "Comprobante pendiente de revisión: no se detectó claramente el número de referencia.",
                            )
                            continue

                        referencia_detectada = coincidencias[0]

                        if verificar_referencia_en_bd(referencia_detectada):
                            enviar_mensaje_whatsapp(
                                numero_cliente,
                                f"Comprobante rechazado: la referencia {referencia_detectada} ya aparece registrada.",
                            )
                            continue

                        # OCR alone cannot confirm that money arrived. Registering a
                        # reference here only marks it as seen; verify the transaction
                        # in the bank before treating the payment as definitively paid.
                        guardar_referencia_en_bd(referencia_detectada)
                        enviar_mensaje_whatsapp(
                            numero_cliente,
                            f"Referencia {referencia_detectada} recibida y registrada. El pago queda pendiente de confirmar los datos de destino y la acreditación bancaria.",
                        )
                    finally:
                        try:
                            os.remove(ruta_temporal)
                        except OSError:
                            pass

        # Always acknowledge valid webhook deliveries so Meta does not retry needlessly.
        return jsonify({"status": "EVENT_RECEIVED"}), 200

    except Exception:
        app.logger.exception("Error procesando el webhook de WhatsApp.")
        # Acknowledge the webhook but log the issue; review Render logs to diagnose it.
        return jsonify({"status": "received_with_processing_error"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
