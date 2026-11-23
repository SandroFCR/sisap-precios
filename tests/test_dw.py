"""Pruebas del data warehouse dimensional (sisap.dw): grano, pivot, atipicos,
jerarquias, claves estables, carga incremental con auditoria y rollback."""

from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from sisap import cli
from sisap.cli import CATALOGO_PRODUCTOS, CATALOGO_REGIONES
from sisap.dw import ErrorDeIntegridad, cargar_dw, construir_dw, construir_dw_en_memoria
from sisap.dw.semillas import (
    CATEGORIA_DE_GENERO,
    CATEGORIAS,
    CODIGOS_VARIEDAD,
    GENEROS,
    MACRORREGIONES,
    REGIONES,
    SIN_CLASIFICAR,
    clasificar_variedad,
)
from sisap.storage import leer_precios
from sisap.warehouse import construir_en_memoria

PARQUET_REAL = Path(__file__).parent.parent / "data" / "processed" / "precios.parquet"
VARIABLES = ["may_precio_min", "may_precio_prom", "may_precio_max",
             "min_precio_min", "min_precio_prom", "min_precio_max"]


def _fila(**cambios) -> dict:
    fila = {
        "fecha": date(2026, 1, 1), "region": "Lima", "producto": "Papa blanca",
        "unidad": "Kilogramo", "equivalencia_kg": 1.0, "variable": "may_precio_prom",
        "precio": 1.0, "periodicidad": "mensual", "fecha_captura": date(2026, 2, 1),
    }
    return {**fila, **cambios}


def _tres_precios(minimo, promedio, maximo, mercado="may", **cambios) -> list[dict]:
    """Las 3 filas (min/prom/max) de una misma clave, como las guarda el parquet."""
    return [
        _fila(variable=f"{mercado}_precio_{tipo}", precio=precio, **cambios)
        for tipo, precio in [("min", minimo), ("prom", promedio), ("max", maximo)]
    ]


def _parquet(tmp_path, filas, nombre="p.parquet") -> Path:
    ruta = tmp_path / nombre
    pd.DataFrame(filas).to_parquet(ruta, index=False)
    return ruta


def _q(con, sql, parametros=None):
    return con.execute(sql, parametros or []).fetchall()


def _serie_arroz(precio_mayo: float) -> list[dict]:
    """6 meses de Arroz extra en Arequipa con un valor de mayo controlado."""
    filas = []
    for mes, precio in zip(range(1, 7), [166, 173, 175, 170, precio_mayo, 176], strict=True):
        filas += _tres_precios(precio * 0.95, precio, precio * 1.05, region="Arequipa",
                               producto="Arroz extra", unidad="Saco", equivalencia_kg=50.0,
                               fecha=date(2024, mes, 1))
    return filas


# --- Esquema, grano y pivot ---------------------------------------------------

def test_modelo_tiene_las_tablas_del_diseno_y_pivotea_los_tres_precios(tmp_path):
    filas = (_tres_precios(1.0, 1.5, 2.0)
             + _tres_precios(2.0, 2.5, 3.0, mercado="min"))
    con = construir_dw_en_memoria(_parquet(tmp_path, filas))

    tablas = {t for (t,) in _q(con, "SELECT table_name FROM information_schema.tables")}
    assert {"dim_tiempo", "dim_mes", "dim_region", "dim_producto", "dim_unidad",
            "dim_mercado", "fact_precio_diario", "fact_precio_mensual", "etl_carga"} <= tablas

    # 6 filas largas -> 2 hechos (uno por mercado), con las 3 medidas en columnas
    hechos = _q(con, """
        SELECT m.nombre_mercado, f.precio_min, f.precio_prom, f.precio_max, f.precio_kg_prom,
               f.precio_prom_crudo, f.flag_atipico, f.fuente, f.n_dias
        FROM fact_precio_mensual f JOIN dim_mercado m USING (mercado_id)
        ORDER BY 1""")
    assert hechos == [
        ("Mayorista", 1.0, 1.5, 2.0, 1.5, 1.5, False, "oficial", None),
        ("Minorista", 2.0, 2.5, 3.0, 2.5, 2.5, False, "oficial", None),
    ]
    assert _q(con, "SELECT COUNT(*) FROM fact_precio_diario") == [(0,)]


