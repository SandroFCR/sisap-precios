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
COLOR_MALO = "#e66767"    # el precio subio: malo para quien compra (paso "critical" dark)
COLOR_SERIE = "#22d3ee"   # cian electrico (mismo tono que primaryColor del tema oscuro)
COLOR_SERIE_RGB = "34,211,238"
# Superficie oscura tipo "vidrio": mas clara que el fondo de pagina para que
# las tarjetas/graficos lean como paneles elevados, no como huecos en la pagina.
SUPERFICIE = "#16283d"
TEXTO = "#e8eef4"
GRID = "#28405a"
EJE = "#3d5770"

ETIQUETAS_TIPO_PRECIO = {"Mínimo": "Minimo", "Promedio": "Promedio", "Máximo": "Maximo"}
# Iconos por palabra clave del genero (no hay fotos con licencia por ahora,
# ver conversacion: la galeria de fotos reales queda como tarea aparte).
# Se busca por substring sobre el nombre de la variedad, ej. "Papa amarilla"
# contiene "papa" -> 🥔. Orden importa poco porque los nombres no se pisan.
ICONOS_PRODUCTO = {
    "aceituna": "🫒", "aceite": "🫒", "aji": "🌶️", "ajo": "🧄", "arroz": "🍚",
    "arveja": "🫛", "azucar": "🧂", "camote": "🍠", "carne": "🥩",
    "cebolla": "🧅", "cereza": "🍒", "chirimoya": "🍈", "choclo": "🌽",
    "fideos": "🍝", "fresa": "🍓", "frijol": "🫘", "gallo": "🐔",
    "gallina": "🐔", "garbanzo": "🫘", "granadilla": "🍈", "haba": "🫘",
    "harina": "🌾", "huevo": "🥚", "leche": "🥛", "lenteja": "🫘",
    "limon": "🍋", "mandarina": "🍊", "mango": "🥭", "manzana": "🍎",
    "melocoton": "🍑", "melon": "🍈", "naranja": "🍊", "olluco": "🥔",
    "pallar": "🫘", "palta": "🥑", "papa": "🥔", "papaya": "🍈",
    "pera": "🍐", "pescado": "🐟", "piña": "🍍", "platano": "🍌",
    "quinua": "🌾", "sandia": "🍉", "tarhui": "🫘", "tomate": "🍅",
    "uva": "🍇", "vainita": "🫛", "yuca": "🍠", "zanahoria": "🥕",
    "zapallo": "🎃",
}


def _icono_producto(nombre: str) -> str:
    nombre_normalizado = nombre.lower()
    for clave, icono in ICONOS_PRODUCTO.items():
        if clave in nombre_normalizado:
            return icono
    return "🛒"
NOMBRES_MES_ABREV = {
    1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Ago", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic",
}

st.set_page_config(page_title="Precios SISAP", layout="wide")

