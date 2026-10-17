import math
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# "dia": un dato de un dia puntual (modos "Dia" e "Intervalo de Tiempo").
# "mensual": el agregado oficial de SISAP para todo el mes (modo "Mensual"),
# fechado siempre el dia 1. Bug real encontrado 2026-10-17: sin esta
# columna, el agregado mensual y el dato diario del DIA 1 de ese mismo mes
# compartian la clave natural (fecha, region, producto, variable) -- uno
# pisaba al otro en silencio, y el dashboard no podia saber si el punto del
# dia 1 era un promedio de todo el mes o el precio de ese dia.
Periodicidad = Literal["dia", "mensual"]


class PriceRecord(BaseModel):
    """Un precio de un producto, en una ciudad, en una fecha, para una variable
    (ej. mayorista precio promedio)."""

    fecha: date
    region: str = Field(min_length=1)
    producto: str = Field(min_length=1)
    unidad: str
    equivalencia_kg: float
    variable: str = Field(min_length=1)
    precio: float | None
    periodicidad: Periodicidad
    # Cuando se scrapeo esta fila. Ante dos lecturas de la misma clave
    # (ej. el agregado mensual del mes en curso, que SISAP va recalculando
    # a medida que llegan dias nuevos), gana la mas reciente -- no la de
    # numero mas alto.
    fecha_captura: date = Field(default_factory=date.today)

    @field_validator("precio")
    @classmethod
    def _precio_positivo_y_finito(cls, precio: float | None) -> float | None:
        # Un precio <= 0, infinito o NaN no es un precio de mercado: es un
        # error de parseo o del sitio. Mejor fallar en el borde que dejarlo
        # llegar al dashboard como un "S/ 0.00" creible.
        if precio is not None and (not math.isfinite(precio) or precio <= 0):
            raise ValueError(f"precio invalido: {precio!r}")
        return precio
