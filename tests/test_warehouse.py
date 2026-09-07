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


def test_outlier_evidente_se_anula_pero_el_crudo_se_conserva(tmp_path):
    """Regresion: SISAP a veces tiene errores de tipeo (ej. '1.70' en vez de
    '170.00' en un mes rodeado de meses ~170-185). Un mes que vale menos del
    20% de sus dos vecinos se anula en fact_precios, pero el valor original
    debe seguir intacto en staging_precios (no se pierde, solo se excluye
    del analisis)."""
    serie_normal = [166, 173, 175, 170]
    filas = [
        {
            "fecha": date(2024, mes, 1), "region": "Arequipa", "producto": "Arroz extra",
            "unidad": "Saco", "equivalencia_kg": 50.0,
            "variable": "may_precio_min", "precio": precio,
        }
        for mes, precio in zip([1, 2, 3, 4], serie_normal)
    ]
    filas.append({
        "fecha": date(2024, 5, 1), "region": "Arequipa", "producto": "Arroz extra",
        "unidad": "Saco", "equivalencia_kg": 50.0,
        "variable": "may_precio_min", "precio": 1.70,  # el typo real que encontramos
    })
    filas.append({
        "fecha": date(2024, 6, 1), "region": "Arequipa", "producto": "Arroz extra",
        "unidad": "Saco", "equivalencia_kg": 50.0,
        "variable": "may_precio_min", "precio": 176.0,
    })

    ruta_parquet = tmp_path / "precios.parquet"
    pd.DataFrame(filas).to_parquet(ruta_parquet, index=False)
    ruta_duckdb = tmp_path / "sisap.duckdb"

    construir_modelo_dimensional(ruta_parquet, ruta_duckdb)

    con = duckdb.connect(str(ruta_duckdb), read_only=True)

    precio_en_hechos = con.execute(
        "SELECT precio FROM fact_precios WHERE fecha = DATE '2024-05-01'"
    ).fetchone()[0]
    assert precio_en_hechos is None

    precio_crudo = con.execute(
        "SELECT precio FROM staging_precios WHERE fecha = DATE '2024-05-01'"
    ).fetchone()[0]
    assert precio_crudo == 1.70

    # los meses normales alrededor no deben verse afectados
    precio_abril = con.execute(
        "SELECT precio FROM fact_precios WHERE fecha = DATE '2024-04-01'"
    ).fetchone()[0]
    assert precio_abril == 170.0

    con.close()


def test_outlier_moderado_tambien_se_anula(tmp_path):
    """Regresion: el primer fix solo anulaba typos EXTREMOS (menos del 20%
    de ambos vecinos). Bug real encontrado despues: Leche reconstituida/Lima,
    mayo-2026 = 49 rodeado por 172 y 172 (28% del vecino) -- se escapaba del
    20%. La regla ahora tambien exige que los vecinos concuerden entre si
    (para no confundir esto con una caida real de mercado seguida de un
    nivel distinto)."""
    filas = [
        {
            "fecha": date(2026, mes, 1), "region": "Lima", "producto": "Leche reconstituida",
            "unidad": "Caja", "equivalencia_kg": 48.0,
            "variable": "may_precio_min", "precio": precio,
        }
        for mes, precio in zip([3, 4], [172.0, 172.0])
    ]
    filas.append({
        "fecha": date(2026, 5, 1), "region": "Lima", "producto": "Leche reconstituida",
        "unidad": "Caja", "equivalencia_kg": 48.0,
        "variable": "may_precio_min", "precio": 49.0,  # el typo real que encontramos
    })
    filas.append({
        "fecha": date(2026, 6, 1), "region": "Lima", "producto": "Leche reconstituida",
        "unidad": "Caja", "equivalencia_kg": 48.0,
        "variable": "may_precio_min", "precio": 172.0,
    })

    ruta_parquet = tmp_path / "precios.parquet"
    pd.DataFrame(filas).to_parquet(ruta_parquet, index=False)
    ruta_duckdb = tmp_path / "sisap.duckdb"

    construir_modelo_dimensional(ruta_parquet, ruta_duckdb)

    con = duckdb.connect(str(ruta_duckdb), read_only=True)
    precio_mayo = con.execute(
        "SELECT precio FROM fact_precios WHERE fecha = DATE '2026-05-01'"
    ).fetchone()[0]
    assert precio_mayo is None


def test_caida_real_de_mercado_no_se_confunde_con_typo(tmp_path):
    """Falso positivo a evitar: si los vecinos NO concuerdan entre si (uno
    representa el nivel viejo, el otro un nivel nuevo distinto), una caida
    fuerte en el medio es un cambio de tendencia real, no un typo -- no debe
    anularse aunque el mes se aleje mucho de sus vecinos."""
    filas = [
        {"fecha": date(2026, 1, 1), "region": "Puno", "producto": "Papa huayro",
         "unidad": "Saco", "equivalencia_kg": 100.0, "variable": "may_precio_min", "precio": 300.0},
        {"fecha": date(2026, 2, 1), "region": "Puno", "producto": "Papa huayro",
         "unidad": "Saco", "equivalencia_kg": 100.0, "variable": "may_precio_min", "precio": 90.0},
        {"fecha": date(2026, 3, 1), "region": "Puno", "producto": "Papa huayro",
         "unidad": "Saco", "equivalencia_kg": 100.0, "variable": "may_precio_min", "precio": 95.0},
    ]
    ruta_parquet = tmp_path / "precios.parquet"
    pd.DataFrame(filas).to_parquet(ruta_parquet, index=False)
    ruta_duckdb = tmp_path / "sisap.duckdb"

    construir_modelo_dimensional(ruta_parquet, ruta_duckdb)

    con = duckdb.connect(str(ruta_duckdb), read_only=True)
    precio_febrero = con.execute(
        "SELECT precio FROM fact_precios WHERE fecha = DATE '2026-02-01'"
    ).fetchone()[0]
    assert precio_febrero == 90.0


