import argparse
import re
import sys
import time
from datetime import date, timedelta

import httpx

from sisap.models import PriceRecord
from sisap.parser import (
    PaginaDeErrorSisap,
    parse_resumen_dia,
    parse_resumen_intervalo,
    parse_resumen_mensual,
)
from sisap.scraper import (
    crear_cliente,
    fetch_resumen_dia,
    fetch_resumen_intervalo,
    fetch_resumen_mensual,
)
from sisap.storage import (
    PROCESSED_DIR,
    guardar_html_crudo,
    guardar_html_crudo_intervalo,
    guardar_html_crudo_mensual,
    guardar_registros,
    leer_precios,
)
from sisap.warehouse import construir_modelo_dimensional

# Catalogo completo de SISAP (51 generos), extraido del checkbox de
# productos de la pagina. El modo mensual acepta VARIOS productos[] en una
# sola peticion (probado: los 51 juntos = 1 request de ~8s, no 51 requests),
# asi que poblar_historico los pide todos de una vez por region+variable.
CATALOGO_PRODUCTOS = {
    "1001": "Aceite", "1018": "Aceituna botija", "0202": "Aji fresco",
    "0203": "Aji seco", "0204": "Ajo", "0401": "Arroz",
    "0301": "Arveja grano verde", "1005": "Azucar comercial", "0101": "Camote",
    "1101": "Carne fresca", "0212": "Cebolla", "0641": "Cerezas",
    "0603": "Chirimoya", "0403": "Choclo", "1010": "Fideos", "0607": "Fresa",
    "0501": "Frijol grano seco", "0302": "Frijol grano verde", "1305": "Gallo",
    "0502": "Garbanzo grano seco", "0608": "Granadilla",
    "0303": "Haba grano verde", "1011": "Harina", "1105": "Huevos",
    "1104": "Leche", "0504": "Lenteja grano seco", "0611": "Limon",
    "0614": "Mandarina", "0615": "Mango", "0617": "Manzana",
    "0620": "Melocoton", "0619": "Melon", "0622": "Naranja", "0102": "Olluco",
    "0506": "Pallar grano seco", "0626": "Palta", "0104": "Papa",
    "0627": "Papaya", "0631": "Pera", "1201": "Pescado fresco/congelado",
    "0628": "Piña", "0629": "Platano", "0405": "Quinua", "0633": "Sandia",
    "0306": "Tarhui", "0228": "Tomate", "0637": "Uva", "0229": "Vainita",
    "0105": "Yuca", "0230": "Zanahoria", "0231": "Zapallo",
}
# Catalogo completo de regiones de SISAP (28), incluye algunas provincias
# reportadas aparte de su region (Andahuaylas, Chota, Jaen, Callao).
CATALOGO_REGIONES = {
    "010000": "Amazonas", "020000": "Ancash", "030000": "Apurimac",
    "030201": "Andahuaylas", "040000": "Arequipa", "050000": "Ayacucho",
    "060000": "Cajamarca", "060401": "Chota", "060801": "Jaen",
    "070000": "Callao", "080000": "Cusco", "090000": "Huancavelica",
    "100000": "Huanuco", "110000": "Ica", "120000": "Junin",
    "130000": "La libertad", "140000": "Lambayeque", "150000": "Lima",
    "160000": "Loreto", "170000": "Madre de dios", "180000": "Moquegua",
    "190000": "Pasco", "200000": "Piura", "210000": "Puno",
    "220000": "San martin", "230000": "Tacna", "240000": "Tumbes",
    "250000": "Ucayali",
}
VARIABLES_MVP = [
    "may_precio_min", "may_precio_prom", "may_precio_max",
    "min_precio_min", "min_precio_prom", "min_precio_max",
]
# Regiones con automatizacion diaria/intervalo (dato SUB-mensual real, no
# solo el agregado mensual de poblar-historico). Bug real encontrado
# 2026-09-09/10: primero Huevos, despues Carne de pollo aparecieron con
# dato diario real en el sitio de SISAP que nunca capturamos porque
# cmd_hoy/cmd_historico solo pedian un puñado de productos fijos
# (PRODUCTOS_MVP, ahora eliminado) -- y solo Lima. Como fetch_resumen_dia y
# fetch_resumen_intervalo ya mandan TODOS los productos[] pedidos en una
# sola peticion (ver scraper.py), pedir el CATALOGO_PRODUCTOS completo no
# cuesta mas requests que pedir 6 -- el costo real crece con la cantidad de
# REGIONES (una peticion por region), no de productos. Se eligieron estas
# (la de mas trafico del dashboard + regiones grandes de prueba) en vez de
# las 28 para no disparar el tiempo de la automatizacion diaria; se puede
# sumar mas regiones despues sin tocar la logica, solo esta lista. Cusco
# salio de la lista el 2026-10-17: no tiene reporte "Dia" ni "Intervalo de
# Tiempo" en SISAP (confirmado 2026-09-10), asi que solo gastaba 2 pedidos
# condenados a fallar en cada corrida.
REGIONES_AUTOMATIZADAS = {
    "150000": "Lima",
    "040000": "Arequipa",
    "200000": "Piura",
}
PAUSA_ENTRE_REQUESTS_SEGUNDOS = 1.5
# Lo que puede fallar en UNA region/pedido sin que sea un bug nuestro: la
# red o SISAP (despues de los reintentos de scraper.py), un dato que no pasa
# la validacion de PriceRecord (ValidationError hereda de ValueError), o el
# disco. Eso se registra y la corrida sigue con las demas regiones; cualquier
# otra excepcion (un TypeError, un KeyError) es un bug y si debe cortar todo.
ERRORES_RECUPERABLES = (httpx.HTTPError, ValueError, OSError)


