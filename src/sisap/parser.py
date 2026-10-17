from datetime import date

from bs4 import BeautifulSoup

from sisap.models import PriceRecord


class PaginaDeErrorSisap(ValueError):
    """SISAP devolvio su pagina de error ("Se ha excedido el tiempo
    limite...", o la region no tiene ese tipo de reporte) en vez de la
    tabla de precios. Es una excepcion propia -- no un ValueError suelto --
    para que los reintentos de cli.py reaccionen SOLO a esto: un precio
    invalido (ValidationError, que tambien hereda de ValueError) no debe
    confundirse con "la consulta era muy grande" y disparar particiones de
    productos que nunca lo van a arreglar."""


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
    # Mismo chequeo que parse_resumen_mensual/parse_resumen_intervalo: pedir
    # de mas (ahora cmd_hoy pide el catalogo completo por region, ver
    # cli.py) puede hacer que SISAP devuelva la pagina de error en vez de la
    # tabla -- sin este chequeo, quedaria como "0 registros" sin avisar.
    if "mensajeDeError" in html:
        raise PaginaDeErrorSisap(
            f"SISAP devolvio una pagina de error en vez de datos (region={region}, "
            f"fecha={fecha}) -- probablemente la consulta es muy grande. Pedi "
            "menos productos a la vez."
        )

    soup = BeautifulSoup(html, "html.parser")
    registros: list[PriceRecord] = []

    for fila in soup.select("tr.contenido"):
        celdas = fila.find_all("td")
        producto = celdas[0].get_text(strip=True)

        # Cada grupo de variables (mayorista, minorista) trae SU PROPIO par
        # "Unidad de medida"/"Equiv." justo antes de sus celdas de precio.
        # Bug real encontrado 2026-10-17: se usaba el par del primer grupo
        # para todas las variables. Aji rocoto/Lima se vende al por mayor en
        # "Cajon" (18 kg) y al por menor en "Kilogramo": el precio minorista
        # (S/ 13 por kg) quedaba dividido entre 18 -- S/ 0.72 por kg. Y un
        # producto sin mayorista (Carne de pollo eviscerado) dejaba sus
        # precios minoristas con unidad vacia y equivalencia NaN, inutiles.
        # Ahora cada celda de precio toma el ultimo par unidad/equivalencia
        # que aparecio antes que ella en la fila.
        unidad, equivalencia_kg = "", float("nan")
        no_numericas: list[str] = []
        celdas_precio = []
        for celda in celdas[1:]:
            if "numero" in (celda.get("class") or []):
                if len(no_numericas) >= 2:
                    unidad = no_numericas[-2]
                    try:
                        equivalencia_kg = float(no_numericas[-1])
                    except ValueError:
                        equivalencia_kg = float("nan")
                no_numericas = []
                celdas_precio.append((celda, unidad, equivalencia_kg))
            else:
                no_numericas.append(celda.get_text(strip=True))

        for variable, (celda, unidad, equivalencia_kg) in zip(variables, celdas_precio):
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
                    periodicidad="dia",
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
    # Bug real encontrado 2026-09-10: el mismo problema ya documentado en
    # parse_resumen_mensual (pedir de mas hace que SISAP devuelva una pagina
    # de error en vez de la tabla) tambien pasa en modo intervalo -- probado
    # pidiendo el catalogo completo (51 productos) para Cusco, que devolvio
    # la pagina de error en las 12 combinaciones de mes/variable. Como esta
    # funcion no tenia el chequeo, esas 12 llamadas se guardaron como "0
    # registros" sin ningun aviso -- Cusco se hubiera quedado sin dato de
    # intervalo por completo, en silencio. Mismo fix: fallar fuerte.
    if "mensajeDeError" in html:
        raise PaginaDeErrorSisap(
            f"SISAP devolvio una pagina de error en vez de datos (region={region}, "
            f"variable={variable}) -- probablemente la consulta es muy grande. "
            "Pedi menos productos o un rango mas chico a la vez."
        )

    soup = BeautifulSoup(html, "html.parser")
    registros: list[PriceRecord] = []

    for fila in soup.select("tr.contenido"):
        fecha_texto = fila.find("td").get_text(strip=True)
        fecha = date(*reversed([int(p) for p in fecha_texto.split("/")]))

        for celda_precio in fila.select("td.numero"):
            if not celda_precio.get("rel"):
                texto = celda_precio.get_text(strip=True)
                # raise explicito, no assert: "python -O" borra los assert,
                # y esta es justo la red que evita perder datos en silencio.
                if texto:
                    raise ValueError(
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
                    periodicidad="dia",
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
    # Bug real encontrado 2026-09-07: cuando la consulta es muy grande (ej.
    # los 51 productos x 6 anios para Lima/Minorista, que tiene mucho mas
    # historial que otras regiones), SISAP no devuelve la tabla de precios
    # sino una pagina de error ("Se ha excedido el tiempo limite...").  Como
    # esa pagina no tiene ninguna fila <tr class=contenido>, el resto de esta
    # funcion simplemente no encontraba nada que parsear y devolvia una
    # lista vacia SIN avisar -- guardar_registros() lo guardaba como "0
    # registros nuevos" sin error, y la falla quedaba invisible. Lima entero
    # se quedo con 14 productos minoristas en vez de ~85 durante meses por
    # esto. Fallar fuerte y claro aca evita que se repita en silencio.
    if "mensajeDeError" in html:
        raise PaginaDeErrorSisap(
            f"SISAP devolvio una pagina de error en vez de datos (region={region}, "
            f"variable={variable}) -- probablemente la consulta es muy grande. "
            "Pedi menos anios o menos productos a la vez."
        )

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
                    periodicidad="mensual",
                )
            )

    return registros


def _parsear_precio(texto: str) -> float | None:
    if not texto or texto in {"-", "N/D", "s/d", "S/D"}:
        return None
    return float(texto.replace(",", ""))
