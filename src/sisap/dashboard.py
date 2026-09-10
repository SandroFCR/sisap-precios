import base64
import json
from pathlib import Path

import duckdb
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

RUTA_DUCKDB = Path("data/processed/sisap.duckdb")
RUTA_GEOJSON = Path(__file__).parent / "assets" / "peru_regiones.geojson"

# El geojson (geoBoundaries, dominio publico) trae las 24 regiones + El
# Callao + Lima departamento + "Municipalidad Metropolitana de Lima" como
# poligonos separados -- las dos ultimas se mapean a nuestra unica region
# "Lima" (SISAP no las distingue). 3 regiones de nuestro catalogo son
# provincias sueltas (Andahuaylas, Chota, Jaen) que no existen como poligono
# propio a este nivel -- esas solo se eligen por la lista, no en el mapa.
NOMBRE_GEOJSON_A_REGION = {
    "Puno": "Puno", "Tumbes": "Tumbes", "Piura": "Piura",
    "Lambayeque": "Lambayeque", "Cajamarca": "Cajamarca", "Amazonas": "Amazonas",
    "La Libertad": "La libertad", "Ancash": "Ancash", "San Martín": "San martin",
    "Huánuco": "Huanuco", "Pasco": "Pasco", "Lima": "Lima",
    "El Callao": "Callao", "Municipalidad Metropolitana de Lima": "Lima",
    "Ucayali": "Ucayali", "Junín": "Junin", "Ica": "Ica",
    "Huancavelica": "Huancavelica", "Madre de Dios": "Madre de dios",
    "Cusco": "Cusco", "Apurímac": "Apurimac", "Ayacucho": "Ayacucho",
    "Arequipa": "Arequipa", "Moquegua": "Moquegua", "Tacna": "Tacna",
    "Loreto": "Loreto",
}

# Paleta: colores de estado (no categoricos) para "subio/bajo", porque para
# el precio de un alimento subir SI significa algo malo y bajar significa
# algo bueno -- no es una serie mas, es un estado. Ver dataviz skill. Tonos
# mate/pastel (no el rojo/verde puro de antes) para cansar menos la vista en
# modo oscuro -- validados con el mismo verificador de contraste/daltonismo
# del skill (contraste >=3:1 vs superficie, separacion CVD adjunta 6.5,
# misma banda "aceptable con mitigacion" que los colores anteriores -- por
# eso el icono+texto en cada badge sigue siendo obligatorio, nunca solo color).
COLOR_BUENO = "#34D399"   # verde menta: el precio bajo, bueno para quien compra
COLOR_MALO = "#F87171"    # rojo coral: el precio subio, malo para quien compra
COLOR_SERIE = "#22d3ee"   # cian electrico (mismo tono que primaryColor del tema oscuro)
COLOR_SERIE_RGB = "34,211,238"
# Superficie de pagina muy oscura + tarjetas UN TONO mas claras (elevacion
# solida, no glass-blur) para que se lean como paneles, no como huecos.
FONDO_PAGINA = "#0f172a"
FONDO_TARJETA = "#1e293b"
SUPERFICIE = FONDO_TARJETA
TEXTO = "#e8eef4"
GRID = "#28405a"
EJE = "#3d5770"

# Estilo MapLibre casero (fondo solido, sin tiles) para el mapa de regiones,
# codificado como STRING data:-URI en vez de pasar el dict directo en
# layout.map.style. Bug real encontrado 2026-09-07: un dict se reconstruye
# de cero en CADA rerun (dentro de Python es un objeto nuevo cada vez, y
# cruza a JSON otra vez del lado del navegador), y Plotly.js/MapLibre no
# comparan el CONTENIDO de layout.map.style para decidir si hace falta
# recargar el estilo -- solo notan que "cambio" y llaman a map.setStyle(),
# una operacion pesada que tira todo el mapa (WebGL) y lo reconstruye de
# cero. Eso se veia como el mapa quedando en BLANCO totalmente por medio
# segundo en CADA clic de region, antes de volver a aparecer -- mucho mas
# grave que un simple parpadeo de color, y es justo lo que lo hacia sentir
# pesado comparado al zoom (que es 100% del lado del cliente, nunca pasa
# por Python). Una STRING constante, calculada UNA sola vez al importar el
# modulo, es identica byte a byte entre reruns -- MapLibre puede notar que
# no cambio y saltarse el setStyle() por completo.
# Bug real encontrado 2026-09-07 (segunda vuelta, confirmado en 2 grabaciones
# distintas cuadro por cuadro): con un layer "background" (sin fuente real,
# "sources: {}"), el zoom en vivo mostraba saltos de un solo cuadro donde el
# nivel de zoom retrocedia y volvia a saltar adelante -- un layer
# "background" es zoom-invariante por diseño (no tiene geometria real ligada
# al nivel de zoom), asi que MapLibre no tiene con que sincronizar sus
# repintados durante el zoom con los poligonos de las regiones que SI dibuja
# Plotly encima. Darle una fuente geojson real (aunque sea un poligono
# invisible que cubre el mundo entero, con los datos INLINE -- no una URL,
# cero pedidos de red) le da a MapLibre contenido de verdad que gestionar,
# activando su logica normal de repintado sincronizado con el zoom.
_ESTILO_MAPA_JSON = {
    "version": 8,
    "sources": {
        "fondo-src": {
            "type": "geojson",
            "data": {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[-180, -90], [180, -90], [180, 90], [-180, 90], [-180, -90]]],
                },
            },
        },
    },
    "layers": [{"id": "fondo", "type": "fill", "source": "fondo-src", "paint": {"fill-color": FONDO_TARJETA}}],
}
ESTILO_MAPA_DATA_URI = "data:application/json;base64," + base64.b64encode(
    json.dumps(_ESTILO_MAPA_JSON).encode()
).decode()

