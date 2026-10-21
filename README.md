# CO Dashboard — Seguimiento de Ventas

Dashboard interactivo que reemplaza el tablero de Power BI del equipo de CO.
Se actualiza automáticamente todos los días con datos de Trino/Presto.

## Arquitectura

```
Trino/Presto (Data Lake) → Query diaria → PostgreSQL (tabla ventas) → Flask App (dashboard)
CSV histórico (subido por Diego) ─────────────────────────────────→ PostgreSQL
```

## Componentes

| Componente | Descripción |
|---|---|
| `app/app.py` | Backend Flask con API endpoints |
| `app/templates/dashboard.html` | Frontend interactivo con filtros |
| `load_csv.py` | Script de carga masiva desde CSV a PostgreSQL |
| `daily_update.py` | Referencia de query diaria (usado por la tarea recurrente) |

## Estructura de datos

Tabla `ventas` en PostgreSQL:
- Datos transaccionales (una fila por transacción × producto)
- 26 columnas: pais, transaction_code, marca, fecha_confirmacion, fecha_inicio, fecha_fin, producto, producto_agrupado, origen_codigo, destino_codigo, pais_destino, cantidad_pasajeros, oid, nombre_hotel, cadena, viaje, aerolinea_principal, canal, tipodepago, gb, fee, comision, descuento, fvm, fuente_grupo, fuente_subgrupo
- Carga incremental: DELETE del día + INSERT nuevos datos

## KPIs calculados en la APP

| KPI | Fórmula |
|---|---|
| GB | SUM(gb) |
| FVM | SUM(fvm) |
| Bookings | COUNT(DISTINCT transaction_code) |
| ASP | GB / Bookings |
| Share | GB de la marca / GB total |
| vs LY | (Semana actual / Misma semana año anterior - 1) × 100 |
| vs L4W | (Semana actual / Promedio 4 semanas anteriores - 1) × 100 |

## Filtros

- **Semana**: Selector de semanas ISO (W1, W2, ... W52)
- **Marca**: Despegar, Viajes Falabella
- **País**: CO, EC
- **Canal**: Site, App, Call Center, Sucursales
- **Producto**: Vuelos, Hoteles, Excursiones, etc. (producto_agrupado)

## Datos cargados

- **Histórico**: Enero - Junio 2025 (432,886 filas, 317,307 bookings) — cargado desde CSV
- **Actualización diaria**: Tarea recurrente a las 9:00 AM Colombia, carga el día anterior desde Trino

## Tarea recurrente

- **Horario**: 9:00 AM Colombia (America/Bogota), todos los días
- **Qué hace**: Ejecuta query reducida de Trino para el día anterior → carga en PostgreSQL

## Despliegue

- **Categoría**: dashboards
- **Stack**: Python/Flask + PostgreSQL + gunicorn