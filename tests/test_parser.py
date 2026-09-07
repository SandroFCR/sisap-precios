from datetime import date
from pathlib import Path

import pytest

from sisap.parser import (
    parse_resumen_dia,
    parse_resumen_intervalo,
    parse_resumen_mensual,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_resumen_dia_un_producto_una_variable():
    html = (FIXTURES / "resumen_dia_ajo_lima.html").read_text(encoding="utf-8")

    registros = parse_resumen_dia(
        html,
        fecha=date(2026, 9, 5),
        region="Lima",
        variables=["may_precio_prom"],
    )

    assert len(registros) == 2

    ajo_criollo = registros[0]
    assert ajo_criollo.producto == "Ajo criollo o napuri"
    assert ajo_criollo.unidad == "Kilogramo"
    assert ajo_criollo.equivalencia_kg == 1.00
    assert ajo_criollo.variable == "may_precio_prom"
    assert ajo_criollo.precio == 5.75

    ajo_morado = registros[1]
    assert ajo_morado.producto == "Ajo morado"
    assert ajo_morado.precio == 7.75


def test_parse_resumen_dia_variables_de_grupos_distintos():
    """Regresion: mayorista y minorista repiten columnas de unidad/equiv
    entre medio, y no se pueden ubicar los precios por posicion fija."""
    html = (
        FIXTURES / "resumen_dia_papa_lima_mayorista_minorista.html"
    ).read_text(encoding="utf-8")

    registros = parse_resumen_dia(
        html,
        fecha=date(2026, 9, 5),
        region="Lima",
        variables=["may_precio_prom", "min_precio_prom"],
    )

    papa_blanca = next(r for r in registros if r.producto == "Papa blanca")
    mayorista = next(
        r for r in registros
        if r.producto == "Papa blanca" and r.variable == "may_precio_prom"
    )
    minorista = next(
        r for r in registros
        if r.producto == "Papa blanca" and r.variable == "min_precio_prom"
    )

    assert papa_blanca.unidad == "Kilogramo"
    assert mayorista.precio == 0.89
    assert minorista.precio is None


def test_parse_resumen_intervalo_varios_dias_varios_productos():
    html = (FIXTURES / "resumen_intervalo_papa_lima.html").read_text(
        encoding="utf-8"
    )

    registros = parse_resumen_intervalo(html, region="Lima", variable="may_precio_prom")

    fechas = {r.fecha for r in registros}
    assert fechas == {
        date(2026, 9, 1),
        date(2026, 9, 2),
        date(2026, 9, 3),
        date(2026, 9, 4),
        date(2026, 9, 5),
    }

    productos = {r.producto for r in registros}
    assert "Papa amarilla" in productos
    assert "Papa yungay" in productos
    assert len(registros) == 5 * len(productos)

    papa_blanca_05 = next(
        r
        for r in registros
        if r.producto == "Papa blanca" and r.fecha == date(2026, 9, 5)
    )
    assert papa_blanca_05.precio == 0.89
    assert papa_blanca_05.unidad == "Kilogramo"
    assert papa_blanca_05.equivalencia_kg == 1.0


def test_parse_resumen_intervalo_dia_sin_ningun_dato():
    """Regresion: cuando ningun producto tiene dato ese dia, el servidor
    manda <td class=numero> vacias sin rel. No deben generar registros con
    producto vacio."""
    html = (FIXTURES / "resumen_intervalo_dia_sin_datos.html").read_text(
        encoding="utf-8"
    )

    registros = parse_resumen_intervalo(html, region="Lima", variable="min_precio_prom")

    assert all(r.producto != "" for r in registros)
    assert date(2026, 8, 30) not in {r.fecha for r in registros}


def test_parse_resumen_mensual_dos_anios_doce_meses():
    html = (FIXTURES / "resumen_mensual_yuca_lima.html").read_text(encoding="utf-8")

    registros = parse_resumen_mensual(html, region="Lima", variable="may_precio_prom")

    # 2 anios x 12 meses, la columna 'Anual' (promedio) se descarta
    assert len(registros) == 24
    assert all(r.fecha.day == 1 for r in registros)
    assert all(r.producto == "Yuca amarilla" for r in registros)
    assert all(r.unidad == "Kilogramo" for r in registros)

    setiembre_2025 = next(
        r for r in registros if r.fecha == date(2025, 9, 1)
    )
    assert setiembre_2025.precio == 1.83

    diciembre_2025 = next(
        r for r in registros if r.fecha == date(2025, 12, 1)
    )
    assert diciembre_2025.precio == 2.67


def test_parse_resumen_mensual_pagina_de_error_falla_fuerte():
    """Bug real encontrado 2026-09-07: cuando la consulta es muy grande (ej.
    51 productos x 6 anios para Lima/Minorista, que tiene mas historial que
    otras regiones), SISAP devuelve una pagina de error ("Se ha excedido el
    tiempo limite...") en vez de la tabla de precios. Como esa pagina no
    tiene ninguna fila <tr class=contenido>, antes de este fix la funcion
    devolvia una lista VACIA en silencio -- guardar_registros() lo guardaba
    como "0 registros nuevos" sin ningun error visible, y Lima se quedo con
    14 productos minoristas en vez de ~85 durante meses sin que nadie lo
    notara. Ahora debe fallar fuerte (ValueError) en vez de fallar en
    silencio."""
    html = (
        '<p class=mensajeDeError>Se ha excedido el tiempo l&iacute;mite de '
        "espera para la ejecuci&oacute;n de la consulta.</p>"
    )

    with pytest.raises(ValueError, match="SISAP devolvio una pagina de error"):
        parse_resumen_mensual(html, region="Lima", variable="min_precio_prom")
