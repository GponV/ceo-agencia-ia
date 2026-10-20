# Aprobación: timeout y avisos de Green-API

## Cambio propuesto

- Mantener la URL de instancia `https://{GREEN_API_ID}.api.greenapi.com/waInstance{GREEN_API_ID}/sendMessage/{GREEN_API_TOKEN}`.
- Usar `timeout=10` en la solicitud POST.
- Capturar fallos de configuración, conexión y HTTP dentro del envío de WhatsApp.
- Presentar los fallos como avisos en la interfaz, con detalles sanitizados para no exponer secretos; un fallo de notificación no interrumpe el panel ni el correo.

## Validación

- Comprobar URL, `chatId`, texto, timeout y fallos HTTP/conexión mediante solicitudes simuladas.
- No enviar mensajes reales durante la prueba.

## Despliegue

El CEO confirmó explícitamente el push a GitHub y la recarga de Streamlit Cloud el 2026-10-20.
