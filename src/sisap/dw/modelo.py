"""Esquema del data warehouse y SQL de la transformacion.

Todo el SQL es fijo: ningun valor se pega dentro de una consulta (los
valores van como parametros o como DataFrames registrados), igual que en
sisap.warehouse (tests/test_seguridad.py lo vigila).

Decisiones de diseno:
- Sin PRIMARY KEY ni FOREIGN KEY: la carga reemplaza el contenido de las
  tablas dentro de una transaccion, y DuckDB no deja borrar filas
  referenciadas por una FK ni reinsertar la misma PK en la misma
  transaccion. La integridad (grano unico, FKs sin huerfanos, cuadre con
  la fuente) la verifica VALIDACIONES despues de cada carga.
- Claves sustitutas estables: una dimension conserva el id de sus miembros
  entre cargas; los nuevos reciben max(id)+1.
"""

# --- Esquema ---------------------------------------------------------------

DDL: dict[str, str] = {
    "etl_carga": """
        CREATE TABLE IF NOT EXISTS etl_carga (
            carga_id INTEGER, inicio TIMESTAMP, fin TIMESTAMP,
            filas_leidas BIGINT, filas_cargadas BIGINT, filas_eliminadas BIGINT,
            filas_atipicas BIGINT, estado VARCHAR, mensaje VARCHAR
        )""",
    "dim_tiempo": """
        CREATE TABLE IF NOT EXISTS dim_tiempo (
            tiempo_id INTEGER, fecha DATE, dia INTEGER, dia_semana INTEGER,
            semana_iso INTEGER, anio_iso INTEGER, mes INTEGER, nombre_mes VARCHAR,
            trimestre INTEGER, anio INTEGER, es_primer_dia_mes BOOLEAN
        )""",
    "dim_region": """
        CREATE TABLE IF NOT EXISTS dim_region (
            region_id INTEGER, codigo_sisap VARCHAR, nombre_region VARCHAR,
            tipo_region VARCHAR, departamento VARCHAR, macrorregion VARCHAR
        )""",
    "dim_producto": """
        CREATE TABLE IF NOT EXISTS dim_producto (
            producto_id INTEGER, codigo_variedad VARCHAR, nombre_variedad VARCHAR,
            codigo_genero VARCHAR,
            genero VARCHAR, categoria VARCHAR
        )""",
    "dim_unidad": """
        CREATE TABLE IF NOT EXISTS dim_unidad (
            unidad_id INTEGER, nombre_unidad VARCHAR, equivalencia_kg DOUBLE
        )""",
    "dim_mercado": """
        CREATE TABLE IF NOT EXISTS dim_mercado (
            mercado_id INTEGER, nombre_mercado VARCHAR
        )""",
    "fact_precio_diario": """
        CREATE TABLE IF NOT EXISTS fact_precio_diario (
            tiempo_id INTEGER, region_id INTEGER, producto_id INTEGER,
            mercado_id INTEGER, unidad_id INTEGER, carga_id INTEGER,
            precio_min DOUBLE, precio_prom DOUBLE, precio_max DOUBLE,
            precio_min_crudo DOUBLE, precio_prom_crudo DOUBLE, precio_max_crudo DOUBLE,
            precio_kg_prom DOUBLE, flag_atipico BOOLEAN
        )""",
    "fact_precio_mensual": """
        CREATE TABLE IF NOT EXISTS fact_precio_mensual (
            mes_id INTEGER, region_id INTEGER, producto_id INTEGER,
            mercado_id INTEGER, unidad_id INTEGER, carga_id INTEGER,
            precio_min DOUBLE, precio_prom DOUBLE, precio_max DOUBLE,
            precio_min_crudo DOUBLE, precio_prom_crudo DOUBLE, precio_max_crudo DOUBLE,
            precio_kg_prom DOUBLE, flag_atipico BOOLEAN,
            fuente VARCHAR, n_dias INTEGER
        )""",
}

# dim_mes: la dimension de tiempo en su rol "mes" (el dia 1 de cada mes) para
# el hecho mensual
VISTA_DIM_MES = """
    CREATE OR REPLACE VIEW dim_mes AS
    SELECT tiempo_id AS mes_id, fecha AS mes, mes AS numero_mes, nombre_mes,
           trimestre, anio
    FROM dim_tiempo WHERE es_primer_dia_mes
"""

SQL_MERCADOS = "INSERT INTO dim_mercado VALUES (1, 'Mayorista'), (2, 'Minorista')"

