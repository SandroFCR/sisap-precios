"""Jerarquias y clasificaciones de las dimensiones.

Que viene de SISAP y que es propio:
- Los 51 GENEROS y los 28 nombres de region salen del catalogo del portal
  (los mismos codigos de sisap.cli.CATALOGO_PRODUCTOS / CATALOGO_REGIONES;
  tests/test_dw.py verifica que no se desalineen).
- El parquet solo guarda el NOMBRE de la variedad (125). Su codigo oficial
  (6 digitos; los 4 primeros son el codigo del genero) se tomo del propio
  portal el 23/11/2026 consultando cada genero (CODIGOS_VARIEDAD). Una
  variedad nueva que aun no este en esa tabla se clasifica por el nombre
  (REGLAS_VARIEDAD, que coincide con el portal en las 125 conocidas) y la
  carga avisa para agregar su codigo.
- CATEGORIA, TIPO de region (donde no sea evidente) y MACRORREGION son
  clasificaciones PROPIAS del proyecto, no oficiales.
"""

# codigo SISAP -> nombre del genero (catalogo del portal, 51)
GENEROS: dict[str, str] = {
    "1001": "Aceite", "1018": "Aceituna botija", "0202": "Aji fresco",
    "0203": "Aji seco", "0204": "Ajo", "0401": "Arroz",
    "0301": "Arveja grano verde", "1005": "Azucar comercial", "0101": "Camote",
    "1101": "Carne fresca", "0212": "Cebolla", "0641": "Cerezas",
    "0603": "Chirimoya", "0403": "Choclo", "1010": "Fideos", "0607": "Fresa",
    "0501": "Frijol grano seco", "0302": "Frijol grano verde", "1305": "Gallo",
    "0502": "Garbanzo grano seco", "0608": "Granadilla",
    "0303": "Haba grano verde", "1011": "Harina", "1105": "Huevos",
    "1104": "Leche", "0504": "Lenteja grano seco", "0611": "Limon",
    "0614": "Mandarina", "0615": "Mango", "0617": "Manzana",
    "0620": "Melocoton", "0619": "Melon", "0622": "Naranja", "0102": "Olluco",
    "0506": "Pallar grano seco", "0626": "Palta", "0104": "Papa",
    "0627": "Papaya", "0631": "Pera", "1201": "Pescado fresco/congelado",
    "0628": "Piña", "0629": "Platano", "0405": "Quinua", "0633": "Sandia",
    "0306": "Tarhui", "0228": "Tomate", "0637": "Uva", "0229": "Vainita",
    "0105": "Yuca", "0230": "Zanahoria", "0231": "Zapallo",
}

SIN_CLASIFICAR = "Sin clasificar"

_HORTALIZAS = "Hortalizas"
_FRUTAS = "Frutas"
_TUBERCULOS = "Tubérculos y raíces"
_CEREALES = "Cereales y menestras"
_ANIMAL = "Carnes, pescado, huevos y lácteos"
_ABARROTES = "Abarrotes"

CATEGORIAS = (_HORTALIZAS, _FRUTAS, _TUBERCULOS, _CEREALES, _ANIMAL, _ABARROTES)

# nombre de genero -> categoria (clasificacion PROPIA). Criterio: los granos
# verdes (arveja, haba, frijol, vainita) van con hortalizas; solo los granos
# secos son menestras. El choclo va con los cereales (es maiz).
CATEGORIA_DE_GENERO: dict[str, str] = {
    **dict.fromkeys(
        ["Aji fresco", "Aji seco", "Ajo", "Arveja grano verde", "Cebolla",
         "Frijol grano verde", "Haba grano verde", "Tomate", "Vainita",
         "Zanahoria", "Zapallo"], _HORTALIZAS),
    **dict.fromkeys(
        ["Aceituna botija", "Cerezas", "Chirimoya", "Fresa", "Granadilla",
         "Limon", "Mandarina", "Mango", "Manzana", "Melocoton", "Melon",
         "Naranja", "Palta", "Papaya", "Pera", "Piña", "Platano", "Sandia",
         "Uva"], _FRUTAS),
    **dict.fromkeys(["Camote", "Olluco", "Papa", "Yuca"], _TUBERCULOS),
    **dict.fromkeys(
        ["Arroz", "Choclo", "Frijol grano seco", "Garbanzo grano seco",
         "Lenteja grano seco", "Pallar grano seco", "Quinua", "Tarhui"], _CEREALES),
    **dict.fromkeys(
        ["Carne fresca", "Gallo", "Huevos", "Leche",
         "Pescado fresco/congelado"], _ANIMAL),
    **dict.fromkeys(["Aceite", "Azucar comercial", "Fideos", "Harina"], _ABARROTES),
}