def cmd_hoy(_args: argparse.Namespace) -> int:
    """Snapshot de precios de hoy: una peticion por region (con TODOS los
    productos y variables juntos en cada una, ver parse_resumen_dia).

    Ya NO es parte de la corrida diaria (desde 2026-10-17): el workflow usa
    'historico --ultimos-dias 7', que incluye hoy. El modo "Dia" de SISAP no
    cuadra con el "Intervalo" en el precio maximo, y es el "Intervalo" el que
    coincide con el agregado mensual oficial; ademas el dato de hoy todavia
    va a cambiar (SISAP lo sigue corrigiendo). Queda para consultas a mano."""
    fecha = date.today()
    codigos_producto = list(CATALOGO_PRODUCTOS)
    total_registros = 0
    fallas = 0

    with crear_cliente() as client:
        for cod_region, nombre_region in REGIONES_AUTOMATIZADAS.items():
            # Una region que falla (SISAP caido despues de los reintentos,
            # un precio invalido) no debe dejar sin datos a las demas: se
            # registra, se sigue, y el comando termina con codigo de error
            # para que el workflow lo marque en rojo.
            try:
                registros = _traer_resumen_dia_con_reintento(
                    client, fecha=fecha, region=cod_region, nombre_region=nombre_region,
                    productos=codigos_producto,
                )
                ruta = guardar_registros(registros)
            except ERRORES_RECUPERABLES as error:
                fallas += 1
                print(f"  ERROR {nombre_region}: {type(error).__name__}: {error}")
                continue
            total_registros += len(registros)
            print(f"  {nombre_region}: {len(registros)} registros leidos, guardados en {ruta}")
            time.sleep(PAUSA_ENTRE_REQUESTS_SEGUNDOS)

    print(f"Total: {total_registros} registros leidos ({fallas} regiones con error)")
    return fallas