# --- Staging y verificaciones previas ---------------------------------------

SQL_STAGING = """
    CREATE OR REPLACE TEMP TABLE stg_precios AS
    SELECT CAST(fecha AS DATE) AS fecha, region, producto, unidad,
           CAST(equivalencia_kg AS DOUBLE) AS equivalencia_kg, variable,
           CAST(precio AS DOUBLE) AS precio, periodicidad,
           CAST(fecha_captura AS DATE) AS fecha_captura
    FROM precios_origen
"""

# (descripcion, SQL que cuenta los problemas): si alguno es > 0, la carga falla
VERIFICACIONES_PREVIAS: list[tuple[str, str]] = [
    ("claves (fecha, region, producto, variable, periodicidad) repetidas en la fuente", """
        SELECT COUNT(*) FROM (
            SELECT 1 FROM stg_precios
            GROUP BY fecha, region, producto, variable, periodicidad HAVING COUNT(*) > 1)"""),
    ("filas con una variable que no es may_/min_ + _min/_prom/_max", """
        SELECT COUNT(*) FROM stg_precios
        WHERE NOT (starts_with(variable, 'may_') OR starts_with(variable, 'min_'))
           OR NOT (ends_with(variable, '_min') OR ends_with(variable, '_prom')
                   OR ends_with(variable, '_max'))"""),
    ("filas con periodicidad distinta de 'dia' o 'mensual'", """
        SELECT COUNT(*) FROM stg_precios WHERE periodicidad NOT IN ('dia', 'mensual')"""),
    ("filas con equivalencia_kg no positiva o no finita", """
        SELECT COUNT(*) FROM stg_precios
        WHERE equivalencia_kg IS NULL OR NOT isfinite(equivalencia_kg) OR equivalencia_kg <= 0"""),
    ("filas con precio nulo o no positivo", """
        SELECT COUNT(*) FROM stg_precios
        WHERE precio IS NULL OR NOT isfinite(precio) OR precio <= 0"""),
]

# --- Deteccion de atipicos ---------------------------------------------------
# La MISMA regla de sisap.warehouse (ver ahi el razonamiento completo y los
# casos reales que la motivaron), con una diferencia de diseno: el modelo
# anterior ANULABA el precio; aqui se MARCA (es_atipico) y el crudo se
# conserva en el hecho. tests/test_dw.py comprueba que anula exactamente los
# mismos valores que el modelo anterior.
#
# En una serie (region, producto, variable, periodicidad) un valor es atipico
# cuando sus dos vecinos concuerdan entre si (dentro del 20%) y el precio por
# kg de este valor se aleja fuerte de ese nivel (menos del 40% o mas del
# 250%) bajo una de dos lecturas: (a) typo numerico con la misma unidad que
# los vecinos, o (b) etiqueta de unidad mal puesta: la unidad no coincide con
# la de los vecinos, la lectura propia ya es extrema y, leido con la unidad de
# los vecinos, el precio crudo encaja perfecto en la tendencia.
SQL_EVALUAR_ATIPICOS = """
    CREATE OR REPLACE TEMP TABLE stg_eval AS
    WITH base AS (
        SELECT *, precio / equivalencia_kg AS precio_kg FROM stg_precios
    ),
    con_vecinos AS (
        SELECT *,
            LAG(precio_kg) OVER serie AS precio_kg_anterior,
            LEAD(precio_kg) OVER serie AS precio_kg_siguiente,
            LAG(equivalencia_kg) OVER serie AS equiv_anterior,
            LEAD(equivalencia_kg) OVER serie AS equiv_siguiente
        FROM base
        WINDOW serie AS (
            PARTITION BY region, producto, variable, periodicidad ORDER BY fecha
        )
    ),
    evaluado AS (
        SELECT *, precio / NULLIF(equiv_anterior, 0) AS precio_kg_si_fuera_unidad_vecina
        FROM con_vecinos
    )
    SELECT fecha, region, producto, unidad, equivalencia_kg, variable, precio,
           periodicidad, fecha_captura,
           COALESCE(
               precio_kg IS NOT NULL
               AND precio_kg_anterior IS NOT NULL AND precio_kg_siguiente IS NOT NULL
               AND equiv_anterior = equiv_siguiente
               AND ABS(precio_kg_anterior - precio_kg_siguiente)
                   < 0.2 * LEAST(precio_kg_anterior, precio_kg_siguiente)
               AND (precio_kg < 0.4 * LEAST(precio_kg_anterior, precio_kg_siguiente)
                    OR precio_kg > 2.5 * GREATEST(precio_kg_anterior, precio_kg_siguiente))
               AND (
                   equivalencia_kg = equiv_anterior
                   OR (precio_kg_si_fuera_unidad_vecina
                           >= 0.4 * LEAST(precio_kg_anterior, precio_kg_siguiente)
                       AND precio_kg_si_fuera_unidad_vecina
                           <= 2.5 * GREATEST(precio_kg_anterior, precio_kg_siguiente))
               ),
               FALSE
           ) AS es_atipico
    FROM evaluado
"""

