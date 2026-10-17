from datetime import date

import pandas as pd

from sisap.models import PriceRecord
from sisap.storage import guardar_registros, leer_precios


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
        "periodicidad": "mensual",
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

    base = {
        "region": "Andahuaylas", "producto": "Naranja washington naval",
        "variable": "may_precio_min", "periodicidad": "mensual",
    }
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


def test_la_captura_mas_reciente_gana_aunque_su_precio_sea_menor(tmp_path, monkeypatch):
    """Bug real encontrado 2026-10-17: el desempate de la deduplicacion era
    "el precio mas alto". El agregado mensual del mes en curso se scrapeo el
    2026-09-05 (promedio de solo 5 dias) y, al refrescarlo con el mes
    completo, el valor viejo le ganaba al nuevo cada vez que era mayor --
    el refresco mensual era imposible."""
    monkeypatch.setattr("sisap.storage.PROCESSED_DIR", tmp_path)
    base = {
        "fecha": date(2026, 9, 1), "region": "Puno", "producto": "Papa blanca",
        "unidad": "Kilogramo", "equivalencia_kg": 1.0, "variable": "may_precio_prom",
        "periodicidad": "mensual",
    }

    guardar_registros([PriceRecord(**base, precio=1.50, fecha_captura=date(2026, 9, 5))], "test.parquet")
    ruta = guardar_registros([PriceRecord(**base, precio=1.20, fecha_captura=date(2026, 10, 2))], "test.parquet")

    df = pd.read_parquet(ruta)
    assert len(df) == 1
    assert df.iloc[0]["precio"] == 1.20


def test_mensual_y_diario_del_dia_1_conviven(tmp_path, monkeypatch):
    """Bug real encontrado 2026-10-17: el agregado mensual (fechado el dia 1)
    y el dato diario del dia 1 compartian la clave natural, y uno pisaba al
    otro en silencio. Con `periodicidad` en la clave, los dos se guardan."""
    monkeypatch.setattr("sisap.storage.PROCESSED_DIR", tmp_path)
    base = {
        "fecha": date(2026, 10, 1), "region": "Lima", "producto": "Tomate italiano",
        "unidad": "Kilogramo", "equivalencia_kg": 1.0, "variable": "may_precio_prom",
    }

    ruta = guardar_registros(
        [
            PriceRecord(**base, precio=2.10, periodicidad="mensual"),
            PriceRecord(**base, precio=1.80, periodicidad="dia"),
        ],
        "test.parquet",
    )

    df = pd.read_parquet(ruta).set_index("periodicidad")
    assert df.loc["mensual", "precio"] == 2.10
    assert df.loc["dia", "precio"] == 1.80


def test_no_se_guardan_filas_sin_precio(tmp_path, monkeypatch):
    """SISAP manda una celda vacia por cada producto sin dato en una variable
    (y por los meses futuros del anio en curso): eran el 14% del parquet."""
    monkeypatch.setattr("sisap.storage.PROCESSED_DIR", tmp_path)
    base = {
        "region": "Lima", "producto": "Aji panca", "unidad": "Kilogramo",
        "equivalencia_kg": 1.0, "periodicidad": "dia", "fecha": date(2026, 10, 15),
    }

    ruta = guardar_registros(
        [
            PriceRecord(**base, variable="may_precio_prom", precio=17.0),
            PriceRecord(**base, variable="min_precio_prom", precio=None),
        ],
        "test.parquet",
    )

    df = pd.read_parquet(ruta)
    assert df["variable"].tolist() == ["may_precio_prom"]


def test_un_lote_sin_precios_no_reescribe_el_parquet(tmp_path, monkeypatch):
    monkeypatch.setattr("sisap.storage.PROCESSED_DIR", tmp_path)

    ruta = guardar_registros([], "test.parquet")

    assert not ruta.exists()


def test_parquet_de_esquema_viejo_se_migra_al_leer(tmp_path):
    """Un parquet anterior al 2026-10-17 no tiene `periodicidad` ni
    `fecha_captura` (ej. el del checkout del runner): se infieren en vez de
    fallar -- el dia 1 era el unico dia donde caia el agregado mensual."""
    ruta = tmp_path / "viejo.parquet"
    pd.DataFrame([
        {"fecha": date(2026, 9, 1), "region": "Puno", "producto": "Papa", "unidad": "Kilogramo",
         "equivalencia_kg": 1.0, "variable": "may_precio_prom", "precio": 1.0},
        {"fecha": date(2026, 9, 2), "region": "Lima", "producto": "Papa", "unidad": "Kilogramo",
         "equivalencia_kg": 1.0, "variable": "may_precio_prom", "precio": 1.1},
    ]).to_parquet(ruta, index=False)

    df = leer_precios(ruta)

    assert df["periodicidad"].tolist() == ["mensual", "dia"]
    assert df["fecha_captura"].isna().all()


def test_la_escritura_es_deterministica(tmp_path, monkeypatch):
    """El mismo contenido produce el mismo archivo byte a byte: git no guarda
    una version nueva del parquet en una corrida que no trajo nada nuevo."""
    monkeypatch.setattr("sisap.storage.PROCESSED_DIR", tmp_path)
    registros = [
        PriceRecord(fecha=date(2026, 9, dia), region="Lima", producto="Papa", unidad="Kilogramo",
                    equivalencia_kg=1.0, variable="may_precio_prom", precio=1.0 + dia / 100,
                    periodicidad="dia", fecha_captura=date(2026, 9, 30))
        for dia in (3, 1, 2)
    ]

    ruta = guardar_registros(registros, "test.parquet")
    primera = ruta.read_bytes()
    guardar_registros(registros, "test.parquet")

    assert ruta.read_bytes() == primera