def _traer_resumen_dia_con_reintento(
    client, fecha: date, region: str, nombre_region: str, productos: list[str]
) -> list[PriceRecord]:
    """Igual estrategia que _traer_resumen_mensual_con_reintento: si SISAP
    devuelve la pagina de error (parse_resumen_dia ahora lo detecta y falla
    fuerte), se intenta diagnosticar POR QUE antes de reintentar a ciegas:
    - Si hasta un solo producto falla, la causa no es "pedimos demasiado"
      (splitting no ayudaria nunca) sino que la region/periodicidad no
      existe en SISAP -- bug real encontrado 2026-09-10: Cusco no tiene
      reporte "Dia" NI "Intervalo de Tiempo" (falla incluso 1 producto, 1
      dia), aunque si tiene "Mensual". Reintentar particionando productos
      ahi solo agrega minutos de requests condenados a fallar -- se rinde
      rapido en su lugar.
    - Si el producto solo SI funciona, el problema era de tamaño (como el
      bug ya conocido en modo mensual): se reintenta UNA vez partiendo el
      resto de productos a la mitad (sin recursion mas profunda, para
      acotar el costo)."""
    try:
        html = fetch_resumen_dia(
            client, fecha=fecha, region=region, productos=productos, variables=VARIABLES_MVP
        )
        guardar_html_crudo(html, fecha=fecha, region=region)
        return parse_resumen_dia(html, fecha=fecha, region=nombre_region, variables=VARIABLES_MVP)
    except PaginaDeErrorSisap:
        if len(productos) <= 1:
            return []
        time.sleep(PAUSA_ENTRE_REQUESTS_SEGUNDOS)
        try:
            html_probe = fetch_resumen_dia(
                client, fecha=fecha, region=region, productos=productos[:1], variables=VARIABLES_MVP
            )
            parse_resumen_dia(html_probe, fecha=fecha, region=nombre_region, variables=VARIABLES_MVP)
        except PaginaDeErrorSisap:
            print(f"    {nombre_region}: sin reporte 'Dia' en SISAP para esta fecha, se salta la region")
            return []

        mitad = len(productos) // 2
        registros: list[PriceRecord] = []
        for sub in (productos[:mitad], productos[mitad:]):
            time.sleep(PAUSA_ENTRE_REQUESTS_SEGUNDOS)
            try:
                html_sub = fetch_resumen_dia(
                    client, fecha=fecha, region=region, productos=sub, variables=VARIABLES_MVP
                )
                guardar_html_crudo(html_sub, fecha=fecha, region=region)
                registros.extend(
                    parse_resumen_dia(html_sub, fecha=fecha, region=nombre_region, variables=VARIABLES_MVP)
                )
            except PaginaDeErrorSisap:
                print(f"    {nombre_region} / {len(sub)} productos: fallo, se salta ese grupo")
        return registros


def cmd_historico(args: argparse.Namespace) -> int:
    """Backfill de dato diario: una peticion por region, mes y variable (ver
    parse_resumen_intervalo), con pausa entre requests para no saturar
    el servidor.

    Con --ultimos-dias N es la red de seguridad de la corrida diaria: 'hoy'
    solo pide la fecha de hoy, asi que un dia en que el runner estuvo
    apagado (o SISAP no respondio) se perdia para siempre -- bug real
    encontrado 2026-10-17: 12-sep, 16-sep, 17-sep, 19-sep, 5-oct y 7-oct
    quedaron sin dato. Volver a pedir la ultima semana en cada corrida los
    recupera solos."""
    if args.ultimos_dias is not None:
        hasta = date.today()
        desde = hasta - timedelta(days=args.ultimos_dias)
    else:
        desde, hasta = args.desde, args.hasta
    if desde > hasta:
        raise SystemExit(f"--desde ({desde}) es posterior a --hasta ({hasta})")
    codigos_producto = list(CATALOGO_PRODUCTOS)
    total_registros = 0
    fallas = 0

    with crear_cliente() as client:
        for cod_region, nombre_region in REGIONES_AUTOMATIZADAS.items():
            for inicio_mes, fin_mes in _dividir_por_mes(desde, hasta):
                for variable in VARIABLES_MVP:
                    try:
                        registros = _traer_resumen_intervalo_con_reintento(
                            client, desde=inicio_mes, hasta=fin_mes, region=cod_region,
                            nombre_region=nombre_region, productos=codigos_producto,
                            variable=variable,
                        )
                        guardar_registros(registros)
                    except ERRORES_RECUPERABLES as error:
                        fallas += 1
                        print(
                            f"  ERROR {nombre_region} / {inicio_mes} a {fin_mes} / {variable}: "
                            f"{type(error).__name__}: {error}"
                        )
                        continue
                    total_registros += len(registros)
                    print(
                        f"  {nombre_region} / {inicio_mes} a {fin_mes} / {variable}: "
                        f"{len(registros)} registros"
                    )
                    time.sleep(PAUSA_ENTRE_REQUESTS_SEGUNDOS)

    print(f"Total: {total_registros} registros leidos ({fallas} pedidos con error)")
    return fallas