def test_diario_y_mensual_son_hechos_separados_con_su_propio_grano(tmp_path):
    filas = (
        _tres_precios(1.0, 1.2, 1.4, fecha=date(2026, 3, 1), periodicidad="mensual")
        # el dia 1 de ese mismo mes tambien tiene su dato diario: no se pisan
        + _tres_precios(2.0, 2.2, 2.4, fecha=date(2026, 3, 1), periodicidad="dia")
        + _tres_precios(3.0, 3.2, 3.4, fecha=date(2026, 3, 2), periodicidad="dia")
    )
    con = construir_dw_en_memoria(_parquet(tmp_path, filas))

    assert _q(con, "SELECT COUNT(*) FROM fact_precio_mensual") == [(1,)]
    assert _q(con, "SELECT tiempo_id, precio_prom FROM fact_precio_diario ORDER BY 1") == [
        (20260301, 2.2), (20260302, 3.2)]
    assert _q(con, "SELECT precio_prom, fuente FROM fact_precio_mensual") == [(1.2, "oficial")]


def test_precio_por_kg_usa_la_equivalencia_de_la_unidad(tmp_path):
    filas = _tres_precios(90.0, 100.0, 110.0, unidad="Saco", equivalencia_kg=50.0)
    con = construir_dw_en_memoria(_parquet(tmp_path, filas))

    assert _q(con, "SELECT precio_prom, precio_kg_prom FROM fact_precio_mensual") == [(100.0, 2.0)]
    assert _q(con, "SELECT nombre_unidad, equivalencia_kg FROM dim_unidad") == [("Saco", 50.0)]


def test_misma_unidad_con_distinta_equivalencia_son_dos_miembros(tmp_path):
    filas = (_tres_precios(1, 2, 3, producto="Arroz extra", unidad="Saco", equivalencia_kg=50.0)
             + _tres_precios(1, 2, 3, producto="Yuca blanca", unidad="Saco", equivalencia_kg=70.0))
    con = construir_dw_en_memoria(_parquet(tmp_path, filas))

    assert _q(con, "SELECT COUNT(*) FROM dim_unidad WHERE nombre_unidad = 'Saco'") == [(2,)]


# --- Atipicos: se marcan, no se borran ------------------------------------------

def test_atipico_se_marca_y_el_crudo_se_conserva(tmp_path):
    con = construir_dw_en_memoria(_parquet(tmp_path, _serie_arroz(1.7)))

    mayo = _q(con, """
        SELECT f.precio_prom, f.precio_prom_crudo, f.precio_kg_prom, f.flag_atipico
        FROM fact_precio_mensual f JOIN dim_mes m USING (mes_id) WHERE m.numero_mes = 5""")
    # el typo "1.70" en vez de ~170 queda marcado; validado NULL, crudo intacto
    assert mayo[0][0] is None
    assert mayo[0][1] == 1.7
    assert mayo[0][2] is None
    assert mayo[0][3] is True
    assert _q(con, "SELECT COUNT(*) FROM fact_precio_mensual WHERE flag_atipico") == [(1,)]
    assert _q(con, "SELECT filas_atipicas FROM etl_carga") == [(3,)]  # min, prom y max de mayo


def test_serie_sin_atipicos_no_marca_nada(tmp_path):
    con = construir_dw_en_memoria(_parquet(tmp_path, _serie_arroz(172.0)))

    assert _q(con, "SELECT COUNT(*) FROM fact_precio_mensual WHERE flag_atipico") == [(0,)]
    assert _q(con, "SELECT filas_atipicas FROM etl_carga") == [(0,)]


# --- Mensual: oficial o promedio de dias ------------------------------------------

def test_mes_sin_agregado_oficial_se_promedia_desde_los_dias_en_kg(tmp_path):
    filas = (
        _tres_precios(1.0, 2.0, 3.0, fecha=date(2026, 5, 4), periodicidad="dia")
        + _tres_precios(3.0, 4.0, 5.0, fecha=date(2026, 5, 5), periodicidad="dia")
        # un dia con la unidad distinta: se normaliza por kg antes de promediar
        + _tres_precios(300.0, 600.0, 900.0, fecha=date(2026, 5, 6), periodicidad="dia",
                        unidad="Saco", equivalencia_kg=100.0)
    )
    con = construir_dw_en_memoria(_parquet(tmp_path, filas))

    fila = _q(con, """
        SELECT f.precio_prom, f.precio_kg_prom, u.nombre_unidad, f.fuente, f.n_dias
        FROM fact_precio_mensual f JOIN dim_unidad u USING (unidad_id)""")
    # kg: 2, 4 y 6 -> promedio 4.0 por kg; unidad "modal" (empate 2-1 -> Kilogramo)
    assert fila == [(4.0, 4.0, "Kilogramo", "promedio de dias", 3)]