# prefijo (minusculas) del nombre de la variedad -> genero. Gana el prefijo
# mas largo ("frijol verde" antes que "frijol"). "papa " lleva el espacio
# para no capturar "papaya".
REGLAS_VARIEDAD: list[tuple[str, str]] = [
    ("aceite", "Aceite"),
    ("aji amarillo seco", "Aji seco"), ("aji panca", "Aji seco"),
    ("aji paprika", "Aji seco"),
    ("aji escabeche", "Aji fresco"), ("aji rocoto", "Aji fresco"),
    ("ajo", "Ajo"), ("arroz", "Arroz"), ("arveja verde", "Arveja grano verde"),
    ("azucar", "Azucar comercial"), ("camote", "Camote"),
    ("carne", "Carne fresca"), ("cebolla", "Cebolla"), ("cereza", "Cerezas"),
    ("chirimoya", "Chirimoya"), ("choclo", "Choclo"), ("fideos", "Fideos"),
    ("frijol verde", "Frijol grano verde"), ("frijol", "Frijol grano seco"),
    ("garbanzo", "Garbanzo grano seco"), ("granadilla", "Granadilla"),
    ("haba verde", "Haba grano verde"), ("harina", "Harina"),
    ("huevos", "Huevos"), ("leche", "Leche"),
    ("lenteja", "Lenteja grano seco"), ("limon", "Limon"),
    ("mandarina", "Mandarina"), ("mango", "Mango"), ("manzana", "Manzana"),
    ("melocoton", "Melocoton"), ("melon", "Melon"), ("naranja", "Naranja"),
    ("olluco", "Olluco"), ("pallar", "Pallar grano seco"), ("palta", "Palta"),
    ("papa ", "Papa"), ("papaya", "Papaya"), ("pera", "Pera"),
    ("pescado", "Pescado fresco/congelado"), ("piña", "Piña"),
    ("platano", "Platano"), ("quinua", "Quinua"), ("sandia", "Sandia"),
    ("tarhui", "Tarhui"), ("tomate", "Tomate"), ("uva", "Uva"),
    ("vainita", "Vainita"), ("yuca", "Yuca"), ("zanahoria", "Zanahoria"),
    ("zapallo", "Zapallo"),
]
# nombre de la variedad -> codigo oficial del portal (consultado 23/11/2026)
CODIGOS_VARIEDAD: dict[str, str] = {
    "Camote amarillo": "010101",
    "Camote morado": "010103",
    "Olluco largo": "010201",
    "Olluco redondo": "010202",
    "Papa amarilla": "010401",
    "Papa huayro": "010402",
    "Papa peruanita": "010403",
    "Papa huamantanga": "010404",
    "Papa blanca": "010406",
    "Papa color": "010407",
    "Papa canchan": "010411",
    "Papa perricholi": "010415",
    "Papa unica": "010417",
    "Papa yungay": "010418",
    "Papa negra andina": "010419",
    "Yuca amarilla": "010501",
    "Yuca blanca": "010502",
    "Aji escabeche": "020202",
    "Aji rocoto": "020209",
    "Aji amarillo seco": "020301",
    "Aji paprika": "020303",
    "Aji panca": "020304",
    "Ajo criollo o napuri": "020402",
    "Ajo morado": "020403",
    "Cebolla cabeza roja": "021202",
    "Tomate marzano": "022802",
    "Tomate": "022810",
    "Vainita americana": "022901",
    "Zanahoria": "023001",
    "Zapallo macre": "023103",
    "Zapallo crespo": "023105",
    "Zapallo tambeño": "023106",
    "Arveja verde americana": "030101",
    "Arveja verde blanca serrana": "030104",
    "Frijol verde canario": "030201",
    "Haba verde criolla": "030301",
    "Haba verde serrana": "030302",
    "Tarhui o chocho": "030602",
    "Arroz corriente": "040101",
    "Arroz superior": "040102",
    "Arroz extra": "040103",
    "Choclo pardo": "040302",
    "Choclo serrano semilla corriente": "040303",
    "Choclo (tipo cusco)": "040304",
    "Quinua blanca": "040505",
    "Quinua roja": "040506",
    "Quinua negra": "040507",
    "Frijol canario": "050101",
    "Frijol castilla": "050102",
    "Garbanzo": "050201",
    "Lenteja": "050406",
    "Pallar": "050601",
    "Chirimoya cumbe": "060302",
    "Granadilla (costa)": "060801",
    "Granadilla (selva)": "060802",
    "Limon sutil bolsa": "061101",
    "Limon sutil cajon": "061102",
    "Mandarina malvacea": "061410",
    "Mandarina rio de oro": "061418",
    "Mandarina satsuma": "061419",
    "Mandarina tangerina": "061421",
    "Mandarina primosole": "061423",
    "Mango criollo": "061510",
    "Mango edward huaca": "061511",
    "Mango edward": "061513",
    "Mango haden /hayde": "061516",
    "Manzana cte/para agua": "061701",
    "Manzana delicia": "061703",
    "Manzana israel": "061709",
    "Melon coquito": "061901",
    "Melon menta": "061909",
    "Melocoton blanquillo": "062003",
    "Melocoton durazno huayco": "062006",
    "Naranja tangelo costa": "062209",
    "Naranja tangelo selva": "062210",
    "Naranja valencia": "062212",
    "Naranja washington naval": "062214",
    "Palta criolla": "062605",
    "Palta fuerte": "062607",
    "Papaya": "062702",
    "Piña hawaiana": "062802",
    "Piña criolla": "062803",
    "Piña golden": "062809",
    "Platano bellaco": "062901",
    "Platano seda congo": "062903",
    "Platano isla": "062904",
    "Platano palillo": "062907",
    "Platano seda (selva)": "062910",
    "Pera agua criolla": "063101",
    "Sandia": "063301",
    "Uva alfonso lavalett": "063701",
    "Uva italia": "063707",
    "Uva red globe": "063712",
    "Uva rosada": "063716",
    "Uva thompson": "063725",
    "Uva zafiro": "063726",
    "Uva magenta": "063728",
    "Uva candy": "063730",
    "Cereza": "064101",
    "Aceite clásico botella x 1l": "100130",
    "Aceite clásico botella 900 ml": "100136",
    "Aceite cocinero 12 x 900 ml": "100137",
    "Aceite clásico botella 12 x 900 ml": "100138",
    "Azucar blanca nacional (granel)": "100507",
    "Azucar rubia nacional (granel)": "100519",
    "Fideos nicolini tallarin": "101015",
    "Fideos cogorno a granel.": "101017",
    "Fideos anita a granel": "101018",
    "Harina especial sta. anita": "101121",
    "Carne de oveja (corte único)": "110104",
    "Carne de porcino (corte único)": "110105",
    "Carne fresca de vacuno": "110106",
    "Carne de pavo (fresco)": "110108",
    "Carne de vacuno (bistec)": "110112",
    "Carne de vacuno (sancochado/corte único)": "110113",
    "Carne de pollo (eviscerado)": "110114",
    "Carne de pavo (congelado)": "110116",
    "Carne de cerdo (lechón)": "110117",
    "Leche fresca": "110401",
    "Leche reconstituida (azul) 395 gr": "110407",
    "Leche entera uht vigor en bolsa (12 bolsas de 800 ml)": "110416",
    "Leche entera uht laive en bolsa (12 bolsas de 900 ml)": "110417",
    "Huevos rosados": "110501",
    "Pescado jurel fresco": "120109",
    "Pescado bonito fresco": "120115",
}