def _traer_resumen_intervalo_con_reintento(
    client, desde: date, hasta: date, region: str, nombre_region: str,
    productos: list[str], variable: str,
) -> list[PriceRecord]:
    """Mismo diagnostico que _traer_resumen_dia_con_reintento (ver arriba):
    primero prueba si un solo producto pasa (si ni eso funciona, la region
    no tiene este tipo de reporte en SISAP -- no vale la pena particionar) y
    recien ahi, si el problema era de tamaño, reintenta partiendo el resto a
    la mitad UNA vez. Bug real encontrado 2026-09-10: pedir el catalogo
    completo (51 productos) para Cusco devolvio la pagina de error de SISAP
    en las 12 combinaciones de mes/variable -- resulto ser que Cusco no
    tiene reporte de Intervalo de Tiempo en absoluto (falla incluso con 1
    solo producto), asi que particionar productos ahi solo agregaba minutos
    de requests condenados a fallar."""
    try:
        html = fetch_resumen_intervalo(
            client, desde=desde, hasta=hasta, region=region, productos=productos, variable=variable
        )
        guardar_html_crudo_intervalo(html, desde=desde, hasta=hasta, region=region, variable=variable)
        return parse_resumen_intervalo(html, region=nombre_region, variable=variable)
    except PaginaDeErrorSisap:
        if len(productos) <= 1:
            return []
        time.sleep(PAUSA_ENTRE_REQUESTS_SEGUNDOS)
        try:
            html_probe = fetch_resumen_intervalo(
                client, desde=desde, hasta=hasta, region=region, productos=productos[:1], variable=variable
            )
            parse_resumen_intervalo(html_probe, region=nombre_region, variable=variable)
        except PaginaDeErrorSisap:
            print(f"    {nombre_region} / {variable}: sin reporte 'Intervalo de Tiempo' en SISAP, se salta la region")
            return []

        mitad = len(productos) // 2
        registros: list[PriceRecord] = []
        for sub in (productos[:mitad], productos[mitad:]):
            time.sleep(PAUSA_ENTRE_REQUESTS_SEGUNDOS)
            try:
                html_sub = fetch_resumen_intervalo(
                    client, desde=desde, hasta=hasta, region=region, productos=sub, variable=variable
                )
                guardar_html_crudo_intervalo(html_sub, desde=desde, hasta=hasta, region=region, variable=variable)
                registros.extend(parse_resumen_intervalo(html_sub, region=nombre_region, variable=variable))
            except PaginaDeErrorSisap:
                print(f"    {nombre_region} / {variable} / {len(sub)} productos: fallo, se salta ese grupo")
        return registros