def test_el_agregado_oficial_gana_sobre_los_dias_del_mismo_mes(tmp_path):
    filas = (
        _tres_precios(1.0, 1.1, 1.2, fecha=date(2026, 5, 1), periodicidad="mensual")
        + _tres_precios(9.0, 9.1, 9.2, fecha=date(2026, 5, 4), periodicidad="dia")
    )
    con = construir_dw_en_memoria(_parquet(tmp_path, filas))

    assert _q(con, "SELECT precio_prom, fuente FROM fact_precio_mensual") == [(1.1, "oficial")]


# --- Dimensiones y jerarquias -------------------------------------------------------

def test_dim_tiempo_es_un_calendario_completo_con_su_jerarquia(tmp_path):
    filas = _tres_precios(1, 2, 3, fecha=date(2026, 1, 15), periodicidad="dia") \
        + _tres_precios(1, 2, 3, fecha=date(2026, 3, 10), periodicidad="dia")
    con = construir_dw_en_memoria(_parquet(tmp_path, filas))

    # ene, feb y mar completos aunque solo haya datos en dos fechas
    assert _q(con, "SELECT COUNT(*), MIN(fecha), MAX(fecha) FROM dim_tiempo") == [
        (90, date(2026, 1, 1), date(2026, 3, 31))]
    assert _q(con, """SELECT tiempo_id, anio, trimestre, mes, nombre_mes, semana_iso, dia_semana
                      FROM dim_tiempo WHERE fecha = DATE '2026-03-10'""") == [
        (20260310, 2026, 1, 3, "Marzo", 11, 2)]
    # la semana ISO cruza meses: 1-ene-2026 (jueves) cae en la semana 1
    assert _q(con, "SELECT semana_iso FROM dim_tiempo WHERE fecha = DATE '2026-01-01'") == [(1,)]
    assert _q(con, "SELECT COUNT(*) FROM dim_mes") == [(3,)]


def test_variedades_clasifican_en_genero_y_categoria(tmp_path):
    # variedades fuera de la tabla oficial: se prueban las reglas por nombre
    assert clasificar_variedad("Papa nueva")[1:] == ("Papa", "Tubérculos y raíces")
    assert clasificar_variedad("Papa blanca")[1:] == ("Papa", "Tubérculos y raíces")
    # "papa " no debe capturar a la papaya
    assert clasificar_variedad("Papaya")[1:] == ("Papaya", "Frutas")
    # el prefijo mas largo gana: grano verde vs grano seco
    assert clasificar_variedad("Frijol verde canario")[1] == "Frijol grano verde"
    assert clasificar_variedad("Frijol canario")[1] == "Frijol grano seco"
    assert clasificar_variedad("Aji panca")[1] == "Aji seco"
    assert clasificar_variedad("Aji rocoto")[1] == "Aji fresco"
    assert clasificar_variedad("Producto que no existe") == (None, SIN_CLASIFICAR, SIN_CLASIFICAR)


def test_el_codigo_oficial_de_cada_variedad_apunta_a_un_genero_del_catalogo():
    for variedad, codigo in CODIGOS_VARIEDAD.items():
        assert len(codigo) == 6 and codigo.isdigit(), variedad
        assert codigo[:4] in GENEROS, variedad
        assert clasificar_variedad(variedad)[0] == codigo[:4]


def test_las_reglas_por_nombre_coinciden_con_el_portal_en_todas_las_variedades():
    """Las reglas solo se usan para variedades nuevas, pero deben acertar en
    las 125 conocidas: si alguna regla se desvia, aqui se ve."""
    from sisap.dw.semillas import _CODIGO_DE_GENERO, _REGLAS_POR_LARGO
    for variedad, codigo in CODIGOS_VARIEDAD.items():
        genero = next(g for prefijo, g in _REGLAS_POR_LARGO if variedad.lower().startswith(prefijo))
        assert _CODIGO_DE_GENERO[genero] == codigo[:4], variedad


