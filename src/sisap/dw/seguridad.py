"""Seguridad del data warehouse: vistas por perfil y extractos por perfil.

DuckDB es una base embebida: no tiene usuarios, roles ni GRANT. Por eso la
seguridad se implementa en dos niveles, y es importante no confundirlos:

1. VISTAS POR PERFIL (esquemas publico, analista, auditor, regional).
   Definen QUE ve cada perfil: columnas permitidas (enmascaramiento de los
   precios crudos, del linaje de carga y de los mensajes de auditoria) y
   filas permitidas (el perfil regional solo ve su macrorregion). Es control
   de acceso a nivel de la capa de consulta: quien abra el archivo .duckdb
   completo puede consultar las tablas base igual. Simula lo que en un
   motor con roles (PostgreSQL) haria GRANT SELECT sobre las vistas.

2. EXTRACTO POR PERFIL (exportar_perfil). Materializa SOLO las vistas de un
   perfil en un archivo .duckdb aparte. Ese archivo no contiene las tablas
   base, asi que la restriccion si es real: lo que no se exporto no esta.
   Es lo que se le entregaria a cada tipo de usuario.

Todo el SQL es fijo (ningun nombre ni valor se interpola).
"""

from dataclasses import dataclass
from pathlib import Path

import duckdb

# --- Perfiles -------------------------------------------------------------------


@dataclass(frozen=True)
class Perfil:
    esquema: str
    descripcion: str
    vistas: tuple[str, ...]


PERFILES: dict[str, Perfil] = {
    "publico": Perfil(
        "publico",
        "Usuario externo / dashboard: solo precios validados; sin precios "
        "crudos, sin marca de atipico, sin linaje de carga ni auditoria.",
        ("precios_mensuales", "precios_diarios"),
    ),
    "analista": Perfil(
        "analista",
        "Analista interno: ademas ve el precio crudo, la marca de atipico y el "
        "resumen de cada carga (sin los mensajes internos).",
        ("precios_mensuales", "precios_diarios", "cargas"),
    ),
    "auditor": Perfil(
        "auditor",
        "Responsable de calidad / ETL: auditoria completa de cargas, detalle de "
        "atipicos y trazabilidad de hechos por carga.",
        ("cargas", "atipicos", "hechos_por_carga"),
    ),
    "regional": Perfil(
        "regional",
        "Analista de una macrorregion: lo mismo que el analista, pero solo las "
        "filas de su macrorregion (seguridad a nivel de filas). Sin macrorregion "
        "asignada no ve nada (denegado por defecto).",
        ("precios_mensuales", "precios_diarios"),
    ),
}

MACRORREGIONES_PERMITIDAS = ("Norte", "Centro", "Sur", "Oriente")

# Variable de sesion que asigna la macrorregion al perfil regional.
SQL_ASIGNAR_MACRORREGION = "SET VARIABLE macrorregion_permitida = ?"


# --- Vistas -----------------------------------------------------------------------

_MENSUAL_BASE = """
    FROM main.fact_precio_mensual f
    JOIN main.dim_mes m ON m.mes_id = f.mes_id
    JOIN main.dim_region r ON r.region_id = f.region_id
    JOIN main.dim_producto p ON p.producto_id = f.producto_id
    JOIN main.dim_mercado k ON k.mercado_id = f.mercado_id
    JOIN main.dim_unidad u ON u.unidad_id = f.unidad_id
"""
_DIARIO_BASE = """
    FROM main.fact_precio_diario f
    JOIN main.dim_tiempo t ON t.tiempo_id = f.tiempo_id
    JOIN main.dim_region r ON r.region_id = f.region_id
    JOIN main.dim_producto p ON p.producto_id = f.producto_id
    JOIN main.dim_mercado k ON k.mercado_id = f.mercado_id
    JOIN main.dim_unidad u ON u.unidad_id = f.unidad_id
"""
_ATRIBUTOS = """
    r.nombre_region AS region, r.tipo_region, r.departamento, r.macrorregion,
    p.nombre_variedad AS variedad, p.genero, p.categoria,
    k.nombre_mercado AS mercado, u.nombre_unidad AS unidad, u.equivalencia_kg,
"""
_VALIDADOS = "f.precio_min, f.precio_prom, f.precio_max, f.precio_kg_prom"
_INTERNOS = """,
    f.precio_min_crudo, f.precio_prom_crudo, f.precio_max_crudo,
    f.flag_atipico, f.carga_id"""

