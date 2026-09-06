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

    # A veces SISAP reporta el mismo producto/region dos veces con distinta
    # unidad (ej. "Papa amarilla" en Puno: "Arroba" en anios viejos,
    # "Kilogramo" desde 2026), y ambas series comparten la misma clave
    # natural. Sin este orden, drop_duplicates(keep="last") puede quedarse
    # con la fila vacia de una serie y borrar un precio real que si existia
    # en la otra. Ordenar por precio (nulos primero) antes de deduplicar
    # asegura que un valor real nunca sea tapado por uno vacio.
    df_final = df_final.sort_values("precio", na_position="first", kind="stable")
    df_final = df_final.drop_duplicates(subset=CLAVE_NATURAL, keep="last")
    df_final.to_parquet(ruta, index=False)
    return ruta
