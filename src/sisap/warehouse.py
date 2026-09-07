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
        WITH base AS (
            SELECT
                p.fecha, r.region_id, pr.producto_id, v.variable_id,
                p.unidad, p.equivalencia_kg, p.precio,
                p.precio / p.equivalencia_kg AS precio_kg
            FROM staging_precios p
            JOIN dim_region r ON p.region = r.nombre_region
            JOIN dim_producto pr ON p.producto = pr.nombre_producto
            JOIN dim_variable v ON p.variable = v.codigo_variable
        ),
        con_vecinos AS (
            SELECT *,
                LAG(precio_kg) OVER serie AS precio_kg_anterior,
                LEAD(precio_kg) OVER serie AS precio_kg_siguiente,
                LAG(equivalencia_kg) OVER serie AS equiv_anterior,
                LEAD(equivalencia_kg) OVER serie AS equiv_siguiente
            FROM base
            WINDOW serie AS (
                PARTITION BY region_id, producto_id, variable_id ORDER BY fecha
            )
        ),
        evaluado AS (
            SELECT *,
                -- si la unidad de este mes no coincide con la de los vecinos,
                -- probamos "que precio_kg daria si este precio se hubiera
                -- reportado con la unidad de los vecinos". Revela si el
                -- numero crudo ya esta en esa escala (evidencia de que el
                -- campo UNIDAD es el error, no el precio).
                precio / NULLIF(equiv_anterior, 0) AS precio_kg_si_fuera_unidad_vecina
            FROM con_vecinos
        )
        SELECT
            fecha, region_id, producto_id, variable_id, unidad, equivalencia_kg,
            -- Un mes se anula cuando sus dos vecinos concuerdan entre si
            -- (dentro del 20%, señal de que representan el nivel real y no
            -- estan en medio de su propio cambio de tendencia) Y el precio de
            -- este mes se aleja fuerte de ese nivel (menos del 40% o mas del
            -- 250%) bajo alguna de estas dos lecturas:
            --   (a) typo numerico clasico: la unidad coincide con los
            --       vecinos, pero el numero esta mal (ej. "1.70" en vez de
            --       "170.00", Arroz extra/Arequipa nov-2024; o "49" en vez de
            --       "149", Leche/Lima may-2026).
            --   (b) etiqueta de unidad mal puesta: la unidad de este mes NO
            --       coincide con los vecinos, la lectura CON SU PROPIA unidad
            --       ya se ve tan extrema como en (a), Y ademas el precio
            --       crudo, leido con LA UNIDAD DE LOS VECINOS, cae perfecto
            --       dentro de lo normal -- ej. Huevos rosados/Ayacucho
            --       mayo-2023 se reporto como "10.00" con unidad Bandeja (23
            --       kg) en medio de un tramo en Kilogramo de ~9.3-9.5, dando
            --       un precio/kg absurdo de 0.43; leido como Kilogramo (la
            --       unidad real de sus vecinos) el mismo "10.00" encaja
            --       perfecto en la tendencia. Exigir AMBAS cosas (no solo la
            --       segunda) importa: Yuca blanca/Jaen alterna legitimamente
            --       entre "Saco" (70 kg) y "Saco mediano" (85 kg) mes a mes,
            --       y con cualquiera de las dos unidades el precio/kg cae
            --       dentro del rango 0.4x-2.5x de sus vecinos (el precio
            --       sube suave con el tiempo) -- sin el requisito de que la
            --       PROPIA lectura sea extrema primero, esa alternancia
            --       normal se anulaba por error.
            --
            --       Un primer intento de esta regla exigia la MISMA unidad
            --       en los 3 meses para evitar falsos positivos -- pero eso
            --       terminaba exentando exactamente el patron de arriba
            --       (encontrado tambien en Huevos rosados/Amazonas y Haba
            --       verde criolla/Amazonas, siempre con la misma firma: el
            --       numero crudo no cambia, solo la unidad declarada).
            -- Si NINGUNA de las dos lecturas calza con la tendencia, se deja
            -- el valor tal cual: no hay base para asumir que esta mal.
            -- El valor crudo se conserva intacto en staging_precios siempre.
            CASE
                WHEN precio_kg IS NOT NULL
                 AND precio_kg_anterior IS NOT NULL AND precio_kg_siguiente IS NOT NULL
                 AND equiv_anterior = equiv_siguiente
                 AND ABS(precio_kg_anterior - precio_kg_siguiente)
                     < 0.2 * LEAST(precio_kg_anterior, precio_kg_siguiente)
                 AND (precio_kg < 0.4 * LEAST(precio_kg_anterior, precio_kg_siguiente)
                      OR precio_kg > 2.5 * GREATEST(precio_kg_anterior, precio_kg_siguiente))
                 AND (
                     equivalencia_kg = equiv_anterior
                     OR (precio_kg_si_fuera_unidad_vecina >= 0.4 * LEAST(precio_kg_anterior, precio_kg_siguiente)
                         AND precio_kg_si_fuera_unidad_vecina <= 2.5 * GREATEST(precio_kg_anterior, precio_kg_siguiente))
                 )
                THEN NULL
                ELSE precio
            END AS precio
        FROM evaluado
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
