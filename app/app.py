import os
import io
import csv
import json
from datetime import datetime, timedelta, date
from flask import Flask, render_template, request, jsonify
import psycopg2
import psycopg2.extras

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

def get_db():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL not set")
    conn = psycopg2.connect(db_url)
    conn.autocommit = True
    return conn

def query_db(sql, params=None):
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or ())
        rows = cur.fetchall()
        cur.close()
        return rows
    finally:
        conn.close()

# --- Week helpers ---

def get_weeks():
    """Return list of available weeks (2026-W1, 2026-W2, ...) based on data in DB."""
    rows = query_db(
        "SELECT DISTINCT date_trunc('week', fecha_confirmacion) as week_start "
        "FROM ventas WHERE fecha_confirmacion IS NOT NULL ORDER BY 1"
    )
    weeks = []
    for r in rows:
        ws = r['week_start']
        we = ws + timedelta(days=6)
        week_num = ws.isocalendar()[1]
        weeks.append({
            'label': f"{ws.isocalendar()[0]}-W{week_num}",
            'week_num': week_num,
            'year': ws.isocalendar()[0],
            'start': ws.isoformat(),
            'end': we.isoformat(),
        })
    return weeks

def iso_week_start(year, week):
    """Get the Monday of an ISO week."""
    jan4 = date(year, 1, 4)
    week1_monday = jan4 - timedelta(days=jan4.weekday())
    return week1_monday + timedelta(weeks=week - 1)

# --- Filter options ---

def get_filter_options():
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("SELECT MIN(fecha_confirmacion) as min_date, MAX(fecha_confirmacion) as max_date FROM ventas")
        date_range = cur.fetchone()

        cur.execute("SELECT DISTINCT marca FROM ventas WHERE marca IS NOT NULL ORDER BY marca")
        marcas = [r['marca'] for r in cur.fetchall()]

        cur.execute("SELECT DISTINCT pais FROM ventas WHERE pais IS NOT NULL ORDER BY pais")
        paises = [r['pais'] for r in cur.fetchall()]

        cur.execute("SELECT DISTINCT canal FROM ventas WHERE canal IS NOT NULL ORDER BY canal")
        canales = [r['canal'] for r in cur.fetchall()]

        cur.execute("SELECT DISTINCT producto_agrupado FROM ventas WHERE producto_agrupado IS NOT NULL ORDER BY producto_agrupado")
        productos = [r['producto_agrupado'] for r in cur.fetchall()]

        cur.close()
        return {
            'date_range': {
                'min': date_range['min_date'].isoformat() if date_range and date_range['min_date'] else None,
                'max': date_range['max_date'].isoformat() if date_range and date_range['max_date'] else None,
            },
            'weeks': get_weeks(),
            'marcas': marcas,
            'paises': paises,
            'canales': canales,
            'productos': productos,
        }
    finally:
        conn.close()

# --- WHERE builder ---

def build_where(filters):
    conditions = []
    params = []
    if filters.get('marca'):
        params.append(filters['marca'])
        conditions.append("marca = %s")
    if filters.get('pais'):
        params.append(filters['pais'])
        conditions.append("pais = %s")
    if filters.get('canal'):
        params.append(filters['canal'])
        conditions.append("canal = %s")
    if filters.get('producto'):
        params.append(filters['producto'])
        conditions.append("producto_agrupado = %s")
    if filters.get('fecha_desde'):
        params.append(filters['fecha_desde'])
        conditions.append("fecha_confirmacion >= %s::date")
    if filters.get('fecha_hasta'):
        params.append(filters['fecha_hasta'])
        conditions.append("fecha_confirmacion <= %s::date")
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    return where, params

# --- KPI calculation ---

def get_kpis_for_period(filters, date_desde, date_hasta):
    """Calculate KPIs for a given date range with dimension filters."""
    f = dict(filters)
    f['fecha_desde'] = date_desde
    f['fecha_hasta'] = date_hasta
    where, params = build_where(f)

    sql = f"""
        SELECT marca,
            SUM(gb) as gb, SUM(fvm) as fvm,
            COUNT(DISTINCT transaction_code) as bookings
        FROM ventas {where} GROUP BY marca ORDER BY SUM(gb) DESC
    """
    rows = query_db(sql, params)

    total_gb = sum(float(r['gb'] or 0) for r in rows)
    total_bookings = sum(int(r['bookings']) for r in rows)
    total_fvm = sum(float(r['fvm'] or 0) for r in rows)

    results = {}
    for r in rows:
        gb = float(r['gb'] or 0)
        fvm = float(r['fvm'] or 0)
        bookings = int(r['bookings'])
        results[r['marca']] = {
            'gb': gb,
            'fvm': fvm,
            'bookings': bookings,
            'asp': gb / bookings if bookings > 0 else 0,
            'share': gb / total_gb * 100 if total_gb > 0 else 0,
        }
    results['Total'] = {
        'gb': total_gb,
        'fvm': total_fvm,
        'bookings': total_bookings,
        'asp': total_gb / total_bookings if total_bookings > 0 else 0,
        'share': 100.0,
    }
    return results

