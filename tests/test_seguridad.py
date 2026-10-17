"""Pruebas de seguridad: cada una fija una defensa concreta para que no se
pierda en un refactor. El modelo de amenaza del proyecto es acotado (datos
publicos, sin usuarios ni secretos), pero hay tres superficies reales:

1. Texto de un sitio externo, bajado por HTTP sin TLS (SISAP no soporta
   HTTPS), que termina renderizado en un dashboard publico y en un CSV.
2. La CLI escribe archivos con nombres armados a partir de sus argumentos.
3. Un runner autohospedado (la PC del autor) en un repo publico.
"""

import math
import re
import tomllib
from datetime import date
from pathlib import Path

import httpx
import pandas as pd
import pytest
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest
from tenacity import stop_after_attempt, wait_none

from sisap.cli import crear_parser
from sisap.models import PriceRecord
from sisap.parser import PaginaDeErrorSisap, parse_resumen_dia, parse_resumen_intervalo
from sisap.sanitizar import csv_sin_formulas, escapar_markdown, nombre_de_archivo
from sisap.scraper import BASE_URL, crear_cliente, fetch_resumen_dia
from sisap.warehouse import ErrorDeIntegridad, construir_en_memoria

RAIZ = Path(__file__).parent.parent
RUTA_DASHBOARD = str(RAIZ / "src" / "sisap" / "dashboard.py")

NOMBRES_HOSTILES = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "[clic aca](javascript:alert(1))",
    "![pixel](http://evil.example/track.png)",
    "$\\href{http://evil.example}{x}$",
    "Papa'; DROP TABLE fact_precios; --",
]


def _fila(**cambios) -> dict:
    fila = {
        "fecha": date(2026, 9, 1), "region": "Lima", "producto": "Papa blanca",
        "unidad": "Kilogramo", "equivalencia_kg": 1.0, "variable": "may_precio_prom",
        "precio": 1.0, "periodicidad": "mensual", "fecha_captura": date(2026, 10, 1),
    }
    return {**fila, **cambios}


# --- 1. Texto no confiable: Markdown, HTML, CSV, nombres de archivo ---------

@pytest.mark.parametrize("nombre", NOMBRES_HOSTILES)
def test_escapar_markdown_neutraliza_toda_la_sintaxis_activa(nombre):
    escapado = escapar_markdown(nombre)

    # ningun caracter con significado en Markdown queda sin su barra
    sin_escapar = re.findall(r"(?<!\\)[\[\]()<>!$`*_]", escapado.replace("\\\\", ""))
    assert sin_escapar == []
    # y quitando las barras se recupera el texto original intacto
    assert re.sub(r"\\(.)", r"\1", escapado) == nombre


def test_csv_sin_formulas_neutraliza_celdas_que_empiezan_como_formula():
    df = pd.DataFrame({
        "unidad": ["=HYPERLINK(\"http://evil.example\")", "+1+1", "-2+3", "@SUM(A1)", "\tx", "Kilogramo"],
        "precio": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
    })

    leido = pd.read_csv(pd.io.common.BytesIO(csv_sin_formulas(df)), keep_default_na=False)

    assert all(not v.startswith(("=", "+", "-", "@", "\t")) for v in leido["unidad"])
    assert leido["unidad"].iloc[-1] == "Kilogramo"  # el texto normal no se toca
    assert leido["precio"].tolist() == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]  # ni los numeros


@pytest.mark.parametrize("texto", ["Pescado fresco/congelado", "../../etc/passwd", "a\\b:c*?\"<>|", "..", ""])
def test_nombre_de_archivo_no_permite_separadores_ni_subir_de_carpeta(texto):
    nombre = nombre_de_archivo(texto)

    assert nombre
    assert "/" not in nombre and "\\" not in nombre and ".." not in nombre
    assert re.fullmatch(r"[\w.-]+", nombre)


