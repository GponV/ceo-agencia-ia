# Aprobación: endpoint Green-API para Streamlit Cloud

## Cambio propuesto

- Actualizar `notificar_ceo_whatsapp` en `app_ceo.py` para construir el endpoint usando el dominio de instancia:
  `https://{GREEN_API_ID}.api.greenapi.com/waInstance{GREEN_API_ID}/sendMessage/{GREEN_API_TOKEN}`.
- Enviar el `CEO_WHATSAPP` configurado como `chatId` y el mensaje recortado de espacios iniciales/finales.
- Rechazar mensajes vacíos antes de llamar a Green-API.

## Validación

- Verificar el endpoint y JSON con una prueba de `requests.post` simulada.
- No se envía ningún mensaje real durante la validación.

## Despliegue

El CEO confirmó explícitamente el push a GitHub y la recarga de Streamlit Cloud el 2026-10-20.
