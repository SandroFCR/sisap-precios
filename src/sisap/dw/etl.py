"""Carga del data warehouse: transaccion atomica + auditoria en etl_carga.

Flujo de cada carga:
  1. Se registra una fila 'en_curso' en etl_carga y se confirma de inmediato
     (si el proceso muere a medias, esa fila queda como evidencia).
  2. En UNA transaccion: staging -> verificaciones -> atipicos -> pivot ->
     dimensiones -> hechos -> reemplazo del contenido -> validaciones.
  3. Si todo cuadra, COMMIT y la fila pasa a 'exitosa' con sus conteos.
     Si algo falla, ROLLBACK (el modelo queda exactamente como estaba), la
     fila pasa a 'fallida' con el motivo, y el error se vuelve a lanzar.

La fuente de verdad sigue siendo el parquet: la carga lee TODO el parquet
cada vez (~1 s) y compara contra lo cargado antes, de modo que solo las
filas nuevas o cambiadas reciben el carga_id nuevo.
"""

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd

from sisap.dw import modelo
from sisap.dw.semillas import (
    SIN_CLASIFICAR,
    clasificar_region,
    clasificar_variedad,
    codigo_variedad,
)
from sisap.storage import leer_precios
from sisap.warehouse import NOMBRES_MES_ES


class ErrorDeIntegridad(RuntimeError):
    """La fuente o el modelo cargado no cumple una regla de integridad: la
    carga se revierte y queda registrada como fallida."""


@dataclass(frozen=True)
class ResultadoCarga:
    carga_id: int
    filas_leidas: int
    filas_cargadas: int
    filas_eliminadas: int
    filas_atipicas: int
    advertencias: list[str] = field(default_factory=list)


def construir_dw(ruta_parquet: Path, ruta_duckdb: Path) -> ResultadoCarga:
    """Carga el DW persistente en `ruta_duckdb` (se crea si no existe). Es
    incremental en el sentido del carga_id: el historial de etl_carga se
    conserva entre corridas."""
    con = duckdb.connect(str(ruta_duckdb))
    try:
        return cargar_dw(con, ruta_parquet)
    finally:
        con.close()


def construir_dw_en_memoria(ruta_parquet: Path) -> duckdb.DuckDBPyConnection:
    """El mismo modelo, en memoria (para un dashboard que lo arme al
    arrancar). Aqui etl_carga solo tiene la carga actual."""
    con = duckdb.connect(":memory:")
    cargar_dw(con, ruta_parquet)
    return con


def cargar_dw(con: duckdb.DuckDBPyConnection, ruta_parquet: Path) -> ResultadoCarga:
    # se lee antes de abrir nada: un parquet ilegible no deja filas en_curso
    df_origen = leer_precios(ruta_parquet)
    _asegurar_esquema(con)
    carga_id = _abrir_carga(con)
    try:
        con.execute("BEGIN TRANSACTION")
        resultado = _cargar(con, df_origen, carga_id)
        _cerrar_carga(con, resultado)
        con.execute("COMMIT")
        return resultado
    except Exception as error:
        _revertir(con)
        _registrar_fallo(con, carga_id, error)
        raise
    finally:
        _borrar_temporales(con)


# --- Auditoria -----------------------------------------------------------------

def _ahora_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _abrir_carga(con: duckdb.DuckDBPyConnection) -> int:
    carga_id = con.execute("SELECT COALESCE(MAX(carga_id), 0) + 1 FROM etl_carga").fetchone()[0]
    con.execute(
        "INSERT INTO etl_carga (carga_id, inicio, estado) VALUES (?, ?, 'en_curso')",
        [carga_id, _ahora_utc()],
    )
    return carga_id


def _cerrar_carga(con: duckdb.DuckDBPyConnection, r: ResultadoCarga) -> None:
    con.execute(
        """UPDATE etl_carga SET fin = ?, filas_leidas = ?, filas_cargadas = ?,
           filas_eliminadas = ?, filas_atipicas = ?, estado = 'exitosa', mensaje = ?
           WHERE carga_id = ?""",
        [_ahora_utc(), r.filas_leidas, r.filas_cargadas, r.filas_eliminadas,
         r.filas_atipicas, "; ".join(r.advertencias) or None, r.carga_id],
    )


