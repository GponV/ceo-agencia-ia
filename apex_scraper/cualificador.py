import sqlite3
import csv

def calificar_y_filtrar_leads():
    conn = sqlite3.connect("prospectos_apex_scraping.db")
    cursor = conn.cursor()

    # Seleccionamos todos los registros que tienen al menos un dato de contacto válido
    cursor.execute("""
        SELECT id, nombre, web, email, telefono, mercado, estado 
        FROM leads_scraping 
        WHERE (email IS NOT NULL AND email != 'Pendiente' AND email != 'No detectado') 
           OR (telefono IS NOT NULL AND telefono != 'Pendiente' AND telefono != 'No detectado' AND telefono != 'No disponible')
    """)
    leads = cursor.fetchall()

    leads_calificados = []

    print(f"=== ANALIZANDO Y CALIFICANDO {len(leads)} EMPRESAS CON CONTACTO ===")

    for lead in leads:
        lead_id, nombre, web, email, telefono, mercado, estado = lead
        
        # Sistema de puntuación (Lead Scoring básico)
        score = 0
        razones = []

        # 1. Validación de correo directo y profesional
        if email and email not in ('Pendiente', 'No detectado'):
            score += 50
            if not any(grati in email for grati in ["gmail.com", "yahoo.com", "hotmail.com", "outlook.com"]):
                score += 20  # Correo corporativo propio
                razones.append("Correo corporativo")
            else:
                razones.append("Correo personal/genérico")

        # 2. Validación de teléfono directo
        if telefono and telefono not in ('Pendiente', 'No detectado', 'No disponible'):
            score += 30
            razones.append("Teléfono directo")

        # 3. Relevancia del mercado
        if any(term in mercado.lower() for term in ["clinica dental", "marketing", "software", "development"]):
            score += 20
            razones.append("Nicho de alta conversión")

        # Clasificamos según la puntuación obtenida
        if score >= 70:
            prioridad = "ALTA - Calificado"
        elif score >= 40:
            prioridad = "MEDIA - Potencial"
        else:
            prioridad = "BAJA"

        # Actualizamos el estado en la base de datos
        cursor.execute("""
            UPDATE leads_scraping 
            SET estado = ? 
            WHERE id = ?
        """, (prioridad, lead_id))
        
        if prioridad != "BAJA":
            leads_calificados.append((lead_id, nombre, web, email, telefono, mercado, prioridad))

    conn.commit()
    conn.close()

    # Exportamos un archivo CSV exclusivo para los leads de alta y media prioridad
    archivo_salida = "leads_alta_prioridad.csv"
    with open(archivo_salida, mode="w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["ID", "Empresa", "Sitio Web", "Email", "Teléfono", "Mercado", "Prioridad"])
        writer.writerows(leads_calificados)

    print(f"\n=== FILTRADO INTELIGENTE COMPLETADO ===")
    print(f"Se seleccionaron {len(leads_calificados)} prospectos de alta y media prioridad.")
    print(f"Archivo generado: {archivo_salida}\n")

    print("Top prospectos listos para cerrar:")
    for l in leads_calificados[:5]:
        print(f"[{l[6]}] {l[1][:30]} | Email: {l[3]}")

if __name__ == "__main__":
    calificar_y_filtrar_leads()