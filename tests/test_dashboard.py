from pathlib import Path

from streamlit.elements.plotly_chart import PlotlyState
from streamlit.testing.v1 import AppTest

RUTA_DASHBOARD = str(Path(__file__).parent.parent / "src" / "sisap" / "dashboard.py")


def _simular_clic_mapa(at: AppTest, ubicacion: str) -> AppTest:
    """Escribe directamente el estado que Plotly manda al hacer clic con
    on_select="rerun" -- AppTest no sabe simular gestos de mouse sobre un
    mapa, asi que esto imita el mensaje que el frontend le mandaria al
    backend."""
    at.session_state["mapa_regiones"] = PlotlyState(
        selection={"points": [{"location": ubicacion}], "point_indices": [0], "box": [], "lasso": []}
    )
    return at


def test_dashboard_carga_sin_excepciones():
    """Regresion: un curl a /_stcore/health NUNCA agarra errores como este,
    porque solo confirma que el servidor esta vivo -- el script de Streamlit
    recien corre de verdad cuando una sesion (navegador o AppTest) se conecta.
    Este bug puntual (StreamlitWidgetAlreadyInstantiatedError) paso porque el
    form y el session_state usaban la misma clave "filtros"."""
    at = AppTest.from_file(RUTA_DASHBOARD)
    at.run(timeout=30)

    assert not at.exception


def test_dashboard_aplicar_filtros_no_rompe():
    at = AppTest.from_file(RUTA_DASHBOARD)
    at.run(timeout=30)

    at.button[0].click().run(timeout=30)

    assert not at.exception


def test_cambiar_region_conserva_producto_si_existe_en_la_nueva():
    """Bug real encontrado en produccion (2026-09-06): el selectbox de
    Producto no tenia "key" propia, asi que Streamlit lo identificaba (entre
    otras cosas) por su lista de opciones -- al cambiar de Región, la lista
    cambiaba y Streamlit trataba el selectbox como un widget nuevo,
    reseteandolo al primer producto de la region nueva SIN que el usuario lo
    eligiera. El titulo/graficos (que solo se actualizan al apretar "Aplicar
    filtros") seguian mostrando la combinacion anterior mientras el sidebar
    ya mostraba otra region y otro producto -- se leia como si la pagina
    tuviera 3 selecciones distintas a la vez."""
    at = AppTest.from_file(RUTA_DASHBOARD)
    at.run(timeout=30)

    # cada widget se vuelve a pedir a "at" despues de cada .run(): la
    # referencia vieja queda atada al arbol anterior (con la lista de
    # opciones de ANTES), pedirla de nuevo evita comparar contra opciones
    # obsoletas.
    at.selectbox(key="region_seleccionada").set_value("Lima").run(timeout=30)
    at.selectbox(key="producto_seleccionado").set_value("Uva candy").run(timeout=30)
    at.selectbox(key="region_seleccionada").set_value("Puno").run(timeout=30)  # Puno no tiene "Uva candy"

    assert not at.exception
    assert at.selectbox(key="producto_seleccionado").value != "Uva candy"


def test_cambiar_region_a_una_con_el_mismo_producto_lo_conserva():
    """Complemento del test de arriba: si el producto elegido SI existe en
    la nueva region, no debe perderse -- el reseteo solo debe pasar cuando
    hace falta, no siempre que cambia la region."""
    at = AppTest.from_file(RUTA_DASHBOARD)
    at.run(timeout=30)

    at.selectbox(key="region_seleccionada").set_value("Amazonas").run(timeout=30)
    at.selectbox(key="producto_seleccionado").set_value("Papa huayro").run(timeout=30)
    at.selectbox(key="region_seleccionada").set_value("Puno").run(timeout=30)

    assert not at.exception
    assert at.selectbox(key="producto_seleccionado").value == "Papa huayro"


