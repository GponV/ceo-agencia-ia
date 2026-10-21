import sqlite3
import requests
from bs4 import BeautifulSoup
import re
import time

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}

def extraer_datos_contacto(url):
    emails_encontrados = set()
    telefonos_encontrados = set()
    try:
        resp = requests.get(url, headers=HEADERS, timeout=6)
        if resp.status_code in [200, 202]:
            html = resp.text
            soup = BeautifulSoup(html, "html.parser")
            
            # Buscar correos en enlaces mailto:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.startswith("mailto:"):
                    correo = href.replace("mailto:", "").split("?")[0].strip()
                    if "@" in correo:
                        emails_encontrados.add(correo.lower())
                elif href.startswith("tel:"):
                    tel = href.replace("tel:", "").strip()
                    if len(re.sub(r"\D", "", tel)) >= 7:
                        telefonos_encontrados.add(tel)
            
            # Buscar correos en el texto mediante expresiones regulares
            patron_email = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
            for correo in re.findall(patron_email, html):
                correo_low = correo.lower()
                if not correo_low.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".js", ".css")) and "sentry" not in correo_low and "example" not in correo_low:
                    emails_encontrados.add(correo_low)
    except Exception:
        pass

    email_final = ", ".join(list(emails_encontrados)[:2]) if emails_encontrados else "No detectado"
    tel_final = ", ".join(list(telefonos_encontrados)[:2]) if telefonos_encontrados else "No detectado"
    return email_final, tel_final

def procesar_enriquecimiento():
    conn = sqlite3.connect("prospectos_apex_scraping.db")
    cursor = conn.cursor()
    
    # Selecciona los registros que aún tienen el correo pendiente
    cursor.execute("SELECT id, web FROM leads_scraping WHERE email = 'Pendiente' OR email IS NULL")
    pendientes = cursor.fetchall()
    
    print(f"=== INICIANDO ENRIQUECIMIENTO DE {len(pendientes)} EMPRESAS ===")
    
    actualizados = 0
    for lead_id, web in pendientes:
        if not web.startswith("http"):
            continue
        print(f"[*] Analizando web: {web}")
        email, telefono = extraer_datos_contacto(web)
        
        cursor.execute("""
            UPDATE leads_scraping 
            SET email = ?, telefono = ? 
            WHERE id = ?
        """, (email, telefono, lead_id))
        conn.commit()
        actualizados += 1
        time.sleep(0.3)
        
    conn.close()
    print(f"\n=== ENRIQUECIMIENTO COMPLETADO ===")
    print(f"Se actualizaron {actualizados} empresas con sus datos de contacto.")

if __name__ == "__main__":
    procesar_enriquecimiento()