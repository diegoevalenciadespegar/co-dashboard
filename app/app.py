import os
import io
import csv
import json
import re
import time
from datetime import datetime, timedelta, date
from flask import Flask, render_template, request, jsonify
import psycopg2
import psycopg2.extras

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

_cache = {}
def cached(key, ttl_seconds, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl_seconds:
        return hit[1]
    value = fn()
    _cache[key] = (time.time(), value)
    return value

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

def _get_weeks_db():
    rows = query_db(
        "SELECT DISTINCT date_trunc('week', fecha_confirmacion) as week_start "
        "FROM ventas WHERE fecha_confirmacion IS NOT NULL ORDER BY 1 DESC"
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

def get_weeks():
    return cached('weeks', 600, _get_weeks_db)

def iso_week_start(year, week):
    jan4 = date(year, 1, 4)
    week1_monday = jan4 - timedelta(days=jan4.weekday())
    return week1_monday + timedelta(weeks=week - 1)

# --- Filter options ---

def _get_filter_options_db():
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
            'marcas': marcas, 'paises': paises, 'canales': canales, 'productos': productos,
        }
    finally:
        conn.close()

def get_filter_options():
    return cached('filter_options', 600, _get_filter_options_db)

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

# --- Searchers ---

def get_searchers_for_week(year, week_num, dimension, filters):
    where_parts = ["s.year = %s", "s.week = %s"]
    params = [str(year), f"{week_num:02d}"]
    if filters.get('marca'):
        params.append(filters['marca'])
        where_parts.append("s.brand = %s")
    if filters.get('canal'):
        params.append(filters['canal'])
        where_parts.append("s.canal = %s")
    if filters.get('producto'):
        params.append(filters['producto'])
        where_parts.append("s.producto_agrupado = %s")
    where = " AND ".join(where_parts)
    dim_col = f"s.{dimension}"
    sql = f"SELECT {dim_col} as dim, SUM(s.searchers) as searchers FROM searchers s WHERE {where} GROUP BY {dim_col}"
    rows = query_db(sql, params)
    result = {}
    for r in rows:
        result[r['dim']] = int(r['searchers'] or 0)
    return result

# --- Multi-period KPIs (single query) ---

ALLOWED_DIMS = {'marca', 'canal', 'producto_agrupado'}

def get_kpis_multi_period(filters, periods, dim='marca'):
    """KPIs grouped by dim for multiple periods in ONE query."""
    if dim not in ALLOWED_DIMS:
        raise ValueError(f"Dimension no permitida: {dim}")
    base_where, base_params = build_where(filters)
    case_parts, case_params, range_conds, range_params = [], [], [], []
    for name, (d, h) in periods.items():
        case_parts.append("WHEN fecha_confirmacion >= %s::date AND fecha_confirmacion < %s::date + 1 THEN %s")
        case_params += [d[:10], h[:10], name]
        range_conds.append("(fecha_confirmacion >= %s::date AND fecha_confirmacion < %s::date + 1)")
        range_params += [d[:10], h[:10]]
    where = (base_where + " AND " if base_where else " WHERE ") + "(" + " OR ".join(range_conds) + ")"
    sql = f"""
        SELECT periodo, grp,
            SUM(gb) AS gb, SUM(fvm) AS fvm,
            COUNT(DISTINCT transaction_code) AS bookings
        FROM (
            SELECT CASE {' '.join(case_parts)} END AS periodo,
                   COALESCE({dim}, 'Sin dato') AS grp, gb, fvm, transaction_code
            FROM ventas {where}
        ) t
        GROUP BY periodo, grp
    """
    rows = query_db(sql, case_params + range_params + base_params)
    out = {}
    for name in periods:
        prow = [r for r in rows if r['periodo'] == name]
        total_gb = sum(float(r['gb'] or 0) for r in prow)
        total_fvm = sum(float(r['fvm'] or 0) for r in prow)
        total_bookings = sum(int(r['bookings']) for r in prow)
        res = {}
        for r in prow:
            gb = float(r['gb'] or 0)
            fvm = float(r['fvm'] or 0)
            bk = int(r['bookings'])
            res[r['grp']] = {
                'gb': gb, 'fvm': fvm, 'bookings': bk,
                'asp': gb / bk if bk > 0 else 0,
                'share': gb / total_gb * 100 if total_gb > 0 else 0,
            }
        res['Total'] = {
            'gb': total_gb, 'fvm': total_fvm, 'bookings': total_bookings,
            'asp': total_gb / total_bookings if total_bookings > 0 else 0, 'share': 100.0,
        }
        out[name] = res
    return out

def resolve_week(filters):
    week_label = filters.get('week', '')
    weeks = get_weeks()
    selected = None
    for w in weeks:
        if w['label'] == week_label:
            selected = w
            break
    if not selected and weeks:
        selected = weeks[0]
    if selected:
        filters['fecha_desde'] = selected['start'][:10]
        filters['fecha_hasta'] = selected['end'][:10]
    filters.pop('week', None)
    return filters, selected

def get_comparisons(filters, dim='marca'):
    """Calculate current week, vs LY, and vs L4W with Sch and CVR."""
    filters, selected = resolve_week(filters)
    if not selected:
        return {'data': [], 'dates': {}}
    cur_start = selected['start']
    cur_end = selected['end']
    cur_year = selected['year']
    cur_week_num = selected['week_num']
    ly_start_dt = iso_week_start(cur_year - 1, cur_week_num)
    ly_end_dt = ly_start_dt + timedelta(days=6)
    ly_start = ly_start_dt.isoformat()
    ly_end = ly_end_dt.isoformat()
    weeks = get_weeks()
    selected_idx = None
    for i, w in enumerate(weeks):
        if w['label'] == selected['label']:
            selected_idx = i
            break
    l4w_weeks = weeks[selected_idx + 1:selected_idx + 5] if selected_idx is not None else []

    # All periods in one query - remove fecha_desde/hasta since periods handle dates
    multi_filters = {k: v for k, v in filters.items() if k not in ('fecha_desde', 'fecha_hasta')}
    periods = {'cur': (cur_start, cur_end), 'ly': (ly_start, ly_end)}
    for i, w in enumerate(l4w_weeks):
        periods[f'l4w_{i}'] = (w['start'], w['end'])
    multi = get_kpis_multi_period(multi_filters, periods, dim=dim)
    cur_data = multi['cur']
    ly_data = multi['ly']
    l4w_data = {}
    if l4w_weeks:
        weekly_kpis = [multi[f'l4w_{i}'] for i in range(len(l4w_weeks))]
        all_marcas = set()
        for wk in weekly_kpis:
            all_marcas.update(wk.keys())
        for marca in all_marcas:
            gbs = [wk.get(marca, {}).get('gb', 0) for wk in weekly_kpis]
            fvms = [wk.get(marca, {}).get('fvm', 0) for wk in weekly_kpis]
            bks = [wk.get(marca, {}).get('bookings', 0) for wk in weekly_kpis]
            l4w_data[marca] = {
                'gb': sum(gbs) / len(gbs), 'fvm': sum(fvms) / len(fvms),
                'bookings': sum(bks) / len(bks),
            }
            bk = l4w_data[marca]['bookings']
            l4w_data[marca]['asp'] = l4w_data[marca]['gb'] / bk if bk else 0

    # Searchers
    sch_filters = {k: v for k, v in filters.items() if k in ('marca', 'canal', 'producto')}
    sch_dim = 'brand' if dim == 'marca' else dim
    sch_cur = get_searchers_for_week(cur_year, cur_week_num, sch_dim, sch_filters)
    sch_ly = get_searchers_for_week(cur_year - 1, cur_week_num, sch_dim, sch_filters)
    sch_l4w = {}
    if l4w_weeks:
        weekly_sch = [get_searchers_for_week(w['year'], w['week_num'], sch_dim, sch_filters) for w in l4w_weeks]
        all_sch = set()
        for ws in weekly_sch:
            all_sch.update(ws.keys())
        for d in all_sch:
            vals = [ws.get(d, 0) for ws in weekly_sch]
            sch_l4w[d] = sum(vals) / len(vals) if vals else 0

    # Build table
    dim_key = 'marca' if dim == 'marca' else 'name'
    table_data = []
    for val in cur_data:
        c = cur_data[val]
        l = ly_data.get(val, {'gb': 0, 'fvm': 0, 'bookings': 0, 'asp': 0})
        lw = l4w_data.get(val, {'gb': 0, 'fvm': 0, 'bookings': 0, 'asp': 0})
        sch_c = sch_cur.get(val, 0) if val != 'Total' else sum(sch_cur.values())
        sch_l = sch_ly.get(val, 0) if val != 'Total' else sum(sch_ly.values())
        sch_lw = sch_l4w.get(val, 0) if val != 'Total' else sum(sch_l4w.values())
        cvr_c = c['bookings'] / sch_c * 100 if sch_c else 0
        cvr_l = l['bookings'] / sch_l * 100 if sch_l else 0
        cvr_lw = lw['bookings'] / sch_lw * 100 if sch_lw else 0
        row = {
            dim_key: val, 'gb': c['gb'], 'share': round(c['share'], 1),
            'fvm': c['fvm'], 'bookings': c['bookings'], 'asp': round(c['asp'], 2),
            'sch': sch_c, 'cvr': round(cvr_c, 2),
            'gb_vs_ly': round((c['gb']/l['gb']-1)*100, 1) if l['gb'] else None,
            'fvm_vs_ly': round((c['fvm']/l['fvm']-1)*100, 1) if l['fvm'] else None,
            'bookings_vs_ly': round((c['bookings']/l['bookings']-1)*100, 1) if l['bookings'] else None,
            'asp_vs_ly': round((c['asp']/l['asp']-1)*100, 1) if l['asp'] else None,
            'sch_vs_ly': round((sch_c/sch_l-1)*100, 1) if sch_l else None,
            'cvr_vs_ly': round((cvr_c/cvr_l-1)*100, 1) if cvr_l else None,
            'gb_vs_l4w': round((c['gb']/lw['gb']-1)*100, 1) if lw['gb'] else None,
            'fvm_vs_l4w': round((c['fvm']/lw['fvm']-1)*100, 1) if lw['fvm'] else None,
            'bookings_vs_l4w': round((c['bookings']/lw['bookings']-1)*100, 1) if lw['bookings'] else None,
            'asp_vs_l4w': round((c['asp']/lw['asp']-1)*100, 1) if lw['asp'] else None,
            'sch_vs_l4w': round((sch_c/sch_lw-1)*100, 1) if sch_lw else None,
            'cvr_vs_l4w': round((cvr_c/cvr_lw-1)*100, 1) if cvr_lw else None,
        }
        table_data.append(row)
    table_data.sort(key=lambda x: (x[dim_key] == 'Total', -(x['gb'] if x[dim_key] != 'Total' else 0)))

    l4w_labels = [w['label'] for w in l4w_weeks] if l4w_weeks else []
    return {
        'data': table_data,
        'dates': {
            'current': {'desde': cur_start, 'hasta': cur_end, 'label': selected['label']},
            'ly': {'desde': ly_start, 'hasta': ly_end, 'label': f"{cur_year - 1}-W{cur_week_num}"},
            'l4w': {'desde': l4w_weeks[-1]['start'] if l4w_weeks else None, 'hasta': l4w_weeks[0]['end'] if l4w_weeks else None,
                    'label': f"Promedio de {l4w_labels[0]} a {l4w_labels[-1]}" if len(l4w_labels) >= 2 else (l4w_labels[0] if l4w_labels else '')},
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
    filters = {k: v for k, v in {
        'week': request.args.get('week', ''),
        'marca': request.args.get('marca', ''),
        'pais': request.args.get('pais', ''),
        'canal': request.args.get('canal', ''),
        'producto': request.args.get('producto', ''),
    }.items() if v}
    try:
        result = get_comparisons(filters)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/breakdown')
def api_breakdown():
    filters = {k: v for k, v in {
        'week': request.args.get('week', ''),
        'marca': request.args.get('marca', ''),
        'pais': request.args.get('pais', ''),
    }.items() if v}
    try:
        out = {}
        for key, dim in (('canal', 'canal'), ('producto', 'producto_agrupado')):
            res = get_comparisons(filters, dim=dim)
            out[key] = [dict(r, name=r.get('marca', r.get('name', ''))) for r in res['data']]
        return jsonify(out)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# --- Trend ---

@app.route('/trend')
def trend():
    try:
        filter_options = get_filter_options()
    except Exception as e:
        return render_template('trend.html', error=str(e), filter_options=None)
    return render_template('trend.html', filter_options=filter_options)

@app.route('/api/trend')
def api_trend():
    marca = request.args.get('marca', '')
    producto = request.args.get('producto', '')
    canal = request.args.get('canal', '')
    indicator1 = request.args.get('i1', 'gb')
    indicator2 = request.args.get('i2', '')
    week_from = int(request.args.get('wf', '1'))
    week_to = int(request.args.get('wt', '53'))
    try:
        conditions = []
        params = []
        if marca: params.append(marca); conditions.append("marca = %s")
        if producto: params.append(producto); conditions.append("producto_agrupado = %s")
        if canal: params.append(canal); conditions.append("canal = %s")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        metrics = {'gb': 'SUM(gb)', 'bookings': 'COUNT(DISTINCT transaction_code)',
                   'asp': 'SUM(gb) / NULLIF(COUNT(DISTINCT transaction_code), 0)', 'fvm': 'SUM(fvm)'}
        selected = [m for m in [indicator1, indicator2] if m and m in metrics]
        if not selected: selected = ['gb']
        select_parts = ["EXTRACT(YEAR FROM fecha_confirmacion) as year", "EXTRACT(WEEK FROM fecha_confirmacion) as week_num"]
        for m in selected: select_parts.append(f"{metrics[m]} as {m}")
        sql = f"SELECT {', '.join(select_parts)} FROM ventas {where} GROUP BY 1, 2 ORDER BY 1, 2"
        rows = query_db(sql, params)
        data_2025 = {}
        data_2026 = {}
        for r in rows:
            year = int(r['year'])
            week = int(r['week_num'])
            if week < week_from or week > week_to: continue
            point = {'week': f"W{week}", 'week_num': week}
            for m in selected: point[m] = float(r[m] or 0) if r[m] is not None else 0
            if year == 2025: data_2025[week] = point
            elif year == 2026: data_2026[week] = point
        all_weeks = sorted(set(list(data_2025.keys()) + list(data_2026.keys())))
        result = {'weeks': [f"W{w}" for w in all_weeks], 'series': {}}
        for m in selected:
            result['series'][f'{m}_2025'] = [data_2025.get(w, {}).get(m, None) for w in all_weeks]
            result['series'][f'{m}_2026'] = [data_2026.get(w, {}).get(m, None) for w in all_weeks]
        result['indicators'] = selected
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# --- Assistant endpoint ---

import urllib.request
import urllib.error

TOQAN_API_URL = "https://api.toqan.ai/api"
try:
    from config import TOQAN_API_KEY
except ImportError:
    TOQAN_API_KEY = os.environ.get("TOQAN_API_KEY", "")

def build_context(filters):
    filters, selected = resolve_week(filters)
    if not selected:
        return "Sin datos disponibles"
    filters = {k: v for k, v in filters.items() if k != 'week'}
    where, params = build_where(filters)
    context_parts = []
    rows = query_db(f"SELECT marca, SUM(gb) as gb, SUM(fvm) as fvm, COUNT(DISTINCT transaction_code) as bookings FROM ventas {where} GROUP BY marca ORDER BY SUM(gb) DESC", params)
    total_gb = sum(float(r['gb'] or 0) for r in rows)
    total_bk = sum(int(r['bookings']) for r in rows)
    total_fvm = sum(float(r['fvm'] or 0) for r in rows)
    context_parts.append("TABLA 1: Venta por Marca")
    context_parts.append("marca,gb,fvm,bookings,asp,share")
    for r in rows:
        gb = float(r['gb'] or 0); bk = int(r['bookings'])
        context_parts.append(f"{r['marca']},{gb:.0f},{float(r['fvm'] or 0):.0f},{bk},{gb/bk if bk else 0:.2f},{gb/total_gb*100 if total_gb else 0:.1f}%")
    context_parts.append(f"Total,{total_gb:.0f},{total_fvm:.0f},{total_bk},{total_gb/total_bk if total_bk else 0:.2f},100%")
    rows = query_db(f"SELECT canal, SUM(gb) as gb, SUM(fvm) as fvm, COUNT(DISTINCT transaction_code) as bookings FROM ventas {where} GROUP BY canal ORDER BY SUM(gb) DESC", params)
    context_parts.append("\nTABLA 2: Venta por Canal")
    context_parts.append("canal,gb,fvm,bookings,asp")
    for r in rows:
        gb = float(r['gb'] or 0); bk = int(r['bookings'])
        context_parts.append(f"{r['canal']},{gb:.0f},{float(r['fvm'] or 0):.0f},{bk},{gb/bk if bk else 0:.2f}")
    rows = query_db(f"SELECT producto_agrupado, SUM(gb) as gb, SUM(fvm) as fvm, COUNT(DISTINCT transaction_code) as bookings FROM ventas {where} GROUP BY producto_agrupado ORDER BY SUM(gb) DESC", params)
    context_parts.append("\nTABLA 3: Venta por Producto")
    context_parts.append("producto,gb,fvm,bookings,asp")
    for r in rows:
        gb = float(r['gb'] or 0); bk = int(r['bookings'])
        context_parts.append(f"{r['producto_agrupado']},{gb:.0f},{float(r['fvm'] or 0):.0f},{bk},{gb/bk if bk else 0:.2f}")
    return "\n".join(context_parts)

def call_toqan_agent(message, conversation_id=None):
    if not TOQAN_API_KEY:
        return {"error": "TOQAN_API_KEY not configured"}
    endpoint = "/continue_conversation" if conversation_id else "/create_conversation"
    body = {"user_message": message}
    if conversation_id:
        body["conversation_id"] = conversation_id
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{TOQAN_API_URL}{endpoint}", data=data,
        headers={"Content-Type": "application/json", "X-Api-Key": TOQAN_API_KEY}, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read())
        conv_id = result.get("conversation_id", conversation_id or "")
        request_id = result.get("request_id")
        if not request_id: return {"error": "No request_id returned"}
    except Exception as e:
        return {"error": str(e)}
    for attempt in range(25):
        time.sleep(6)
        req = urllib.request.Request(f"{TOQAN_API_URL}/get_answer?conversation_id={conv_id}&request_id={request_id}",
            headers={"X-Api-Key": TOQAN_API_KEY}, method="GET")
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            result = json.loads(resp.read())
            status = result.get("status", "")
            if status == "finished":
                answer = result.get("answer", "")
                answer = re.sub("", "", answer, flags=re.DOTALL).strip()
                return {"answer": answer, "conversation_id": conv_id}
            elif status == "error":
                return {"error": result.get("answer", "Agent error")}
        except Exception:
            continue
    return {"error": "El asistente esta tardando. Intenta de nuevo.", "conversation_id": conv_id}

@app.route('/api/assistant', methods=['POST'])
def assistant():
    try:
        data = request.get_json()
        question = data.get('question', '')
        conversation_id = data.get('conversation_id', None)
        if not question:
            return jsonify({"error": "No question provided"}), 400
        simple_greetings = ['hola', 'holaa', 'hi', 'hello', 'buenas', 'buenos dias', 'que tal', 'hey', 'saludos']
        is_simple = question.lower().strip() in simple_greetings or len(question.lower().strip()) < 5
        if not is_simple:
            filters = {k: v for k, v in {
                'week': data.get('week', ''), 'marca': data.get('marca', ''),
                'pais': data.get('pais', ''), 'canal': data.get('canal', ''),
                'producto': data.get('producto', ''),
            }.items() if v}
            try:
                data_context = build_context(filters)
                full_message = f"{data_context}\n\n{question}"
            except Exception as e:
                full_message = f"Error: {e}\n\nPregunta: {question}"
        else:
            full_message = question
        result = call_toqan_agent(full_message, conversation_id)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