def cmd_consultar(args: argparse.Namespace) -> int:
    """Consulta exploratoria bajo demanda: un producto, una region, un rango
    de anios completos, en una sola peticion (modo mensual). Pensado para
    analisis puntual (ej. 'precio de la yuca mayorista, ultimos 6 anios,
    Lima'), a diferencia de 'hoy'/'historico' que son para la recoleccion
    automatica programada."""
    anios = list(range(args.desde_anio, args.hasta_anio + 1))
    region_nombre = args.region_nombre or CATALOGO_REGIONES.get(args.region, args.region)

    with crear_cliente() as client:
        html = fetch_resumen_mensual(
            client,
            anios=anios,
            region=args.region,
            productos=[args.producto],
            variable=args.variable,
        )

    guardar_html_crudo_mensual(
        html, anios=anios, region=args.region, producto=args.producto, variable=args.variable
    )
    registros = parse_resumen_mensual(
        html, region=region_nombre, variable=args.variable
    )
    ruta = guardar_registros(registros)
    print(f"{len(registros)} registros leidos, guardados en {ruta}")
    for registro in sorted(registros, key=lambda r: r.fecha):
        print(f"  {registro.fecha}  {registro.precio}")
    return 0


def cmd_poblar_historico(args: argparse.Namespace) -> int:
    """Puebla el historico mensual para TODO el catalogo (51 productos x 28
    regiones) en un rango de anios. El modo mensual acepta varios
    productos[] en una sola peticion, asi que se piden los 51 juntos: solo
    1 peticion por region x variable (28 x 6 = 168), no por producto x
    region x variable (que serian miles). Pensado para correr una vez para
    tener una base amplia de analisis; para mantenerlo al dia esta
    'refrescar-mensual'."""
    anios = list(range(args.desde_anio, args.hasta_anio + 1))
    return _poblar_mensual({anio: list(range(1, 13)) for anio in anios})


def cmd_refrescar_mensual(args: argparse.Namespace) -> int:
    """Re-pide el agregado mensual de los ultimos N meses (incluido el
    actual) para las 28 regiones. Bug real encontrado 2026-10-17: nada
    refrescaba el modo mensual despues de la carga inicial (poblar-historico
    se corrio una sola vez, el 2026-09-05), asi que 25 de 28 regiones se
    quedaron congeladas en un septiembre parcial.

    Pide SOLO los meses que hacen falta (meses[] acepta cualquier subconjunto,
    probado en vivo: 2 meses x 51 productos = 1 request de ~5s), no el anio
    entero. Con --solo-si-pasaron-dias, se salta la corrida si el ultimo
    refresco es mas nuevo que eso -- asi el workflow diario puede llamarlo
    siempre y el costo real (168 pedidos) se paga ~1 vez por semana, aunque
    el runner haya estado apagado justo el dia "que tocaba"."""
    if args.solo_si_pasaron_dias is not None:
        ultima = _ultima_captura_mensual()
        hoy = date.today()
        # La primera corrida de un mes nuevo SIEMPRE refresca, aunque no se
        # cumpla el umbral: cierra el mes anterior con su valor final, y el
        # mes nuevo aparece para las 28 regiones a la vez (si no, durante
        # hasta 6 dias solo existia en las regiones con dato diario).
        mismo_mes = ultima is not None and (ultima.year, ultima.month) == (hoy.year, hoy.month)
        if mismo_mes and (hoy - ultima).days < args.solo_si_pasaron_dias:
            print(f"Ultimo refresco mensual: {ultima} -- todavia no toca (umbral {args.solo_si_pasaron_dias} dias)")
            return 0

    meses_por_anio: dict[int, list[int]] = {}
    for anio, mes in _ultimos_meses(date.today(), args.meses):
        meses_por_anio.setdefault(anio, []).append(mes)
    return _poblar_mensual(meses_por_anio)


