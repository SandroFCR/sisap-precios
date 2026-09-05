from datetime import date

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

BASE_URL = "http://sistemas.midagri.gob.pe/sisap/portal2/ciudades"
USER_AGENT = "sisap-precios-portfolio/0.1 (proyecto educativo, no comercial)"


def crear_cliente() -> httpx.Client:
    """Un solo cliente reutilizado mantiene la conexion TCP y las cookies de
    sesion entre requests, en vez de abrir una conexion nueva por cada pedido."""
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    reraise=True,
)
def fetch_resumen_dia(
    client: httpx.Client,
    fecha: date,
    region: str,
    productos: list[str],
    variables: list[str],
) -> str:
    """Trae el HTML crudo de un reporte de un solo dia, para una region.

    Reintenta hasta 3 veces con espera creciente (2s, 4s, 8s...) si el
    servidor falla o se cae la conexion: es un servidor gubernamental legado,
    fallar una vez no significa que el dato no exista.
    """
    fecha_str = fecha.strftime("%d/%m/%Y")
    params = [
        ("region", region),
        ("periodicidad", "dia"),
        ("fecha", fecha_str),
        ("desde", fecha_str),
        ("hasta", fecha_str),
        ("__ajax_carga_final", "consulta"),
    ]
    params += [("productos[]", producto) for producto in productos]
    params += [("variables[]", variable) for variable in variables]

    respuesta = client.get(f"{BASE_URL}/resumenes/filtrar", params=params)
    respuesta.raise_for_status()
    respuesta.encoding = "iso-8859-1"
    return respuesta.text


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    reraise=True,
)
def fetch_resumen_mensual(
    client: httpx.Client,
    anios: list[int],
    region: str,
    productos: list[str],
    variable: str,
) -> str:
    """Trae varios anios completos (12 meses c/u) en una sola peticion (modo
    'Mensual'). Para series largas es muchisimo mas eficiente que pedir mes
    a mes o dia a dia: 6 anios = 1 request en vez de ~72."""
    hoy_str = date.today().strftime("%d/%m/%Y")
    params = [
        ("region", region),
        ("periodicidad", "mensual"),
        ("fecha", hoy_str),
        ("desde", hoy_str),
        ("hasta", hoy_str),
        ("__ajax_carga_final", "consulta"),
    ]
    params += [("productos[]", producto) for producto in productos]
    params += [("meses[]", f"{mes:02d}") for mes in range(1, 13)]
    params += [("anios[]", str(anio)) for anio in anios]
    params.append(("variables[]", variable))

    respuesta = client.get(f"{BASE_URL}/resumenes/filtrar", params=params)
    respuesta.raise_for_status()
    respuesta.encoding = "iso-8859-1"
    return respuesta.text


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    reraise=True,
)
def fetch_resumen_intervalo(
    client: httpx.Client,
    desde: date,
    hasta: date,
    region: str,
    productos: list[str],
    variable: str,
) -> str:
    """Trae el HTML crudo de un rango de fechas en una sola peticion (modo
    'Intervalo de Tiempo'). Mucho mas eficiente que pedir dia por dia, pero
    solo acepta una variable (ver parser.parse_resumen_intervalo)."""
    params = [
        ("region", region),
        ("periodicidad", "intervalo"),
        ("fecha", hasta.strftime("%d/%m/%Y")),
        ("desde", desde.strftime("%d/%m/%Y")),
        ("hasta", hasta.strftime("%d/%m/%Y")),
        ("__ajax_carga_final", "consulta"),
    ]
    params += [("productos[]", producto) for producto in productos]
    params.append(("variables[]", variable))

    respuesta = client.get(f"{BASE_URL}/resumenes/filtrar", params=params)
    respuesta.raise_for_status()
    respuesta.encoding = "iso-8859-1"
    return respuesta.text