def test_dashboard_no_renderiza_html_ni_markdown_de_los_datos(tmp_path, monkeypatch):
    """Recorrido completo: un parquet con nombres de producto y region
    hostiles, el dashboard real (AppTest) eligiendo esa region y ese
    producto. Nada de lo que vino de los datos llega crudo a un elemento que
    interprete Markdown o HTML."""
    producto_hostil = "<img src=x onerror=alert(1)>"
    region_hostil = "<script>alert(1)</script>"
    filas = [_fila(fecha=date(2026, mes, 1), precio=1.0 + mes / 10) for mes in (7, 8, 9)]
    filas += [
        _fila(fecha=date(2026, mes, 1), region=region_hostil, producto=producto_hostil,
              unidad="[x](javascript:alert(1))", precio=2.0 + mes / 10)
        for mes in (7, 8, 9)
    ]
    ruta = tmp_path / "hostil.parquet"
    pd.DataFrame(filas).to_parquet(ruta, index=False)
    monkeypatch.setenv("SISAP_PARQUET", str(ruta))

    at = AppTest.from_file(RUTA_DASHBOARD)
    at.run(timeout=30)
    at.selectbox(key="region_seleccionada").set_value(region_hostil).run(timeout=30)
    at.selectbox(key="producto_seleccionado").set_value(producto_hostil).run(timeout=30)
    at.button(key="FormSubmitter:form_filtros-Aplicar filtros").click().run(timeout=30)

    assert not at.exception
    assert at.title[0].value == escapar_markdown(producto_hostil)
    textos = [m.value for m in at.markdown] + [c.value for c in at.caption]
    for texto in textos:
        assert "<script>" not in texto and "<img" not in texto
        assert "](javascript:" not in texto
    assert any("&lt;script&gt;" in m.value for m in at.markdown)


# --- 2. Validacion en el borde ------------------------------------------------

@pytest.mark.parametrize("precio", [0.0, -1.5, math.inf, -math.inf, math.nan])
def test_price_record_rechaza_precios_imposibles(precio):
    with pytest.raises(ValidationError):
        PriceRecord(**_fila(precio=precio))


@pytest.mark.parametrize("campo", ["region", "producto", "variable"])
def test_price_record_rechaza_claves_vacias(campo):
    with pytest.raises(ValidationError):
        PriceRecord(**_fila(**{campo: ""}))


def test_price_record_rechaza_periodicidad_desconocida():
    with pytest.raises(ValidationError):
        PriceRecord(**_fila(periodicidad="semanal"))


def test_pagina_de_error_es_una_excepcion_propia_no_un_valor_invalido():
    """Los reintentos de cli.py solo reaccionan a PaginaDeErrorSisap: un
    precio invalido (ValidationError, que tambien es ValueError) no debe
    confundirse con "la consulta era muy grande"."""
    with pytest.raises(PaginaDeErrorSisap):
        parse_resumen_dia("<p class=mensajeDeError>x</p>", fecha=date(2026, 9, 1), region="Lima", variables=["may_precio_prom"])
    assert not issubclass(ValidationError, PaginaDeErrorSisap)


def test_celda_sin_rel_con_texto_falla_aunque_python_corra_con_menos_O():
    """Era un assert: con "python -O" desaparecia y el dato se perdia en
    silencio. Ahora es un raise explicito."""
    html = (
        "<table><tr class=contenido><td>01/09/2026</td><td>Kg</td><td>1</td>"
        "<td class=numero>9.99</td></tr></table>"
    )
    with pytest.raises(ValueError, match="sin rel pero con texto"):
        parse_resumen_intervalo(html, region="Lima", variable="may_precio_prom")


# --- 3. SQL ------------------------------------------------------------------

@pytest.mark.parametrize("nombre", NOMBRES_HOSTILES)
def test_nombres_hostiles_no_rompen_ni_inyectan_sql(tmp_path, nombre):
    ruta = tmp_path / "p.parquet"
    pd.DataFrame([_fila(producto=nombre), _fila(producto="Yuca")]).to_parquet(ruta, index=False)

    con = construir_en_memoria(ruta)

    # la tabla sigue existiendo y el nombre se encuentra tal cual, por
    # parametro (asi consulta el dashboard)
    assert con.execute("SELECT COUNT(*) FROM fact_precios").fetchone()[0] == 2
    assert con.execute(
        "SELECT COUNT(*) FROM dim_producto WHERE nombre_producto = ?", [nombre]
    ).fetchone()[0] == 1


def test_ningun_sql_del_paquete_se_arma_interpolando_strings():
    """Guardia estatica: ningun execute()/sql() del codigo de produccion
    recibe un f-string (las reglas S608 de ruff cubren lo mismo en CI)."""
    for archivo in (RAIZ / "src" / "sisap").glob("*.py"):
        codigo = archivo.read_text(encoding="utf-8")
        assert not re.search(r"\.(execute|sql)\(\s*f[\"']", codigo), archivo.name


def test_claves_duplicadas_en_el_parquet_fallan_fuerte(tmp_path):
    ruta = tmp_path / "p.parquet"
    pd.DataFrame([_fila(precio=1.0), _fila(precio=2.0)]).to_parquet(ruta, index=False)

    with pytest.raises(ErrorDeIntegridad, match="repetidas"):
        construir_en_memoria(ruta)


# --- 4. CLI: argumentos que terminan en rutas y URLs -----------------------