# --- Pivot: 6 variables en filas -> 3 precios en columnas por mercado -------

SQL_CLAVES = """
    CREATE OR REPLACE TEMP TABLE hecho_base AS
    SELECT
        fecha, region, producto, periodicidad,
        CASE WHEN starts_with(variable, 'may_') THEN 'Mayorista' ELSE 'Minorista' END AS mercado,
        MIN(unidad) AS unidad,
        MIN(equivalencia_kg) AS equivalencia_kg,
        COUNT(DISTINCT unidad || '|' || CAST(equivalencia_kg AS VARCHAR)) AS n_unidades,
        COUNT(*) AS n_variables,
        MAX(CASE WHEN NOT es_atipico THEN precio END)
            FILTER (WHERE ends_with(variable, '_min')) AS precio_min,
        MAX(CASE WHEN NOT es_atipico THEN precio END)
            FILTER (WHERE ends_with(variable, '_prom')) AS precio_prom,
        MAX(CASE WHEN NOT es_atipico THEN precio END)
            FILTER (WHERE ends_with(variable, '_max')) AS precio_max,
        MAX(precio) FILTER (WHERE ends_with(variable, '_min')) AS precio_min_crudo,
        MAX(precio) FILTER (WHERE ends_with(variable, '_prom')) AS precio_prom_crudo,
        MAX(precio) FILTER (WHERE ends_with(variable, '_max')) AS precio_max_crudo,
        BOOL_OR(es_atipico) AS flag_atipico
    FROM stg_eval
    GROUP BY ALL
"""

SQL_CLAVES_CON_VARIAS_UNIDADES = "SELECT COUNT(*) FROM hecho_base WHERE n_unidades > 1"
SQL_CLAVES_INCOMPLETAS = "SELECT COUNT(*) FROM hecho_base WHERE n_variables < 3"

# --- Dimensiones: miembros de los datos + atributos de las semillas ----------
# Cada una conserva el id previo de sus miembros y numera a los nuevos.

SQL_DIM_REGION_NUEVA = """
    CREATE OR REPLACE TEMP TABLE dim_region_nueva AS
    SELECT
        COALESCE(o.region_id,
                 (SELECT COALESCE(MAX(region_id), 0) FROM dim_region)
                 + ROW_NUMBER() OVER (PARTITION BY o.region_id IS NULL ORDER BY d.nombre_region)
        ) AS region_id,
        s.codigo_sisap, d.nombre_region, s.tipo_region, s.departamento, s.macrorregion
    FROM (SELECT DISTINCT region AS nombre_region FROM stg_precios) d
    JOIN semilla_region s ON s.nombre_region = d.nombre_region
    LEFT JOIN dim_region o ON o.nombre_region = d.nombre_region
"""

SQL_DIM_PRODUCTO_NUEVA = """
    CREATE OR REPLACE TEMP TABLE dim_producto_nueva AS
    SELECT
        COALESCE(o.producto_id,
                 (SELECT COALESCE(MAX(producto_id), 0) FROM dim_producto)
                 + ROW_NUMBER() OVER (PARTITION BY o.producto_id IS NULL ORDER BY d.nombre_variedad)
        ) AS producto_id,
        s.codigo_variedad, d.nombre_variedad, s.codigo_genero, s.genero, s.categoria
    FROM (SELECT DISTINCT producto AS nombre_variedad FROM stg_precios) d
    JOIN semilla_producto s ON s.nombre_variedad = d.nombre_variedad
    LEFT JOIN dim_producto o ON o.nombre_variedad = d.nombre_variedad
"""

