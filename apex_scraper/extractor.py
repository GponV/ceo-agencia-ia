from ddgs import DDGS
import requests
from bs4 import BeautifulSoup
import sqlite3
import time
from urllib.parse import urlparse

# Ciudades y nichos estratégicos para Estados Unidos y España
COMBINACIONES = [
    # US - Marketing y Software
    ("digital marketing agency", "Miami", "us-en"),
    ("software development company", "Miami", "us-en"),
    ("web design firm", "New York", "us-en"),
    ("digital marketing agency", "New York", "us-en"),
    ("custom software development", "Austin Texas", "us-en"),
    ("marketing agency", "Chicago IL", "us-en"),
    ("software company", "Orlando Florida", "us-en"),
    ("digital agency", "Los Angeles CA", "us-en"),
    
    # España - Clínicas y Tecnología
    ("clinica dental", "Madrid", "es-es"),
    ("agencia marketing digital", "Madrid", "es-es"),
    ("empresa desarrollo software", "Barcelona", "es-es"),
    ("agencia marketing online", "Barcelona", "es-es"),
    ("clinica dental", "Valencia", "es-es"),
    ("desarrollo web", "Valencia", "es-es"),
    ("empresa software", "Sevilla", "es-es"),
    ("clinica dental", "Bilbao", "es-es")
]

EXCLUSIONES = [
    "wikipedia.", "significados.", "definicion.", "humanidades.", 
    "lifeder.", "conceptoabc.", "arimetrics.", "microsoft.com",
    "youtube.com", "facebook.com", "instagram.com", "linkedin.com",
    "yelp.com", "clutch.co", "forbes.com", "indeed.com", 
    "topdentistas.", "paginasamarillas.", "doctoralia.", "upcity.com",
    "designrush.com", "goodfirms.co", "sortlist.", "trustpilot.com",
    "f6s.com", "itcompanies.net", "techbehemoths.com", "tripadvisor.",
    "amazon.com", "mercadolibre.com", "twitter.com", "x.com"
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}

def inicializar_base_datos():
    conn = sqlite3.connect("prospectos_apex_scraping.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS leads_scraping (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT,
            direccion TEXT,
            telefono TEXT,
            web TEXT,
            mercado TEXT,
            estado TEXT DEFAULT 'Nuevo',
            email TEXT DEFAULT 'Pendiente'
        )
    """)
    conn.commit()
    conn.close()

def guardar_lead(nombre, direccion, web, mercado):
    try:
        parsed = urlparse(web)
        dominio_base = f"{parsed.scheme}://{parsed.netloc}".lower()
    except Exception:
        dominio_base = web.lower()

    if any(exc in dominio_base for exc in EXCLUSIONES) or not dominio_base.startswith("http"):
        return False

    conn = sqlite3.connect("prospectos_apex_scraping.db")
    cursor = conn.cursor()
    
    cursor.execute("SELECT id FROM leads_scraping WHERE LOWER(web) LIKE ?", (f"{dominio_base}%",))
    existe = cursor.fetchone()
    
    if not existe:
        cursor.execute("""
            INSERT INTO leads_scraping (nombre, direccion, telefono, web, mercado, email)
            VALUES (?, ?, 'Pendiente', ?, ?, 'Pendiente')
        """, (nombre, direccion, dominio_base, mercado))
        conn.commit()
        conn.close()
        return True
        
    conn.close()
    return False

def buscar_motor_duckduckgo(query, region):
    """Capa 1: Motor estructurado DuckDuckGo."""
    resultados = []
    try:
        with DDGS() as ddgs:
            res = list(ddgs.text(query, region=region, max_results=25))
            for r in res:
                resultados.append({
                    "nombre": r.get("title", "Empresa B2B"),
                    "web": r.get("href", "")
                })
    except Exception:
        pass
    return resultados

def buscar_motor_html_alternativo(query, region):
    """Capa 2: Motor alternativo directo por HTML (Sin API Keys)."""
    resultados = []
    try:
        url = f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}"
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code in [200, 202]:
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", class_="result__url"):
                web = a.get("href", "")
                nombre = a.get_text(strip=True) or "Empresa B2B"
                if web:
                    resultados.append({"nombre": nombre, "web": web})
    except Exception:
        pass
    return resultados

def ejecutar_motor_multicapa():
    print("=== INICIANDO MOTOR DE PROSPECCIÓN MULTICAPA (MÚLTIPLES FUENTES) ===")
    total_nuevos_sesion = 0
    
    for nicher, ciudad, region in COMBINACIONES:
        query = f"{nicher} {ciudad}"
        print(f"\n[*] Consultando motores para: [{region.upper()}] {query}")
        
        # Ejecutamos las dos capas de búsqueda en paralelo para la misma consulta
        lotes_resultados = []
        lotes_resultados.extend(buscar_motor_duckduckgo(query, region))
        lotes_resultados.extend(buscar_motor_html_alternativo(query, region))
        
        agregados_query = 0
        for item in lotes_resultados:
            nombre = item["nombre"]
            web = item["web"]
            direccion = f"Mercado Internacional ({region.upper()})"
            
            if guardar_lead(nombre, direccion, web, query):
                agregados_query += 1
                total_nuevos_sesion += 1
                
        print(f" -> Empresas nuevas capturadas en este bloque multicapa: {agregados_query}")
        time.sleep(1)

    print(f"\n=== BARRIDO MULTICAPA FINALIZADO ===")
    print(f"Total de nuevas empresas añadidas en esta ejecución: {total_nuevos_sesion}")

def mostrar_conteo():
    conn = sqlite3.connect("prospectos_apex_scraping.db")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM leads_scraping")
    total = cursor.fetchone()[0]
    conn.close()
    print(f"=== TOTAL ACUMULADO EN LA BASE DE DATOS: {total} EMPRESAS ===\n")

if __name__ == "__main__":
    inicializar_base_datos()
    ejecutar_motor_multicapa()
    mostrar_conteo()