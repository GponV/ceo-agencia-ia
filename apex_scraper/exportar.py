import sqlite3
import csv

def exportar_leads_calificados():
    conn = sqlite3.connect("prospectos_apex_scraping.db")
    cursor = conn.cursor()

    # Selecciona ÚNICAMENTE las empresas que tienen Email válido o Teléfono válido
    cursor.execute("""
        SELECT id, nombre, web, email, telefono, mercado, estado 
        FROM leads_scraping 
        WHERE (email IS NOT NULL AND email != 'Pendiente' AND email != 'No detectado') 
           OR (telefono IS NOT NULL AND telefono != 'Pendiente' AND telefono != 'No detectado' AND telefono != 'No disponible')
    """)
    leads = cursor.fetchall()
    conn.close()

    archivo_salida = "leads_listos_apex.csv"
    with open(archivo_salida, mode="w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["ID", "Empresa", "Sitio Web", "Email", "Teléfono", "Mercado", "Estado"])
        writer.writerows(leads)

    print(f"=== EXPORTACIÓN DE PROSPECTOS CALIFICADOS COMPLETADA ===")
    print(f"Total de leads listos para la agencia exportados: {len(leads)}")
    print(f"Archivo guardado: {archivo_salida}\n")
    
    # Muestra los primeros 5 prospectos calificados de ejemplo
    print("Ejemplos de leads listos:")
    for lead in leads[:5]:
        print(f"[{lead[0]}] {lead[1][:30]} | Email: {lead[3]} | Tel: {lead[4]}")

if __name__ == "__main__":
    exportar_leads_calificados()