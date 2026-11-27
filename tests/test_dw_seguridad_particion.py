"""Pruebas de la seguridad por perfil y del particionamiento (sisap.dw)."""

from datetime import date

import duckdb
import pandas as pd
import pytest

from sisap import cli
from sisap.dw import construir_dw
from sisap.dw.particion import comparar_rendimiento, particionar
from sisap.dw.seguridad import (
    PERFILES,
    SQL_ASIGNAR_MACRORREGION,
    PerfilInvalido,
    exportar_perfil,
)

COLUMNAS_INTERNAS = {"precio_min_crudo", "precio_prom_crudo", "precio_max_crudo",
                     "flag_atipico", "carga_id"}


def _filas() -> list[dict]:
    """Arroz en Arequipa (Sur) con un atipico en mayo, y papa en Piura (Norte)."""
    filas = []
    for mes, precio in zip(range(1, 7), [166, 173, 175, 170, 1.7, 176], strict=True):
        for tipo, factor in [("min", 0.95), ("prom", 1.0), ("max", 1.05)]:
            filas.append({
                "fecha": date(2024, mes, 1), "region": "Arequipa", "producto": "Arroz extra",
                "unidad": "Saco", "equivalencia_kg": 50.0, "variable": f"may_precio_{tipo}",
                "precio": precio * factor, "periodicidad": "mensual",
                "fecha_captura": date(2024, 7, 1)})
    for anio in (2023, 2024, 2025):
        for tipo, precio in [("min", 1.0), ("prom", 1.2), ("max", 1.4)]:
            filas.append({
                "fecha": date(anio, 3, 1), "region": "Piura", "producto": "Papa blanca",
                "unidad": "Kilogramo", "equivalencia_kg": 1.0, "variable": f"may_precio_{tipo}",
                "precio": precio, "periodicidad": "mensual", "fecha_captura": date(anio, 4, 1)})
    return filas


@pytest.fixture
def dw(tmp_path):
    ruta_parquet = tmp_path / "precios.parquet"
    pd.DataFrame(_filas()).to_parquet(ruta_parquet, index=False)
    ruta_dw = tmp_path / "dw.duckdb"
    construir_dw(ruta_parquet, ruta_dw)
    return ruta_dw


def _columnas(con, vista):
    return {fila[0] for fila in con.execute("DESCRIBE " + vista).fetchall()}


# --- Vistas por perfil ---------------------------------------------------------

def test_perfil_publico_no_ve_crudos_ni_linaje(dw):
    con = duckdb.connect(str(dw), read_only=True)
    for vista in ("publico.precios_mensuales", "publico.precios_diarios"):
        assert not (_columnas(con, vista) & COLUMNAS_INTERNAS)
    # el atipico de mayo aparece como precio sin valor, sin decir por que
    assert con.execute("SELECT precio_prom FROM publico.precios_mensuales "
                       "WHERE region = 'Arequipa' AND mes = DATE '2024-05-01'").fetchall() == [(None,)]
    assert con.execute("SELECT COUNT(*) FROM publico.precios_mensuales").fetchone()[0] == \
        con.execute("SELECT COUNT(*) FROM fact_precio_mensual").fetchone()[0]
    con.close()


def test_perfil_analista_ve_crudos_pero_no_el_mensaje_de_auditoria(dw):
    con = duckdb.connect(str(dw), read_only=True)
    assert COLUMNAS_INTERNAS <= _columnas(con, "analista.precios_mensuales")
    assert con.execute("SELECT precio_prom_crudo, flag_atipico FROM analista.precios_mensuales "
                       "WHERE region = 'Arequipa' AND mes = DATE '2024-05-01'").fetchall() == [(1.7, True)]
    con.close()

    # una carga con advertencia: el analista ve que hay mensaje, no su contenido
    con = duckdb.connect(str(dw))
    con.execute("UPDATE etl_carga SET mensaje = 'detalle interno'")
    assert con.execute("SELECT mensaje FROM analista.cargas").fetchall() == [("(reservado al auditor)",)]
    assert con.execute("SELECT mensaje FROM auditor.cargas").fetchall() == [("detalle interno",)]
    con.close()