# la unidad se identifica por (nombre, equivalencia): "Saco" son 50 kg para el
# arroz y 70 kg para la yuca
SQL_DIM_UNIDAD_NUEVA = """
    CREATE OR REPLACE TEMP TABLE dim_unidad_nueva AS
    SELECT
        COALESCE(o.unidad_id,
                 (SELECT COALESCE(MAX(unidad_id), 0) FROM dim_unidad)
                 + ROW_NUMBER() OVER (
                       PARTITION BY o.unidad_id IS NULL
                       ORDER BY d.nombre_unidad, d.equivalencia_kg)
        ) AS unidad_id,
        d.nombre_unidad, d.equivalencia_kg
    FROM (SELECT DISTINCT unidad AS nombre_unidad, equivalencia_kg FROM stg_precios) d
    LEFT JOIN dim_unidad o
        ON o.nombre_unidad = d.nombre_unidad AND o.equivalencia_kg = d.equivalencia_kg
"""

# calendario completo (todos los dias de los meses cubiertos), no solo las
# fechas con dato; los nombres de mes van como parametro
SQL_DIM_TIEMPO_NUEVA = """
    CREATE OR REPLACE TEMP TABLE dim_tiempo_nueva AS
    SELECT
        CAST(strftime(d.fecha, '%Y%m%d') AS INTEGER) AS tiempo_id,
        d.fecha,
        CAST(EXTRACT(day FROM d.fecha) AS INTEGER) AS dia,
        CAST(EXTRACT(isodow FROM d.fecha) AS INTEGER) AS dia_semana,
        CAST(EXTRACT(week FROM d.fecha) AS INTEGER) AS semana_iso,
        CAST(EXTRACT(isoyear FROM d.fecha) AS INTEGER) AS anio_iso,
        CAST(EXTRACT(month FROM d.fecha) AS INTEGER) AS mes,
        list_extract(CAST(? AS VARCHAR[]), CAST(EXTRACT(month FROM d.fecha) AS INTEGER))
            AS nombre_mes,
        CAST(EXTRACT(quarter FROM d.fecha) AS INTEGER) AS trimestre,
        CAST(EXTRACT(year FROM d.fecha) AS INTEGER) AS anio,
        EXTRACT(day FROM d.fecha) = 1 AS es_primer_dia_mes
    FROM (
        SELECT CAST(UNNEST(generate_series(
            CAST(DATE_TRUNC('month', MIN(fecha)) AS DATE),
            CAST(DATE_TRUNC('month', MAX(fecha)) + INTERVAL 1 MONTH - INTERVAL 1 DAY AS DATE),
            INTERVAL 1 DAY)) AS DATE) AS fecha
        FROM stg_precios
    ) d
"""

# --- Hechos -------------------------------------------------------------------

SQL_HECHO_DIARIO_NUEVO = """
    CREATE OR REPLACE TEMP TABLE hecho_diario_nuevo AS
    SELECT
        CAST(strftime(b.fecha, '%Y%m%d') AS INTEGER) AS tiempo_id,
        r.region_id, p.producto_id, m.mercado_id, u.unidad_id,
        b.precio_min, b.precio_prom, b.precio_max,
        b.precio_min_crudo, b.precio_prom_crudo, b.precio_max_crudo,
        b.precio_prom / b.equivalencia_kg AS precio_kg_prom,
        b.flag_atipico
    FROM hecho_base b
    JOIN dim_region_nueva r ON r.nombre_region = b.region
    JOIN dim_producto_nueva p ON p.nombre_variedad = b.producto
    JOIN dim_mercado m ON m.nombre_mercado = b.mercado
    JOIN dim_unidad_nueva u
        ON u.nombre_unidad = b.unidad AND u.equivalencia_kg = b.equivalencia_kg
    WHERE b.periodicidad = 'dia'
"""

