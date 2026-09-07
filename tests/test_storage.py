from datetime import date

import pandas as pd

from sisap.models import PriceRecord
from sisap.storage import guardar_registros


def test_un_valor_real_nunca_se_tapa_con_uno_vacio(tmp_path, monkeypatch):
    """Regresion: SISAP a veces reporta el mismo producto/region dos veces
    con distinta unidad (ej. Papa amarilla en Puno: Arroba en anios viejos,
    Kilogramo desde 2026). Ambas 'series' comparten la misma clave natural
    (fecha, region, producto, variable), y un precio real de una no debe
    perderse por culpa de la fila vacia de la otra."""
    monkeypatch.setattr("sisap.storage.PROCESSED_DIR", tmp_path)

    clave_comun = {
        "fecha": date(2026, 6, 1), "region": "Puno", "producto": "Papa amarilla",
        "unidad": "Kilogramo", "equivalencia_kg": 1.0, "variable": "may_precio_max",
    }
    registros = [
        PriceRecord(**{**clave_comun, "precio": 3.48}),
        PriceRecord(**{**clave_comun, "unidad": "Arroba", "equivalencia_kg": 11.5, "precio": None}),
    ]

    ruta = guardar_registros(registros, nombre_archivo="test.parquet")
    df = pd.read_parquet(ruta)

    assert len(df) == 1
    assert df.iloc[0]["precio"] == 3.48


def test_colision_de_dos_valores_reales_prefiere_la_unidad_habitual(tmp_path, monkeypatch):
    """Bug real encontrado en produccion (2026-09-06): a veces SISAP reporta
    el MISMO mes dos veces con AMBAS lecturas reales (no una vacia), cada
    una con su propia unidad -- ej. Naranja washington naval/Andahuaylas
    ene-2023: "Kilogramo"=3.10 Y "Ciento"=3.40 al mismo tiempo. El orden
    anterior (por precio, quedandose con el mas alto) elegia la unidad
    ATIPICA cada vez que su numero era mayor, sin importar si esa unidad
    era la de siempre para esa serie -- un error sutil (~10%) que no
    disparaba la regla de outliers de warehouse.py y corrompia datos en
    silencio. La regla ahora prioriza la unidad HABITUAL de la serie (la
    que mas meses tiene con dato real), no el precio mas alto."""
    monkeypatch.setattr("sisap.storage.PROCESSED_DIR", tmp_path)

    base = {"region": "Andahuaylas", "producto": "Naranja washington naval", "variable": "may_precio_min"}
    # 3 meses normales, todos en Kilogramo -- establecen cual es la unidad
    # habitual de esta serie.
    registros = [
        PriceRecord(**base, fecha=date(2023, 2, 1), unidad="Kilogramo", equivalencia_kg=1.0, precio=3.80),
        PriceRecord(**base, fecha=date(2023, 3, 1), unidad="Kilogramo", equivalencia_kg=1.0, precio=3.50),
        PriceRecord(**base, fecha=date(2023, 5, 1), unidad="Kilogramo", equivalencia_kg=1.0, precio=3.30),
        # el mes en colision: dos lecturas reales el mismo mes, la atipica
        # ("Ciento") con un numero mas alto que la habitual ("Kilogramo").
        PriceRecord(**base, fecha=date(2023, 1, 1), unidad="Kilogramo", equivalencia_kg=1.0, precio=3.10),
        PriceRecord(**base, fecha=date(2023, 1, 1), unidad="Ciento", equivalencia_kg=20.0, precio=3.40),
    ]

    ruta = guardar_registros(registros, nombre_archivo="test.parquet")
    df = pd.read_parquet(ruta)

    fila_enero = df[df["fecha"] == date(2023, 1, 1)]
    assert len(fila_enero) == 1
    assert fila_enero.iloc[0]["unidad"] == "Kilogramo"
    assert fila_enero.iloc[0]["precio"] == 3.10