def _poblar_mensual(meses_por_anio: dict[int, list[int]]) -> int:
    """Una peticion por region x variable x (grupo de anios con los mismos
    meses). Los anios completos van todos juntos en un solo pedido; un
    refresco que cruza el cambio de anio (ej. dic + ene) va en dos."""
    grupos: dict[tuple[int, ...], list[int]] = {}
    for anio, meses in meses_por_anio.items():
        grupos.setdefault(tuple(sorted(meses)), []).append(anio)

    codigos_producto = list(CATALOGO_PRODUCTOS)
    combinaciones = [
        (cod_region, variable, meses, anios)
        for cod_region in CATALOGO_REGIONES
        for variable in VARIABLES_MVP
        for meses, anios in grupos.items()
    ]
    total_registros = 0
    fallas = 0

    with crear_cliente() as client:
        for i, (cod_region, variable, meses, anios) in enumerate(combinaciones, start=1):
            nombre_region = CATALOGO_REGIONES[cod_region]
            try:
                registros = _traer_resumen_mensual_con_reintento(
                    client, anios=anios, region=cod_region, nombre_region=nombre_region,
                    productos=codigos_producto, variable=variable, meses=list(meses),
                )
                guardar_registros(registros)
            except ERRORES_RECUPERABLES as error:
                fallas += 1
                print(f"  [{i}/{len(combinaciones)}] ERROR {nombre_region} / {variable}: {type(error).__name__}: {error}")
                continue
            total_registros += len(registros)
            print(
                f"  [{i}/{len(combinaciones)}] {nombre_region} / {variable}: "
                f"{len(registros)} registros"
            )
            time.sleep(PAUSA_ENTRE_REQUESTS_SEGUNDOS)

    print(f"Total: {total_registros} registros leidos ({fallas} pedidos con error)")
    return fallas


def _traer_resumen_mensual_con_reintento(
    client, anios: list[int], region: str, nombre_region: str, productos: list[str],
    variable: str, meses: list[int] | None = None,
) -> list[PriceRecord]:
    """Bug real encontrado 2026-09-07: Lima/Minorista y otras combinaciones
    con mucho historial hacen que SISAP devuelva una pagina de error ("Se ha
    excedido el tiempo limite...") en vez de la tabla de precios cuando se
    piden todos los anios juntos -- parse_resumen_mensual ahora lo detecta y
    falla fuerte en vez de devolver una lista vacia en silencio. Aca se
    aprovecha esa señal: si el pedido completo falla, se reintenta AÑO POR
    AÑO (que si entra dentro del limite de SISAP) en vez de perder toda la
    combinacion region+variable de una."""
    try:
        html = fetch_resumen_mensual(
            client, anios=anios, region=region, productos=productos, variable=variable, meses=meses
        )
        guardar_html_crudo_mensual(html, anios=anios, region=region, producto="todos", variable=variable, meses=meses)
        return parse_resumen_mensual(html, region=nombre_region, variable=variable)
    except PaginaDeErrorSisap:
        if len(anios) <= 1:
            print(f"    {nombre_region} / {variable} / {anios}: SISAP devolvio error, se salta")
            return []
        print(f"    (pedido completo {min(anios)}-{max(anios)} fallo, reintentando anio por anio)")
        registros: list[PriceRecord] = []
        for anio in anios:
            time.sleep(PAUSA_ENTRE_REQUESTS_SEGUNDOS)
            try:
                html = fetch_resumen_mensual(
                    client, anios=[anio], region=region, productos=productos, variable=variable, meses=meses
                )
                guardar_html_crudo_mensual(html, anios=[anio], region=region, producto="todos", variable=variable, meses=meses)
                registros.extend(parse_resumen_mensual(html, region=nombre_region, variable=variable))
            except PaginaDeErrorSisap:
                print(f"      {nombre_region} / {variable} / {anio}: tambien fallo, se salta")
        return registros


