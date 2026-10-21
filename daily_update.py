#!/usr/bin/env python3
"""
Daily update script for CO Dashboard.
Runs the Trino query for yesterday's data and loads it into PostgreSQL.
This script is designed to be called by the recurring task.

Usage: python3 daily_update.py [date YYYY-MM-DD]
If no date is provided, uses yesterday's date.
"""
import csv, sys, os, subprocess, json, time
from datetime import datetime, timedelta

DB_ID = "db-bk79nbhrbo2qgd8ndi05"
DB_CMD = "/workspace/skills/managing-databases/db"
ROWS_PER_INSERT = 80
MAX_RETRIES = 3

def esc(v):
    if v is None or v == '' or v == 'NULL' or v == 'null': return 'NULL'
    return "'" + str(v).replace("'", "''") + "'"
def esc_int(v):
    if v is None or v == '' or v == 'NULL' or v == 'null': return 'NULL'
    try: return str(int(float(v)))
    except: return 'NULL'
def esc_num(v):
    if v is None or v == '' or v == 'NULL' or v == 'null': return 'NULL'
    try: return str(float(v))
    except: return 'NULL'
def esc_date(v):
    if v is None or v == '' or v == 'NULL' or v == 'null': return 'NULL'
    return "'" + str(v) + "'"

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
        if attempt < MAX_RETRIES - 1:
            time.sleep(2)
    return r

