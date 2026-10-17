"""Saneamiento de texto que viene de SISAP antes de mostrarlo o exportarlo.

Los nombres de producto y las unidades son datos de un sitio externo, que
ademas se baja por HTTP sin TLS (ver scraper.py): se tratan como texto no
confiable en cada punto donde podrian interpretarse como algo mas que texto.
"""

import re

import pandas as pd

# Puntuacion que Markdown (y el Markdown de Streamlit, que suma $...$ para
# formulas y :nombre: para emojis) puede interpretar. CommonMark permite
# escapar cualquier signo de puntuacion ASCII con una barra invertida.
_CARACTERES_MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$:])")
# Excel/LibreOffice ejecutan como formula una celda de texto que empieza con
# alguno de estos (inyeccion de CSV / "formula injection").
_INICIO_DE_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def escapar_markdown(texto: str) -> str:
    """Para st.title/st.caption/st.markdown: un nombre como "[x](url)",
    "![](url)" o "$x$" se muestra literal en vez de volverse un link, una
    imagen externa o una formula."""
    return _CARACTERES_MARKDOWN.sub(r"\\\1", str(texto))


def csv_sin_formulas(df: pd.DataFrame) -> bytes:
    """CSV para descargar en el que ninguna celda de texto se ejecute como
    formula al abrirlo en una planilla: se le antepone un apostrofe, que la
    planilla muestra como texto. Las columnas numericas no se tocan."""
    df = df.copy()
    for columna in df.columns:
        if df[columna].dtype == object or pd.api.types.is_string_dtype(df[columna]):
            df[columna] = df[columna].map(
                lambda v: f"'{v}" if isinstance(v, str) and v.startswith(_INICIO_DE_FORMULA) else v
            )
    return df.to_csv(index=False).encode("utf-8")


def nombre_de_archivo(texto: str) -> str:
    """Solo letras, numeros, guiones, puntos y guion bajo: un nombre de
    producto como "Pescado fresco/congelado" no puede meter una barra (ni
    un "..") en el nombre del archivo descargado."""
    limpio = re.sub(r"[^\w.-]+", "_", texto).strip("._")
    return limpio or "datos"