# Mensual: el agregado OFICIAL de SISAP cuando existe; si no (ej. el mes en
# curso antes del refresco semanal), el promedio de los dias validos. El
# promedio se hace en soles por kg (los dias de un mes pueden venir en
# unidades distintas) y se expresa en la unidad de mas dias del mes (en un
# empate, la de mayor equivalencia).
SQL_HECHO_MENSUAL_NUEVO = """
    CREATE OR REPLACE TEMP TABLE hecho_mensual_nuevo AS
    WITH oficial AS (
        SELECT
            CAST(strftime(DATE_TRUNC('month', b.fecha), '%Y%m%d') AS INTEGER) AS mes_id,
            r.region_id, p.producto_id, m.mercado_id, u.unidad_id,
            b.precio_min, b.precio_prom, b.precio_max,
            b.precio_min_crudo, b.precio_prom_crudo, b.precio_max_crudo,
            b.precio_prom / b.equivalencia_kg AS precio_kg_prom,
            b.flag_atipico,
            'oficial' AS fuente, CAST(NULL AS INTEGER) AS n_dias
        FROM hecho_base b
        JOIN dim_region_nueva r ON r.nombre_region = b.region
        JOIN dim_producto_nueva p ON p.nombre_variedad = b.producto
        JOIN dim_mercado m ON m.nombre_mercado = b.mercado
        JOIN dim_unidad_nueva u
            ON u.nombre_unidad = b.unidad AND u.equivalencia_kg = b.equivalencia_kg
        WHERE b.periodicidad = 'mensual'
    ),
    dias AS (
        SELECT
            CAST(DATE_TRUNC('month', fecha) AS DATE) AS mes, region, producto, mercado,
            unidad, equivalencia_kg, flag_atipico,
            precio_min / equivalencia_kg AS kg_min,
            precio_prom / equivalencia_kg AS kg_prom,
            precio_max / equivalencia_kg AS kg_max,
            precio_min_crudo / equivalencia_kg AS kgc_min,
            precio_prom_crudo / equivalencia_kg AS kgc_prom,
            precio_max_crudo / equivalencia_kg AS kgc_max
        FROM hecho_base b
        WHERE periodicidad = 'dia'
          AND NOT EXISTS (
              SELECT 1 FROM hecho_base o
              WHERE o.periodicidad = 'mensual'
                AND CAST(DATE_TRUNC('month', o.fecha) AS DATE) = CAST(DATE_TRUNC('month', b.fecha) AS DATE)
                AND o.region = b.region AND o.producto = b.producto AND o.mercado = b.mercado)
    ),
    por_unidad AS (
        SELECT mes, region, producto, mercado, unidad, equivalencia_kg, COUNT(*) AS n
        FROM dias GROUP BY ALL
    ),
    modal AS (
        SELECT mes, region, producto, mercado, unidad, equivalencia_kg
        FROM por_unidad
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY mes, region, producto, mercado
            ORDER BY n DESC, equivalencia_kg DESC, unidad) = 1
    ),
    promedio AS (
        SELECT mes, region, producto, mercado, COUNT(*) AS n_dias,
            AVG(kg_min) AS kg_min, AVG(kg_prom) AS kg_prom, AVG(kg_max) AS kg_max,
            AVG(kgc_min) AS kgc_min, AVG(kgc_prom) AS kgc_prom, AVG(kgc_max) AS kgc_max,
            BOOL_OR(flag_atipico) AS flag_atipico
        FROM dias GROUP BY ALL
    ),
    derivado AS (
        SELECT
            CAST(strftime(a.mes, '%Y%m%d') AS INTEGER) AS mes_id,
            r.region_id, p.producto_id, mer.mercado_id, u.unidad_id,
            ROUND(a.kg_min * m.equivalencia_kg, 6) AS precio_min,
            ROUND(a.kg_prom * m.equivalencia_kg, 6) AS precio_prom,
            ROUND(a.kg_max * m.equivalencia_kg, 6) AS precio_max,
            ROUND(a.kgc_min * m.equivalencia_kg, 6) AS precio_min_crudo,
            ROUND(a.kgc_prom * m.equivalencia_kg, 6) AS precio_prom_crudo,
            ROUND(a.kgc_max * m.equivalencia_kg, 6) AS precio_max_crudo,
            ROUND(a.kg_prom, 6) AS precio_kg_prom,
            a.flag_atipico,
            'promedio de dias' AS fuente, CAST(a.n_dias AS INTEGER) AS n_dias
        FROM promedio a
        JOIN modal m USING (mes, region, producto, mercado)
        JOIN dim_region_nueva r ON r.nombre_region = a.region
        JOIN dim_producto_nueva p ON p.nombre_variedad = a.producto
        JOIN dim_mercado mer ON mer.nombre_mercado = a.mercado
        JOIN dim_unidad_nueva u
            ON u.nombre_unidad = m.unidad AND u.equivalencia_kg = m.equivalencia_kg
    )
    SELECT * FROM oficial
    UNION ALL
    SELECT * FROM derivado
"""