def test_clic_en_mapa_actualiza_region_en_el_mismo_rerun():
    """Bug real encontrado en produccion (2026-09-06): antes, la figura del
    mapa se armaba usando la region seleccionada ANTES de leer el clic
    nuevo, asi que corregir la seleccion exigia un st.rerun() extra --
    ademas del que "on_select=rerun" ya dispara solo. Ese doble rerun
    pintaba el mapa con el color VIEJO un instante antes de que el segundo
    lo reemplazara: un parpadeo real en cada clic. Leer el clic ANTES de
    armar la figura corrige la seleccion en el mismo (unico) rerun."""
    at = AppTest.from_file(RUTA_DASHBOARD)
    at.run(timeout=30)

    _simular_clic_mapa(at, "Puno").run(timeout=30)

    assert not at.exception
    assert at.selectbox(key="region_seleccionada").value == "Puno"


def test_clic_viejo_en_mapa_no_pisa_un_cambio_posterior_del_dropdown():
    """Complemento del test de arriba: la seleccion de Plotly persiste del
    lado del cliente entre reruns futuros no relacionados con el mapa. Si
    despues de clickear el mapa el usuario cambia de region por el
    dropdown, ese clic viejo (que Streamlit sigue devolviendo) no debe
    pisar la eleccion nueva."""
    at = AppTest.from_file(RUTA_DASHBOARD)
    at.run(timeout=30)

    _simular_clic_mapa(at, "Puno").run(timeout=30)
    at.selectbox(key="region_seleccionada").set_value("Cusco").run(timeout=30)

    assert not at.exception
    assert at.selectbox(key="region_seleccionada").value == "Cusco"


def test_boton_de_producto_destacado_no_pisa_una_region_no_aplicada():
    """Bug real encontrado en produccion (2026-09-08): el boton de un
    producto destacado (con foto) llamaba a st.rerun() antes de que el
    script llegara a instanciar el selectbox de Región, mas abajo. Si el
    usuario habia cambiado de región por el dropdown SIN apretar "Aplicar
    filtros" y despues clickeaba un producto destacado, el script se
    abortaba (por el rerun) antes de que Streamlit confirmara el valor
    pendiente del selectbox -- la siguiente vez que se instanciaba, volvia
    a su default ("Lima"). El producto se aplicaba bien, pero la región se
    pisaba de vuelta a Lima sin que el usuario lo pidiera. El boton de un
    widget YA dispara un rerun completo por si solo -- no hacia falta un
    rerun propio, alcanzaba con dejar que el script siguiera su curso."""
    at = AppTest.from_file(RUTA_DASHBOARD)
    at.run(timeout=30)

    # cambiar de region por el dropdown SIN aplicar filtros
    at.selectbox(key="region_seleccionada").set_value("Arequipa").run(timeout=30)
    assert at.session_state["filtros_aplicados"]["region"] == "Lima"  # todavia no aplicado

    at.sidebar.button(key="btn_destacado_papa").click().run(timeout=30)

    assert not at.exception
    assert at.selectbox(key="region_seleccionada").value == "Arequipa"
    assert at.session_state["filtros_aplicados"]["region"] == "Arequipa"


def test_todo_el_peru_agrega_entre_regiones_sin_romper():
    """Feature pedida explicitamente (2026-09-09): una opcion "Todo el Peru"
    en el selector de Region que promedia el precio entre todas las regiones
    que reportan cada fecha, en vez de mostrar una sola. Cubre el camino
    completo: la opcion existe en el dropdown, se puede aplicar, y no rompe
    ni la lista de productos (que ya no filtra por region) ni los graficos
    (que dependen de que el df resultante tenga las mismas columnas que el
    modo de una region)."""
    at = AppTest.from_file(RUTA_DASHBOARD)
    at.run(timeout=30)

    assert "Todo el Perú" in at.selectbox(key="region_seleccionada").options

    at.selectbox(key="region_seleccionada").set_value("Todo el Perú").run(timeout=30)
    at.button(key="FormSubmitter:form_filtros-Aplicar filtros").click().run(timeout=30)

    assert not at.exception
    assert at.session_state["filtros_aplicados"]["region"] == "Todo el Perú"
