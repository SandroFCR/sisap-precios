import argparse
import time
from datetime import date, timedelta

from sisap.parser import (
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
)
from sisap.warehouse import construir_modelo_dimensional

REGION_LIMA = "150000"
PRODUCTOS_MVP = {
    "0104": "Papa",
    "0212": "Cebolla",
    "0401": "Arroz",
    "0228": "Tomate",
    "0105": "Yuca",
}
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
REGIONES_MVP = {
    "150000": "Lima",
    "040000": "Arequipa",
    "080000": "Cusco",
    "200000": "Piura",
    "210000": "Puno",
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
PAUSA_ENTRE_REQUESTS_SEGUNDOS = 1.5


def cmd_hoy(_args: argparse.Namespace) -> None:
    """Snapshot de precios de hoy: una fila por producto, todas las
    variables en la misma peticion (ver parse_resumen_dia)."""
    fecha = date.today()
    codigos_producto = list(PRODUCTOS_MVP)

    with crear_cliente() as client:
        html = fetch_resumen_dia(
            client,
            fecha=fecha,
            region=REGION_LIMA,
            productos=codigos_producto,
            variables=VARIABLES_MVP,
        )

    guardar_html_crudo(html, fecha=fecha, region=REGION_LIMA)
    registros = parse_resumen_dia(
        html, fecha=fecha, region="Lima", variables=VARIABLES_MVP
    )
    ruta = guardar_registros(registros)
    print(f"{len(registros)} registros guardados en {ruta}")


def cmd_historico(args: argparse.Namespace) -> None:
    """Backfill historico: una peticion por mes y por variable (ver
    parse_resumen_intervalo), con pausa entre requests para no saturar
    el servidor."""
    desde = date.fromisoformat(args.desde)
    hasta = date.fromisoformat(args.hasta)
    codigos_producto = list(PRODUCTOS_MVP)
    total_registros = 0

    with crear_cliente() as client:
        for inicio_mes, fin_mes in _dividir_por_mes(desde, hasta):
            for variable in VARIABLES_MVP:
                html = fetch_resumen_intervalo(
                    client,
                    desde=inicio_mes,
                    hasta=fin_mes,
                    region=REGION_LIMA,
                    productos=codigos_producto,
                    variable=variable,
                )
                guardar_html_crudo_intervalo(
                    html,
                    desde=inicio_mes,
                    hasta=fin_mes,
                    region=REGION_LIMA,
                    variable=variable,
                )
                registros = parse_resumen_intervalo(
                    html, region="Lima", variable=variable
                )
                guardar_registros(registros)
                total_registros += len(registros)
                print(
                    f"  {inicio_mes} a {fin_mes} / {variable}: "
                    f"{len(registros)} registros"
                )
                time.sleep(PAUSA_ENTRE_REQUESTS_SEGUNDOS)

    print(f"Total: {total_registros} registros guardados (con posibles duplicados ya filtrados)")


def cmd_consultar(args: argparse.Namespace) -> None:
    """Consulta exploratoria bajo demanda: un producto, una region, un rango
    de anios completos, en una sola peticion (modo mensual). Pensado para
    analisis puntual (ej. 'precio de la yuca mayorista, ultimos 6 anios,
    Lima'), a diferencia de 'hoy'/'historico' que son para la recoleccion
    automatica programada."""
    anios = list(range(args.desde_anio, args.hasta_anio + 1))
    region_nombre = args.region_nombre or args.region

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
    print(f"{len(registros)} registros guardados en {ruta}")
    for registro in sorted(registros, key=lambda r: r.fecha):
        print(f"  {registro.fecha}  {registro.precio}")


def cmd_poblar_historico(args: argparse.Namespace) -> None:
    """Puebla el historico mensual para TODO el catalogo (51 productos x 28
    regiones) en un rango de anios. El modo mensual acepta varios
    productos[] en una sola peticion, asi que se piden los 51 juntos: solo
    1 peticion por region x variable (28 x 6 = 168), no por producto x
    region x variable (que serian miles). Pensado para correr una vez para
    tener una base amplia de analisis, no para uso diario (para eso esta
    'hoy'/'historico')."""
    anios = list(range(args.desde_anio, args.hasta_anio + 1))
    codigos_producto = list(CATALOGO_PRODUCTOS)
    combinaciones = [
        (cod_region, variable)
        for cod_region in CATALOGO_REGIONES
        for variable in VARIABLES_MVP
    ]
    total_registros = 0

    with crear_cliente() as client:
        for i, (cod_region, variable) in enumerate(combinaciones, start=1):
            nombre_region = CATALOGO_REGIONES[cod_region]
            html = fetch_resumen_mensual(
                client, anios=anios, region=cod_region,
                productos=codigos_producto, variable=variable,
            )
            guardar_html_crudo_mensual(
                html, anios=anios, region=cod_region,
                producto="todos", variable=variable,
            )
            registros = parse_resumen_mensual(html, region=nombre_region, variable=variable)
            guardar_registros(registros)
            total_registros += len(registros)
            print(
                f"  [{i}/{len(combinaciones)}] {nombre_region} / {variable}: "
                f"{len(registros)} registros"
            )
            time.sleep(PAUSA_ENTRE_REQUESTS_SEGUNDOS)

    print(f"Total: {total_registros} registros guardados (con posibles duplicados ya filtrados)")


def cmd_construir_dwh(_args: argparse.Namespace) -> None:
    """Reconstruye el modelo dimensional en DuckDB a partir del parquet
    historico acumulado."""
    ruta_parquet = PROCESSED_DIR / "precios.parquet"
    ruta_duckdb = PROCESSED_DIR / "sisap.duckdb"
    construir_modelo_dimensional(ruta_parquet, ruta_duckdb)
    print(f"Modelo dimensional reconstruido en {ruta_duckdb}")


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Pipeline de precios SISAP")
    subparsers = parser.add_subparsers(required=True)

    parser_hoy = subparsers.add_parser("hoy", help="Trae el precio de hoy")
    parser_hoy.set_defaults(func=cmd_hoy)

    parser_historico = subparsers.add_parser(
        "historico", help="Trae un rango historico de fechas"
    )
    parser_historico.add_argument("--desde", required=True, help="YYYY-MM-DD")
    parser_historico.add_argument("--hasta", required=True, help="YYYY-MM-DD")
    parser_historico.set_defaults(func=cmd_historico)

    parser_consultar = subparsers.add_parser(
        "consultar", help="Consulta exploratoria: 1 producto, 1 region, rango de anios"
    )
    parser_consultar.add_argument("--producto", required=True, help="codigo de genero, ej. 0105")
    parser_consultar.add_argument("--region", required=True, help="codigo de region, ej. 150000")
    parser_consultar.add_argument("--region-nombre", dest="region_nombre", default=None)
    parser_consultar.add_argument("--desde-anio", dest="desde_anio", type=int, required=True)
    parser_consultar.add_argument("--hasta-anio", dest="hasta_anio", type=int, required=True)
    parser_consultar.add_argument("--variable", default="may_precio_prom")
    parser_consultar.set_defaults(func=cmd_consultar)

    parser_poblar = subparsers.add_parser(
        "poblar-historico",
        help="Puebla el historico mensual de todo el catalogo MVP (productos x regiones)",
    )
    parser_poblar.add_argument("--desde-anio", dest="desde_anio", type=int, required=True)
    parser_poblar.add_argument("--hasta-anio", dest="hasta_anio", type=int, required=True)
    parser_poblar.set_defaults(func=cmd_poblar_historico)

    parser_dwh = subparsers.add_parser(
        "construir-dwh", help="Reconstruye el modelo dimensional en DuckDB"
    )
    parser_dwh.set_defaults(func=cmd_construir_dwh)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