# carga_id de cada fila: el de la carga que la creo o la modifico por ultima
# vez. Una fila que no cambio conserva el carga_id de antes (asi recargar sin
# cambios en la fuente no toca ninguna fila: filas_cargadas = 0).
SQL_HECHO_DIARIO_FINAL = """
    CREATE OR REPLACE TEMP TABLE hecho_diario_final AS
    SELECT n.tiempo_id, n.region_id, n.producto_id, n.mercado_id, n.unidad_id,
        CASE WHEN o.tiempo_id IS NOT NULL
              AND o.unidad_id IS NOT DISTINCT FROM n.unidad_id
              AND o.precio_min IS NOT DISTINCT FROM n.precio_min
              AND o.precio_prom IS NOT DISTINCT FROM n.precio_prom
              AND o.precio_max IS NOT DISTINCT FROM n.precio_max
              AND o.precio_min_crudo IS NOT DISTINCT FROM n.precio_min_crudo
              AND o.precio_prom_crudo IS NOT DISTINCT FROM n.precio_prom_crudo
              AND o.precio_max_crudo IS NOT DISTINCT FROM n.precio_max_crudo
              AND o.precio_kg_prom IS NOT DISTINCT FROM n.precio_kg_prom
              AND o.flag_atipico IS NOT DISTINCT FROM n.flag_atipico
             THEN o.carga_id ELSE ? END AS carga_id,
        n.precio_min, n.precio_prom, n.precio_max,
        n.precio_min_crudo, n.precio_prom_crudo, n.precio_max_crudo,
        n.precio_kg_prom, n.flag_atipico
    FROM hecho_diario_nuevo n
    LEFT JOIN fact_precio_diario o
        ON o.tiempo_id = n.tiempo_id AND o.region_id = n.region_id
       AND o.producto_id = n.producto_id AND o.mercado_id = n.mercado_id
"""

SQL_HECHO_MENSUAL_FINAL = """
    CREATE OR REPLACE TEMP TABLE hecho_mensual_final AS
    SELECT n.mes_id, n.region_id, n.producto_id, n.mercado_id, n.unidad_id,
        CASE WHEN o.mes_id IS NOT NULL
              AND o.unidad_id IS NOT DISTINCT FROM n.unidad_id
              AND o.precio_min IS NOT DISTINCT FROM n.precio_min
              AND o.precio_prom IS NOT DISTINCT FROM n.precio_prom
              AND o.precio_max IS NOT DISTINCT FROM n.precio_max
              AND o.precio_min_crudo IS NOT DISTINCT FROM n.precio_min_crudo
              AND o.precio_prom_crudo IS NOT DISTINCT FROM n.precio_prom_crudo
              AND o.precio_max_crudo IS NOT DISTINCT FROM n.precio_max_crudo
              AND o.precio_kg_prom IS NOT DISTINCT FROM n.precio_kg_prom
              AND o.flag_atipico IS NOT DISTINCT FROM n.flag_atipico
              AND o.fuente IS NOT DISTINCT FROM n.fuente
              AND o.n_dias IS NOT DISTINCT FROM n.n_dias
             THEN o.carga_id ELSE ? END AS carga_id,
        n.precio_min, n.precio_prom, n.precio_max,
        n.precio_min_crudo, n.precio_prom_crudo, n.precio_max_crudo,
        n.precio_kg_prom, n.flag_atipico, n.fuente, n.n_dias
    FROM hecho_mensual_nuevo n
    LEFT JOIN fact_precio_mensual o
        ON o.mes_id = n.mes_id AND o.region_id = n.region_id
       AND o.producto_id = n.producto_id AND o.mercado_id = n.mercado_id
"""

# filas del hecho anterior cuya clave ya no existe en la fuente
SQL_ELIMINADAS = """
    SELECT
        (SELECT COUNT(*) FROM fact_precio_diario o WHERE NOT EXISTS (
            SELECT 1 FROM hecho_diario_final n
            WHERE n.tiempo_id = o.tiempo_id AND n.region_id = o.region_id
              AND n.producto_id = o.producto_id AND n.mercado_id = o.mercado_id))
      + (SELECT COUNT(*) FROM fact_precio_mensual o WHERE NOT EXISTS (
            SELECT 1 FROM hecho_mensual_final n
            WHERE n.mes_id = o.mes_id AND n.region_id = o.region_id
              AND n.producto_id = o.producto_id AND n.mercado_id = o.mercado_id))
"""

SQL_CONTAR_CARGADAS = """
    SELECT (SELECT COUNT(*) FROM hecho_diario_final WHERE carga_id = ?)
         + (SELECT COUNT(*) FROM hecho_mensual_final WHERE carga_id = ?)
"""

# --- Reemplazo del contenido (dentro de la transaccion) ----------------------

