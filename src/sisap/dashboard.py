from pathlib import Path

import duckdb
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

RUTA_DUCKDB = Path("data/processed/sisap.duckdb")

# Paleta: colores de estado (no categoricos) para "subio/bajo", porque para
# el precio de un alimento subir SI significa algo malo y bajar significa
# algo bueno -- no es una serie mas, es un estado. Ver dataviz skill.
COLOR_BUENO = "#0ca30c"   # el precio bajo: bueno para quien compra
COLOR_MALO = "#d03b3b"    # el precio subio: malo para quien compra
COLOR_SERIE = "#2a78d6"   # azul: unica serie en la linea historica, no hay "identidad" que codificar
SUPERFICIE = "#fcfcfb"
TEXTO = "#0b0b0b"
GRID = "#e1e0d9"
EJE = "#c3c2b7"

ETIQUETAS_TIPO_PRECIO = {"Mínimo": "Minimo", "Promedio": "Promedio", "Máximo": "Maximo"}
NOMBRES_MES_ABREV = {
    1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Ago", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic",
}

st.set_page_config(page_title="Precios SISAP", layout="wide")


@st.cache_resource
def conectar() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(RUTA_DUCKDB), read_only=True)


def _opciones(con: duckdb.DuckDBPyConnection, sql: str) -> list[str]:
    return con.execute(sql).df().iloc[:, 0].tolist()


def _layout_base(fig: go.Figure, titulo: str, titulo_y: str) -> go.Figure:
    fig.update_layout(
        title=titulo,
        yaxis_title=titulo_y,
        plot_bgcolor=SUPERFICIE,
        paper_bgcolor=SUPERFICIE,
        font={"color": TEXTO},
        yaxis={"gridcolor": GRID, "zerolinecolor": EJE},
        xaxis={"showgrid": False},
        showlegend=False,
        margin={"t": 48, "l": 10, "r": 10, "b": 10},
    )
    return fig


if not RUTA_DUCKDB.exists():
    st.error(
        f"No existe {RUTA_DUCKDB}. Corre primero:\n\n"
        "`python -m sisap.cli construir-dwh`"
    )
    st.stop()

con = conectar()

st.sidebar.header("Filtros")
producto = st.sidebar.selectbox(
    "Producto",
    _opciones(con, "SELECT nombre_producto FROM dim_producto ORDER BY nombre_producto"),
)
region = st.sidebar.selectbox(
    "Región",
    _opciones(con, "SELECT nombre_region FROM dim_region ORDER BY nombre_region"),
)
tipo_mercado = st.sidebar.radio(
    "Tipo de mercado",
    _opciones(con, "SELECT DISTINCT tipo_mercado FROM dim_variable ORDER BY 1"),
)
etiqueta_precio = st.sidebar.selectbox("Tipo de precio", list(ETIQUETAS_TIPO_PRECIO))
tipo_precio = ETIQUETAS_TIPO_PRECIO[etiqueta_precio]

fecha_min, fecha_max = con.execute(
    "SELECT MIN(fecha), MAX(fecha) FROM fact_precios WHERE precio IS NOT NULL"
).fetchone()
rango = st.sidebar.date_input(
    "Rango de fechas", value=(fecha_min, fecha_max),
    min_value=fecha_min, max_value=fecha_max,
)
desde, hasta = rango if len(rango) == 2 else (fecha_min, fecha_max)

df = con.execute(
    """
    SELECT f.fecha, f.precio / f.equivalencia_kg AS precio, f.unidad, f.equivalencia_kg
    FROM fact_precios f
    JOIN dim_producto p ON f.producto_id = p.producto_id
    JOIN dim_region r ON f.region_id = r.region_id
    JOIN dim_variable v ON f.variable_id = v.variable_id
    WHERE p.nombre_producto = ? AND r.nombre_region = ?
      AND v.tipo_mercado = ? AND v.tipo_precio = ?
      AND f.fecha BETWEEN ? AND ?
      AND f.precio IS NOT NULL
    ORDER BY f.fecha
    """,
    [producto, region, tipo_mercado, tipo_precio, desde, hasta],
).df()

st.title(f"{producto} · {tipo_mercado} · {etiqueta_precio} · {region}")

if df.empty:
    st.warning(
        "No hay datos para esta combinación de filtros todavía "
        "(el proyecto por ahora solo recolecta Lima, precio promedio)."
    )
    st.stop()

df["fecha"] = pd.to_datetime(df["fecha"])
unidad_venta = df["unidad"].iloc[-1]
equivalencia = df["equivalencia_kg"].iloc[-1]
st.caption(
    f"Todos los precios de esta página están normalizados a **soles por kilogramo**. "
    f"En el mercado, {producto.lower()} se vende por unidades de **{unidad_venta}** "
    f"({equivalencia:g} kg cada una)."
)