_REGLAS_POR_LARGO = sorted(REGLAS_VARIEDAD, key=lambda regla: len(regla[0]), reverse=True)
_CODIGO_DE_GENERO = {nombre: codigo for codigo, nombre in GENEROS.items()}


def codigo_variedad(variedad: str) -> str | None:
    """Codigo oficial del portal, o None si la variedad aun no esta en la tabla."""
    return CODIGOS_VARIEDAD.get(variedad)


def clasificar_variedad(variedad: str) -> tuple[str | None, str, str]:
    """(codigo_genero, genero, categoria) de una variedad. Con codigo oficial,
    el genero sale de sus 4 primeros digitos; si no, de REGLAS_VARIEDAD. Una
    variedad que nada reconoce queda 'Sin clasificar': se carga igual y la
    carga lo avisa, en vez de romper."""
    codigo = CODIGOS_VARIEDAD.get(variedad)
    if codigo is not None and codigo[:4] in GENEROS:
        genero = GENEROS[codigo[:4]]
        return codigo[:4], genero, CATEGORIA_DE_GENERO[genero]
    nombre = variedad.lower()
    for prefijo, genero in _REGLAS_POR_LARGO:
        if nombre.startswith(prefijo):
            return _CODIGO_DE_GENERO[genero], genero, CATEGORIA_DE_GENERO[genero]
    return None, SIN_CLASIFICAR, SIN_CLASIFICAR