SQL_REEMPLAZO: list[str] = [
    "DELETE FROM dim_tiempo",
    "INSERT INTO dim_tiempo SELECT * FROM dim_tiempo_nueva",
    "DELETE FROM dim_region",
    "INSERT INTO dim_region SELECT * FROM dim_region_nueva",
    "DELETE FROM dim_producto",
    "INSERT INTO dim_producto SELECT * FROM dim_producto_nueva",
    "DELETE FROM dim_unidad",
    "INSERT INTO dim_unidad SELECT * FROM dim_unidad_nueva",
    "DELETE FROM fact_precio_diario",
    """INSERT INTO fact_precio_diario
       SELECT tiempo_id, region_id, producto_id, mercado_id, unidad_id, carga_id,
              precio_min, precio_prom, precio_max,
              precio_min_crudo, precio_prom_crudo, precio_max_crudo,
              precio_kg_prom, flag_atipico
       FROM hecho_diario_final""",
    "DELETE FROM fact_precio_mensual",
    """INSERT INTO fact_precio_mensual
       SELECT mes_id, region_id, producto_id, mercado_id, unidad_id, carga_id,
              precio_min, precio_prom, precio_max,
              precio_min_crudo, precio_prom_crudo, precio_max_crudo,
              precio_kg_prom, flag_atipico, fuente, n_dias
       FROM hecho_mensual_final""",
]

# sentencias fijas (nunca se arma un DROP con un nombre en una variable)
SQL_BORRAR_TEMPORALES: list[str] = [
    "DROP TABLE IF EXISTS temp.stg_precios",
    "DROP TABLE IF EXISTS temp.stg_eval",
    "DROP TABLE IF EXISTS temp.hecho_base",
    "DROP TABLE IF EXISTS temp.dim_tiempo_nueva",
    "DROP TABLE IF EXISTS temp.dim_region_nueva",
    "DROP TABLE IF EXISTS temp.dim_producto_nueva",
    "DROP TABLE IF EXISTS temp.dim_unidad_nueva",
    "DROP TABLE IF EXISTS temp.hecho_diario_nuevo",
    "DROP TABLE IF EXISTS temp.hecho_mensual_nuevo",
    "DROP TABLE IF EXISTS temp.hecho_diario_final",
    "DROP TABLE IF EXISTS temp.hecho_mensual_final",
]

SQL_BORRAR_TABLA: dict[str, str] = {
    "dim_tiempo": "DROP TABLE dim_tiempo",
    "dim_region": "DROP TABLE dim_region",
    "dim_producto": "DROP TABLE dim_producto",
    "dim_unidad": "DROP TABLE dim_unidad",
    "dim_mercado": "DROP TABLE dim_mercado",
    "fact_precio_diario": "DROP TABLE fact_precio_diario",
    "fact_precio_mensual": "DROP TABLE fact_precio_mensual",
}

# --- Validaciones posteriores a la carga (dentro de la transaccion) ----------
# (descripcion, SQL que cuenta los problemas, o None si se compara aparte)