@pytest.mark.parametrize("argumentos", [
    ["consultar", "--producto", "../x", "--region", "150000", "--desde-anio", "2025", "--hasta-anio", "2025"],
    ["consultar", "--producto", "0105", "--region", "../../etc", "--desde-anio", "2025", "--hasta-anio", "2025"],
    ["consultar", "--producto", "0105", "--region", "150000&x=1", "--desde-anio", "2025", "--hasta-anio", "2025"],
    ["consultar", "--producto", "0105", "--region", "150000", "--desde-anio", "2025", "--hasta-anio", "2025",
     "--variable", "may_precio_prom/../x"],
    ["historico", "--ultimos-dias", "0"],
    ["refrescar-mensual", "--meses", "-1"],
])
def test_cli_rechaza_argumentos_fuera_de_formato(argumentos):
    with pytest.raises(SystemExit):
        crear_parser().parse_args(argumentos)


# --- 5. Scraper ----------------------------------------------------------------

def test_scraper_no_sigue_redirecciones_a_otro_host():
    """El sitio va por HTTP: una respuesta manipulada no debe poder mandar al
    scraper a otro servidor."""
    pedidos = []

    def responder(request: httpx.Request) -> httpx.Response:
        pedidos.append(request.url.host)
        return httpx.Response(302, headers={"Location": "http://evil.example/"})

    client = httpx.Client(transport=httpx.MockTransport(responder))
    sin_esperas = fetch_resumen_dia.retry_with(wait=wait_none(), stop=stop_after_attempt(1))

    with pytest.raises(httpx.HTTPStatusError):
        sin_esperas(client, fecha=date(2026, 9, 1), region="150000", productos=["0104"], variables=["may_precio_prom"])
    assert pedidos == [httpx.URL(BASE_URL).host]


def test_cliente_tiene_timeout_e_identificacion():
    with crear_cliente() as client:
        assert client.timeout.read is not None
        assert client.follow_redirects is False
        assert "sisap-precios" in client.headers["User-Agent"]


# --- 6. Configuracion del despliegue y de CI ---------------------------------

def test_streamlit_no_muestra_tracebacks_a_los_visitantes():
    config = tomllib.loads((RAIZ / ".streamlit" / "config.toml").read_text(encoding="utf-8"))

    assert config["client"]["showErrorDetails"] in ("type", "none")


def _workflows() -> dict[str, str]:
    return {
        ruta.name: ruta.read_text(encoding="utf-8")
        for ruta in (RAIZ / ".github" / "workflows").glob("*.yml")
    }


def test_runner_autohospedado_nunca_corre_codigo_de_pull_requests():
    """Repo publico + runner en la PC del autor: un workflow que use el
    runner autohospedado y se dispare con pull_request ejecutaria codigo de
    cualquier fork en esa PC."""
    usan_el_runner = 0
    for nombre, contenido in _workflows().items():
        # solo las lineas runs-on: (un comentario que nombre el runner no cuenta)
        if re.search(r"^\s*runs-on:.*self-hosted", contenido, re.MULTILINE):
            usan_el_runner += 1
            assert not re.search(r"^\s*pull_request(_target)?\s*:", contenido, re.MULTILINE), nombre
    # actualizar_datos.yml SI usa el runner: si esto da 0, el test dejo de
    # mirar lo que dice mirar
    assert usan_el_runner == 1


def test_actions_de_terceros_fijadas_por_commit():
    """Un tag (@v4) se puede mover; un SHA de 40 caracteres no."""
    for nombre, contenido in _workflows().items():
        for accion in re.findall(r"uses:\s*(\S+)", contenido):
            assert re.search(r"@[0-9a-f]{40}$", accion), f"{nombre}: {accion}"


def test_cada_workflow_declara_permisos_minimos():
    for nombre, contenido in _workflows().items():
        assert re.search(r"^permissions:", contenido, re.MULTILINE), nombre
        assert "write-all" not in contenido, nombre


def test_dependencias_de_produccion_fijadas():
    """Streamlit Cloud instala requirements.txt: sin versiones fijas, una
    release nueva de cualquier dependencia puede romper (o comprometer) el
    dashboard publicado sin que cambie una linea del repo."""
    for linea in (RAIZ / "requirements.txt").read_text(encoding="utf-8").splitlines():
        linea = linea.split("#")[0].strip()
        if linea:
            assert re.fullmatch(r"[A-Za-z0-9_.\-\[\]]+==[\w.]+", linea), linea


def test_el_warehouse_no_se_versiona():
    """El .duckdb (19 MB, distinto en cada corrida) se arma en memoria; si
    volviera a commitearse, el repo creceria ~5 MB por dia."""
    gitignore = (RAIZ / ".gitignore").read_text(encoding="utf-8")
    assert "data/processed/sisap.duckdb" in gitignore
