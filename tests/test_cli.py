from datetime import date

import pandas as pd
import pytest

from sisap import cli


@pytest.mark.parametrize(("hoy", "cantidad", "esperado"), [
    (date(2026, 10, 17), 2, [(2026, 9), (2026, 10)]),
    # cruzar el cambio de anio: enero tambien tiene que cerrar diciembre
    (date(2027, 1, 5), 2, [(2026, 12), (2027, 1)]),
    (date(2027, 2, 1), 3, [(2026, 12), (2027, 1), (2027, 2)]),
    (date(2026, 10, 17), 1, [(2026, 10)]),
])
def test_ultimos_meses(hoy, cantidad, esperado):
    assert cli._ultimos_meses(hoy, cantidad) == esperado


def test_refrescar_mensual_pide_solo_los_meses_que_hacen_falta(monkeypatch):
    """Un refresco que cruza el cambio de anio va en un pedido por anio, cada
    uno con SUS meses -- no el anio entero (12 meses x 51 productos)."""
    pedidos = []

    def falso_traer(client, anios, region, nombre_region, productos, variable, meses=None):
        pedidos.append((tuple(anios), tuple(meses), region, variable))
        return []

    monkeypatch.setattr(cli, "_traer_resumen_mensual_con_reintento", falso_traer)
    monkeypatch.setattr(cli, "PAUSA_ENTRE_REQUESTS_SEGUNDOS", 0)
    monkeypatch.setattr(cli, "_ultima_captura_mensual", lambda: None)
    _fijar_hoy(monkeypatch, date(2027, 1, 5))

    fallas = cli.cmd_refrescar_mensual(cli.crear_parser().parse_args(["refrescar-mensual", "--meses", "2"]))

    assert fallas == 0
    assert {(anios, meses) for anios, meses, _, _ in pedidos} == {((2026,), (12,)), ((2027,), (1,))}
    assert len(pedidos) == 2 * len(cli.CATALOGO_REGIONES) * len(cli.VARIABLES_MVP)


def _fijar_hoy(monkeypatch, hoy: date) -> None:
    monkeypatch.setattr(cli, "date", type("FalsoDate", (date,), {"today": staticmethod(lambda: hoy)}))


def test_refrescar_mensual_se_salta_si_el_ultimo_refresco_es_reciente(monkeypatch):
    _fijar_hoy(monkeypatch, date(2026, 10, 17))
    monkeypatch.setattr(cli, "_ultima_captura_mensual", lambda: date(2026, 10, 14))
    monkeypatch.setattr(cli, "_poblar_mensual", lambda _: pytest.fail("no deberia pedir nada"))

    args = cli.crear_parser().parse_args(["refrescar-mensual", "--solo-si-pasaron-dias", "6"])

    assert cli.cmd_refrescar_mensual(args) == 0


def test_refrescar_mensual_corre_siempre_al_empezar_un_mes_nuevo(monkeypatch):
    """Aunque el ultimo refresco tenga 2 dias, si fue el mes pasado hay que
    cerrar ese mes con su valor final y traer el mes nuevo para las 28
    regiones: si no, "Todo el Peru" promediaba el mes nuevo con solo las
    regiones de dato diario."""
    _fijar_hoy(monkeypatch, date(2026, 11, 1))
    monkeypatch.setattr(cli, "_ultima_captura_mensual", lambda: date(2026, 10, 30))
    pedidos = []
    monkeypatch.setattr(cli, "_poblar_mensual", lambda meses: pedidos.append(meses) or 0)

    args = cli.crear_parser().parse_args(["refrescar-mensual", "--solo-si-pasaron-dias", "6"])

    assert cli.cmd_refrescar_mensual(args) == 0
    assert pedidos == [{2026: [10, 11]}]


def test_una_region_que_falla_no_frena_a_las_demas(monkeypatch, tmp_path):
    """Antes, una excepcion en Lima (SISAP caido despues de los reintentos)
    cortaba 'hoy' entero: Arequipa y Piura se quedaban sin pedir. Ahora la
    falla se cuenta y la corrida sigue; el codigo de salida avisa."""
    import httpx

    pedidas = []

    def falso_traer(client, fecha, region, nombre_region, productos):
        pedidas.append(nombre_region)
        if nombre_region == "Lima":
            raise httpx.ConnectTimeout("timeout")
        return []

    monkeypatch.setattr(cli, "_traer_resumen_dia_con_reintento", falso_traer)
    monkeypatch.setattr(cli, "PAUSA_ENTRE_REQUESTS_SEGUNDOS", 0)

    fallas = cli.cmd_hoy(cli.crear_parser().parse_args(["hoy"]))

    assert fallas == 1
    assert pedidas == list(cli.REGIONES_AUTOMATIZADAS.values())


def test_un_bug_de_programacion_si_corta_la_corrida(monkeypatch):
    """Lo recuperable es red/SISAP/validacion/disco; un TypeError es un bug
    nuestro y no debe quedar escondido como "una region fallo"."""
    def falso_traer(*_args, **_kwargs):
        raise TypeError("bug")

    monkeypatch.setattr(cli, "_traer_resumen_dia_con_reintento", falso_traer)

    with pytest.raises(TypeError):
        cli.cmd_hoy(cli.crear_parser().parse_args(["hoy"]))


@pytest.mark.parametrize(("ultimo_diario", "ultima_captura_mensual", "codigo"), [
    (date.today(), date.today(), 0),
    (date(2020, 1, 1), date.today(), 1),   # diario congelado
    (date.today(), date(2020, 1, 1), 1),   # mensual sin refrescar
])
def test_verificar_frescura(tmp_path, monkeypatch, ultimo_diario, ultima_captura_mensual, codigo):
    monkeypatch.setattr(cli, "PROCESSED_DIR", tmp_path)
    base = {"region": "Lima", "producto": "Papa", "unidad": "Kilogramo", "equivalencia_kg": 1.0,
            "variable": "may_precio_prom", "precio": 1.0}
    pd.DataFrame([
        {**base, "fecha": ultimo_diario, "periodicidad": "dia", "fecha_captura": ultimo_diario},
        {**base, "fecha": date(2026, 9, 1), "periodicidad": "mensual", "fecha_captura": ultima_captura_mensual},
    ]).to_parquet(tmp_path / "precios.parquet", index=False)

    args = cli.crear_parser().parse_args(["verificar-frescura"])

    assert cli.cmd_verificar_frescura(args) == codigo


def test_historico_rechaza_desde_posterior_a_hasta():
    args = cli.crear_parser().parse_args(["historico", "--desde", "2026-10-10", "--hasta", "2026-10-01"])

    with pytest.raises(SystemExit):
        cli.cmd_historico(args)
