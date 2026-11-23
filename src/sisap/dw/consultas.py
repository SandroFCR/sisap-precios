"""Consultas reutilizables sobre el perfil publico del data warehouse.

Las usa el dashboard (una aplicacion publica: solo lee vistas `publico.*`).
Son macros de tabla de DuckDB: el tipo de precio entra como parametro
(`FROM serie_publica(?)`), asi ninguna consulta interpola columnas ni valores.
"""

import duckdb

SQL_MACROS = [
    # serie_publica: el dato como se publico -- el diario y el agregado
    # mensual OFICIAL de SISAP, cada uno con su periodicidad
    """
    CREATE OR REPLACE MACRO serie_publica(tipo_precio) AS TABLE
    WITH serie AS (
        SELECT fecha, 'dia' AS periodicidad, region, variedad, mercado, unidad,
               equivalencia_kg, precio_min, precio_prom, precio_max
        FROM publico.precios_diarios
        UNION ALL
        SELECT mes, 'mensual', region, variedad, mercado, unidad,
               equivalencia_kg, precio_min, precio_prom, precio_max
        FROM publico.precios_mensuales WHERE fuente = 'oficial'
    )
    SELECT *, CASE tipo_precio WHEN 'Minimo' THEN precio_min
                               WHEN 'Promedio' THEN precio_prom
                               ELSE precio_max END / equivalencia_kg AS precio_kg
    FROM serie
    """,
    # mensual_publico: un valor por mes (hecho mensual) -- el oficial y, para
    # los meses que aun no lo tienen, el promedio de los dias. `fuente` usa las
    # etiquetas del resto de la pagina ('mensual' = oficial, 'dia' = promedio)
    """
    CREATE OR REPLACE MACRO mensual_publico(tipo_precio) AS TABLE
    SELECT mes, region, variedad, mercado,
           CASE fuente WHEN 'oficial' THEN 'mensual' ELSE 'dia' END AS fuente,
           CASE tipo_precio WHEN 'Minimo' THEN precio_min
                            WHEN 'Promedio' THEN precio_prom
                            ELSE precio_max END / equivalencia_kg AS precio_kg
    FROM publico.precios_mensuales
    """,
]


def crear_macros_publicas(con: duckdb.DuckDBPyConnection) -> None:
    for sentencia in SQL_MACROS:
        con.execute(sentencia)