# (codigo SISAP, nombre, tipo, departamento). Las provincias que el portal
# reporta aparte (Andahuaylas, Chota, Jaen) solapan con su departamento:
# sumar departamentos y provincias juntos cuenta dos veces esas zonas.
REGIONES: list[tuple[str, str, str, str]] = [
    ("010000", "Amazonas", "departamento", "Amazonas"),
    ("020000", "Ancash", "departamento", "Ancash"),
    ("030000", "Apurimac", "departamento", "Apurimac"),
    ("030201", "Andahuaylas", "provincia", "Apurimac"),
    ("040000", "Arequipa", "departamento", "Arequipa"),
    ("050000", "Ayacucho", "departamento", "Ayacucho"),
    ("060000", "Cajamarca", "departamento", "Cajamarca"),
    ("060401", "Chota", "provincia", "Cajamarca"),
    ("060801", "Jaen", "provincia", "Cajamarca"),
    ("070000", "Callao", "departamento", "Callao"),
    ("080000", "Cusco", "departamento", "Cusco"),
    ("090000", "Huancavelica", "departamento", "Huancavelica"),
    ("100000", "Huanuco", "departamento", "Huanuco"),
    ("110000", "Ica", "departamento", "Ica"),
    ("120000", "Junin", "departamento", "Junin"),
    ("130000", "La libertad", "departamento", "La libertad"),
    ("140000", "Lambayeque", "departamento", "Lambayeque"),
    ("150000", "Lima", "departamento", "Lima"),
    ("160000", "Loreto", "departamento", "Loreto"),
    ("170000", "Madre de dios", "departamento", "Madre de dios"),
    ("180000", "Moquegua", "departamento", "Moquegua"),
    ("190000", "Pasco", "departamento", "Pasco"),
    ("200000", "Piura", "departamento", "Piura"),
    ("210000", "Puno", "departamento", "Puno"),
    ("220000", "San martin", "departamento", "San martin"),
    ("230000", "Tacna", "departamento", "Tacna"),
    ("240000", "Tumbes", "departamento", "Tumbes"),
    ("250000", "Ucayali", "departamento", "Ucayali"),
]

MACRORREGIONES = ("Norte", "Centro", "Sur", "Oriente")

# departamento -> macrorregion (clasificacion PROPIA, simple y no oficial: un
# departamento va entero a una sola macrorregion aunque tenga costa y sierra).
MACRORREGION_DE_DEPARTAMENTO: dict[str, str] = {
    **dict.fromkeys(
        ["Tumbes", "Piura", "Lambayeque", "La libertad", "Cajamarca", "Amazonas"], "Norte"),
    **dict.fromkeys(
        ["Ancash", "Lima", "Callao", "Huanuco", "Pasco", "Junin", "Ica",
         "Huancavelica"], "Centro"),
    **dict.fromkeys(
        ["Apurimac", "Ayacucho", "Arequipa", "Cusco", "Moquegua", "Tacna", "Puno"], "Sur"),
    **dict.fromkeys(["Loreto", "San martin", "Ucayali", "Madre de dios"], "Oriente"),
}


def clasificar_region(nombre_region: str) -> tuple[str, str, str, str]:
    """(codigo_sisap, tipo_region, departamento, macrorregion). Una region
    que no esta en el catalogo queda 'Sin clasificar' y la carga lo avisa."""
    for codigo, nombre, tipo, departamento in REGIONES:
        if nombre == nombre_region:
            return codigo, tipo, departamento, MACRORREGION_DE_DEPARTAMENTO[departamento]
    return "SIN-CODIGO", SIN_CLASIFICAR, SIN_CLASIFICAR, SIN_CLASIFICAR