def cmd_construir_dwh(_args: argparse.Namespace) -> int:
    """Reconstruye el modelo dimensional en DuckDB a partir del parquet
    historico acumulado."""
    ruta_parquet = PROCESSED_DIR / "precios.parquet"
    ruta_duckdb = PROCESSED_DIR / "sisap.duckdb"
    construir_modelo_dimensional(ruta_parquet, ruta_duckdb)
    print(f"Modelo dimensional reconstruido en {ruta_duckdb}")
    return 0


def cmd_verificar_frescura(args: argparse.Namespace) -> int:
    """Falla (codigo 1) si los datos estan viejos. Es la alarma del workflow
    diario: un job que corre "bien" pero no trae nada nuevo (SISAP cambio el
    HTML, el runner perdio la red, etc.) se veia verde igual, y el dashboard
    se quedaba congelado sin que nadie se enterara."""
    df = leer_precios(PROCESSED_DIR / "precios.parquet")
    hoy = date.today()
    problemas = []

    diarios = df[(df["periodicidad"] == "dia") & df["region"].isin(REGIONES_AUTOMATIZADAS.values())]
    ultimo_diario = max(diarios["fecha"], default=None)
    print(f"Ultimo dato diario: {ultimo_diario}")
    if ultimo_diario is None or (hoy - ultimo_diario).days > args.max_dias_diario:
        problemas.append(f"no hay dato diario nuevo hace mas de {args.max_dias_diario} dias")

    ultima_mensual = _ultima_captura_mensual()
    print(f"Ultimo refresco mensual: {ultima_mensual}")
    if ultima_mensual is None or (hoy - ultima_mensual).days > args.max_dias_mensual:
        problemas.append(f"el agregado mensual no se refresca hace mas de {args.max_dias_mensual} dias")

    for problema in problemas:
        print(f"ALERTA: {problema}")
    return 1 if problemas else 0


def _ultima_captura_mensual() -> date | None:
    ruta = PROCESSED_DIR / "precios.parquet"
    if not ruta.exists():
        return None
    df = leer_precios(ruta)
    capturas = df.loc[df["periodicidad"] == "mensual", "fecha_captura"].dropna()
    return max(capturas, default=None)


def _ultimos_meses(hoy: date, cantidad: int) -> list[tuple[int, int]]:
    """Los ultimos `cantidad` meses calendario hasta el de `hoy` inclusive,
    como (anio, mes), cruzando el cambio de anio si hace falta."""
    meses = []
    anio, mes = hoy.year, hoy.month
    for _ in range(cantidad):
        meses.append((anio, mes))
        anio, mes = (anio, mes - 1) if mes > 1 else (anio - 1, 12)
    return sorted(meses)