# CSS: apoyado en lo que Streamlit YA hace nativo (st.container(border=True)
# para las tarjetas) en vez de pelear con clases internas que cambian entre
# versiones. El selector de "stVerticalBlockBorderWrapper" es el mas estable
# que existe hoy para ese contenedor con borde; si en una version futura
# cambia, la tarjeta sigue viendose bien igual (Streamlit ya le pone su
# propio borde), solo se pierde el efecto vidrio esmerilado extra.
st.markdown(
    """
    <style>
    [data-testid="stAppViewContainer"] { background-color: #0d1b2a; }
    [data-testid="stSidebar"] { background-color: #0a1520; }
    [data-testid="stVerticalBlockBorderWrapper"] {
        background: rgba(22, 40, 61, 0.55);
        backdrop-filter: blur(10px);
        -webkit-backdrop-filter: blur(10px);
        border: 1px solid rgba(34, 211, 238, 0.15);
        border-radius: 12px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


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

st.sidebar.markdown("##### FILTROS AVANZADOS")
# Región va reactiva y fuera del form (a proposito): no todas las variedades
# de un producto existen en todas las regiones (ej. la yuca es "Yuca
# amarilla" en Lima pero "Yuca blanca" en Arequipa), y el dropdown de
# Producto necesita actualizarse al toque cuando cambias de region -- eso no
# pasa si esta dentro de un form, que solo procesa cambios al enviarlo.
region = st.sidebar.selectbox(
    "Región",
    _opciones(con, "SELECT nombre_region FROM dim_region ORDER BY nombre_region"),
)

fecha_min, fecha_max = con.execute(
    "SELECT MIN(fecha), MAX(fecha) FROM fact_precios WHERE precio IS NOT NULL"
).fetchone()

# El resto de filtros si va en un form: cambiarlos no dispara nada hasta que
# se aprieta "Aplicar filtros" -- evita recalcular los 3 graficos con cada
# clic suelto en un radio button o cada tecla en el rango de fechas.
with st.sidebar.form("form_filtros"):
    producto = st.selectbox(
        "Producto",
        con.execute(
            """
            SELECT DISTINCT p.nombre_producto
            FROM fact_precios f
            JOIN dim_producto p ON f.producto_id = p.producto_id
            JOIN dim_region r ON f.region_id = r.region_id
            WHERE r.nombre_region = ?
            ORDER BY 1
            """,
            [region],
        ).df().iloc[:, 0].tolist(),
        format_func=lambda p: f"{_icono_producto(p)} {p}",
    )
    tipo_mercado = st.radio(
        "Tipo de mercado",
        _opciones(con, "SELECT DISTINCT tipo_mercado FROM dim_variable ORDER BY 1"),
    )
    etiqueta_precio = st.selectbox("Tipo de precio", list(ETIQUETAS_TIPO_PRECIO))
    rango = st.date_input(
        "Rango de fechas", value=(fecha_min, fecha_max),
        min_value=fecha_min, max_value=fecha_max,
    )
    aplicar = st.form_submit_button("Aplicar filtros", type="primary")

desde, hasta = rango if len(rango) == 2 else (fecha_min, fecha_max)
tipo_precio = ETIQUETAS_TIPO_PRECIO[etiqueta_precio]

if aplicar or "filtros_aplicados" not in st.session_state:
    st.session_state["filtros_aplicados"] = {
        "region": region, "producto": producto, "tipo_mercado": tipo_mercado,
        "etiqueta_precio": etiqueta_precio, "tipo_precio": tipo_precio,
        "desde": desde, "hasta": hasta,
    }

filtros_aplicados = st.session_state["filtros_aplicados"]
region, producto = filtros_aplicados["region"], filtros_aplicados["producto"]
tipo_mercado = filtros_aplicados["tipo_mercado"]
etiqueta_precio, tipo_precio = filtros_aplicados["etiqueta_precio"], filtros_aplicados["tipo_precio"]
desde, hasta = filtros_aplicados["desde"], filtros_aplicados["hasta"]

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

st.title(f"{_icono_producto(producto)} {producto} · {tipo_mercado} · {etiqueta_precio} · 📍 {region}")

if df.empty:
    st.warning(
        "No hay datos para esta combinación exacta de filtros. SISAP no "
        "reporta precio minorista para todos los productos/regiones/fechas "
        "por igual -- prueba otro producto, región o tipo de precio."
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
with col1, st.container(border=True):
    st.caption(f"PRECIO ACTUAL ({df['fecha'].iloc[-1].strftime('%d/%m/%Y')}) ⓘ", help="Último dato dentro del rango elegido")
    st.markdown(f"### S/ {precio_actual:.2f}")
    if delta_pct is not None:
        # badge de tendencia: fondo tenue + texto en color de estado + icono
        # -- nunca solo color, para que no dependa de distinguir verde de
        # rojo (ver dataviz skill, regla de status colors)
        if delta_pct > 0:
            fondo, texto, flecha, leyenda = "rgba(230,103,103,0.15)", COLOR_MALO, "▲", "vs. periodo anterior"
        else:
            fondo, texto, flecha, leyenda = "rgba(12,163,12,0.15)", COLOR_BUENO, "▼", "vs. periodo anterior"
        st.markdown(
            f'<span style="background:{fondo};color:{texto};padding:3px 10px;'
            f'border-radius:12px;font-size:0.8rem;font-weight:600;">'
            f'{flecha} {delta_pct:+.1f}% {leyenda}</span>',
            unsafe_allow_html=True,
        )
with col2, st.container(border=True):
    st.caption("PROMEDIO PERIODO ⓘ", help="Promedio de todo el rango de fechas elegido")
    st.markdown(f"### S/ {df['precio'].mean():.2f}")
with col3, st.container(border=True):
    st.caption("MÍNIMO PERIODO ⓘ", help="Precio más bajo dentro del rango elegido")
    st.markdown(f"### S/ {df['precio'].min():.2f}")
with col4, st.container(border=True):
    st.caption("MÁXIMO PERIODO ⓘ", help="Precio más alto dentro del rango elegido")
    st.markdown(f"### S/ {df['precio'].max():.2f}")

st.caption("🔺 rojo = el precio subió · 🟢 verde = el precio bajó, en ambos gráficos de abajo")

# --- Linea: evolucion historica (correcta para series de tiempo, no barras) ---
# Muchos productos/regiones tienen huecos grandes (meses sin ningun dato
# reportado por SISAP). Conectar esos huecos con una curva suave da la
# impresion falsa de una tendencia continua mes a mes. Se corta la linea
# (insertando un punto nulo) donde el hueco entre dos datos reales es mayor
# a 45 dias, y se muestran marcadores para que se vea cuantos puntos reales
# hay de verdad -- ver bug real encontrado 2026-09-06 (Aceite/Amazonas: 4
# puntos reales en 2 anios, spline los mostraba como caida continua).
UMBRAL_HUECO_DIAS = 45
df_linea = df[["fecha", "precio"]].copy()
huecos = df_linea["fecha"].diff().dt.days > UMBRAL_HUECO_DIAS
if huecos.any():
    filas_corte = df_linea.loc[huecos].copy()
    filas_corte["fecha"] = filas_corte["fecha"] - pd.to_timedelta(1, unit="D")
    filas_corte["precio"] = None
    df_linea = pd.concat([df_linea, filas_corte]).sort_values("fecha")

with st.container(border=True):
    fig_linea = go.Figure()
    fig_linea.add_scatter(
        x=df_linea["fecha"], y=df_linea["precio"], mode="lines+markers",
        line={"color": COLOR_SERIE, "width": 2.5, "shape": "spline", "smoothing": 0.3},
        marker={"size": 6},
        # relleno mas visible que un wash de 10%, pero sigue siendo
        # translucido -- un bloque solido taparia la cuadricula y volveria
        # ilegible el eje Y (ver dataviz skill, "nunca un bloque saturado")
        fill="tozeroy", fillcolor=f"rgba({COLOR_SERIE_RGB},0.22)",
        connectgaps=False,
        hovertemplate="%{x|%d %b %Y}<br>S/ %{y:.2f}<extra></extra>",
    )
    st.plotly_chart(
        _layout_base(fig_linea, "Evolución histórica", "Precio (S/ por kg)"),
        use_container_width=True,
    )
    if huecos.any():
        st.caption(
            f"⚠ Hay {int(huecos.sum())} hueco(s) de más de {UMBRAL_HUECO_DIAS} días "
            "sin dato en este rango — la línea se corta ahí en vez de dibujar una "
            "tendencia que no existe."
        )

# --- Barras: variacion vs el ULTIMO dato real (no el mes calendario
# anterior) -- si se resamplea a meses y se calcula pct_change antes de
# quitar los meses vacios, un hueco hace que pandas compare contra NaN y el
# cambio real (a veces el mas grande) desaparece en silencio. Bug real
# encontrado 2026-09-06: el cambio de -39.75% de dic-2024 a ene-2026 no
# aparecia porque el "mes anterior" (dic-2025) estaba vacio.
mensual = (
    df.set_index("fecha")["precio"]
    .resample("MS").mean()
    .dropna()
    .reset_index()
)
mensual["meses_desde_dato_anterior"] = (
    mensual["fecha"].diff().dt.days / 30.4
).round().astype("Int64")
mensual["cambio_pct"] = mensual["precio"].pct_change() * 100
mensual = mensual.dropna(subset=["cambio_pct"])
# etiqueta categorica (no fecha continua): cada barra es un mes discreto, y
# con 1 sola barra un eje de fechas no tiene de donde inferir el ancho
# (Plotly termina dibujando un bloque gigante con ticks en microsegundos)
mensual["etiqueta"] = mensual["fecha"].apply(
    lambda f: f"{NOMBRES_MES_ABREV[f.month]} {f.year}"
)

with st.container(border=True):
    if not mensual.empty:
        colores = [COLOR_MALO if v > 0 else COLOR_BUENO for v in mensual["cambio_pct"]]
        # el texto de comparacion es honesto sobre el hueco: "vs mes anterior"
        # si son 1-2 meses, o "vs hace N meses" si hubo un salto mas grande
        etiqueta_comparacion = [
            "vs. mes anterior" if n <= 2 else f"vs. hace {n} meses"
            for n in mensual["meses_desde_dato_anterior"]
        ]
        fig_barras = go.Figure()
        fig_barras.add_bar(
            x=mensual["etiqueta"], y=mensual["cambio_pct"],
            marker_color=colores, marker_line_width=0,
            customdata=etiqueta_comparacion,
            hovertemplate="%{x}<br>%{y:+.1f}% %{customdata}<extra></extra>",
        )
        fig_barras.update_traces(marker={"cornerradius": 4})
        fig_barras.update_layout(bargap=0.15, xaxis={"type": "category"})
        st.plotly_chart(
            _layout_base(fig_barras, "Variación del precio", "% vs. dato anterior"),
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

with st.container(border=True):
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
            "No hay suficientes regiones con dato reciente para este producto/"
            "variable como para comparar."
        )

# --- Tabla paginada + export ---
with st.expander("DATOS CRUDOS Y EXPORTACIÓN"):
    FILAS_POR_PAGINA = 15
    df_tabla = df.sort_values("fecha", ascending=False).reset_index(drop=True)
    total_paginas = max(1, -(-len(df_tabla) // FILAS_POR_PAGINA))  # division hacia arriba

    col_tabla, col_boton = st.columns([3, 1])
    with col_tabla:
        pagina = st.number_input(
            "Página", min_value=1, max_value=total_paginas, value=1, step=1,
            label_visibility="collapsed",
        )
    with col_boton:
        st.download_button(
            "⬇ Descargar CSV",
            df_tabla.to_csv(index=False).encode("utf-8"),
            file_name=f"{producto}_{region}_{tipo_mercado}_{tipo_precio}.csv",
            mime="text/csv",
            use_container_width=True,
        )

    inicio = (pagina - 1) * FILAS_POR_PAGINA
    st.dataframe(df_tabla.iloc[inicio : inicio + FILAS_POR_PAGINA], use_container_width=True, hide_index=True)
    st.caption(f"Página {pagina} de {total_paginas} · {len(df_tabla)} filas en total")
