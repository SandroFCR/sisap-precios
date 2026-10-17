import os
from datetime import date
from pathlib import Path

import pandas as pd

from sisap.models import PriceRecord

# SISAP_DATA_DIR permite correr el pipeline contra otra carpeta (ej. un
# re-scrapeo completo en limpio, para validarlo contra el dataset vigente
# antes de reemplazarlo) sin tocar data/ del repo.
DATA_DIR = Path(os.environ.get("SISAP_DATA_DIR", "data"))
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
CLAVE_NATURAL = ["fecha", "region", "producto", "variable", "periodicidad"]
COLUMNAS = [
    "fecha", "region", "producto", "unidad", "equivalencia_kg", "variable",
    "precio", "periodicidad", "fecha_captura",
]


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
    html: str, anios: list[int], region: str, producto: str, variable: str,
    meses: list[int] | None = None,
) -> Path:
    # un refresco de solo algunos meses va a su propia carpeta: no debe
    # pisar el HTML del anio completo bajado por poblar-historico
    sufijo = f"_meses{min(meses):02d}-{max(meses):02d}" if meses else ""
    carpeta = RAW_DIR / "mensual" / f"{min(anios)}_{max(anios)}{sufijo}"
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta = carpeta / f"{region}_{producto}_{variable}.html"
    ruta.write_text(html, encoding="utf-8")
    return ruta


def leer_precios(ruta: Path) -> pd.DataFrame:
    """Lee el parquet historico normalizado al esquema actual.

    Un parquet de antes del 2026-10-17 no trae `periodicidad` ni
    `fecha_captura`. Se infieren en vez de fallar, para que un checkout
    viejo (ej. el del runner, o un merge que se quedo con el parquet del
    bot) siga funcionando: el dia 1 era el unico dia donde caia el agregado
    mensual, asi que todo lo demas es dato diario. El dia 1 queda como
    "mensual" aunque en Lima/Arequipa/Piura a veces fuera el dato de ese
    dia -- el refresco mensual y el backfill diario lo corrigen solos en la
    siguiente corrida, porque una captura nueva siempre gana."""
    df = pd.read_parquet(ruta)
    if "periodicidad" not in df.columns:
        es_dia_1 = pd.to_datetime(df["fecha"]).dt.day == 1
        df["periodicidad"] = es_dia_1.map({True: "mensual", False: "dia"})
    if "fecha_captura" not in df.columns:
        df["fecha_captura"] = pd.Series([None] * len(df), dtype="object")
    return df[COLUMNAS]


def guardar_registros(
    registros: list[PriceRecord], nombre_archivo: str = "precios.parquet"
) -> Path:
    """Agrega los registros nuevos al parquet historico, sin duplicar filas
    si se vuelve a correr el scraper para una fecha ya guardada (idempotencia)."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    ruta = PROCESSED_DIR / nombre_archivo
    # Las filas sin precio no se guardan: SISAP manda una celda vacia por
    # cada producto que no tiene dato en esa variable (ej. minorista de un
    # producto que solo se vende al por mayor), y por los meses futuros del
    # anio en curso. Llegaron a ser el 14% del parquet sin aportar nada --
    # una celda vacia nunca debe pisar un precio real, asi que no hay nada
    # que conservar de ellas.
    df_nuevo = pd.DataFrame(
        [r.model_dump() for r in registros if r.precio is not None], columns=COLUMNAS
    )
    if df_nuevo.empty:
        return ruta

    if ruta.exists():
        df_existente = leer_precios(ruta)
        df_final = pd.concat([df_existente, df_nuevo], ignore_index=True)
    else:
        df_final = df_nuevo
    df_final = df_final.dropna(subset=["precio"])

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
    # El fix: para cada serie (region+producto+variable+periodicidad),
    # calcular cual unidad es la HABITUAL (la que mas fechas tiene con dato
    # real) y, ante una colision, preferir siempre esa.
    #
    # Entre lecturas con el mismo estatus de unidad gana la CAPTURA MAS
    # RECIENTE (y, dentro de la misma captura, la que llego despues). Bug
    # real encontrado 2026-10-17: el desempate anterior era "el precio mas
    # alto", lo que hacia imposible refrescar el agregado mensual del mes en
    # curso -- el promedio parcial viejo le ganaba al completo nuevo cada vez
    # que era numericamente mayor.
    grupo_serie = ["region", "producto", "variable", "periodicidad"]
    # Vectorizado (no un groupby con lambda por serie): esto corre en cada
    # guardado, ~210 veces en una corrida con refresco mensual, sobre todo el
    # historico. Empates: gana la unidad que aparecio primero (groupby con
    # sort=False + orden estable), igual que value_counts().idxmax().
    conteo = (
        df_final.groupby(grupo_serie + ["unidad"], sort=False).size()
        .rename("_n").reset_index()
    )
    habitual = (
        conteo.sort_values("_n", ascending=False, kind="stable")
        .drop_duplicates(grupo_serie)[grupo_serie + ["unidad"]]
        .assign(_es_unidad_habitual=True)
    )
    df_final = df_final.merge(habitual, on=grupo_serie + ["unidad"], how="left")
    df_final = df_final.assign(
        _es_unidad_habitual=df_final["_es_unidad_habitual"].astype("boolean").fillna(False).astype(bool),
        _captura=pd.to_datetime(df_final["fecha_captura"]),
        _orden=range(len(df_final)),
    )
    df_final = df_final.sort_values(
        ["_es_unidad_habitual", "_captura", "_orden"], na_position="first", kind="stable"
    )
    df_final = df_final.drop_duplicates(subset=CLAVE_NATURAL, keep="last")

    # Orden fijo al escribir: el mismo contenido produce el mismo archivo
    # byte a byte (git no guarda una version nueva si nada cambio), y las
    # columnas repetitivas ordenadas comprimen mucho mejor en parquet.
    df_final = (
        df_final[COLUMNAS]
        .sort_values(["periodicidad", "region", "producto", "variable", "fecha"], kind="stable")
        .reset_index(drop=True)
    )
    df_final.to_parquet(ruta, index=False)
    return ruta
