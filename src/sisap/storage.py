from datetime import date
from pathlib import Path

import pandas as pd

from sisap.models import PriceRecord

RAW_DIR = Path("data/raw")
PROCESSED_DIR = Path("data/processed")
CLAVE_NATURAL = ["fecha", "region", "producto", "variable"]


def guardar_html_crudo(html: str, fecha: date, region: str) -> Path:
    """Guarda la respuesta tal cual llego del servidor. Si mas adelante
    encontramos un bug en el parser, podemos re-parsear el historico sin
    volver a golpear el sitio."""
    carpeta = RAW_DIR / fecha.isoformat()
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta = carpeta / f"{region}.html"
    ruta.write_text(html, encoding="utf-8")
    return ruta


def guardar_html_crudo_intervalo(
    html: str, desde: date, hasta: date, region: str, variable: str
) -> Path:
    carpeta = RAW_DIR / "intervalo" / f"{desde.isoformat()}_{hasta.isoformat()}"
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta = carpeta / f"{region}_{variable}.html"
    ruta.write_text(html, encoding="utf-8")
    return ruta


def guardar_html_crudo_mensual(
    html: str, anios: list[int], region: str, producto: str, variable: str
) -> Path:
    carpeta = RAW_DIR / "mensual" / f"{min(anios)}_{max(anios)}"
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta = carpeta / f"{region}_{producto}_{variable}.html"
    ruta.write_text(html, encoding="utf-8")
    return ruta


def guardar_registros(
    registros: list[PriceRecord], nombre_archivo: str = "precios.parquet"
) -> Path:
    """Agrega los registros nuevos al parquet historico, sin duplicar filas
    si se vuelve a correr el scraper para una fecha ya guardada (idempotencia)."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    ruta = PROCESSED_DIR / nombre_archivo
    df_nuevo = pd.DataFrame([registro.model_dump() for registro in registros])

    if ruta.exists():
        df_existente = pd.read_parquet(ruta)
        df_final = pd.concat([df_existente, df_nuevo], ignore_index=True)
    else:
        df_final = df_nuevo

    # A veces SISAP reporta el mismo producto/region/mes DOS VECES con
    # distinta unidad al mismo tiempo (ej. Naranja washington naval/
    # Andahuaylas ene-2023: "Kilogramo"=3.10 Y "Ciento"=3.40 juntos; Huevos
    # rosados/Ayacucho feb-2024: "Kilogramo"=6.67 Y "Bandeja"=68.5 juntos).
    # Como CLAVE_NATURAL no incluye la unidad, solo una de las dos filas
    # puede sobrevivir. Bug real encontrado 2026-09-06: el orden anterior
    # (por precio, quedandose con el numero mas alto) elegia la unidad
    # ATIPICA cada vez que su precio crudo era numericamente mayor -- sin
    # importar si esa unidad era la de siempre para esa serie o una
    # aparicion aislada. El error resultante (dividir por la equivalencia_kg
    # de la unidad atipica) a veces era sutil (~10-20%, ej. 3.8 en vez de
    # 3.10 -- no lo bastante extremo para que la regla de outliers de
    # warehouse.py lo detecte) y llevaba corrompiendo datos en silencio.
    #
    # El fix: para cada serie (region+producto+variable), calcular cual
    # unidad es la HABITUAL (la que mas meses tiene con dato real) y, ante
    # una colision, preferir siempre esa -- no la que tenga el precio mas
    # alto. Con una sola lectura real (el caso original que este orden
    # buscaba arreglar: una fila vacia vs una con dato), la unidad habitual
    # y la unica con dato coinciden, asi que ese comportamiento no cambia.
    grupo_serie = ["region", "producto", "variable"]
    unidad_habitual = (
        df_final.dropna(subset=["precio"])
        .groupby(grupo_serie)["unidad"]
        .agg(lambda serie: serie.value_counts().idxmax())
    )
    es_unidad_habitual = (
        df_final.set_index(grupo_serie)["unidad"] == unidad_habitual.reindex(
            df_final.set_index(grupo_serie).index
        )
    ).to_numpy()
    df_final = df_final.assign(_es_unidad_habitual=es_unidad_habitual)
    df_final = df_final.sort_values(
        ["_es_unidad_habitual", "precio"], na_position="first", kind="stable"
    )
    df_final = df_final.drop_duplicates(subset=CLAVE_NATURAL, keep="last")
    df_final = df_final.drop(columns="_es_unidad_habitual")
    df_final.to_parquet(ruta, index=False)
    return ruta
