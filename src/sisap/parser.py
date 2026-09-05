from datetime import date

from bs4 import BeautifulSoup

from sisap.models import PriceRecord


def parse_resumen_dia(
    html: str, fecha: date, region: str, variables: list[str]
) -> list[PriceRecord]:
    """Parsea el HTML devuelto por /resumenes/filtrar para una consulta de un
    solo dia y una sola region.

    Cada fila <tr class=contenido> tiene: producto, unidad, equivalencia, y
    luego una celda <td class=numero> por cada variable pedida, en el mismo
    orden en que se pidieron. Ojo: si las variables pertenecen a grupos
    distintos (ej. mayorista y minorista), el HTML repite un par extra de
    columnas "Unidad de medida"/"Equiv." (vacias si no hay dato) entre medio
    de los valores. Por eso no se puede usar la posicion fija de la celda:
    hay que filtrar especificamente por `class=numero`, que es la unica marca
    confiable de "esto es un valor, no una columna de unidad repetida".
    """
    soup = BeautifulSoup(html, "html.parser")
    registros: list[PriceRecord] = []

    for fila in soup.select("tr.contenido"):
        celdas = fila.find_all("td")
        producto = celdas[0].get_text(strip=True)
        unidad = celdas[1].get_text(strip=True)
        equivalencia_texto = celdas[2].get_text(strip=True)
        celdas_precio = fila.select("td.numero")

        try:
            equivalencia_kg = float(equivalencia_texto)
        except ValueError:
            equivalencia_kg = float("nan")

        for variable, celda in zip(variables, celdas_precio):
            texto_precio = celda.get_text(strip=True)
            precio = _parsear_precio(texto_precio)
            registros.append(
                PriceRecord(
                    fecha=fecha,
                    region=region,
                    producto=producto,
                    unidad=unidad,
                    equivalencia_kg=equivalencia_kg,
                    variable=variable,
                    precio=precio,
                )
            )

    return registros


def parse_resumen_intervalo(
    html: str, region: str, variable: str
) -> list[PriceRecord]:
    """Parsea el HTML del modo 'Intervalo de Tiempo': una sola peticion trae
    varios dias (una fila por fecha), con los productos pivoteados en columnas.

    Aca la forma de la tabla es la inversa de parse_resumen_dia (filas=fecha,
    columnas=producto), y ademas el numero de productos-columna lo decide el
    servidor (expande el genero pedido en sus variedades), asi que no podemos
    alinear celdas por posicion como en modo dia.

    En su lugar usamos dos trucos mas robustos:
    - El producto se lee directo del atributo `rel` de cada td.numero (aca si
      es inequivoco, a diferencia del modo dia).
    - La unidad y equivalencia de ESA celda puntual son sus 2 <td> hermanos
      inmediatamente anteriores, sin importar cuantos otros productos haya
      antes en la fila.

    Nota: por eso mismo esta funcion solo soporta una variable por llamada
    (ej. solo mayorista-promedio); si el rel no distingue "precio minimo" de
    "precio promedio" dentro del mismo grupo, no hay forma segura de separarlos
    sin position-matching, que es justo lo que causo el bug anterior.

    Cuando NINGUN producto tiene dato en un dia, el servidor emite celdas
    <td class=numero> vacias, sin atributo rel. Sin rel no hay forma de saber
    a que producto pertenecen, asi que se descartan (no se puede construir un
    PriceRecord sin producto). Si alguna vez aparece una celda sin rel pero
    con texto, es una violacion de este supuesto: mejor fallar fuerte que
    perder datos en silencio.
    """
    soup = BeautifulSoup(html, "html.parser")
    registros: list[PriceRecord] = []

    for fila in soup.select("tr.contenido"):
        fecha_texto = fila.find("td").get_text(strip=True)
        fecha = date(*reversed([int(p) for p in fecha_texto.split("/")]))

        for celda_precio in fila.select("td.numero"):
            if not celda_precio.get("rel"):
                texto = celda_precio.get_text(strip=True)
                assert not texto, (
                    f"Celda td.numero sin rel pero con texto {texto!r}: "
                    "supuesto de parseo invalido, revisar HTML fuente."
                )
                continue

            _, _, columna = celda_precio["rel"].partition("~")
            producto = columna.split(",")[1].strip() if "," in columna else ""

            equivalencia_celda, unidad_celda = celda_precio.find_previous_siblings(
                "td", limit=2
            )
            unidad = unidad_celda.get_text(strip=True)
            try:
                equivalencia_kg = float(equivalencia_celda.get_text(strip=True))
            except ValueError:
                equivalencia_kg = float("nan")

            precio = _parsear_precio(celda_precio.get_text(strip=True))
            registros.append(
                PriceRecord(
                    fecha=fecha,
                    region=region,
                    producto=producto,
                    unidad=unidad,
                    equivalencia_kg=equivalencia_kg,
                    variable=variable,
                    precio=precio,
                )
            )

    return registros


MESES_ABREVIADOS = {
    "Ene": 1, "Feb": 2, "Mar": 3, "Abr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Ago": 8, "Set": 9, "Oct": 10, "Nov": 11, "Dic": 12,
}


def parse_resumen_mensual(
    html: str, region: str, variable: str
) -> list[PriceRecord]:
    """Parsea el modo 'Mensual': un solo request puede traer varios anios
    completos (12 meses c/u) para un producto. Es el modo mas compacto para
    series largas (ej. 6 anios = 1 sola peticion en vez de ~72 en modo dia).

    Cada producto ocupa 3 filas <tr class=contenido>: una de encabezado (codigo
    + nombre), una de unidad/equivalencia, y luego una fila POR ANIO pedido con
    las 12 columnas de mes + una columna 'Anual' (promedio del anio, que se
    descarta aca porque no es un punto en el tiempo real).

    A diferencia de los otros modos, aca la celda del anio (ej. "2025") tambien
    tiene class=numero pero SIN atributo rel -- por eso filtramos por
    `td.numero[rel]`, que deja afuera tanto esa celda como la de equivalencia
    (que tampoco tiene rel en este modo).
    """
    soup = BeautifulSoup(html, "html.parser")
    registros: list[PriceRecord] = []
    unidad_actual = ""
    equivalencia_actual = float("nan")

    for fila in soup.select("tr.contenido"):
        celdas_precio = fila.select("td.numero[rel]")
        if not celdas_precio:
            celdas = fila.find_all("td")
            texto_unidad = celdas[1].get_text(strip=True) if len(celdas) > 1 else ""
            if texto_unidad:
                unidad_actual = texto_unidad
                try:
                    equivalencia_actual = float(celdas[2].get_text(strip=True))
                except (ValueError, IndexError):
                    equivalencia_actual = float("nan")
            continue

        for celda in celdas_precio:
            mes_texto, _, columna = celda["rel"].partition("~")
            mes_texto = mes_texto.strip()
            if mes_texto == "Anual":
                continue

            partes = [p.strip() for p in columna.split(",")]
            producto = partes[0]
            anio = int(partes[2])
            fecha = date(anio, MESES_ABREVIADOS[mes_texto], 1)

            registros.append(
                PriceRecord(
                    fecha=fecha,
                    region=region,
                    producto=producto,
                    unidad=unidad_actual,
                    equivalencia_kg=equivalencia_actual,
                    variable=variable,
                    precio=_parsear_precio(celda.get_text(strip=True)),
                )
            )

    return registros


def _parsear_precio(texto: str) -> float | None:
    if not texto or texto in {"-", "N/D", "s/d", "S/D"}:
        return None
    return float(texto.replace(",", ""))