def _registrar_fallo(con: duckdb.DuckDBPyConnection, carga_id: int, error: Exception) -> None:
    con.execute(
        "UPDATE etl_carga SET fin = ?, estado = 'fallida', mensaje = ? WHERE carga_id = ?",
        [_ahora_utc(), f"{type(error).__name__}: {error}"[:2000], carga_id],
    )


def _revertir(con: duckdb.DuckDBPyConnection) -> None:
    try:
        con.execute("ROLLBACK")
    except duckdb.TransactionException:
        pass  # el error ocurrio antes del BEGIN (o la transaccion ya se cerro)


# --- Esquema -------------------------------------------------------------------

def _columnas(con: duckdb.DuckDBPyConnection, tabla: str) -> list[str]:
    filas = con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position",
        [tabla],
    ).fetchall()
    return [fila[0] for fila in filas]


def _asegurar_esquema(con: duckdb.DuckDBPyConnection) -> None:
    """Crea lo que falte. Si una tabla existe con otras columnas (el esquema
    cambio entre versiones del codigo) se recrea: el modelo siempre se puede
    reconstruir desde el parquet; solo se pierde el carga_id previo de esas
    filas. etl_carga (el historial) nunca se recrea."""
    referencia = duckdb.connect(":memory:")
    try:
        for ddl in modelo.DDL.values():
            referencia.execute(ddl)
        esperadas = {tabla: _columnas(referencia, tabla) for tabla in modelo.DDL}
    finally:
        referencia.close()
    for tabla, columnas in esperadas.items():
        actuales = _columnas(con, tabla)
        if actuales and actuales != columnas and tabla != "etl_carga":
            con.execute(modelo.SQL_BORRAR_TABLA[tabla])
        con.execute(modelo.DDL[tabla])
    con.execute(modelo.VISTA_DIM_MES)
    if con.execute("SELECT COUNT(*) FROM dim_mercado").fetchone()[0] == 0:
        con.execute(modelo.SQL_MERCADOS)


def _borrar_temporales(con: duckdb.DuckDBPyConnection) -> None:
    for sentencia in modelo.SQL_BORRAR_TEMPORALES:
        con.execute(sentencia)


# --- Carga ---------------------------------------------------------------------

def _contar(con: duckdb.DuckDBPyConnection, sql: str, parametros: list | None = None) -> int:
    return int(con.execute(sql, parametros or []).fetchone()[0])


def _cargar(
    con: duckdb.DuckDBPyConnection, df_origen: pd.DataFrame, carga_id: int
) -> ResultadoCarga:
    con.register("precios_origen", df_origen)
    try:
        con.execute(modelo.SQL_STAGING)
    finally:
        con.unregister("precios_origen")
    filas_leidas = _contar(con, "SELECT COUNT(*) FROM stg_precios")
    if filas_leidas == 0:
        raise ErrorDeIntegridad("la fuente no tiene filas: no hay nada que cargar")

    for descripcion, sql in modelo.VERIFICACIONES_PREVIAS:
        problemas = _contar(con, sql)
        if problemas:
            raise ErrorDeIntegridad(f"{problemas} {descripcion}")

    con.execute(modelo.SQL_EVALUAR_ATIPICOS)
    filas_atipicas = _contar(con, modelo.SQL_ATIPICAS_FUENTE)

    con.execute(modelo.SQL_CLAVES)
    con_varias = _contar(con, modelo.SQL_CLAVES_CON_VARIAS_UNIDADES)
    if con_varias:
        raise ErrorDeIntegridad(
            f"{con_varias} claves (fecha, region, producto, mercado) con mas de una unidad: "
            "la deduplicacion de storage.py no se aplico."
        )
    advertencias: list[str] = []
    incompletas = _contar(con, modelo.SQL_CLAVES_INCOMPLETAS)
    if incompletas:
        advertencias.append(
            f"{incompletas} claves con menos de 3 precios (se cargan con NULL en los faltantes)"
        )

    _preparar_dimensiones(con, advertencias)
    con.execute(modelo.SQL_HECHO_DIARIO_NUEVO)
    con.execute(modelo.SQL_HECHO_MENSUAL_NUEVO)
    con.execute(modelo.SQL_HECHO_DIARIO_FINAL, [carga_id])
    con.execute(modelo.SQL_HECHO_MENSUAL_FINAL, [carga_id])

    filas_eliminadas = _contar(con, modelo.SQL_ELIMINADAS)
    filas_cargadas = _contar(con, modelo.SQL_CONTAR_CARGADAS, [carga_id, carga_id])
    for sentencia in modelo.SQL_REEMPLAZO:
        con.execute(sentencia)

    _validar(con, filas_atipicas)
    return ResultadoCarga(
        carga_id=carga_id, filas_leidas=filas_leidas, filas_cargadas=filas_cargadas,
        filas_eliminadas=filas_eliminadas, filas_atipicas=filas_atipicas,
        advertencias=advertencias,
    )