def test_perfil_auditor_ve_atipicos_y_trazabilidad(dw):
    con = duckdb.connect(str(dw), read_only=True)
    assert con.execute("SELECT region, precio_prom_crudo FROM auditor.atipicos").fetchall() == [
        ("Arequipa", 1.7)]
    assert con.execute("SELECT carga_id, hechos_mensuales_vigentes FROM auditor.hechos_por_carga"
                       ).fetchall() == [(1, 9)]
    assert con.execute("SELECT duracion_s >= 0 FROM auditor.cargas").fetchall() == [(True,)]
    con.close()


def test_perfil_regional_deniega_por_defecto_y_filtra_por_macrorregion(dw):
    con = duckdb.connect(str(dw), read_only=True)
    assert con.execute("SELECT COUNT(*) FROM regional.precios_mensuales").fetchone()[0] == 0

    con.execute(SQL_ASIGNAR_MACRORREGION, ["Sur"])
    assert con.execute("SELECT DISTINCT region FROM regional.precios_mensuales").fetchall() == [
        ("Arequipa",)]
    con.execute(SQL_ASIGNAR_MACRORREGION, ["Norte"])
    assert con.execute("SELECT DISTINCT region FROM regional.precios_mensuales").fetchall() == [
        ("Piura",)]
    # un valor que no es una macrorregion no abre nada ni inyecta SQL
    con.execute(SQL_ASIGNAR_MACRORREGION, ["Sur' OR '1'='1"])
    assert con.execute("SELECT COUNT(*) FROM regional.precios_mensuales").fetchone()[0] == 0
    con.close()


def test_las_vistas_sobreviven_a_una_recarga(dw, tmp_path):
    construir_dw(tmp_path / "precios.parquet", dw)
    con = duckdb.connect(str(dw), read_only=True)
    for perfil in PERFILES.values():
        for vista in perfil.vistas:
            con.execute("SELECT COUNT(*) FROM " + perfil.esquema + "." + vista).fetchone()
    con.close()


# --- Extractos por perfil ----------------------------------------------------------