def test_provincias_apuntan_a_su_departamento_y_macrorregion(tmp_path):
    filas = (_tres_precios(1, 2, 3, region="Jaen") + _tres_precios(1, 2, 3, region="Cajamarca")
             + _tres_precios(1, 2, 3, region="Loreto"))
    con = construir_dw_en_memoria(_parquet(tmp_path, filas))

    assert _q(con, """SELECT nombre_region, tipo_region, departamento, macrorregion, codigo_sisap
                      FROM dim_region ORDER BY 1""") == [
        ("Cajamarca", "departamento", "Cajamarca", "Norte", "060000"),
        ("Jaen", "provincia", "Cajamarca", "Norte", "060801"),
        ("Loreto", "departamento", "Loreto", "Oriente", "160000"),
    ]


def test_producto_o_region_desconocidos_se_cargan_sin_clasificar_y_avisan(tmp_path):
    filas = _tres_precios(1, 2, 3, region="Region nueva", producto="Fruta exotica")
    ruta = _parquet(tmp_path, filas)
    con = duckdb.connect(":memory:")
    resultado = cargar_dw(con, ruta)

    assert len(resultado.advertencias) == 2  # variedad y region sin clasificar
    assert _q(con, "SELECT genero, categoria FROM dim_producto") == [(SIN_CLASIFICAR, SIN_CLASIFICAR)]
    assert _q(con, "SELECT tipo_region, macrorregion FROM dim_region") == [(SIN_CLASIFICAR, SIN_CLASIFICAR)]
    assert _q(con, "SELECT mensaje FROM etl_carga")[0][0] is not None


def test_semillas_coinciden_con_el_catalogo_del_portal_de_la_cli():
    assert GENEROS == {c: n for c, n in CATALOGO_PRODUCTOS.items()}
    assert {r[0]: r[1] for r in REGIONES} == {c: n for c, n in CATALOGO_REGIONES.items()}
    assert set(CATEGORIA_DE_GENERO) == set(GENEROS.values())
    assert set(CATEGORIA_DE_GENERO.values()) <= set(CATEGORIAS)


# --- Carga incremental, auditoria y rollback ----------------------------------------

def test_primera_carga_queda_auditada(tmp_path):
    ruta = _parquet(tmp_path, _serie_arroz(1.7))
    con = duckdb.connect(":memory:")
    resultado = cargar_dw(con, ruta)

    assert resultado.filas_leidas == 18
    assert resultado.filas_cargadas == 6  # 6 hechos (6 meses x 1 mercado)
    assert resultado.filas_atipicas == 3
    auditoria = _q(con, """SELECT carga_id, estado, filas_leidas, filas_cargadas, filas_eliminadas,
                                  filas_atipicas, fin >= inicio FROM etl_carga""")
    assert auditoria == [(1, "exitosa", 18, 6, 0, 3, True)]
    assert _q(con, "SELECT DISTINCT carga_id FROM fact_precio_mensual") == [(1,)]


def test_recargar_sin_cambios_no_toca_ninguna_fila(tmp_path):
    ruta = _parquet(tmp_path, _serie_arroz(172.0))
    con = duckdb.connect(":memory:")
    cargar_dw(con, ruta)
    antes = _q(con, "SELECT * FROM fact_precio_mensual ORDER BY ALL")

    segunda = cargar_dw(con, ruta)

    assert segunda.filas_cargadas == 0
    assert segunda.filas_eliminadas == 0
    assert _q(con, "SELECT * FROM fact_precio_mensual ORDER BY ALL") == antes
    assert _q(con, "SELECT carga_id, estado, filas_cargadas FROM etl_carga ORDER BY 1") == [
        (1, "exitosa", 6), (2, "exitosa", 0)]