SQL_VISTAS: list[str] = [
    "CREATE SCHEMA IF NOT EXISTS publico",
    "CREATE SCHEMA IF NOT EXISTS analista",
    "CREATE SCHEMA IF NOT EXISTS auditor",
    "CREATE SCHEMA IF NOT EXISTS regional",
    # publico: solo validados; el atipico aparece como precio NULL, sin decir por que
    "CREATE OR REPLACE VIEW publico.precios_mensuales AS SELECT m.mes, m.anio, m.trimestre, "
    + _ATRIBUTOS + _VALIDADOS + ", f.fuente, f.n_dias" + _MENSUAL_BASE,
    "CREATE OR REPLACE VIEW publico.precios_diarios AS SELECT t.fecha, t.anio, t.mes, "
    "t.semana_iso, " + _ATRIBUTOS + _VALIDADOS + _DIARIO_BASE,
    # analista: + crudos, marca de atipico y linaje
    "CREATE OR REPLACE VIEW analista.precios_mensuales AS SELECT m.mes, m.anio, m.trimestre, "
    + _ATRIBUTOS + _VALIDADOS + ", f.fuente, f.n_dias" + _INTERNOS + _MENSUAL_BASE,
    "CREATE OR REPLACE VIEW analista.precios_diarios AS SELECT t.fecha, t.anio, t.mes, "
    "t.semana_iso, " + _ATRIBUTOS + _VALIDADOS + _INTERNOS + _DIARIO_BASE,
    # el analista ve el resumen de cada carga, pero el mensaje interno se enmascara
    """CREATE OR REPLACE VIEW analista.cargas AS
       SELECT carga_id, inicio, fin, estado, filas_leidas, filas_cargadas,
              filas_eliminadas, filas_atipicas,
              CASE WHEN mensaje IS NULL THEN NULL ELSE '(reservado al auditor)' END AS mensaje
       FROM main.etl_carga""",
    # auditor
    """CREATE OR REPLACE VIEW auditor.cargas AS
       SELECT *, date_diff('millisecond', inicio, fin) / 1000.0 AS duracion_s
       FROM main.etl_carga""",
    # un atipico por fila de hecho (mensual o diario), con el crudo a la vista
    """CREATE OR REPLACE VIEW auditor.atipicos AS
       SELECT * FROM analista.precios_mensuales WHERE flag_atipico
       UNION ALL BY NAME
       SELECT * EXCLUDE (fecha, semana_iso, mes),
              CAST(date_trunc('month', fecha) AS DATE) AS mes, fecha AS fecha_dia,
              'diario' AS fuente
       FROM analista.precios_diarios WHERE flag_atipico""",
    """CREATE OR REPLACE VIEW auditor.hechos_por_carga AS
       SELECT c.carga_id, c.inicio, c.estado,
              (SELECT COUNT(*) FROM main.fact_precio_diario d WHERE d.carga_id = c.carga_id)
                  AS hechos_diarios_vigentes,
              (SELECT COUNT(*) FROM main.fact_precio_mensual m WHERE m.carga_id = c.carga_id)
                  AS hechos_mensuales_vigentes
       FROM main.etl_carga c""",
    # regional: filas de la macrorregion asignada; getvariable() es NULL si no
    # se asigno, y NULL = x nunca es verdadero -> 0 filas (denegado por defecto)
    """CREATE OR REPLACE VIEW regional.precios_mensuales AS
       SELECT * FROM analista.precios_mensuales
       WHERE macrorregion = getvariable('macrorregion_permitida')""",
    """CREATE OR REPLACE VIEW regional.precios_diarios AS
       SELECT * FROM analista.precios_diarios
       WHERE macrorregion = getvariable('macrorregion_permitida')""",
]


def crear_vistas_seguridad(con: duckdb.DuckDBPyConnection) -> None:
    for sentencia in SQL_VISTAS:
        con.execute(sentencia)