def _dividir_por_mes(desde: date, hasta: date) -> list[tuple[date, date]]:
    """Parte un rango de fechas en trozos de ~1 mes, para no pedirle al
    servidor un rango tan grande que se demore o falle."""
    chunks = []
    inicio = desde
    while inicio <= hasta:
        fin_teorico = (inicio.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        fin = min(fin_teorico, hasta)
        chunks.append((inicio, fin))
        inicio = fin + timedelta(days=1)
    return chunks


# Los codigos de los argumentos terminan en la URL a SISAP y en nombres de
# archivo bajo data/raw/ (ver storage.py) -- validarlos aca cierra la puerta
# a un "--region ../../algo" que escriba fuera de esa carpeta.
def _codigo_region(valor: str) -> str:
    if not re.fullmatch(r"\d{6}", valor):
        raise argparse.ArgumentTypeError(f"codigo de region invalido: {valor!r} (6 digitos, ej. 150000)")
    return valor


def _codigo_producto(valor: str) -> str:
    if not re.fullmatch(r"\d{4}", valor):
        raise argparse.ArgumentTypeError(f"codigo de producto invalido: {valor!r} (4 digitos, ej. 0105)")
    return valor


def _entero_positivo(valor: str) -> int:
    numero = int(valor)
    if numero < 1:
        raise argparse.ArgumentTypeError(f"tiene que ser >= 1: {valor}")
    return numero


def crear_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pipeline de precios SISAP")
    subparsers = parser.add_subparsers(required=True)

    parser_hoy = subparsers.add_parser("hoy", help="Trae el precio de hoy")
    parser_hoy.set_defaults(func=cmd_hoy)

    parser_historico = subparsers.add_parser(
        "historico", help="Trae dato diario de un rango de fechas (backfill)"
    )
    rango = parser_historico.add_mutually_exclusive_group(required=True)
    rango.add_argument("--desde", type=date.fromisoformat, help="YYYY-MM-DD (con --hasta)")
    rango.add_argument(
        "--ultimos-dias", dest="ultimos_dias", type=_entero_positivo,
        help="re-pide los ultimos N dias hasta hoy (recupera dias perdidos)",
    )
    parser_historico.add_argument("--hasta", type=date.fromisoformat, help="YYYY-MM-DD")
    parser_historico.set_defaults(func=cmd_historico)

    parser_consultar = subparsers.add_parser(
        "consultar", help="Consulta exploratoria: 1 producto, 1 region, rango de anios"
    )
    parser_consultar.add_argument("--producto", required=True, type=_codigo_producto, help="codigo de genero, ej. 0105")
    parser_consultar.add_argument("--region", required=True, type=_codigo_region, help="codigo de region, ej. 150000")
    parser_consultar.add_argument("--region-nombre", dest="region_nombre", default=None)
    parser_consultar.add_argument("--desde-anio", dest="desde_anio", type=int, required=True)
    parser_consultar.add_argument("--hasta-anio", dest="hasta_anio", type=int, required=True)
    parser_consultar.add_argument("--variable", default="may_precio_prom", choices=VARIABLES_MVP)
    parser_consultar.set_defaults(func=cmd_consultar)

    parser_poblar = subparsers.add_parser(
        "poblar-historico",
        help="Puebla el historico mensual de todo el catalogo (productos x regiones)",
    )
    parser_poblar.add_argument("--desde-anio", dest="desde_anio", type=int, required=True)
    parser_poblar.add_argument("--hasta-anio", dest="hasta_anio", type=int, required=True)
    parser_poblar.set_defaults(func=cmd_poblar_historico)

    parser_refrescar = subparsers.add_parser(
        "refrescar-mensual",
        help="Re-pide el agregado mensual de los ultimos meses para las 28 regiones",
    )
    parser_refrescar.add_argument(
        "--meses", type=_entero_positivo, default=2,
        help="cuantos meses hacia atras, incluido el actual (default 2)",
    )
    parser_refrescar.add_argument(
        "--solo-si-pasaron-dias", dest="solo_si_pasaron_dias", type=_entero_positivo, default=None,
        help="no hace nada si el ultimo refresco tiene menos de N dias",
    )
    parser_refrescar.set_defaults(func=cmd_refrescar_mensual)

    parser_dwh = subparsers.add_parser(
        "construir-dwh", help="Reconstruye el modelo dimensional en DuckDB"
    )
    parser_dwh.set_defaults(func=cmd_construir_dwh)

    parser_frescura = subparsers.add_parser(
        "verificar-frescura", help="Falla si los datos estan desactualizados (alarma del workflow)"
    )
    parser_frescura.add_argument("--max-dias-diario", dest="max_dias_diario", type=_entero_positivo, default=3)
    parser_frescura.add_argument("--max-dias-mensual", dest="max_dias_mensual", type=_entero_positivo, default=10)
    parser_frescura.set_defaults(func=cmd_verificar_frescura)

    return parser


def main() -> None:
    parser = crear_parser()
    args = parser.parse_args()
    if getattr(args, "desde", None) is not None and getattr(args, "hasta", None) is None:
        parser.error("--desde necesita --hasta")
    sys.exit(1 if args.func(args) else 0)


if __name__ == "__main__":
    main()
