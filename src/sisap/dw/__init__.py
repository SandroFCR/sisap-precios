"""Data warehouse dimensional de SISAP (proyecto del curso, rama dw-curso).

- semillas.py: jerarquias y clasificaciones de las dimensiones.
- modelo.py: el esquema (DDL) y el SQL de transformacion.
- etl.py: la carga (con auditoria en etl_carga y validaciones).

Convive con sisap.warehouse (el modelo anterior, que sigue alimentando el
dashboard) hasta que el dashboard se migre a este modelo.
"""

from sisap.dw.etl import (
    ErrorDeIntegridad,
    ResultadoCarga,
    cargar_dw,
    construir_dw,
    construir_dw_en_memoria,
)

__all__ = [
    "ErrorDeIntegridad",
    "ResultadoCarga",
    "cargar_dw",
    "construir_dw",
    "construir_dw_en_memoria",
]
