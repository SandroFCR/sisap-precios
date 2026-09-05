import argparse
import time
from datetime import date, timedelta

from sisap.parser import parse_resumen_dia, parse_resumen_intervalo, parse_resumen_mensual
from sisap.scraper import (
    crear_cliente,
    fetch_resumen_dia,
    fetch_resumen_intervalo,
    fetch_resumen_mensual,
)
from sisap.storage import (
    guardar_html_crudo,
    guardar_html_crudo_intervalo,
    guardar_html_crudo_mensual,
    guardar_registros,
)

REGION_LIMA = "150000"
PRODUCTOS_MVP = {
    "0104": "Papa",
    "0212": "Cebolla",
    "0401": "Arroz",
    "0228": "Tomate",
}
VARIABLES_MVP = ["may_precio_prom", "min_precio_prom"]
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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