def test_unidad_mal_etiquetada_se_anula_si_el_crudo_calza_con_la_unidad_vecina(tmp_path):
    """Bug real encontrado en produccion (2026-09-06), tres veces con la misma
    firma: Huevos rosados/Ayacucho, Huevos rosados/Amazonas y Haba verde
    criolla/Amazonas reportan, en un mes aislado, una unidad distinta a la de
    sus vecinos (ej. "Bandeja" de 23 kg en vez de "Kilogramo"), pero el precio
    CRUDO de ese mes es practicamente igual al de los vecinos (ej. "10.00" en
    un tramo de 9.3-9.5) -- es decir, el numero nunca cambio de escala, solo
    la unidad declarada esta mal. Dividir por la unidad equivocada (23 kg)
    hunde el precio/kg a un valor absurdo (0.43 para huevos).

    Un intento anterior de esta regla exigia que la unidad fuera IDENTICA en
    los 3 meses para anular algo, especificamente para no confundir esto con
    un cambio de unidad legitimo -- pero esa condicion terminaba exentando
    justo este patron (los 3 casos reales de arriba se colaban sin anular).
    La regla ahora tambien anula cuando la unidad NO coincide, siempre que el
    precio crudo, reinterpretado con la unidad de los vecinos, encaje dentro
    de lo normal (fuerte señal de que la unidad es el error, no el precio)."""
    filas = [
        {"fecha": date(2023, 4, 1), "region": "Ayacucho", "producto": "Huevos rosados",
         "unidad": "Kilogramo", "equivalencia_kg": 1.0, "variable": "may_precio_min", "precio": 9.5},
        {"fecha": date(2023, 5, 1), "region": "Ayacucho", "producto": "Huevos rosados",
         "unidad": "Bandeja", "equivalencia_kg": 23.0, "variable": "may_precio_min", "precio": 10.0},
        {"fecha": date(2023, 6, 1), "region": "Ayacucho", "producto": "Huevos rosados",
         "unidad": "Kilogramo", "equivalencia_kg": 1.0, "variable": "may_precio_min", "precio": 9.5},
    ]
    ruta_parquet = tmp_path / "precios.parquet"
    pd.DataFrame(filas).to_parquet(ruta_parquet, index=False)
    ruta_duckdb = tmp_path / "sisap.duckdb"

    construir_modelo_dimensional(ruta_parquet, ruta_duckdb)

    con = duckdb.connect(str(ruta_duckdb), read_only=True)

    precio_mayo = con.execute(
        "SELECT precio FROM fact_precios WHERE fecha = DATE '2023-05-01'"
    ).fetchone()[0]
    assert precio_mayo is None

    precio_crudo = con.execute(
        "SELECT precio FROM staging_precios WHERE fecha = DATE '2023-05-01'"
    ).fetchone()[0]
    assert precio_crudo == 10.0

    con.close()


def test_cambio_de_unidad_sin_evidencia_no_se_anula(tmp_path):
    """Red de seguridad para la regla de arriba: si el mes con unidad
    distinta NO calza con la tendencia bajo NINGUNA de las dos lecturas (ni
    con su propia unidad ni con la de los vecinos), no hay evidencia de cual
    de los dos esta mal -- se deja el valor tal cual en vez de asumir que es
    un error."""
    filas = [
        {"fecha": date(2025, 3, 1), "region": "Arequipa", "producto": "Aceite clasico",
         "unidad": "Caja", "equivalencia_kg": 10.8, "variable": "may_precio_min", "precio": 8.70},
        {"fecha": date(2025, 4, 1), "region": "Arequipa", "producto": "Aceite clasico",
         "unidad": "Litro", "equivalencia_kg": 1.0, "variable": "may_precio_min", "precio": 45.0},
        {"fecha": date(2025, 5, 1), "region": "Arequipa", "producto": "Aceite clasico",
         "unidad": "Caja", "equivalencia_kg": 10.8, "variable": "may_precio_min", "precio": 8.90},
    ]
    ruta_parquet = tmp_path / "precios.parquet"
    pd.DataFrame(filas).to_parquet(ruta_parquet, index=False)
    ruta_duckdb = tmp_path / "sisap.duckdb"

    construir_modelo_dimensional(ruta_parquet, ruta_duckdb)

    con = duckdb.connect(str(ruta_duckdb), read_only=True)
    precio_abril = con.execute(
        "SELECT precio FROM fact_precios WHERE fecha = DATE '2025-04-01'"
    ).fetchone()[0]
    assert precio_abril == 45.0

    con.close()
