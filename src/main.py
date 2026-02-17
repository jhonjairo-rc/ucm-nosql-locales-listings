import pandas as pd
import duckdb
import json
import os
import numpy as np
from pyproj import Transformer

# --- CONFIGURACIÓN ---
# Ajusta estas rutas a donde tengas tus archivos
RAW_DIR = "."  # Directorio donde están los CSV y el JSON original
OUTPUT_DIR = "mongo_ready"  # Directorio de salida
os.makedirs(OUTPUT_DIR, exist_ok=True)

ARCHIVOS = {
    "locales": "muestra_locales.csv",
    "actividades": "muestra_actividadeconomica.csv",
    "licencias": "muestra_licencias.csv",
    "terrazas": "muestra_terrazas.csv",
    "airbnb": "airbnb_listings.json"
}

# Transformador de Coordenadas: De UTM Zona 30N (Madrid) a WGS84 (GPS)
# MongoDB SOLO entiende WGS84.
transformer = Transformer.from_crs("EPSG:25830", "EPSG:4326", always_xy=True)

def convertir_utm_a_geojson(x, y):
    """Convierte coordenadas UTM a formato GeoJSON [Lon, Lat]"""
    try:
        # Verificamos que sean números válidos
        if pd.isna(x) or pd.isna(y) or x == 0 or y == 0:
            return None
        
        # Transformación
        lon, lat = transformer.transform(x, y)
        
        # Devolver estructura GeoJSON
        return {
            "type": "Point",
            "coordinates": [lon, lat]  # IMPORTANTE: MongoDB exige [Longitud, Latitud]
        }
    except Exception:
        return None

def procesar_locales_mongodb():
    print("🚀 1. Procesando Colección Principal: LOCALES (Embedded Model)...")
    
    con = duckdb.connect()
    
    # 1. Cargar CSVs
    print("   - Leyendo archivos CSV...")
    locales = con.execute(f"SELECT * FROM read_csv_auto('{os.path.join(RAW_DIR, ARCHIVOS['locales'])}', sep=';')").df()
    actividades = con.execute(f"SELECT * FROM read_csv_auto('{os.path.join(RAW_DIR, ARCHIVOS['actividades'])}', sep=';')").df()
    licencias = con.execute(f"SELECT * FROM read_csv_auto('{os.path.join(RAW_DIR, ARCHIVOS['licencias'])}', sep=';')").df()
    terrazas = con.execute(f"SELECT * FROM read_csv_auto('{os.path.join(RAW_DIR, ARCHIVOS['terrazas'])}', sep=';')").df()
    
    # 2. Normalizar nombres de columnas (todo a minúsculas para evitar errores)
    for df in [locales, actividades, licencias, terrazas]:
        df.columns = [c.lower().strip() for c in df.columns]

    # 3. Agrupación (Embedding): Convertir hijas en listas de objetos
    print("   - Embebiendo sub-documentos (Actividades, Licencias, Terrazas)...")
    
    # Actividades
    act_grouped = actividades.groupby('id_local').apply(
        lambda x: x[['id_epigrafe', 'desc_epigrafe', 'id_seccion', 'desc_seccion']].to_dict(orient='records')
    ).reset_index(name='actividades')
    
    # Licencias
    lic_grouped = licencias.groupby('id_local').apply(
        lambda x: x[['ref_licencia', 'desc_tipo_licencia', 'desc_tipo_situacion_licencia', 'fecha_dec_lic']].to_dict(orient='records')
    ).reset_index(name='licencias')
    
    # Terrazas
    terr_grouped = terrazas.groupby('id_local').apply(
        lambda x: x[['id_terraza', 'superficie_es', 'mesas_es', 'sillas_es', 'hora_ini_lj_es', 'hora_fin_lj_es']].to_dict(orient='records')
    ).reset_index(name='terrazas')

    # 4. Unión con la tabla maestra
    df_final = locales.merge(act_grouped, on='id_local', how='left')
    df_final = df_final.merge(lic_grouped, on='id_local', how='left')
    df_final = df_final.merge(terr_grouped, on='id_local', how='left')
    
    # 5. Generar GeoJSON desde UTM
    print("   - Convirtiendo coordenadas UTM a Lat/Lon (GeoJSON)...")
    # Asumimos nombres estándar de las columnas de coordenadas en el CSV de Locales
    col_x = 'coordenada_x_local'
    col_y = 'coordenada_y_local'
    
    df_final['location'] = df_final.apply(lambda row: convertir_utm_a_geojson(row.get(col_x), row.get(col_y)), axis=1) # type: ignore

    # 6. Limpieza final y Exportación
    print("   - Limpiando y exportando a JSON...")
    
    # Seleccionamos columnas relevantes para el documento raíz
    cols_base = ['id_local', 'rotulo', 'desc_distrito_local', 'desc_barrio_local', 
                 'desc_vial_edificio', 'nom_edificio', 'num_edificio', 'location', 
                 'actividades', 'licencias', 'terrazas']
    
    # Filtrar solo columnas que existen (por si acaso)
    cols_existentes = [c for c in cols_base if c in df_final.columns]
    records = df_final[cols_existentes].to_dict(orient='records')

    # Limpieza de nulos y listas vacías para MongoDB
    clean_records = []
    for rec in records:
        clean_rec = {}
        for k, v in rec.items():
            # Si es una lista (nuestras hijas embebidas), si es nan/float ponemos []
            if k in ['actividades', 'licencias', 'terrazas']:
                clean_rec[k] = v if isinstance(v, list) else []
            # Si es otro campo y no es nulo, lo guardamos
            elif pd.notnull(v):
                clean_rec[k] = v
        clean_records.append(clean_rec)

    output_path = os.path.join(OUTPUT_DIR, "locales_mongodb.json")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(clean_records, f, ensure_ascii=False, indent=2)
        
    print(f"✅ [LOCALES] Generado: {output_path} ({len(clean_records)} documentos)")