def get_comparisons(filters):
    """Calculate current week, vs LY, and vs L4W."""
    week_label = filters.get('week', '')
    weeks = get_weeks()

    selected = None
    if week_label:
        for w in weeks:
            if w['label'] == week_label:
                selected = w
                break

    if not selected:
        selected = weeks[-1] if weeks else None

    if not selected:
        return {'data': [], 'dates': {}}

    cur_start = selected['start']
    cur_end = selected['end']
    cur_year = selected['year']
    cur_week_num = selected['week_num']

    # LY: same ISO week, previous year
    ly_start_dt = iso_week_start(cur_year - 1, cur_week_num)
    ly_end_dt = ly_start_dt + timedelta(days=6)
    ly_start = ly_start_dt.isoformat()
    ly_end = ly_end_dt.isoformat()

    # L4W: average of the 4 weeks immediately before the current week
    selected_idx = None
    for i, w in enumerate(weeks):
        if w['label'] == selected['label']:
            selected_idx = i
            break

    l4w_weeks = []
    if selected_idx is not None:
        l4w_weeks = weeks[max(0, selected_idx - 4):selected_idx]

    # Current week KPIs
    cur_data = get_kpis_for_period(filters, cur_start, cur_end)

    # LY KPIs
    ly_data = get_kpis_for_period(filters, ly_start, ly_end)

    # L4W KPIs: get each week separately, then average
    l4w_data = {}
    if l4w_weeks:
        weekly_kpis = []
        for w in l4w_weeks:
            wk = get_kpis_for_period(filters, w['start'], w['end'])
            weekly_kpis.append(wk)

        all_marcas = set()
        for wk in weekly_kpis:
            all_marcas.update(wk.keys())

        for marca in all_marcas:
            gbs = [wk.get(marca, {}).get('gb', 0) for wk in weekly_kpis]
            fvms = [wk.get(marca, {}).get('fvm', 0) for wk in weekly_kpis]
            bookings_list = [wk.get(marca, {}).get('bookings', 0) for wk in weekly_kpis]
            l4w_data[marca] = {
                'gb': sum(gbs) / len(gbs) if gbs else 0,
                'fvm': sum(fvms) / len(fvms) if fvms else 0,
                'bookings': sum(bookings_list) / len(bookings_list) if bookings_list else 0,
            }
            bk = l4w_data[marca]['bookings']
            l4w_data[marca]['asp'] = l4w_data[marca]['gb'] / bk if bk > 0 else 0

    # Build comparison table
    table_data = []
    for marca in cur_data:
        cur = cur_data[marca]
        ly = ly_data.get(marca, {'gb': 0, 'fvm': 0, 'bookings': 0, 'asp': 0})
        l4w = l4w_data.get(marca, {'gb': 0, 'fvm': 0, 'bookings': 0, 'asp': 0})

        row = {
            'marca': marca,
            'gb': cur['gb'],
            'share': round(cur['share'], 1),
            'fvm': cur['fvm'],
            'bookings': cur['bookings'],
            'asp': round(cur['asp'], 2),
        }

        # vs LY
        row['gb_vs_ly'] = round((cur['gb'] / ly['gb'] - 1) * 100, 1) if ly['gb'] else None
        row['fvm_vs_ly'] = round((cur['fvm'] / ly['fvm'] - 1) * 100, 1) if ly['fvm'] else None
        row['bookings_vs_ly'] = round((cur['bookings'] / ly['bookings'] - 1) * 100, 1) if ly['bookings'] else None
        row['asp_vs_ly'] = round((cur['asp'] / ly['asp'] - 1) * 100, 1) if ly['asp'] else None

        # vs L4W
        row['gb_vs_l4w'] = round((cur['gb'] / l4w['gb'] - 1) * 100, 1) if l4w['gb'] else None
        row['fvm_vs_l4w'] = round((cur['fvm'] / l4w['fvm'] - 1) * 100, 1) if l4w['fvm'] else None
        row['bookings_vs_l4w'] = round((cur['bookings'] / l4w['bookings'] - 1) * 100, 1) if l4w['bookings'] else None
        row['asp_vs_l4w'] = round((cur['asp'] / l4w['asp'] - 1) * 100, 1) if l4w['asp'] else None

        table_data.append(row)

    # Sort: Total last
    table_data.sort(key=lambda x: (x['marca'] == 'Total', -(x['gb'] if x['marca'] != 'Total' else 0)))

    # Build L4W date label
    if l4w_weeks:
        l4w_start = l4w_weeks[0]['start']
        l4w_end = l4w_weeks[-1]['end']
    else:
        l4w_start = l4w_end = None

    return {
        'data': table_data,
        'dates': {
            'current': {'desde': cur_start, 'hasta': cur_end, 'label': selected['label']},
            'ly': {'desde': ly_start, 'hasta': ly_end, 'label': f"{cur_year - 1}-W{cur_week_num}"},
            'l4w': {
                'desde': l4w_start,
                'hasta': l4w_end,
                'label': f"Promedio de {' a '.join(w['label'] for w in l4w_weeks)}" if l4w_weeks else '',
            },
        }
    }

# --- Routes ---

@app.route('/')
def dashboard():
    try:
        filter_options = get_filter_options()
    except Exception as e:
        return render_template('dashboard.html', error=str(e), filter_options=None)
    return render_template('dashboard.html', filter_options=filter_options)

@app.route('/api/data')
def api_data():
    filters = {
        'week': request.args.get('week', ''),
        'marca': request.args.get('marca', ''),
        'pais': request.args.get('pais', ''),
        'canal': request.args.get('canal', ''),
        'producto': request.args.get('producto', ''),
    }
    filters = {k: v for k, v in filters.items() if v}
    try:
        result = get_comparisons(filters)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/filters')
def api_filters():
    try:
        return jsonify(get_filter_options())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