# --- Extractos por perfil -------------------------------------------------------------

# perfil -> [(tabla del extracto, consulta fija sobre la vista del perfil)]
CONSULTAS_EXTRACTO: dict[str, list[tuple[str, str]]] = {
    "publico": [
        ("precios_mensuales", "SELECT * FROM publico.precios_mensuales"),
        ("precios_diarios", "SELECT * FROM publico.precios_diarios"),
    ],
    "analista": [
        ("precios_mensuales", "SELECT * FROM analista.precios_mensuales"),
        ("precios_diarios", "SELECT * FROM analista.precios_diarios"),
        ("cargas", "SELECT * FROM analista.cargas"),
    ],
    "auditor": [
        ("cargas", "SELECT * FROM auditor.cargas"),
        ("atipicos", "SELECT * FROM auditor.atipicos"),
        ("hechos_por_carga", "SELECT * FROM auditor.hechos_por_carga"),
    ],
    "regional": [
        ("precios_mensuales", "SELECT * FROM regional.precios_mensuales"),
        ("precios_diarios", "SELECT * FROM regional.precios_diarios"),
    ],
}

# tabla del extracto -> sentencia fija que la crea desde los datos recibidos
SQL_CREAR_EN_EXTRACTO: dict[str, str] = {
    "precios_mensuales": "CREATE TABLE precios_mensuales AS SELECT * FROM datos_perfil",
    "precios_diarios": "CREATE TABLE precios_diarios AS SELECT * FROM datos_perfil",
    "cargas": "CREATE TABLE cargas AS SELECT * FROM datos_perfil",
    "atipicos": "CREATE TABLE atipicos AS SELECT * FROM datos_perfil",
    "hechos_por_carga": "CREATE TABLE hechos_por_carga AS SELECT * FROM datos_perfil",
}

SQL_PERFIL_DEL_EXTRACTO = "CREATE TABLE perfil_extracto AS SELECT ? AS perfil, ? AS macrorregion"


class PerfilInvalido(ValueError):
    """Perfil o macrorregion fuera de los permitidos."""


def exportar_perfil(
    ruta_dw: Path, perfil: str, ruta_salida: Path, macrorregion: str | None = None,
    reemplazar: bool = False,
) -> dict[str, int]:
    """Crea `ruta_salida` (un .duckdb nuevo) con SOLO las vistas del perfil,
    materializadas como tablas. Devuelve filas por tabla exportada. Con
    `reemplazar`, borra antes un extracto previo en esa ruta."""
    if perfil not in PERFILES:
        raise PerfilInvalido(f"perfil desconocido: {perfil!r}")
    if perfil == "regional":
        if macrorregion not in MACRORREGIONES_PERMITIDAS:
            raise PerfilInvalido("el perfil regional necesita una macrorregion valida")
    elif macrorregion is not None:
        raise PerfilInvalido("solo el perfil regional lleva macrorregion")
    if ruta_salida.exists() and reemplazar:
        ruta_salida.unlink()
    if ruta_salida.exists():
        raise PerfilInvalido(f"{ruta_salida} ya existe: no se sobrescribe un extracto")

    # el DW se abre en solo lectura; las vistas ya las creo la carga. El
    # extracto es otra base: solo recibe filas ya filtradas por la vista.
    origen = duckdb.connect(str(ruta_dw), read_only=True)
    destino = duckdb.connect(str(ruta_salida))
    filas: dict[str, int] = {}
    try:
        if perfil == "regional":
            origen.execute(SQL_ASIGNAR_MACRORREGION, [macrorregion])
        for tabla, consulta in CONSULTAS_EXTRACTO[perfil]:
            datos = origen.execute(consulta).to_arrow_table()
            destino.register("datos_perfil", datos)
            try:
                destino.execute(SQL_CREAR_EN_EXTRACTO[tabla])
            finally:
                destino.unregister("datos_perfil")
            filas[tabla] = datos.num_rows
        destino.execute(SQL_PERFIL_DEL_EXTRACTO, [perfil, macrorregion])
    finally:
        origen.close()
        destino.close()
    return filas