VALIDACIONES: list[tuple[str, str]] = [
    ("claves repetidas en fact_precio_diario", """
        SELECT COUNT(*) FROM (SELECT 1 FROM fact_precio_diario
        GROUP BY tiempo_id, region_id, producto_id, mercado_id HAVING COUNT(*) > 1)"""),
    ("claves repetidas en fact_precio_mensual", """
        SELECT COUNT(*) FROM (SELECT 1 FROM fact_precio_mensual
        GROUP BY mes_id, region_id, producto_id, mercado_id HAVING COUNT(*) > 1)"""),
    ("miembros repetidos en las dimensiones", """
        SELECT (SELECT COUNT(*) - COUNT(DISTINCT region_id) FROM dim_region)
             + (SELECT COUNT(*) - COUNT(DISTINCT nombre_region) FROM dim_region)
             + (SELECT COUNT(*) - COUNT(DISTINCT producto_id) FROM dim_producto)
             + (SELECT COUNT(*) - COUNT(DISTINCT nombre_variedad) FROM dim_producto)
             + (SELECT COUNT(*) - COUNT(DISTINCT unidad_id) FROM dim_unidad)
             + (SELECT COUNT(*) - COUNT(DISTINCT tiempo_id) FROM dim_tiempo)"""),
    ("hechos diarios con una dimension inexistente (FK huerfana)", """
        SELECT COUNT(*) FROM fact_precio_diario f
        WHERE NOT EXISTS (SELECT 1 FROM dim_tiempo d WHERE d.tiempo_id = f.tiempo_id)
           OR NOT EXISTS (SELECT 1 FROM dim_region d WHERE d.region_id = f.region_id)
           OR NOT EXISTS (SELECT 1 FROM dim_producto d WHERE d.producto_id = f.producto_id)
           OR NOT EXISTS (SELECT 1 FROM dim_mercado d WHERE d.mercado_id = f.mercado_id)
           OR NOT EXISTS (SELECT 1 FROM dim_unidad d WHERE d.unidad_id = f.unidad_id)
           OR NOT EXISTS (SELECT 1 FROM etl_carga d WHERE d.carga_id = f.carga_id)"""),
    ("hechos mensuales con una dimension inexistente (FK huerfana)", """
        SELECT COUNT(*) FROM fact_precio_mensual f
        WHERE NOT EXISTS (SELECT 1 FROM dim_mes d WHERE d.mes_id = f.mes_id)
           OR NOT EXISTS (SELECT 1 FROM dim_region d WHERE d.region_id = f.region_id)
           OR NOT EXISTS (SELECT 1 FROM dim_producto d WHERE d.producto_id = f.producto_id)
           OR NOT EXISTS (SELECT 1 FROM dim_mercado d WHERE d.mercado_id = f.mercado_id)
           OR NOT EXISTS (SELECT 1 FROM dim_unidad d WHERE d.unidad_id = f.unidad_id)
           OR NOT EXISTS (SELECT 1 FROM etl_carga d WHERE d.carga_id = f.carga_id)"""),
    # cada fila de la fuente aporta exactamente un precio crudo a un hecho
    # (diario, o mensual oficial): si un JOIN pierde o multiplica filas, no
    # cuadra
    ("diferencia entre las filas de la fuente y los precios crudos cargados", """
        SELECT ABS(
            (SELECT COUNT(*) FROM stg_precios)
          - (SELECT COUNT(precio_min_crudo) + COUNT(precio_prom_crudo) + COUNT(precio_max_crudo)
             FROM fact_precio_diario)
          - (SELECT COUNT(precio_min_crudo) + COUNT(precio_prom_crudo) + COUNT(precio_max_crudo)
             FROM fact_precio_mensual WHERE fuente = 'oficial'))"""),
    # el atipico se marca, no se borra: crudo siempre presente; validado nulo
    # solo si se marco
    ("precios validados nulos que no corresponden a un atipico marcado", """
        SELECT COUNT(*) FROM (
            SELECT precio_min, precio_prom, precio_max, precio_min_crudo,
                   precio_prom_crudo, precio_max_crudo, flag_atipico FROM fact_precio_diario
            UNION ALL
            SELECT precio_min, precio_prom, precio_max, precio_min_crudo,
                   precio_prom_crudo, precio_max_crudo, flag_atipico
            FROM fact_precio_mensual WHERE fuente = 'oficial')
        WHERE (precio_min IS NULL AND precio_min_crudo IS NOT NULL
               OR precio_prom IS NULL AND precio_prom_crudo IS NOT NULL
               OR precio_max IS NULL AND precio_max_crudo IS NOT NULL)
          AND NOT flag_atipico"""),
    ("filas de dim_tiempo ausentes para fechas con datos", """
        SELECT COUNT(*) FROM (SELECT DISTINCT fecha FROM stg_precios) s
        WHERE NOT EXISTS (SELECT 1 FROM dim_tiempo d WHERE d.fecha = s.fecha)"""),
]

SQL_ATIPICAS_FUENTE = "SELECT COUNT(*) FROM stg_eval WHERE es_atipico"
SQL_ATIPICAS_ANULADAS = """
    SELECT (SELECT COUNT(*) FILTER (WHERE precio_min IS NULL AND precio_min_crudo IS NOT NULL)
                 + COUNT(*) FILTER (WHERE precio_prom IS NULL AND precio_prom_crudo IS NOT NULL)
                 + COUNT(*) FILTER (WHERE precio_max IS NULL AND precio_max_crudo IS NOT NULL)
            FROM fact_precio_diario)
         + (SELECT COUNT(*) FILTER (WHERE precio_min IS NULL AND precio_min_crudo IS NOT NULL)
                 + COUNT(*) FILTER (WHERE precio_prom IS NULL AND precio_prom_crudo IS NOT NULL)
                 + COUNT(*) FILTER (WHERE precio_max IS NULL AND precio_max_crudo IS NOT NULL)
            FROM fact_precio_mensual WHERE fuente = 'oficial')
"""