def test_un_dato_nuevo_y_una_correccion_reciben_la_carga_nueva(tmp_path):
    ruta = _parquet(tmp_path, _serie_arroz(172.0))
    con = duckdb.connect(":memory:")
    cargar_dw(con, ruta)

    # el portal corrige abril (170 -> 171) y llega julio
    filas = _serie_arroz(172.0)
    for fila in filas:
        if fila["fecha"] == date(2024, 4, 1) and fila["variable"] == "may_precio_prom":
            fila["precio"] = 171.0
    filas += _tres_precios(168, 177, 180, region="Arequipa", producto="Arroz extra",
                           unidad="Saco", equivalencia_kg=50.0, fecha=date(2024, 7, 1))
    segunda = cargar_dw(con, _parquet(tmp_path, filas, "p2.parquet"))

    assert segunda.filas_cargadas == 2  # abril corregido + julio nuevo
    por_carga = _q(con, """SELECT m.numero_mes, f.carga_id FROM fact_precio_mensual f
                           JOIN dim_mes m USING (mes_id) ORDER BY 1""")
    assert por_carga == [(1, 1), (2, 1), (3, 1), (4, 2), (5, 1), (6, 1), (7, 2)]


def test_una_clave_que_desaparece_de_la_fuente_se_cuenta_como_eliminada(tmp_path):
    con = duckdb.connect(":memory:")
    cargar_dw(con, _parquet(tmp_path, _serie_arroz(172.0)))

    segunda = cargar_dw(con, _parquet(tmp_path, _serie_arroz(172.0)[:-3], "p2.parquet"))

    assert segunda.filas_eliminadas == 1
    assert _q(con, "SELECT COUNT(*) FROM fact_precio_mensual") == [(5,)]


def test_las_claves_sustitutas_no_cambian_cuando_aparece_un_miembro_nuevo(tmp_path):
    con = duckdb.connect(":memory:")
    cargar_dw(con, _parquet(tmp_path, _tres_precios(1, 2, 3, region="Lima")
                            + _tres_precios(1, 2, 3, region="Puno")))
    ids = dict(_q(con, "SELECT nombre_region, region_id FROM dim_region"))

    # "Amazonas" va primero en orden alfabetico, pero no debe desplazar a los demas
    cargar_dw(con, _parquet(tmp_path, _tres_precios(1, 2, 3, region="Lima")
                            + _tres_precios(1, 2, 3, region="Puno")
                            + _tres_precios(1, 2, 3, region="Amazonas"), "p2.parquet"))
    despues = dict(_q(con, "SELECT nombre_region, region_id FROM dim_region"))

    assert despues["Lima"] == ids["Lima"]
    assert despues["Puno"] == ids["Puno"]
    assert despues["Amazonas"] == max(ids.values()) + 1
    assert _q(con, "SELECT filas_cargadas FROM etl_carga WHERE carga_id = 2") == [(1,)]


def test_carga_fallida_revierte_todo_y_queda_auditada(tmp_path):
    ruta_db = tmp_path / "dw.duckdb"
    construir_dw(_parquet(tmp_path, _serie_arroz(172.0)), ruta_db)
    con = duckdb.connect(str(ruta_db), read_only=True)
    antes = _q(con, "SELECT * FROM fact_precio_mensual ORDER BY ALL")
    con.close()

    # el precio promedio viene en otra unidad que el minimo y el maximo de la
    # misma clave: no se puede pivotar a una sola fila con una sola unidad
    malo = [_fila(variable="may_precio_min", precio=1.0),
            _fila(variable="may_precio_prom", precio=2.0, unidad="Saco", equivalencia_kg=50.0),
            _fila(variable="may_precio_max", precio=3.0)]
    with pytest.raises(ErrorDeIntegridad, match="mas de una unidad"):
        construir_dw(_parquet(tmp_path, malo, "malo.parquet"), ruta_db)

    con = duckdb.connect(str(ruta_db), read_only=True)
    assert _q(con, "SELECT * FROM fact_precio_mensual ORDER BY ALL") == antes
    assert _q(con, "SELECT carga_id, estado FROM etl_carga ORDER BY 1") == [
        (1, "exitosa"), (2, "fallida")]
    assert "mas de una unidad" in _q(con, "SELECT mensaje FROM etl_carga WHERE carga_id = 2")[0][0]
    con.close()

    # y una carga buena posterior sigue funcionando (nada quedo a medias)
    construir_dw(_parquet(tmp_path, _serie_arroz(172.0)), ruta_db)


