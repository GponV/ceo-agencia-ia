import sqlite3

def crear_base_datos():
    conn = sqlite3.connect("prospectos_apex_scraping.db")
    cursor = conn.cursor()
    
    # Creamos la tabla principal para almacenar los prospectos del motor multicapa
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
    print("[*] Base de datos 'prospectos_apex_scraping.db' creada e inicializada correctamente.")

if __name__ == "__main__":
    crear_base_datos()