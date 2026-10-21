#!/usr/bin/env python3
"""Fast CSV loader using db sql with --tx for batch INSERTs."""
import csv, sys, subprocess, time

DB_ID = "db-bk79nbhrbo2qgd8ndi05"
DB_CMD = "/workspace/skills/managing-databases/db"
ROWS_PER_INSERT = 200
MAX_RETRIES = 3

def esc(v):
    if v is None or v == '': return 'NULL'
    return "'" + str(v).replace("'", "''") + "'"
def esc_int(v):
    if v is None or v == '': return 'NULL'
    try: return str(int(float(v)))
    except: return 'NULL'
def esc_num(v):
    if v is None or v == '': return 'NULL'
    try: return str(float(v))
    except: return 'NULL'

COLS = "pais,transaction_code,marca,fecha_confirmacion,fecha_inicio,fecha_fin,producto,producto_agrupado,origen_codigo,destino_codigo,pais_destino,cantidad_pasajeros,oid,nombre_hotel,cadena,viaje,aerolinea_principal,canal,tipodepago,gb,fee,comision,descuento,fvm,fuente_grupo,fuente_subgrupo"

def row_to_vals(r):
    return "(" + ",".join([
        esc(r.get('Pais','')), esc(r.get('TRANSACTION_CODE','')), esc(r.get('Marca','')),
        esc(r.get('Fecha_Confirmacion','')), esc(r.get('Fecha_Inicio','')), esc(r.get('Fecha_Fin','')),
        esc(r.get('Producto','')), esc(r.get('Producto_Agrupado','')), esc(r.get('Origen_Codigo','')),
        esc(r.get('Destino_Codigo','')), esc(r.get('Pais_Destino','')), esc_int(r.get('Cantidad_Pasajeros','')),
        esc(r.get('OID','')), esc(r.get('Nombre_Hotel','')), esc(r.get('Cadena','')),
        esc(r.get('Viaje','')), esc(r.get('Aerolinea_Principal','')), esc(r.get('Canal','')),
        esc(r.get('Tipodepago','')), esc_num(r.get('GB','')), esc_num(r.get('Fee','')),
        esc_num(r.get('Comision','')), esc_num(r.get('Descuento','')), esc_num(r.get('FVM','')),
        esc(r.get('Fuente_Grupo','')), esc(r.get('Fuente_Subgrupo','')),
    ]) + ")"

def run_with_retry(cmd, timeout=30):
    for attempt in range(MAX_RETRIES):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return r
        if '407' in (r.stderr or '') or 'CONNECT' in (r.stderr or ''):
            time.sleep(2)
            continue
        return r
    return r

def load_csv(csv_path, start=0, count=None):
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if count:
        rows = rows[start:start+count]
    else:
        rows = rows[start:]

    total = len(rows)
    print(f"Loading {total} rows (from offset {start})")

    loaded = 0
    errors = 0

    for i in range(0, total, ROWS_PER_INSERT):
        batch = rows[i:i+ROWS_PER_INSERT]
        vals = ",".join(row_to_vals(r) for r in batch)
        stmt = f"INSERT INTO ventas ({COLS}) VALUES {vals}"

        r = run_with_retry([DB_CMD, "sql", DB_ID, stmt])

        if r.returncode != 0:
            errors += 1
            if errors <= 3:
                print(f"ERROR at row {start+i}: {r.stderr[:200]}")
            if errors > 10:
                print("Too many errors")
                break
            continue

        loaded += len(batch)
        if (loaded // ROWS_PER_INSERT) % 50 == 0 or loaded >= total:
            print(f"Progress: {loaded}/{total} ({100*loaded//total}%)")

    print(f"Done: {loaded}/{total} loaded, {errors} errors")
    return loaded, errors

if __name__ == '__main__':
    csv_path = sys.argv[1] if len(sys.argv) > 1 else 'data.csv'
    start = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    count = int(sys.argv[3]) if len(sys.argv) > 3 else None
    load_csv(csv_path, start, count)
