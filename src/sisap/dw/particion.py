"""Particionamiento del dataset por anio (estilo Hive) y prueba de rendimiento.

`particionar` escribe el parquet historico como una carpeta por anio
(anio=2021/, anio=2022/, ...). Una consulta que filtra por `anio` lee solo la
carpeta de ese anio (poda de particiones), en vez de abrir el archivo entero.

`comparar_rendimiento` mide la misma consulta sobre el archivo unico y sobre
la version particionada: tiempo (mediana de varias corridas), archivos y
bytes leidos (los archivos salen del propio plan de DuckDB, EXPLAIN ANALYZE).
Con `factor` > 1 los datos se replican N veces SOLO para la prueba de carga
(datos sinteticos, declarados como tales en el resultado): con 1,2 MB reales
la diferencia es minima, y la replica muestra como escala.
"""

import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from sisap.storage import leer_precios

# precio promedio mensual mayorista de las papas por region en un anio
SQL_ARCHIVO_UNICO = """
    SELECT region, date_trunc('month', fecha) AS mes, AVG(precio / equivalencia_kg) AS soles_kg
    FROM read_parquet(?)
    WHERE year(fecha) = ? AND variable = 'may_precio_prom' AND producto LIKE 'Papa %'
    GROUP BY ALL
"""
SQL_PARTICIONADO = """
    SELECT region, date_trunc('month', fecha) AS mes, AVG(precio / equivalencia_kg) AS soles_kg
    FROM read_parquet(?, hive_partitioning = true)
    WHERE anio = ? AND variable = 'may_precio_prom' AND producto LIKE 'Papa %'
    GROUP BY ALL
"""
_ARCHIVOS_LEIDOS = re.compile(r"Total Files Read:\s*([\d,]+)")


@dataclass(frozen=True)
class Medicion:
    variante: str
    mediana_ms: float
    archivos_leidos: int
    archivos_totales: int
    bytes_en_disco: int
    # tamano de los archivos que la consulta tiene que abrir (no los bytes
    # exactos que DuckDB lee de ellos, que pueden ser menos)
    bytes_leidos: int
    filas_resultado: int


@dataclass(frozen=True)
class Comparacion:
    anio: int
    factor: int
    filas_dataset: int
    archivo_unico: Medicion
    particionado: Medicion


def _tabla_con_anio(df: pd.DataFrame) -> pa.Table:
    df = df.assign(anio=pd.to_datetime(df["fecha"]).dt.year.astype("int32"))
    return pa.Table.from_pandas(df, preserve_index=False)


def particionar(ruta_parquet: Path, carpeta: Path, factor: int = 1) -> dict[int, int]:
    """Escribe `carpeta`/anio=AAAA/*.parquet. Reemplaza lo que hubiera.
    Devuelve filas por anio."""
    if factor < 1:
        raise ValueError("factor debe ser >= 1")
    tabla = _tabla_con_anio(leer_precios(ruta_parquet))
    if factor > 1:
        tabla = pa.concat_tables([tabla] * factor)
    pq.write_to_dataset(
        tabla, str(carpeta), partition_cols=["anio"],
        existing_data_behavior="delete_matching",
    )
    conteo = tabla.group_by("anio").aggregate([("anio", "count")]).to_pylist()
    return {fila["anio"]: fila["anio_count"] for fila in sorted(conteo, key=lambda f: f["anio"])}


def _archivos_leidos(con: duckdb.DuckDBPyConnection, sql: str, parametros: list) -> int:
    plan = con.execute("EXPLAIN ANALYZE " + sql, parametros).fetchall()
    encontrado = _ARCHIVOS_LEIDOS.search(plan[0][1])
    if encontrado is None:
        raise RuntimeError("el plan de DuckDB no informa los archivos leidos")
    return int(encontrado.group(1).replace(",", ""))


def _medir(
    variante: str, sql: str, origen: str, archivos: list[Path], anio: int,
    repeticiones: int, archivos_del_anio: list[Path],
) -> Medicion:
    con = duckdb.connect(":memory:")
    try:
        parametros = [origen, anio]
        filas = len(con.execute(sql, parametros).fetchall())  # calentamiento
        tiempos = []
        for _ in range(repeticiones):
            inicio = time.perf_counter()
            con.execute(sql, parametros).fetchall()
            tiempos.append((time.perf_counter() - inicio) * 1000)
        leidos = _archivos_leidos(con, sql, parametros)
    finally:
        con.close()
    return Medicion(
        variante=variante,
        mediana_ms=round(statistics.median(tiempos), 2),
        archivos_leidos=leidos,
        archivos_totales=len(archivos),
        bytes_en_disco=sum(a.stat().st_size for a in archivos),
        bytes_leidos=sum(a.stat().st_size for a in archivos_del_anio),
        filas_resultado=filas,
    )


def _verificar_mismo_resultado(unico: str, particionado: str, anio: int) -> None:
    """Particionar no puede cambiar la respuesta: misma consulta, mismas filas."""
    con = duckdb.connect(":memory:")
    try:
        a = sorted(con.execute(SQL_ARCHIVO_UNICO, [unico, anio]).fetchall())
        b = sorted(con.execute(SQL_PARTICIONADO, [particionado, anio]).fetchall())
    finally:
        con.close()
    if len(a) != len(b) or any(
        x[:2] != y[:2] or abs(x[2] - y[2]) > 1e-9 for x, y in zip(a, b, strict=True)
    ):
        raise RuntimeError("las dos variantes no devuelven el mismo resultado")


def comparar_rendimiento(
    ruta_parquet: Path, carpeta_trabajo: Path, anio: int,
    factor: int = 1, repeticiones: int = 7,
) -> Comparacion:
    """Arma en `carpeta_trabajo` las dos versiones del MISMO dataset (archivo
    unico y particionado) y mide la consulta de referencia sobre ambas."""
    carpeta_trabajo.mkdir(parents=True, exist_ok=True)
    unico = carpeta_trabajo / "archivo_unico.parquet"
    carpeta_part = carpeta_trabajo / "particionado"

    tabla = _tabla_con_anio(leer_precios(ruta_parquet))
    if factor > 1:
        tabla = pa.concat_tables([tabla] * factor)
    # el archivo unico va SIN la columna anio (como el parquet del repo)
    pq.write_table(tabla.drop_columns(["anio"]), str(unico))
    pq.write_to_dataset(
        tabla, str(carpeta_part), partition_cols=["anio"],
        existing_data_behavior="delete_matching",
    )

    archivos_part = sorted(carpeta_part.rglob("*.parquet"))
    del_anio = sorted((carpeta_part / f"anio={anio}").glob("*.parquet"))
    if not del_anio:
        raise ValueError(f"no hay datos del anio {anio}")

    medicion_unico = _medir(
        "archivo unico", SQL_ARCHIVO_UNICO, str(unico), [unico], anio, repeticiones, [unico]
    )
    medicion_part = _medir(
        "particionado por anio", SQL_PARTICIONADO, str(carpeta_part / "**" / "*.parquet"),
        archivos_part, anio, repeticiones, del_anio,
    )
    _verificar_mismo_resultado(str(unico), str(carpeta_part / "**" / "*.parquet"), anio)
    return Comparacion(
        anio=anio, factor=factor, filas_dataset=tabla.num_rows,
        archivo_unico=medicion_unico, particionado=medicion_part,
    )