ETIQUETAS_TIPO_PRECIO = {"Mínimo": "Minimo", "Promedio": "Promedio", "Máximo": "Maximo"}
# Opcion agregada del selector de Region -- pedido explicito del usuario para
# comparar el pais entero en vez de una region a la vez. No es una region
# real de dim_region: en vez de filtrar por r.nombre_region, las consultas de
# mas abajo la detectan y agregan (promedio simple) entre todas las regiones
# que reportan cada fecha. No todas las regiones reportan el mismo mes, asi
# que el promedio puede variar de a que regiones aportaron ese punto -- se
# aclara con un caption en vez de tratar de rellenar los huecos.
TODO_EL_PERU = "Todo el Perú"

st.set_page_config(page_title="Precios SISAP", layout="wide")

# CSS: apoyado en lo que Streamlit YA hace nativo (st.container(border=True)
# para las tarjetas) en vez de pelear con clases internas que cambian entre
# versiones. El selector de "stVerticalBlockBorderWrapper" es el mas estable
# que existe hoy para ese contenedor con borde; si en una version futura
# cambia, la tarjeta sigue viendose bien igual (Streamlit ya le pone su
# propio borde), solo se pierde el tono de elevacion extra.
#
# El boton "Aplicar filtros" usa outline (no relleno cian solido) para que la
# atencion se la lleven los graficos, no el boton -- se rellena solo al pasar
# el cursor.
st.markdown(
    f"""
    <style>
    [data-testid="stAppViewContainer"] {{ background-color: {FONDO_PAGINA}; }}
    [data-testid="stSidebar"] {{ background-color: #0b1220; }}
    [data-testid="stVerticalBlockBorderWrapper"] {{
        background: {FONDO_TARJETA};
        border: 1px solid rgba(255, 255, 255, 0.06);
        border-radius: 10px;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.25);
    }}
    [data-testid="stFormSubmitButton"] button {{
        background-color: transparent;
        border: 1px solid #3b82f6;
        color: #3b82f6;
        transition: background-color 0.15s ease, color 0.15s ease;
    }}
    [data-testid="stFormSubmitButton"] button:hover {{
        background-color: #3b82f6;
        color: #f8fafc;
        border-color: #3b82f6;
    }}
    /* Barra lateral mas ancha para que el mapa se vea mas grande -- Streamlit
    no deja ensanchar un solo elemento suelto adentro, hay que ensanchar el
    contenedor completo. */
    [data-testid="stSidebar"] {{ width: 420px !important; }}
    [data-testid="stSidebar"] > div {{ width: 420px !important; }}
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def conectar() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(RUTA_DUCKDB), read_only=True)


def _opciones(con: duckdb.DuckDBPyConnection, sql: str) -> list[str]:
    return con.execute(sql).df().iloc[:, 0].tolist()


@st.cache_data
def _cargar_geojson_regiones() -> dict:
    with open(RUTA_GEOJSON, encoding="utf-8") as f:
        return json.load(f)


def _sparkline(df: pd.DataFrame, color: str, fecha_resaltada=None) -> go.Figure:
    """Mini-linea sin ejes ni numeros para las tarjetas KPI -- muestra la
    forma de la serie, y opcionalmente marca en que fecha del periodo se dio
    el valor destacado (ej. el minimo o el maximo), no solo el numero."""
    fig = go.Figure()
    fig.add_scatter(
        x=df["fecha"], y=df["precio"], mode="lines",
        line={"color": color, "width": 1.5}, hoverinfo="skip",
    )
    if fecha_resaltada is not None:
        punto = df[df["fecha"] == fecha_resaltada]
        fig.add_scatter(
            x=punto["fecha"], y=punto["precio"], mode="markers",
            marker={"color": color, "size": 6}, hoverinfo="skip",
        )
    fig.update_layout(
        height=36, margin={"t": 0, "l": 0, "r": 0, "b": 0},
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        xaxis={"visible": False}, yaxis={"visible": False}, showlegend=False,
    )
    return fig


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
        # tooltip oscuro semitransparente + texto blanco, para que no se
        # confunda con la tarjeta de fondo ni tape los datos vecinos
        hoverlabel={
            "bgcolor": "rgba(15,23,42,0.92)",
            "bordercolor": "rgba(255,255,255,0.15)",
            "font": {"color": "#FFFFFF"},
        },
    )
    return fig


if not RUTA_DUCKDB.exists():
    st.error(
        f"No existe {RUTA_DUCKDB}. Corre primero:\n\n"
        "`python -m sisap.cli construir-dwh`"
    )
    st.stop()

con = conectar()

opciones_region = [TODO_EL_PERU] + _opciones(
    con, "SELECT nombre_region FROM dim_region ORDER BY nombre_region"
)
if "region_seleccionada" not in st.session_state:
    # Lima por defecto (no la primera alfabetica, "Amazonas") -- es la region
    # con mas historia y mas relevante para la mayoria de usuarios del
    # dashboard. Si por algun motivo Lima no estuviera en el catalogo, cae al
    # primer valor disponible en vez de fallar.
    st.session_state["region_seleccionada"] = "Lima" if "Lima" in opciones_region else opciones_region[0]

# Mapa clicable de Peru: alternativa visual al dropdown, no lo reemplaza --
# 3 regiones de nuestro catalogo (Andahuaylas, Chota, Jaen) son provincias
# sueltas sin poligono propio en este geojson a nivel departamento, asi que
# solo se pueden elegir por la lista.
geojson_regiones = _cargar_geojson_regiones()
nombres_geojson = [f["properties"]["shapeName"] for f in geojson_regiones["features"]]

# La seleccion de Plotly se lee de session_state ANTES de armar la figura
# (no despues, leyendo el valor que devuelve st.plotly_chart, como se hacia
# antes). Streamlit ya deja el valor actualizado del widget "mapa_regiones"
# en session_state desde el arranque del script, incluso antes de llamar a
# st.plotly_chart -- exactamente igual que lee "region_seleccionada" antes
# de llamar al selectbox unas lineas mas abajo.
#
# Bug real encontrado 2026-09-06: el orden anterior (armar la figura,
# recien despues leer el clic, y si es nuevo, corregir session_state y
# llamar a st.rerun() para forzar un segundo rerun con el color correcto)
# funcionaba, pero cada clic disparaba DOS reruns seguidos: el automatico
# que "on_select=rerun" ya dispara solo, mas el nuestro encima. Streamlit
# alcanza a pintar el mapa con el color VIEJO en el primer rerun (armado
# antes de procesar el clic) antes de que el segundo lo reemplace -- eso se
# sentia como un parpadeo real. Leyendo el clic primero, el color correcto
# sale desde el primer (y unico) rerun, sin necesitar un st.rerun() propio.
#
# La seleccion tambien PERSISTE del lado del cliente entre reruns futuros
# no relacionados (ej. tocar el date picker) -- comparar contra la ULTIMA
# UBICACION YA PROCESADA (no contra la region actualmente seleccionada, que
# puede cambiar por otro motivo, como el dropdown) evita reprocesar un clic
# viejo cada vez. Bug real encontrado el mismo dia: comparar contra la
# region actual rompia el dropdown, porque un clic viejo se veia "distinto"
# a lo que el dropdown eligiera despues y lo pisaba de vuelta.
estado_mapa = st.session_state.get("mapa_regiones")
puntos_clic = estado_mapa.selection.points if estado_mapa and estado_mapa.selection else []
ubicacion_clic = puntos_clic[0].get("location") if puntos_clic else None
if ubicacion_clic and ubicacion_clic != st.session_state.get("_ultima_ubicacion_mapa"):
    st.session_state["_ultima_ubicacion_mapa"] = ubicacion_clic
    region_clic = NOMBRE_GEOJSON_A_REGION.get(ubicacion_clic)
    if region_clic and region_clic in opciones_region:
        st.session_state["region_seleccionada"] = region_clic

fig_mapa = go.Figure(go.Choroplethmap(
    geojson=geojson_regiones,
    locations=nombres_geojson,
    z=[
        1 if NOMBRE_GEOJSON_A_REGION.get(n) == st.session_state["region_seleccionada"] else 0
        for n in nombres_geojson
    ],
    featureidkey="properties.shapeName",
    # el tono "sin seleccionar" tenia casi el mismo color que el fondo de la
    # barra lateral -- se veia como un contorno vacio en vez de un mapa
    # relleno. La region seleccionada usa COLOR_BUENO (el mismo verde menta
    # ya validado por contraste/daltonismo, no un verde nuevo sin probar).
    #
    # go.Choropleth (el trace "geo" clasico) con fitbounds="locations" dejaba
    # una franja azul de todo el ancho del mundo en la latitud de Peru -- no
    # recortaba bien la longitud, y las 28 regiones no se distinguian entre
    # si. go.Choroplethmap (MapLibre, sin necesitar token) renderiza cada
    # region como su propio poligono con borde visible, sin ese problema.
    colorscale=[[0, "#3B73B3"], [1, COLOR_BUENO]],
    showscale=False, marker_line_color="rgba(255,255,255,0.4)", marker_line_width=1,
    hovertemplate="%{location}<extra></extra>",
    # Plotly atenua las regiones "no seleccionadas" apenas se registra el
    # clic DEL LADO DEL CLIENTE -- antes de que el servidor siquiera reciba
    # el evento, mucho menos responda con el color correcto. Esa atenuacion
    # se ve y se va sola cuando la respuesta del servidor llega con el color
    # verde definitivo, lo que se siente como un parpadeo/doble cambio justo
    # antes de que la seleccion "asiente" -- distinto al zoom, que es 100%
    # del lado del cliente (MapLibre) y no pasa por el servidor para nada.
    # Fijar la opacidad de seleccionado/no-seleccionado en 1 para ambos
    # anula esa atenuacion automatica: el unico cambio de color que se ve es
    # el que nosotros mandamos.
    selected={"marker": {"opacity": 1}},
    unselected={"marker": {"opacity": 1}},
))
fig_mapa.update_layout(
    # "carto-darkmatter" es un basemap real: pide tiles de un servidor por
    # red en cada nivel de zoom. Al hacer zoom in/out, los tiles del nuevo
    # nivel tardan un instante en llegar y reemplazan a los anteriores -- eso
    # se ve como parpadeo, y no lo arregla uirevision (que solo evita que
    # Streamlit RESETEE la vista, no controla la carga de tiles). Como aca
    # solo necesitamos los poligonos de las 28 regiones, no calles ni
    # relieve real, un estilo MapLibre casero con un solo layer "background"
    # (sin sources, sin red) elimina el parpadeo de raiz: no hay tiles que
    # cargar. ESTILO_MAPA_DATA_URI (definido arriba, junto a los colores) es
    # una STRING constante -- no un dict armado aca mismo -- para que
    # MapLibre pueda notar que no cambio entre reruns y no recargue todo el
    # mapa (ver comentario junto a la constante).
    map={
        "style": ESTILO_MAPA_DATA_URI,
        "zoom": 4.3, "center": {"lat": -9.2, "lon": -75.0},
    },
    paper_bgcolor=FONDO_TARJETA, margin={"t": 4, "l": 4, "r": 4, "b": 4}, height=520,
    uirevision="mapa_peru",
    # Mapa ESTATICO a proposito: sin zoom ni arrastre, solo clic para elegir
    # region. Se probaron varios fixes para el parpadeo/salto durante el
    # gesto de zoom (estilo sin tiles, fuente geojson real) sin exito --
    # vive en el renderizado WebGL del navegador, fuera de lo que este
    # codigo controla. dragmode=False (aca) + scrollZoom/doubleClick en
    # False (en el config de abajo) apagan la interaccion de raiz en vez de
    # seguir persiguiendo el sintoma.
    dragmode=False,
)
with st.sidebar.container(border=True):
    st.caption("Clic en el mapa para elegir región")
    st.plotly_chart(
        fig_mapa, on_select="rerun", key="mapa_regiones", use_container_width=True,
        config={"displayModeBar": False, "scrollZoom": False, "doubleClick": False},
    )

# Selector visual con fotos reales, debajo del mapa -- pedido explicito del
# usuario. No reemplaza el dropdown de Producto (sigue siendo la forma de
# elegir cualquiera de los ~250 productos del catalogo) -- es un atajo
# directo a los mas reconocibles. Cada boton busca, DENTRO de los productos
# que existen para la region actual, el primero que contenga la palabra
# clave (ej. "yuca" encuentra "Yuca amarilla" en Lima o "Yuca blanca" en
# Arequipa, lo que exista) -- asi funciona en cualquier region sin mapear un
# nombre EXACTO por region, igual que ya resuelve el dropdown de Producto.
PRODUCTOS_DESTACADOS = [
    ("papa.jpg", "Papa", "papa"),
    ("cebolla.jpg", "Cebolla", "cebolla"),
    ("tomate.jpg", "Tomate", "tomate"),
    ("arroz.jpg", "Arroz", "arroz"),
    ("yuca.jpg", "Yuca", "yuca"),
    ("huevos.jpg", "Huevos", "huevos"),
    ("aji.jpg", "Ají", "aji"),
    ("pollo.jpg", "Pollo", "pollo"),
    ("palta.jpg", "Palta", "palta"),
    ("limon.jpg", "Limón", "limon"),
]
RUTA_PRODUCTOS_DESTACADOS = Path(__file__).parent / "assets" / "productos"

with st.sidebar.container(border=True):
    st.caption("Productos destacados")
    if st.session_state["region_seleccionada"] == TODO_EL_PERU:
        productos_de_la_region = con.execute(
            "SELECT DISTINCT p.nombre_producto FROM fact_precios f "
            "JOIN dim_producto p ON f.producto_id = p.producto_id ORDER BY 1"
        ).df().iloc[:, 0].tolist()
    else:
        # ORDER BY: sin el, el orden de "SELECT DISTINCT" no esta garantizado
        # -- para una palabra clave con varias coincidencias (ej. "papa" hace
        # match con "Papa canchan", "Papa negra andina", etc.) el boton
        # terminaba eligiendo una variedad distinta en cada rerun, sin que el
        # usuario cambiara nada. Bug real encontrado 2026-09-09 verificando
        # el modo "Todo el Peru" con Playwright: el mismo boton "Papa"
        # aplicaba un producto diferente entre una corrida y la siguiente.
        productos_de_la_region = con.execute(
            """
            SELECT DISTINCT p.nombre_producto
            FROM fact_precios f
            JOIN dim_producto p ON f.producto_id = p.producto_id
            JOIN dim_region r ON f.region_id = r.region_id
            WHERE r.nombre_region = ?
            ORDER BY 1
            """,
            [st.session_state["region_seleccionada"]],
        ).df().iloc[:, 0].tolist()
    columnas_destacados = st.columns(2)
    for indice, (archivo, etiqueta, palabra_clave) in enumerate(PRODUCTOS_DESTACADOS):
        with columnas_destacados[indice % 2]:
            st.image(str(RUTA_PRODUCTOS_DESTACADOS / archivo), use_container_width=True)
            # Bug real encontrado 2026-09-08: un st.rerun() aca (antes de
            # llegar al selectbox de Región, mas abajo en el script) le
            # gana de mano a Streamlit -- si el script se aborta antes de
            # INSTANCIAR ese selectbox en este mismo rerun, Streamlit no
            # llega a confirmar su valor y lo resetea a "Lima" (el default)
            # la vez siguiente que se instancia. El usuario terminaba
            # viendo el producto correcto pero la region pisada de vuelta a
            # Lima. El clic del boton YA dispara un rerun completo por si
            # solo (como cualquier widget) -- no hace falta un rerun propio,
            # solo dejar que el script siga su curso normal hasta el final,
            # donde el selectbox si lee el session_state ya actualizado.
            if st.button(etiqueta, key=f"btn_destacado_{palabra_clave}", use_container_width=True):
                coincidencia = next(
                    (p for p in productos_de_la_region if palabra_clave in p.lower()), None
                )
                if coincidencia:
                    aplicados = dict(st.session_state["filtros_aplicados"])
                    aplicados["producto"] = coincidencia
                    aplicados["region"] = st.session_state["region_seleccionada"]
                    st.session_state["filtros_aplicados"] = aplicados
                    st.session_state["producto_seleccionado"] = coincidencia
                else:
                    st.toast(f"{etiqueta} no tiene datos en esta región.", icon="⚠️")

# Región va reactiva y fuera del form (a proposito): no todas las variedades
# de un producto existen en todas las regiones (ej. la yuca es "Yuca
# amarilla" en Lima pero "Yuca blanca" en Arequipa), y el dropdown de
# Producto necesita actualizarse al toque cuando cambias de region -- eso no
# pasa si esta dentro de un form, que solo procesa cambios al enviarlo.
#
# Toda la barra de filtros se movio de la barra lateral a una tarjeta
# horizontal arriba del titulo -- pedido explicito del usuario, para que se
# sienta como un dashboard profesional (herramientas arriba, no escondidas
# en un sidebar). Región va en su propia fila (tiene que quedar afuera del
# form, ver arriba) y el resto comparte una segunda fila DENTRO del form con
# columnas anidadas -- un patron simple y confiable en Streamlit, a
# diferencia de intentar "reabrir" el mismo form desde columnas creadas por
# separado (un form recuerda su propia posicion fija la primera vez que se
# crea, asi que sus campos terminarian ahi, no en la columna donde se llamo
# "with formulario:").
fecha_min, fecha_max = con.execute(
    "SELECT MIN(fecha), MAX(fecha) FROM fact_precios WHERE precio IS NOT NULL"
).fetchone()

with st.container(border=True):
    col_region, _ = st.columns([1, 3])
    with col_region:
        region = st.selectbox("Región", opciones_region, key="region_seleccionada")

    # El resto de filtros si va en un form: cambiarlos no dispara nada hasta
    # que se aprieta "Aplicar filtros" -- evita recalcular los 3 graficos con
    # cada clic suelto en un radio button o cada tecla en el rango de fechas.
    with st.form("form_filtros"):
        col_producto, col_mercado, col_precio, col_fechas, col_boton = st.columns(
            [1.8, 1.3, 1.1, 1.7, 1]
        )
        with col_producto:
            if region == TODO_EL_PERU:
                opciones_producto = con.execute(
                    "SELECT DISTINCT p.nombre_producto FROM fact_precios f "
                    "JOIN dim_producto p ON f.producto_id = p.producto_id ORDER BY 1"
                ).df().iloc[:, 0].tolist()
            else:
                opciones_producto = con.execute(
                    """
                    SELECT DISTINCT p.nombre_producto
                    FROM fact_precios f
                    JOIN dim_producto p ON f.producto_id = p.producto_id
                    JOIN dim_region r ON f.region_id = r.region_id
                    WHERE r.nombre_region = ?
                    ORDER BY 1
                    """,
                    [region],
                ).df().iloc[:, 0].tolist()
            # Bug real encontrado 2026-09-06: sin "key", este selectbox se
            # identifica (entre otras cosas) por su lista de opciones -- al
            # cambiar de Región, la lista cambia, Streamlit lo trata como un
            # widget nuevo y lo resetea al primer producto de la nueva
            # región SIN avisar. El usuario terminaba viendo un producto que
            # nunca eligio (ej. cambiar a "Ucayali" saltaba solo a "Aceite
            # clasico botella x 1l" porque es el primero alfabetico ahi),
            # mientras el titulo/graficos seguian mostrando la combinacion
            # aplicada anteriormente -- se leia como si la region, el
            # producto del filtro y los datos de la pagina fueran tres cosas
            # distintas. Con "key" fijo, controlamos el reseteo nosotros: si
            # el producto ya elegido sigue existiendo en la region nueva, se
            # conserva; si no, recien ahi cae al primero de la lista.
            if (
                "producto_seleccionado" not in st.session_state
                or st.session_state["producto_seleccionado"] not in opciones_producto
            ):
                st.session_state["producto_seleccionado"] = opciones_producto[0]
            producto = st.selectbox("Producto", opciones_producto, key="producto_seleccionado")
        with col_mercado:
            tipo_mercado = st.radio(
                "Tipo de mercado",
                _opciones(con, "SELECT DISTINCT tipo_mercado FROM dim_variable ORDER BY 1"),
                horizontal=True,
            )
        with col_precio:
            etiqueta_precio = st.selectbox("Tipo de precio", list(ETIQUETAS_TIPO_PRECIO))
        with col_fechas:
            rango = st.date_input(
                "Rango de fechas", value=(fecha_min, fecha_max),
                min_value=fecha_min, max_value=fecha_max,
            )
        with col_boton:
            # espaciador para que el boton quede a la altura de los otros
            # widgets (que tienen una etiqueta arriba ocupando esa altura)
            st.markdown("<div style='height:1.8rem'></div>", unsafe_allow_html=True)
            aplicar = st.form_submit_button(
                "Aplicar filtros", type="primary", use_container_width=True
            )

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

if region == TODO_EL_PERU:
    # Promedio nacional simple: una fila por fecha, promediando entre TODAS
    # las regiones que reportan ese producto/mercado/tipo_precio ese dia --
    # no todas las regiones reportan el mismo mes, asi que a que regiones
    # contribuyen cada punto puede variar (aclarado abajo con un caption, no
    # rellenado artificialmente). ANY_VALUE(unidad/equivalencia_kg) alcanza
    # porque son atributos del producto, no de la region -- deberian repetirse
    # igual en todas las filas del grupo.
    df = con.execute(
        """
        SELECT f.fecha, AVG(f.precio / f.equivalencia_kg) AS precio,
               ANY_VALUE(f.unidad) AS unidad, ANY_VALUE(f.equivalencia_kg) AS equivalencia_kg,
               COUNT(DISTINCT f.region_id) AS num_regiones
        FROM fact_precios f
        JOIN dim_producto p ON f.producto_id = p.producto_id
        JOIN dim_variable v ON f.variable_id = v.variable_id
        WHERE p.nombre_producto = ?
          AND v.tipo_mercado = ? AND v.tipo_precio = ?
          AND f.fecha BETWEEN ? AND ?
          AND f.precio IS NOT NULL
        GROUP BY f.fecha
        ORDER BY f.fecha
        """,
        [producto, tipo_mercado, tipo_precio, desde, hasta],
    ).df()
else:
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

st.title(f"{producto}")
st.markdown(
    f"<span style='color:{TEXTO};opacity:0.7;font-size:1.1rem;'>"
    f"{tipo_mercado} · {etiqueta_precio} · {region}</span>",
    unsafe_allow_html=True,
)

if df.empty:
    # El mensaje no asume cual de los dos (mayorista/minorista) es el que
    # falta -- SISAP puede no tener cualquiera de los dos segun la region
    # (ej. Lima y Callao no tienen mercado mayorista de carnes, pero si
    # tienen minorista). Bug real encontrado 2026-09-07: el mensaje antes
    # decia "no reporta precio minorista" fijo, que quedaba al reves cuando
    # justo el mayorista era el que faltaba.
    st.warning(
        "No hay datos para esta combinación exacta de filtros. SISAP no "
        "reporta todas las combinaciones de producto/región/tipo de mercado "
        "por igual (ej. Lima y Callao no tienen mercado mayorista para "
        "varios productos) -- prueba otro producto, región, tipo de mercado "
        "o tipo de precio."
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
if region == TODO_EL_PERU:
    st.caption(
        "🌎 Vista nacional: cada punto es el **promedio simple entre las "
        "regiones que reportan este producto en esa fecha** — no todas las "
        "regiones reportan el mismo mes, así que el número de regiones "
        "detrás de cada punto puede variar."
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
    st.caption("PRECIO ACTUAL ⓘ", help="Último dato dentro del rango elegido")
    st.markdown(f"### S/ {precio_actual:.2f}")
    if delta_pct is not None:
        # badge de tendencia: fondo tenue + texto en color de estado + icono
        # -- nunca solo color, para que no dependa de distinguir verde de
        # rojo (ver dataviz skill, regla de status colors)
        #
        # Bug real encontrado 2026-09-10: el "else" trataba delta_pct == 0
        # igual que una baja (flecha verde ▼), pero el formato "+.1f" le
        # pone signo positivo igual a un cero exacto -- la combinacion
        # "▼ +0.0%" se leia contradictoria (flecha de baja, signo de
        # subida). Un tercer estado neutro (sin flecha ni signo) para
        # "no cambio" evita la mezcla.
        if delta_pct > 0:
            fondo, texto, flecha, leyenda = "rgba(248,113,113,0.15)", COLOR_MALO, "▲", "vs. periodo anterior"
            texto_pct = f"{delta_pct:+.1f}%"
        elif delta_pct < 0:
            fondo, texto, flecha, leyenda = "rgba(52,211,153,0.15)", COLOR_BUENO, "▼", "vs. periodo anterior"
            texto_pct = f"{delta_pct:+.1f}%"
        else:
            fondo, texto, flecha, leyenda = "rgba(148,163,184,0.15)", "#94a3b8", "▬", "sin cambio vs. periodo anterior"
            texto_pct = "0.0%"
        st.markdown(
            f'<span style="background:{fondo};color:{texto};padding:3px 10px;'
            f'border-radius:12px;font-size:0.8rem;font-weight:600;">'
            f'{flecha} {texto_pct} {leyenda}</span>',
            unsafe_allow_html=True,
        )
with col2, st.container(border=True):
    st.caption("PROMEDIO PERIODO ⓘ", help="Promedio de todo el rango de fechas elegido")
    st.markdown(f"### S/ {df['precio'].mean():.2f}")
    st.plotly_chart(
        _sparkline(df, COLOR_SERIE), use_container_width=True,
        config={"displayModeBar": False}, key="spark_promedio",
    )
with col3, st.container(border=True):
    st.caption("MÍNIMO PERIODO ⓘ", help="Precio más bajo dentro del rango elegido (marcado en la línea)")
    st.markdown(f"### S/ {df['precio'].min():.2f}")
    st.plotly_chart(
        _sparkline(df, COLOR_BUENO, df.loc[df["precio"].idxmin(), "fecha"]),
        use_container_width=True, config={"displayModeBar": False}, key="spark_minimo",
    )
with col4, st.container(border=True):
    st.caption("MÁXIMO PERIODO ⓘ", help="Precio más alto dentro del rango elegido (marcado en la línea)")
    st.markdown(f"### S/ {df['precio'].max():.2f}")
    st.plotly_chart(
        _sparkline(df, COLOR_MALO, df.loc[df["precio"].idxmax(), "fecha"]),
        use_container_width=True, config={"displayModeBar": False}, key="spark_maximo",
    )

st.caption("▲ rojo = el precio subió · ▼ verde = el precio bajó, en ambos gráficos de abajo")

# --- Linea: evolucion historica (correcta para series de tiempo, no barras) ---
# Muchos productos/regiones tienen huecos grandes (meses sin ningun dato
# reportado por SISAP). Conectar esos huecos con una curva suave da la
# impresion falsa de una tendencia continua mes a mes. Se parte la serie en
# tramos (cada vez que el hueco entre dos datos reales es mayor a 45 dias) y
# se dibuja UN TRAZO POR TRAMO, cada uno con su propio relleno -- insertar
# un punto nulo en un solo trazo corta la LINEA pero Plotly sigue rellenando
# el area por debajo igual (bug real encontrado 2026-09-06 con Aceite/
# Amazonas: el relleno mostraba una caida continua aunque la linea sí se
# cortaba). Con trazos separados el relleno tambien se corta de verdad.
UMBRAL_HUECO_DIAS = 45
# Un punto por mes (el PROMEDIO de ese mes), no una fila por cada fila de
# "df" -- df tiene grano diario real para los productos con automatizacion
# diaria (ver mas abajo, "Ultimos dias"), y graficar eso ACA tal cual metia
# un racimo denso de ~30 puntos apretados al final de una serie que en el
# resto de su historia (2021 en adelante) es puramente mensual. Bug real
# reportado por el usuario 2026-09-10: se veia como un garabato/pico raro
# pegado al borde derecho del grafico.
#
# Se usa PROMEDIO del mes, no el ultimo dato -- probado con Tomate/Lima/
# Mayorista/Minimo (subida real y gradual de S/1.85 a S/4.63 por kg durante
# agosto 2026): tomar solo el ultimo dia comprimia toda esa suba gradual en
# un salto vertical de un mes al siguiente (Jul=50 -> Ago=120 de una), un
# artefacto visual, no la forma real de la curva. El promedio del mes se ve
# como una rampa, igual de fiel al dato real y consistente con como ya se
# interpretan los meses puramente mensuales (para esos, el promedio de 1
# solo valor es ese mismo valor -- no cambia nada ahi).
df_linea = (
    df[["fecha", "precio"]]
    .assign(anio_mes=df["fecha"].dt.to_period("M"))
    .groupby("anio_mes", as_index=False)
    .agg(fecha=("fecha", "max"), precio=("precio", "mean"))
    .reset_index(drop=True)
)
id_tramo = (df_linea["fecha"].diff().dt.days > UMBRAL_HUECO_DIAS).cumsum()

with st.container(border=True):
    fig_linea = go.Figure()
    tramos = [tramo for _, tramo in df_linea.groupby(id_tramo)]
    for i, tramo in enumerate(tramos):
        fig_linea.add_scatter(
            x=tramo["fecha"], y=tramo["precio"], mode="lines+markers",
            line={"color": COLOR_SERIE, "width": 2.5, "shape": "spline", "smoothing": 0.3},
            marker={"size": 6},
            # relleno mas visible que un wash de 10%, pero sigue siendo
            # translucido -- un bloque solido taparia la cuadricula y volveria
            # ilegible el eje Y (ver dataviz skill, "nunca un bloque saturado")
            fill="tozeroy", fillcolor=f"rgba({COLOR_SERIE_RGB},0.15)",
            showlegend=False, legendgroup="precio", name="Precio",
            # Sin dia -- df_linea ya es un punto por mes (ver arriba), asi
            # que mostrar el dia exacto sugeriria una precision distinta a
            # la que tiene el resto de la serie.
            hovertemplate="%{x|%b %Y}<br>S/ %{y:.2f}<extra></extra>",
        )
        # Puente PUNTEADO entre tramos: una recta solida daria a entender que
        # el precio vario suave durante el hueco (justo lo que este mismo
        # grafico evita al cortar la linea -- ver comentario de arriba), pero
        # dejar el hueco completamente vacio se ve roto. Un trazo delgado,
        # punteado, sin marcadores y sin relleno conecta visualmente los dos
        # tramos sin fingir que son datos reales -- se lee como "no sabemos
        # que paso aca", no como una tendencia.
        if i > 0:
            extremo_anterior = tramos[i - 1].iloc[[-1]]
            extremo_actual = tramo.iloc[[0]]
            puente = pd.concat([extremo_anterior, extremo_actual])
            fig_linea.add_scatter(
                x=puente["fecha"], y=puente["precio"], mode="lines",
                line={"color": COLOR_SERIE, "width": 1.5, "dash": "dot"},
                opacity=0.45, showlegend=False, hoverinfo="skip",
            )
    # Barra de rango debajo del grafico: mini-vista de TODA la serie con un
    # cursor que se arrastra para elegir que tramo mirar de cerca -- pedido
    # explicito para que cualquiera (sin saber de scroll-zoom ni de clics
    # con el mouse) pueda navegar el historico con solo arrastrar.
    _layout_base(fig_linea, "Evolución histórica", "Precio (S/ por kg)")
    fig_linea.update_xaxes(
        rangeslider={
            "visible": True, "thickness": 0.09,
            "bgcolor": FONDO_TARJETA, "bordercolor": GRID, "borderwidth": 1,
        },
    )
    st.plotly_chart(fig_linea, use_container_width=True)
    num_tramos = id_tramo.nunique()
    if num_tramos > 1:
        st.caption(
            f"⚠ Hay {num_tramos - 1} hueco(s) de más de {UMBRAL_HUECO_DIAS} días "
            "sin dato en este rango — la línea punteada solo conecta visualmente "
            "los tramos, no representa una tendencia real."
        )

# --- Linea: ultimos dias -- pedido explicito para tener un grafico "por
# dia, del ultimo mes" ademas del historico mensual de arriba. IMPORTANTE
# (corregido 2026-09-09/10): el dato diario/sub-mensual real NO es una
# limitacion pareja de SISAP -- el sitio soporta reporte "Dia"/"Intervalo de
# Tiempo" para practicamente cualquier producto en varias regiones (Lima,
# Arequipa, Piura confirmado), aunque no en todas (Cusco no tiene esos dos
# modos en absoluto, verificado 2026-09-10 -- si tiene "Mensual"). Nuestra
# automatizacion (cli.py cmd_hoy/cmd_historico, REGIONES_AUTOMATIZADAS)
# cubre Lima+Arequipa+Cusco+Piura con el catalogo completo -- el resto de
# las 28 regiones y (en Cusco) todos los productos solo tienen el agregado
# mensual porque todavia no los recolectamos asi, no porque SISAP no lo
# publique. Mostrar esto como un grafico vacio para el resto de productos
# seria peor que no mostrarlo, asi que se detecta si existe densidad diaria
# REAL antes de dibujar: una serie puramente mensual nunca tiene 2 fechas
# distintas en el MISMO mes calendario (cada mes aporta un solo punto, el
# agregado), asi que encontrar 2+ fechas en un mismo mes es prueba de que
# hay dato diario de verdad, no solo el agregado cayendo en la ventana.
ultimo_dato = df["fecha"].max()
ventana_diaria = df[df["fecha"] >= ultimo_dato - pd.Timedelta(days=35)].copy()
ventana_diaria["anio_mes"] = ventana_diaria["fecha"].dt.to_period("M")
hay_dato_diario = (ventana_diaria.groupby("anio_mes").size() > 1).any()

with st.container(border=True):
    if hay_dato_diario:
        fig_dias = go.Figure()
        fig_dias.add_scatter(
            x=ventana_diaria["fecha"], y=ventana_diaria["precio"], mode="lines+markers",
            line={"color": COLOR_SERIE, "width": 2.5, "shape": "spline", "smoothing": 0.3},
            marker={"size": 6},
            fill="tozeroy", fillcolor=f"rgba({COLOR_SERIE_RGB},0.15)",
            hovertemplate="%{x|%d %b %Y}<br>S/ %{y:.2f}<extra></extra>",
        )
        st.plotly_chart(
            _layout_base(fig_dias, "Últimos días", "Precio (S/ por kg)"),
            use_container_width=True,
        )
        st.caption(
            "Dato diario/sub-mensual real (no agregado mensual) — nuestra "
            "automatización diaria cubre Lima, Arequipa y Piura (Cusco no "
            "tiene este tipo de reporte en SISAP). El resto de regiones "
            "todavía no se recolecta así, no porque SISAP no lo publique."
        )
        if region == TODO_EL_PERU:
            st.caption(
                "🌎 El dato diario real hoy solo existe en Lima, Arequipa y "
                "Piura -- este tramo del promedio nacional refleja nada más "
                "las regiones que reportaron ese día específico, que puede "
                "no ser todas."
            )
    else:
        st.info(
            "Todavía no recolectamos dato diario para este producto o región "
            "— nuestra automatización cubre Lima, Arequipa y Piura con el "
            "catálogo completo (Cusco no tiene reporte diario en SISAP; el "
            "resto de regiones todavía no se recolecta así). SISAP sí "
            "publica reporte diario para la mayoría del catálogo en esas "
            "regiones, simplemente esta combinación puntual no cayó en la "
            "ventana reciente; mientras tanto, esta página muestra el "
            "agregado mensual (ver 'Evolución histórica' arriba)."
        )

# --- Barras: comparacion regional (mismo hue: es magnitud, no identidad) ---
# Bug real encontrado 2026-09-10: antes comparaba "f.fecha = MAX(fecha) de
# TODA la tabla" -- funcionaba mientras todas las regiones compartian el
# mismo calendario mensual, pero desde que Lima/Arequipa/Piura tienen
# automatizacion diaria (ver cli.py, REGIONES_AUTOMATIZADAS), el maximo
# global salto a HOY, y las otras ~24 regiones (que solo reportan mensual,
# fecha 01 de cada mes) dejaron de calzar con esa fecha exacta -- el
# grafico se quedaba con 3 barras, no porque el resto no tuviera dato
# reciente, sino porque no tenian dato EXACTAMENTE ese dia. El fix: cada
# region usa SU PROPIA fecha mas reciente (QUALIFY + MAX(...) OVER
# PARTITION BY region), no una fecha compartida.
comparacion = con.execute(
    """
    SELECT r.nombre_region, f.precio / f.equivalencia_kg AS precio, f.fecha
    FROM fact_precios f
    JOIN dim_producto p ON f.producto_id = p.producto_id
    JOIN dim_region r ON f.region_id = r.region_id
    JOIN dim_variable v ON f.variable_id = v.variable_id
    WHERE p.nombre_producto = ? AND v.tipo_mercado = ? AND v.tipo_precio = ?
      AND f.precio IS NOT NULL
    QUALIFY f.fecha = MAX(f.fecha) OVER (PARTITION BY r.nombre_region)
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
            customdata=comparacion["fecha"],
            # cada barra puede ser de una fecha distinta ahora (ver arriba)
            # -- mostrarla evita que el usuario asuma que todas son "hoy".
            hovertemplate="%{x}<br>%{customdata|%d %b %Y}<br>S/ %{y:.2f}<extra></extra>",
        )
        fig_regiones.update_traces(marker={"cornerradius": 4})
        st.plotly_chart(
            _layout_base(fig_regiones, "Comparación entre regiones (dato más reciente de cada una)", "Precio (S/ por kg)"),
            use_container_width=True,
        )
        st.caption(
            "Cada región muestra su propio dato más reciente disponible — "
            "no todas reportan en la misma fecha (pasá el mouse sobre cada "
            "barra para ver de cuándo es)."
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