# --- KPI: precio actual y si subio o bajo vs el dato anterior ---
precio_actual = df["precio"].iloc[-1]
delta_pct = None
if len(df) >= 2:
    precio_anterior = df["precio"].iloc[-2]
    if precio_anterior:
        delta_pct = (precio_actual - precio_anterior) / precio_anterior * 100

col1, col2, col3, col4 = st.columns(4)
col1.metric(
    f"Precio actual ({df['fecha'].iloc[-1].strftime('%d/%m/%Y')})",
    f"S/ {precio_actual:.2f}",
    delta=f"{delta_pct:+.1f}%" if delta_pct is not None else None,
    delta_color="inverse",  # para un precio, subir es la mala noticia
)
col2.metric("Promedio del periodo", f"S/ {df['precio'].mean():.2f}")
col3.metric("Mínimo del periodo", f"S/ {df['precio'].min():.2f}")
col4.metric("Máximo del periodo", f"S/ {df['precio'].max():.2f}")

st.caption("🔺 rojo = el precio subió · 🟢 verde = el precio bajó, en ambos gráficos de abajo")

# --- Linea: evolucion historica (correcta para series de tiempo, no barras) ---
fig_linea = go.Figure()
fig_linea.add_scatter(
    x=df["fecha"], y=df["precio"], mode="lines",
    line={"color": COLOR_SERIE, "width": 2},
    hovertemplate="%{x|%d %b %Y}<br>S/ %{y:.2f}<extra></extra>",
)
st.plotly_chart(
    _layout_base(fig_linea, "Evolución histórica", "Precio (S/ por kg)"),
    use_container_width=True,
)

# --- Barras: variacion mes a mes, coloreada por si subio o bajo ---
mensual = (
    df.set_index("fecha")["precio"]
    .resample("MS").mean()
    .reset_index()
)
mensual["cambio_pct"] = mensual["precio"].pct_change() * 100
mensual = mensual.dropna(subset=["cambio_pct"])
# etiqueta categorica (no fecha continua): cada barra es un mes discreto, y
# con 1 sola barra un eje de fechas no tiene de donde inferir el ancho
# (Plotly termina dibujando un bloque gigante con ticks en microsegundos)
mensual["etiqueta"] = mensual["fecha"].apply(
    lambda f: f"{NOMBRES_MES_ABREV[f.month]} {f.year}"
)

if not mensual.empty:
    colores = [COLOR_MALO if v > 0 else COLOR_BUENO for v in mensual["cambio_pct"]]
    fig_barras = go.Figure()
    fig_barras.add_bar(
        x=mensual["etiqueta"], y=mensual["cambio_pct"],
        marker_color=colores, marker_line_width=0,
        hovertemplate="%{x}<br>%{y:+.1f}%<extra></extra>",
    )
    fig_barras.update_traces(marker={"cornerradius": 4})
    fig_barras.update_layout(bargap=0.15, xaxis={"type": "category"})
    st.plotly_chart(
        _layout_base(fig_barras, "Variación mensual del precio", "% vs mes anterior"),
        use_container_width=True,
    )
else:
    st.info("Se necesita más de un mes de datos para mostrar la variación mensual.")

# --- Barras: comparacion regional (mismo hue: es magnitud, no identidad) ---
comparacion = con.execute(
    """
    SELECT r.nombre_region, f.precio / f.equivalencia_kg AS precio
    FROM fact_precios f
    JOIN dim_producto p ON f.producto_id = p.producto_id
    JOIN dim_region r ON f.region_id = r.region_id
    JOIN dim_variable v ON f.variable_id = v.variable_id
    WHERE p.nombre_producto = ? AND v.tipo_mercado = ? AND v.tipo_precio = ?
      AND f.fecha = (SELECT MAX(fecha) FROM fact_precios WHERE precio IS NOT NULL)
      AND f.precio IS NOT NULL
    ORDER BY f.precio DESC
    """,
    [producto, tipo_mercado, tipo_precio],
).df()

if len(comparacion) > 1:
    fig_regiones = go.Figure()
    fig_regiones.add_bar(
        x=comparacion["nombre_region"], y=comparacion["precio"],
        marker_color=COLOR_SERIE, marker_line_width=0,
        hovertemplate="%{x}<br>S/ %{y:.2f}<extra></extra>",
    )
    fig_regiones.update_traces(marker={"cornerradius": 4})
    st.plotly_chart(
        _layout_base(fig_regiones, "Comparación entre regiones (dato más reciente)", "Precio (S/ por kg)"),
        use_container_width=True,
    )
else:
    st.info(
        "Comparación regional: por ahora el proyecto solo recolecta Lima. "
        "Este gráfico se activa solo cuando haya más de una región con datos."
    )

# --- Tabla + export ---
with st.expander("Ver datos y exportar"):
    st.dataframe(df, use_container_width=True)
    st.download_button(
        "Descargar CSV",
        df.to_csv(index=False).encode("utf-8"),
        file_name=f"{producto}_{region}_{tipo_mercado}_{tipo_precio}.csv",
        mime="text/csv",
    )