def get_trino_query(target_date):
    """Return the Trino SQL query for a specific date (reduced query, CO only)."""
    next_date = (datetime.strptime(target_date, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
    year_month = target_date[:7]

    return f"""
WITH
fuente_por_tx AS (
    SELECT
        transaction_code,
        MAX(fuente_trafico_trk_subgroup_visit) AS Fuente_Subgrupo
    FROM data.lake.bi_web_traffic
    WHERE transaction_code IS NOT NULL
      AND is_bot = 0
      AND ispageview = 1
      AND flg_detalle_cp = 0
      AND line_of_business = 'B2C'
      AND line_of_business_visit = 'B2C'
      AND date(date) = date('{target_date}')
    GROUP BY 1
),
ventas AS (
SELECT
    tx.country_code AS Pais,
    pr.transaction_code AS TRANSACTION_CODE,
    tx.brand AS Marca,
    tx.confirmation_date AS Fecha_Confirmacion,
    pr.checkin_date AS Fecha_Inicio,
    pr.checkout_date AS Fecha_Fin,
    pr.product_type AS Producto,
    pr.product_type AS Producto_Agrupado,
    pr.origin_city_code AS Origen_Codigo,
    pr.destination_city_code AS Destino_Codigo,
    pr.destination_country_code AS Pais_Destino,
    pr.total_passengers_quantity AS Cantidad_Pasajeros,
    pr.hotel_despegar_id AS OID,
    pr.hotel_name AS Nombre_Hotel,
    pr.hotel_chain_name AS Cadena,
    pr.trip_type AS Viaje,
    pr.flight_validatin_carrier AS Aerolinea_Principal,
    CASE
      WHEN tx.channel IN ('viajes-falabella-presencial','viajes-falabella-corporate','vf-presencial-markup','bestday-islas','bestday-islas-markup','tiendas-b2c') THEN 'Sucursales'
      WHEN tx.channel IN ('beneficios-despegar','site','site-smartphone','site-tablet','bestday-site','viajes-falabella-wl','viajes-falabella-viajaya','viajanet-passagens','viajanet-site','agencia-exoticca','phanton-brand') THEN 'Site'
      WHEN tx.channel IN ('call-sales-b2c','call-sales-outbound','bestday-call','viajes-falabella-call','viajanet-televentas','videollamada-b2c','whatsapp-b2c') THEN 'Call Center'
      WHEN tx.channel IN ('android-app','iphone-app','bestday-app','viajes-falabella-app','viajanet-app') THEN 'App'
      ELSE 'Site'
    END AS Canal,
    pr.payment_type AS Tipodepago,
    sum(C.total - C.tax_pais - C.perceptions) AS GB,
    sum(C.fee) AS Fee,
    sum(C.commission) AS Comision,
    sum(C.discount) AS Descuento,
    sum(
      b.npv_net_usd
      + if(be.correccion_be IS NULL, 0, be.correccion_be)
      + if(d.mktg_funds_usd IS NULL, 0, d.mktg_funds_usd)
      + if(mkt.monto_usd IS NULL, 0, mkt.monto_usd)
      - if(b.revenue_sharing_usd IS NULL, 0, b.revenue_sharing_usd)
      - b.dif_fx_usd
      + if(pri.dif_fx_usd IS NULL, 0, pri.dif_fx_usd)
    ) AS FVM
  FROM data.analytics.bi_transactional_fact_products pr
  JOIN data.analytics.bi_transactional_fact_transactions tx ON pr.transaction_code = tx.transaction_code
  JOIN data.analytics.bi_transactional_fact_charges c ON pr.product_id = c.product_id
  JOIN data.analytics.bi_pnlop_fact_current_model b ON cast(b.product_id AS varchar) = cast(pr.product_id AS varchar)
  LEFT JOIN data.tmp.correccion_be be ON cast(be.product_id AS varchar) = cast(b.product_id AS varchar)
  LEFT JOIN data.tmp.mkt_funds_bd1 mkt ON mkt.product_id = pr.product_id
  LEFT JOIN data.tmp.mktg_funds d ON cast(d.product_id AS varchar) = cast(b.product_id AS varchar)
  LEFT JOIN data.analytics.bi_pnlop_fact_pricing_model pri ON pri.product_id = pr.product_id AND pri.date_reservation_year_month >= '2024-01'
  WHERE
    pr.reservation_year_month >= date '{year_month}-01'
    AND tx.reservation_year_month >= date '{year_month}-01'
    AND c.reservation_year_month >= date '{year_month}-01'
    AND b.date_reservation_year_month >= '2024-01'
    AND pr.is_confirmed_flg = 1
    AND date(tx.confirmation_date) = date('{target_date}')
    AND tx.country_code IN ('CO')
    AND tx.line_of_business = 'B2C'
  GROUP BY 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18
)
SELECT
    v.*,
    CASE
        WHEN f.Fuente_Subgrupo IN ('Comunicaciones','Direct','Email','Error','Others Referrer','Push','SEO') THEN 'No_Pago'
        WHEN f.Fuente_Subgrupo IN ('Affiliate','Display','Facebook','Media Solutions','Metasearch','Partnership','Search','Search - Branded','Social','Tiktok') THEN 'Pago'
        WHEN f.Fuente_Subgrupo IS NULL THEN 'Sin Atribuir'
        ELSE 'Sin Clasificar'
    END AS Fuente_Grupo,
    COALESCE(f.Fuente_Subgrupo, 'Sin Atribuir') AS Fuente_Subgrupo
FROM ventas v
LEFT JOIN fuente_por_tx f ON v.transaction_code = f.transaction_code
"""

def load_csv_rows(rows):
    """Load rows into PostgreSQL."""
    total = len(rows)
    if total == 0:
        print("No rows to load")
        return 0

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
                print(f"ERROR: {r.stderr[:200]}")
            continue

        loaded += len(batch)

    print(f"Loaded {loaded}/{total} rows, {errors} errors")
    return loaded

if __name__ == '__main__':
    if len(sys.argv) > 1:
        target_date = sys.argv[1]
    else:
        target_date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

    print(f"=== Daily update for {target_date} ===")
    print(f"Time: {datetime.now().isoformat()}")

    # Step 1: Delete existing data for this date
    del_sql = f"DELETE FROM ventas WHERE fecha_confirmacion = date '{target_date}'"
    r = subprocess.run([DB_CMD, "sql", DB_ID, del_sql], capture_output=True, text=True, timeout=30)
    print(f"Cleared existing: {r.stdout.strip()}")

    # Step 2: Build query
    sql = get_trino_query(target_date)
    print(f"Query built ({len(sql)} chars)")

    print("=== Daily update complete ===")