def procesar_airbnb_mongodb():
    print("\n🚀 2. Procesando Colección Secundaria: AIRBNB...")
    
    input_path = os.path.join(RAW_DIR, ARCHIVOS['airbnb'])
    
    with open(input_path, 'r', encoding='utf-8') as f:
        airbnb_data = json.load(f)
        
    processed_airbnb = []
    
    print("   - Normalizando coordenadas y estructura...")
    for item in airbnb_data:
        # Extraer campos clave
        doc = {
            "id": item.get("id"),
            "name": item.get("name"),
            "price": item.get("price"),
            "room_type": item.get("room_type"),
            "distrito": item.get("neighbourhood_group_cleansed"), # Clave para agregaciones
            "barrio": item.get("neighbourhood_cleansed"),
            "amenities": item.get("amenities", [])
        }
        
        # Corrección Geoespacial: Airbnb viene [Lat, Lon], MongoDB quiere [Lon, Lat]
        loc = item.get("location", {})
        coords = loc.get("coordinates")
        
        if coords and isinstance(coords, list) and len(coords) == 2:
            # coords[0] es Lat, coords[1] es Lon en el original
            lat, lon = coords
            doc["location"] = {
                "type": "Point",
                "coordinates": [lon, lat] # Invertimos el orden
            }
        else:
            doc["location"] = None
            
        processed_airbnb.append(doc)
        
    output_path = os.path.join(OUTPUT_DIR, "airbnb_mongodb.json")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(processed_airbnb, f, ensure_ascii=False, indent=2)
        
    print(f"✅ [AIRBNB] Generado: {output_path} ({len(processed_airbnb)} documentos)")

if __name__ == "__main__":
    try:
        procesar_locales_mongodb()
        procesar_airbnb_mongodb()
        print("\n🎉 PROCESO COMPLETADO. Archivos listos para 'mongoimport'.")
    except Exception as e:
        print(f"\n❌ ERROR CRÍTICO: {e}")
        print("Asegúrate de tener instalada la librería 'pyproj': pip install pyproj")