@pytest.mark.parametrize(("cambios", "mensaje"), [
    ({"precio": 2.0}, "repetidas"),  # se duplica abajo
])
def test_claves_duplicadas_en_la_fuente_fallan_fuerte(tmp_path, cambios, mensaje):
    ruta = _parquet(tmp_path, [_fila(), _fila(**cambios)])
    with pytest.raises(ErrorDeIntegridad, match=mensaje):
        construir_dw_en_memoria(ruta)


@pytest.mark.parametrize(("cambios", "mensaje"), [
    ({"variable": "xxx_precio_prom"}, "variable"),
    ({"equivalencia_kg": 0.0}, "equivalencia_kg"),
    ({"periodicidad": "semanal"}, "periodicidad"),
])
def test_datos_fuera_de_contrato_fallan_con_un_mensaje_claro(tmp_path, cambios, mensaje):
    with pytest.raises(ErrorDeIntegridad, match=mensaje):
        construir_dw_en_memoria(_parquet(tmp_path, [_fila(**cambios)]))


def test_nombres_hostiles_se_cargan_como_dato_sin_inyectar_sql(tmp_path):
    nombre = "Papa'; DROP TABLE fact_precio_mensual; --"
    con = construir_dw_en_memoria(_parquet(tmp_path, _tres_precios(1, 2, 3, producto=nombre)))

    assert _q(con, "SELECT COUNT(*) FROM fact_precio_mensual") == [(1,)]
    assert _q(con, "SELECT COUNT(*) FROM dim_producto WHERE nombre_variedad = ?", [nombre]) == [(1,)]


# --- CLI ------------------------------------------------------------------------------

def test_cli_construir_dw_y_cargas_dw(tmp_path, monkeypatch, capsys):
    _parquet(tmp_path, _serie_arroz(1.7), "precios.parquet")
    monkeypatch.setattr(cli, "PROCESSED_DIR", tmp_path)

    assert cli.cmd_construir_dw(None) == 0
    assert cli.cmd_construir_dw(None) == 0
    capsys.readouterr()
    assert cli.cmd_cargas_dw(type("Args", (), {"ultimas": 5})()) == 0
    salida = capsys.readouterr().out

    assert "#2" in salida
    assert "exitosa" in salida
    assert "cargadas=0" in salida


def test_cli_construir_dw_termina_con_error_si_la_carga_falla(tmp_path, monkeypatch, capsys):
    _parquet(tmp_path, [_fila(), _fila(precio=2.0)], "precios.parquet")
    monkeypatch.setattr(cli, "PROCESSED_DIR", tmp_path)

    assert cli.cmd_construir_dw(None) == 1
    assert "Carga fallida" in capsys.readouterr().out


# --- Contra los datos reales ------------------------------------------------------------

@pytest.fixture(scope="module")
def dw_real():
    return construir_dw_en_memoria(PARQUET_REAL)


def test_real_cuadra_con_la_fuente_fila_por_fila(dw_real):
    con = dw_real
    leidas = len(leer_precios(PARQUET_REAL))
    crudos = _q(con, """
        SELECT (SELECT COUNT(precio_min_crudo) + COUNT(precio_prom_crudo) + COUNT(precio_max_crudo)
                FROM fact_precio_diario)
             + (SELECT COUNT(precio_min_crudo) + COUNT(precio_prom_crudo) + COUNT(precio_max_crudo)
                FROM fact_precio_mensual WHERE fuente = 'oficial')""")[0][0]

    assert crudos == leidas
    assert _q(con, "SELECT filas_leidas, estado FROM etl_carga") == [(leidas, "exitosa")]


