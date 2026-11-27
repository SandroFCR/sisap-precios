"""Las consultas del dashboard sobre el perfil publico (sisap.dw.consultas)
deben dar los mismos numeros que el modelo anterior (sisap.warehouse), salvo
la unica diferencia documentada: un mes cuyo agregado oficial es atipico ya
no se rellena con el promedio de los dias."""

import re
from pathlib import Path

import pytest

from sisap.dw import construir_dw_en_memoria
from sisap.dw.consultas import crear_macros_publicas
from sisap.warehouse import construir_en_memoria

PARQUET_REAL = Path(__file__).parent.parent / "data" / "processed" / "precios.parquet"
DASHBOARD = Path(__file__).parent.parent / "src" / "sisap" / "dashboard.py"
TIPOS = ["Minimo", "Promedio", "Maximo"]


@pytest.fixture(scope="module")
def modelos():
    nuevo = construir_dw_en_memoria(PARQUET_REAL)
    crear_macros_publicas(nuevo)
    legado = construir_en_memoria(PARQUET_REAL)
    return nuevo, legado


def _legado(legado, sql):
    return legado.execute(sql).to_arrow_table()


@pytest.mark.parametrize("tipo", TIPOS)
def test_serie_publica_es_identica_al_fact_precios_anterior(modelos, tipo):
    nuevo, legado = modelos
    nuevo.register("leg", _legado(legado, """
        SELECT f.fecha, f.periodicidad, r.nombre_region AS region, p.nombre_producto AS variedad,
               v.tipo_mercado AS mercado, v.tipo_precio, f.unidad, f.precio / f.equivalencia_kg AS kg
        FROM fact_precios f JOIN dim_region r USING (region_id)
        JOIN dim_producto p USING (producto_id) JOIN dim_variable v USING (variable_id)
        WHERE f.precio IS NOT NULL"""))
    try:
        diferencias = nuevo.execute("""
            WITH l AS (SELECT * EXCLUDE (tipo_precio) FROM leg WHERE tipo_precio = ?),
                 n AS (SELECT fecha, periodicidad, region, variedad, mercado, unidad, precio_kg AS kg
                       FROM serie_publica(?) WHERE precio_kg IS NOT NULL)
            SELECT (SELECT COUNT(*) FROM l), (SELECT COUNT(*) FROM n),
                   (SELECT COUNT(*) FROM l FULL JOIN n USING (fecha, periodicidad, region, variedad, mercado)
                    WHERE l.kg IS NULL OR n.kg IS NULL OR abs(l.kg - n.kg) > 1e-9 OR l.unidad <> n.unidad)
        """, [tipo, tipo]).fetchone()
    finally:
        nuevo.unregister("leg")
    filas_legado, filas_nuevo, distintas = diferencias
    assert filas_legado == filas_nuevo > 0
    assert distintas == 0


def test_mensual_publico_coincide_salvo_el_caso_documentado(modelos):
    nuevo, legado = modelos
    nuevo.register("leg", _legado(legado, """
        SELECT m.mes, r.nombre_region AS region, p.nombre_producto AS variedad,
               v.tipo_mercado AS mercado, v.tipo_precio, m.precio_kg AS kg, m.fuente
        FROM fact_precios_mensual m JOIN dim_region r USING (region_id)
        JOIN dim_producto p USING (producto_id) JOIN dim_variable v USING (variable_id)"""))
    try:
        resultado = nuevo.execute("""
            WITH n AS (
                SELECT 'Minimo' AS tipo_precio, * FROM mensual_publico('Minimo')
                UNION ALL SELECT 'Promedio', * FROM mensual_publico('Promedio')
                UNION ALL SELECT 'Maximo', * FROM mensual_publico('Maximo'))
            SELECT
                (SELECT COUNT(*) FROM leg),
                (SELECT COUNT(*) FROM leg l JOIN n USING (mes, region, variedad, mercado, tipo_precio)
                 WHERE abs(l.kg - n.precio_kg) < 1e-6 AND l.fuente = n.fuente),
                (SELECT list(l.fuente) FROM leg l JOIN n USING (mes, region, variedad, mercado, tipo_precio)
                 WHERE n.precio_kg IS NULL),
                (SELECT COUNT(*) FROM n LEFT JOIN leg l USING (mes, region, variedad, mercado, tipo_precio)
                 WHERE l.kg IS NULL AND n.precio_kg IS NOT NULL)
        """).fetchone()
    finally:
        nuevo.unregister("leg")
    total, iguales, legado_relleno_con_dias, solo_en_nuevo = resultado
    # todo lo que el legado mostraba y el nuevo no, era relleno con dias de un
    # mes cuyo agregado oficial es atipico (hoy, 1 de ~634 mil)
    assert iguales + len(legado_relleno_con_dias) == total
    assert set(legado_relleno_con_dias) <= {"dia"}
    assert len(legado_relleno_con_dias) <= 5
    assert solo_en_nuevo == 0


def test_el_dashboard_solo_consulta_el_perfil_publico():
    """El dashboard es publico: ninguna consulta suya toca las tablas base,
    otros perfiles ni la auditoria."""
    codigo = DASHBOARD.read_text(encoding="utf-8")
    prohibido = re.findall(
        r"\b(fact_precio\w*|dim_\w+|etl_carga|analista\.|auditor\.|regional\.|_crudo|flag_atipico|carga_id)",
        "\n".join(linea for linea in codigo.splitlines() if not linea.lstrip().startswith("#")),
    )
    assert prohibido == []
