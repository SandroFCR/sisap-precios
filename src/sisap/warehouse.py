from pathlib import Path

import duckdb

NOMBRES_MES_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio",
    7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre",
    12: "Diciembre",
}


def construir_modelo_dimensional(ruta_parquet: Path, ruta_duckdb: Path) -> None:
    """Reconstruye el modelo dimensional (fact_precios + dim_*) desde cero,
    leyendo el parquet historico. Es una operacion idempotente: se puede
    correr las veces que haga falta, nunca hay que 'migrar' el esquema a
    mano, porque siempre se reconstruye completo desde la fuente de verdad
    (el parquet).
    """
    con = duckdb.connect(str(ruta_duckdb))

    # capa de staging: el parquet tal cual, sin modelar. Separarla de las
    # tablas finales deja claro que no es parte del esquema en estrella.
    con.execute(
        f"CREATE OR REPLACE TABLE staging_precios AS "
        f"SELECT * FROM read_parquet('{ruta_parquet.as_posix()}')"
    )

    con.execute("""
        CREATE OR REPLACE TABLE dim_producto AS
        SELECT
            ROW_NUMBER() OVER (ORDER BY producto) AS producto_id,
            producto AS nombre_producto
        FROM (SELECT DISTINCT producto FROM staging_precios)
    """)

    con.execute("""
        CREATE OR REPLACE TABLE dim_region AS
        SELECT
            ROW_NUMBER() OVER (ORDER BY region) AS region_id,
            region AS nombre_region
        FROM (SELECT DISTINCT region FROM staging_precios)
    """)

    con.execute("""
        CREATE OR REPLACE TABLE dim_variable AS
        SELECT
            ROW_NUMBER() OVER (ORDER BY variable) AS variable_id,
            variable AS codigo_variable,
            CASE
                WHEN variable LIKE 'may_%' THEN 'Mayorista'
                WHEN variable LIKE 'min_%' THEN 'Minorista'
                ELSE 'Desconocido'
            END AS tipo_mercado,
            CASE
                WHEN variable LIKE '%_min' THEN 'Minimo'
                WHEN variable LIKE '%_prom' THEN 'Promedio'
                WHEN variable LIKE '%_max' THEN 'Maximo'
                ELSE 'Desconocido'
            END AS tipo_precio
        FROM (SELECT DISTINCT variable FROM staging_precios)
    """)

    casos_mes = " ".join(
        f"WHEN {mes} THEN '{nombre}'" for mes, nombre in NOMBRES_MES_ES.items()
    )
    con.execute(f"""
        CREATE OR REPLACE TABLE dim_fecha AS
        SELECT
            fecha,
            EXTRACT(year FROM fecha) AS anio,
            EXTRACT(month FROM fecha) AS mes,
            EXTRACT(day FROM fecha) AS dia,
            EXTRACT(quarter FROM fecha) AS trimestre,
            CASE EXTRACT(month FROM fecha) {casos_mes} END AS nombre_mes
        FROM (SELECT DISTINCT fecha FROM staging_precios)
    """)

    con.execute("""
        CREATE OR REPLACE TABLE fact_precios AS
        SELECT
            p.fecha,
            r.region_id,
            pr.producto_id,
            v.variable_id,
            p.unidad,
            p.equivalencia_kg,
            p.precio
        FROM staging_precios p
        JOIN dim_region r ON p.region = r.nombre_region
        JOIN dim_producto pr ON p.producto = pr.nombre_producto
        JOIN dim_variable v ON p.variable = v.codigo_variable
    """)

    _validar_integridad(con)
    con.close()


def _validar_integridad(con: duckdb.DuckDBPyConnection) -> None:
    """Si un JOIN pierde filas (nombre con espacio extra, etc.) o las
    multiplica (dimension con valores duplicados), fact_precios deja de
    tener exactamente 1 fila por cada fila de staging_precios. Mejor fallar
    aca que publicar un esquema en estrella silenciosamente roto."""
    filas_staging = con.execute("SELECT COUNT(*) FROM staging_precios").fetchone()[0]
    filas_hechos = con.execute("SELECT COUNT(*) FROM fact_precios").fetchone()[0]
    assert filas_staging == filas_hechos, (
        f"fact_precios tiene {filas_hechos} filas pero staging_precios tenia "
        f"{filas_staging}: algun JOIN esta perdiendo o multiplicando filas."
    )