def test_real_anula_exactamente_lo_mismo_que_el_modelo_anterior(dw_real):
    """El rediseno cambia la forma (marca en vez de borrar), no el criterio:
    cada valor que el modelo anterior anulaba aqui queda con validado NULL y
    flag_atipico, y viceversa."""
    legado = construir_en_memoria(PARQUET_REAL)
    anulados_legado = _q(legado, """
        SELECT f.fecha, r.nombre_region, p.nombre_producto, v.codigo_variable, f.periodicidad
        FROM fact_precios f
        JOIN dim_region r USING (region_id) JOIN dim_producto p USING (producto_id)
        JOIN dim_variable v USING (variable_id)
        WHERE f.precio IS NULL ORDER BY ALL""")

    anulados_nuevo = _q(dw_real, """
        WITH hechos AS (
            SELECT t.fecha, r.nombre_region, p.nombre_variedad, 'dia' AS periodicidad,
                   m.nombre_mercado, f.precio_min, f.precio_prom, f.precio_max,
                   f.precio_min_crudo, f.precio_prom_crudo, f.precio_max_crudo
            FROM fact_precio_diario f JOIN dim_tiempo t USING (tiempo_id)
            JOIN dim_region r USING (region_id) JOIN dim_producto p USING (producto_id)
            JOIN dim_mercado m USING (mercado_id)
            UNION ALL
            SELECT t.fecha, r.nombre_region, p.nombre_variedad, 'mensual',
                   m.nombre_mercado, f.precio_min, f.precio_prom, f.precio_max,
                   f.precio_min_crudo, f.precio_prom_crudo, f.precio_max_crudo
            FROM fact_precio_mensual f JOIN dim_tiempo t ON t.tiempo_id = f.mes_id
            JOIN dim_region r USING (region_id) JOIN dim_producto p USING (producto_id)
            JOIN dim_mercado m USING (mercado_id) WHERE f.fuente = 'oficial'
        ), largo AS (
            SELECT fecha, nombre_region, nombre_variedad, periodicidad,
                   CASE WHEN nombre_mercado = 'Mayorista' THEN 'may_' ELSE 'min_' END
                       || 'precio_' || tipo AS variable,
                   CASE tipo WHEN 'min' THEN precio_min WHEN 'prom' THEN precio_prom
                             ELSE precio_max END AS validado,
                   CASE tipo WHEN 'min' THEN precio_min_crudo WHEN 'prom' THEN precio_prom_crudo
                             ELSE precio_max_crudo END AS crudo
            FROM hechos CROSS JOIN (VALUES ('min'), ('prom'), ('max')) AS tp(tipo)
        )
        SELECT fecha, nombre_region, nombre_variedad, variable, periodicidad
        FROM largo WHERE validado IS NULL AND crudo IS NOT NULL ORDER BY ALL""")

    assert len(anulados_legado) > 0  # la comparacion no puede ser vacia
    assert anulados_nuevo == anulados_legado


def test_real_las_jerarquias_estan_completas(dw_real):
    con = dw_real
    assert _q(con, "SELECT COUNT(*) FROM dim_producto WHERE genero = ? OR categoria = ?",
              [SIN_CLASIFICAR, SIN_CLASIFICAR]) == [(0,)]
    assert _q(con, "SELECT COUNT(*) FROM dim_region WHERE macrorregion = ?", [SIN_CLASIFICAR]) == [(0,)]
    assert {c for (c,) in _q(con, "SELECT DISTINCT categoria FROM dim_producto")} <= set(CATEGORIAS)
    assert {m for (m,) in _q(con, "SELECT DISTINCT macrorregion FROM dim_region")} <= set(MACRORREGIONES)
    assert {g for (g,) in _q(con, "SELECT DISTINCT genero FROM dim_producto")} <= set(GENEROS.values())
    assert _q(con, "SELECT COUNT(*) FROM dim_region WHERE tipo_region = 'provincia'") == [(3,)]
    assert _q(con, "SELECT COUNT(*) FROM dim_producto WHERE codigo_variedad IS NULL") == [(0,)]
    assert _q(con, "SELECT codigo_variedad, genero FROM dim_producto "
                   "WHERE nombre_variedad = 'Aji panca'") == [("020304", "Aji seco")]
    assert _q(con, "SELECT filas_atipicas FROM etl_carga")[0][0] > 0
    assert _q(con, "SELECT mensaje FROM etl_carga") == [(None,)]  # sin advertencias


def test_variedad_nueva_sin_codigo_oficial_se_clasifica_por_nombre_y_avisa(tmp_path):
    con = duckdb.connect(":memory:")
    resultado = cargar_dw(con, _parquet(tmp_path, _tres_precios(1, 2, 3, producto="Papa nativa")))

    assert _q(con, "SELECT codigo_variedad, genero FROM dim_producto") == [(None, "Papa")]
    assert any("sin codigo oficial" in aviso for aviso in resultado.advertencias)
