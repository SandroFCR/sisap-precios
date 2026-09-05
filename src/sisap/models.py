from datetime import date

from pydantic import BaseModel


class PriceRecord(BaseModel):
    """Un precio de un producto, en una ciudad, en una fecha, para una variable
    (ej. mayorista precio promedio)."""

    fecha: date
    region: str
    producto: str
    unidad: str
    equivalencia_kg: float
    variable: str
    precio: float | None
