from pathlib import Path

from streamlit.testing.v1 import AppTest

RUTA_DASHBOARD = str(Path(__file__).parent.parent / "src" / "sisap" / "dashboard.py")


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
