from datetime import date

import duckdb
import pandas as pd

from sisap.warehouse import construir_modelo_dimensional


def test_construir_modelo_dimensional(tmp_path):
    filas = [
        {
            "fecha": date(2026, 1, 1), "region": "Lima", "producto": "Yuca amarilla",
            "unidad": "Kilogramo", "equivalencia_kg": 1.0,
            "variable": "may_precio_prom", "precio": 1.35,
        },
        {
            "fecha": date(2026, 1, 1), "region": "Lima", "producto": "Yuca amarilla",
            "unidad": "Kilogramo", "equivalencia_kg": 1.0,
            "variable": "min_precio_max", "precio": 2.10,
        },
        {
            "fecha": date(2026, 1, 1), "region": "Puno", "producto": "Papa blanca",
            "unidad": "Kilogramo", "equivalencia_kg": 1.0,
            "variable": "may_precio_prom", "precio": 0.75,
        },
    ]
    ruta_parquet = tmp_path / "precios.parquet"
    pd.DataFrame(filas).to_parquet(ruta_parquet, index=False)
    ruta_duckdb = tmp_path / "sisap.duckdb"

    construir_modelo_dimensional(ruta_parquet, ruta_duckdb)

    con = duckdb.connect(str(ruta_duckdb), read_only=True)

    assert con.execute("SELECT COUNT(*) FROM fact_precios").fetchone()[0] == len(filas)
    assert con.execute("SELECT COUNT(*) FROM dim_producto").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM dim_region").fetchone()[0] == 2

    tipo_min_max = con.execute(
        "SELECT tipo_mercado, tipo_precio FROM dim_variable WHERE codigo_variable = 'min_precio_max'"
    ).fetchone()
    assert tipo_min_max == ("Minorista", "Maximo")

    precio_yuca = con.execute("""
        SELECT f.precio FROM fact_precios f
        JOIN dim_producto p ON f.producto_id = p.producto_id
        JOIN dim_variable v ON f.variable_id = v.variable_id
        WHERE p.nombre_producto = 'Yuca amarilla' AND v.codigo_variable = 'may_precio_prom'
    """).fetchone()[0]
    assert precio_yuca == 1.35

    con.close()
