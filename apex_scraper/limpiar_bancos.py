import sqlite3
import csv

def purgar_bancos_y_financieras():
    conn = sqlite3.connect("prospectos_apex_scraping.db")
    cursor = conn.cursor()

    # Eliminamos cualquier registro cuyo nombre o web contenga términos bancarios o financieros
    terminos_prohibidos = ["bancrecer", "banco", "bank", "fintech", "credito", "bancario"]
    
    total_eliminados = 0
    for termino in terminos_prohibidos:
        cursor.execute("""
            DELETE FROM leads_scraping 
            WHERE LOWER(nombre) LIKE ? OR LOWER(web) LIKE ?
        """, (f"%{termino}%", f"%{termino}%"))
        total_eliminados += cursor.rowcount

    conn.commit()
    
    # Volvemos a generar los archivos CSV limpios automáticamente
    cursor.execute("""
        SELECT id, nombre, web, email, telefono, mercado, estado 
        FROM leads_scraping 
        WHERE estado LIKE 'ALTA%' OR estado LIKE 'MEDIA%'
    """)
    leads_calificados = cursor.fetchall()
    
    cursor.execute("SELECT COUNT(*) FROM leads_scraping")
    total_restantes = cursor.fetchone()[0]
    
    conn.close()

    # Actualizar archivo de alta prioridad
    archivo_salida = "leads_alta_prioridad.csv"
    with open(archivo_salida, mode="w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["ID", "Empresa", "Sitio Web", "Email", "Teléfono", "Mercado", "Prioridad"])
        writer.writerows(leads_calificados)

    print(f"=== PURGA DE BANCOS COMPLETADA ===")
    print(f"Registros bancarios eliminados de la base de datos: {total_eliminados}")
    print(f"Total de empresas limpias restantes en la base de datos: {total_restantes}")
    print(f"Archivo '{archivo_salida}' actualizado exitosamente.")

if __name__ == "__main__":
    purgar_bancos_y_financieras()