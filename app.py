import os
import re
import cv2
import pytesseract
from flask import Flask, request, jsonify
from PIL import Image
from supabase import create_client

app = Flask(__name__)

# TUS CREDENCIALES REALES DE SUPABASE
SUPABASE_URL = "https://qhxycvhdyafcxjozfyhk.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InFoeHljdmhkeWFmY3hqb3pmeWhrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTE5MTU5NzYsImV4cCI6MjEwNzQ5MTk3Nn0.WCWLF0mYNzzzqB6v9g4o8QgbkMlSlQYfrxdKFJ8jIHM"
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# TUS DATOS OFICIALES DE LA AGENCIA
MIS_DATOS = {
    "banco": "0102",              # Banco de Venezuela
    "telefono": "04249249777",    # Tu Pago Móvil
    "cedula": "28701248"          # Tu Cédula o RIF
}

def preprocesar_imagen(ruta_imagen):
    """Mejora la nitidez del capture para que Tesseract lea mejor los números."""
    imagen = cv2.imread(ruta_imagen)
    if imagen is None:
        return None
    gris = cv2.cvtColor(imagen, cv2.COLOR_BGR2GRAY)
    _, umbral = cv2.threshold(gris, 150, 255, cv2.THRESH_BINARY)
    return umbral

def verificar_referencia_en_bd(referencia):
    """Consulta en Supabase si la referencia ya fue utilizada."""
    respuesta = supabase.table("pagos_agencia").select("referencia").eq("referencia", referencia).execute()
    return len(respuesta.data) > 0

def guardar_referencia_en_bd(referencia):
    """Guarda la referencia aprobada para bloquear futuros intentos de reutilización."""
    supabase.table("pagos_agencia").insert({"referencia": referencia}).execute()

@app.route('/webhook-whatsapp', methods=['POST'])
def webhook_whatsapp():
    """Endpoint que recibe las imágenes y datos enviados al WhatsApp de la agencia."""
    try:
        data = request.json
        
        # Validación de seguridad: Asegurarnos de que sea una imagen
        tipo_mensaje = data.get("type")
        if tipo_mensaje != "image":
            return jsonify({"status": "ignored", "reason": "No es una imagen"}), 200
        
        ruta_temporal = "temp_capture.jpg"
        
        # (Aquí descargarías la imagen usando la URL que te provee tu API de WhatsApp)
        
        # --- PROCESAMIENTO OCR ---
        imagen_limpia = preprocesar_imagen(ruta_temporal)
        if imagen_limpia is None:
            return jsonify({"respuesta": "❌ No se pudo procesar la imagen. Envía la foto más clara."})
            
        texto_extraido = pytesseract.image_to_string(imagen_limpia, lang='spa').upper()
        
        # --- VALIDACIÓN 1: ¿Son tus datos de pago? ---
        es_mi_cuenta = (
            MIS_DATOS["telefono"] in texto_extraido or 
            MIS_DATOS["cedula"] in texto_extraido or 
            MIS_DATOS["banco"] in texto_extraido
        )
        
        if not es_mi_cuenta:
            return jsonify({
                "respuesta": "❌ **Pago Rechazado:** Los datos de destino en el comprobante no coinciden con los de la agencia (Banco de Venezuela, 0424-9249777)."
            })
        
        # --- EXTRACCIÓN DE REFERENCIA ---
        coincidencias = re.findall(r'(?:REF|REFERENCIA|COMPROBANTE|NRO)[:\s]*(\d{6,12})', texto_extraido)
        
        if not coincidencias:
            coincidencias = re.findall(r'\b\d{6,12}\b', texto_extraido)
            
        if not coincidencias:
            return jsonify({
                "respuesta": "⚠️ **Revisión Manual:** No se detectó con claridad el número de referencia en el capture."
            })
            
        referencia_detectada = coincidencias[0]
        
        # --- VALIDACIÓN 2: ¿Ya fue usada esta referencia? ---
        if verificar_referencia_en_bd(referencia_detectada):
            return jsonify({
                "respuesta": f"❌ **Pago Rechazado:** La referencia **{referencia_detectada}** ya fue utilizada anteriormente."
            })
            
        # --- TODO EXITOSO: GUARDAR Y APROBAR ---
        guardar_referencia_en_bd(referencia_detectada)
        
        return jsonify({
            "respuesta": f"✅ **¡Pago Aprobado!** Referencia **{referencia_detectada}** registrada con éxito."
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)