def _preparar_dimensiones(con: duckdb.DuckDBPyConnection, advertencias: list[str]) -> None:
    variedades = [f[0] for f in con.execute(
        "SELECT DISTINCT producto FROM stg_precios ORDER BY 1").fetchall()]
    regiones = [f[0] for f in con.execute(
        "SELECT DISTINCT region FROM stg_precios ORDER BY 1").fetchall()]

    productos = []
    for variedad in variedades:
        codigo, genero, categoria = clasificar_variedad(variedad)
        productos.append((variedad, codigo_variedad(variedad), codigo, genero, categoria))
    sin_genero = [p[0] for p in productos if p[3] == SIN_CLASIFICAR]
    sin_codigo = [p[0] for p in productos if p[1] is None and p[3] != SIN_CLASIFICAR]
    if sin_codigo:
        advertencias.append(
            f"{len(sin_codigo)} variedades sin codigo oficial, clasificadas por el nombre "
            "(agregar su codigo a semillas.CODIGOS_VARIEDAD): " + ", ".join(sin_codigo)
        )
    if sin_genero:
        advertencias.append(
            f"{len(sin_genero)} variedades sin clasificar (agregar a semillas.py): "
            + ", ".join(sin_genero)
        )
    semilla_producto = pd.DataFrame(
        productos,
        columns=["nombre_variedad", "codigo_variedad", "codigo_genero", "genero", "categoria"],
    )

    filas_region = []
    for region in regiones:
        codigo, tipo, departamento, macro = clasificar_region(region)
        filas_region.append((region, codigo, tipo, departamento, macro))
    sin_region = [r[0] for r in filas_region if r[2] == SIN_CLASIFICAR]
    if sin_region:
        advertencias.append(
            f"{len(sin_region)} regiones sin clasificar (agregar a semillas.py): "
            + ", ".join(sin_region)
        )
    semilla_region = pd.DataFrame(
        filas_region,
        columns=["nombre_region", "codigo_sisap", "tipo_region", "departamento", "macrorregion"],
    )

    con.register("semilla_producto", semilla_producto)
    con.register("semilla_region", semilla_region)
    try:
        con.execute(modelo.SQL_DIM_PRODUCTO_NUEVA)
        con.execute(modelo.SQL_DIM_REGION_NUEVA)
    finally:
        con.unregister("semilla_producto")
        con.unregister("semilla_region")
    con.execute(modelo.SQL_DIM_UNIDAD_NUEVA)
    con.execute(modelo.SQL_DIM_TIEMPO_NUEVA, [list(NOMBRES_MES_ES.values())])


def _validar(con: duckdb.DuckDBPyConnection, filas_atipicas: int) -> None:
    for descripcion, sql in modelo.VALIDACIONES:
        problemas = _contar(con, sql)
        if problemas:
            raise ErrorDeIntegridad(f"validacion fallida: {problemas} {descripcion}")
    anuladas = _contar(con, modelo.SQL_ATIPICAS_ANULADAS)
    if anuladas != filas_atipicas:
        raise ErrorDeIntegridad(
            f"validacion fallida: la fuente tiene {filas_atipicas} valores atipicos "
            f"pero el modelo anula {anuladas}"
        )
    if not math.isfinite(anuladas):  # pragma: no cover - guardia de tipos
        raise ErrorDeIntegridad("conteo de atipicos no finito")
