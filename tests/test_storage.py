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