@pytest.mark.parametrize("perfil", ["publico", "analista", "auditor"])
def test_el_extracto_contiene_solo_las_tablas_del_perfil(dw, tmp_path, perfil):
    salida = tmp_path / f"{perfil}.duckdb"
    filas = exportar_perfil(dw, perfil, salida)

    con = duckdb.connect(str(salida), read_only=True)
    tablas = {f[0] for f in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    assert tablas == set(PERFILES[perfil].vistas) | {"perfil_extracto"}
    for tabla, n in filas.items():
        assert con.execute("SELECT COUNT(*) FROM " + tabla).fetchone()[0] == n
    if perfil == "publico":
        for tabla in tablas - {"perfil_extracto"}:
            assert not (_columnas(con, tabla) & COLUMNAS_INTERNAS)
    con.close()


def test_el_extracto_regional_solo_trae_su_macrorregion(dw, tmp_path):
    salida = tmp_path / "sur.duckdb"
    exportar_perfil(dw, "regional", salida, "Sur")

    con = duckdb.connect(str(salida), read_only=True)
    assert con.execute("SELECT DISTINCT macrorregion FROM precios_mensuales").fetchall() == [("Sur",)]
    assert con.execute("SELECT * FROM perfil_extracto").fetchall() == [("regional", "Sur")]
    con.close()


@pytest.mark.parametrize(("perfil", "macro"), [
    ("administrador", None), ("regional", None), ("regional", "Atlantida"), ("publico", "Sur"),
])
def test_exportar_rechaza_perfiles_o_macrorregiones_invalidos(dw, tmp_path, perfil, macro):
    with pytest.raises(PerfilInvalido):
        exportar_perfil(dw, perfil, tmp_path / "x.duckdb", macro)
    assert not (tmp_path / "x.duckdb").exists()


def test_exportar_no_pisa_un_extracto_salvo_que_se_pida(dw, tmp_path):
    salida = tmp_path / "publico.duckdb"
    exportar_perfil(dw, "publico", salida)
    with pytest.raises(PerfilInvalido, match="ya existe"):
        exportar_perfil(dw, "publico", salida)
    exportar_perfil(dw, "publico", salida, reemplazar=True)


def test_exportar_no_modifica_el_dw(dw, tmp_path):
    antes = dw.stat().st_mtime_ns
    exportar_perfil(dw, "regional", tmp_path / "s.duckdb", "Sur")
    assert dw.stat().st_mtime_ns == antes


# --- Particionamiento ------------------------------------------------------------------

def test_particionar_escribe_una_carpeta_por_anio_sin_perder_filas(tmp_path):
    ruta = tmp_path / "precios.parquet"
    pd.DataFrame(_filas()).to_parquet(ruta, index=False)

    filas = particionar(ruta, tmp_path / "part")

    assert filas == {2023: 3, 2024: 21, 2025: 3}
    assert sorted(p.name for p in (tmp_path / "part").iterdir()) == [
        "anio=2023", "anio=2024", "anio=2025"]
    total = duckdb.connect().execute(
        "SELECT COUNT(*) FROM read_parquet(?, hive_partitioning = true)",
        [str(tmp_path / "part" / "**" / "*.parquet")]).fetchone()[0]
    assert total == len(_filas())


def test_particionar_de_nuevo_reemplaza_y_no_duplica(tmp_path):
    ruta = tmp_path / "precios.parquet"
    pd.DataFrame(_filas()).to_parquet(ruta, index=False)
    particionar(ruta, tmp_path / "part")
    particionar(ruta, tmp_path / "part")

    total = duckdb.connect().execute(
        "SELECT COUNT(*) FROM read_parquet(?, hive_partitioning = true)",
        [str(tmp_path / "part" / "**" / "*.parquet")]).fetchone()[0]
    assert total == len(_filas())


def test_la_consulta_particionada_lee_un_solo_anio_y_da_el_mismo_resultado(tmp_path):
    ruta = tmp_path / "precios.parquet"
    filas = [f | {"producto": "Papa blanca"} for f in _filas()]
    pd.DataFrame(filas).to_parquet(ruta, index=False)

    c = comparar_rendimiento(ruta, tmp_path / "bench", 2024, repeticiones=2)

    assert c.archivo_unico.archivos_leidos == 1
    assert c.particionado.archivos_totales == 3
    assert c.particionado.archivos_leidos == 1  # poda de particiones
    assert c.particionado.bytes_leidos < c.particionado.bytes_en_disco
    assert c.archivo_unico.filas_resultado == c.particionado.filas_resultado > 0


def test_factor_replica_los_datos_solo_para_la_prueba(tmp_path):
    ruta = tmp_path / "precios.parquet"
    pd.DataFrame(_filas()).to_parquet(ruta, index=False)

    c = comparar_rendimiento(ruta, tmp_path / "bench", 2024, factor=3, repeticiones=1)

    assert c.filas_dataset == 3 * len(_filas())
    assert len(pd.read_parquet(ruta)) == len(_filas())  # la fuente no se toca


# --- CLI ---------------------------------------------------------------------------------

def test_cli_valida_perfil_macrorregion_y_factor():
    parser = cli.crear_parser()
    for argumentos in (["exportar-perfil", "--perfil", "root"],
                       ["exportar-perfil", "--perfil", "regional", "--macrorregion", "../x"],
                       ["comparar-particion", "--factor", "0"],
                       ["comparar-particion", "--factor", "1000"],
                       ["comparar-particion", "--anio", "1999"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argumentos)


def test_cli_exportar_perfil_de_punta_a_punta(tmp_path, monkeypatch, capsys):
    procesado = tmp_path / "processed"
    procesado.mkdir()
    pd.DataFrame(_filas()).to_parquet(procesado / "precios.parquet", index=False)
    monkeypatch.setattr(cli, "PROCESSED_DIR", procesado)
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    parser = cli.crear_parser()

    assert cli.cmd_construir_dw(None) == 0
    assert cli.cmd_exportar_perfil(parser.parse_args(
        ["exportar-perfil", "--perfil", "regional", "--macrorregion", "Norte"])) == 0
    assert (tmp_path / "extractos" / "regional_norte.duckdb").exists()
    # el regional sin macrorregion se rechaza con un mensaje, no con una traza
    assert cli.cmd_exportar_perfil(parser.parse_args(
        ["exportar-perfil", "--perfil", "regional"])) == 1
    assert "obligatoria" in capsys.readouterr().out
