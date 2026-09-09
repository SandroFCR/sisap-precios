# SISAP Precios

Pipeline de datos end-to-end que convierte el portal de precios agropecuarios de MIDAGRI (Perú) — un sistema legado sin API, sin descarga de datos y sin forma de ver una tendencia histórica — en un dataset limpio, versionado y consultable, con un dashboard interactivo que se actualiza solo todos los días.

**[→ Ver el dashboard en vivo](https://sisap-precios.streamlit.app/)** &nbsp;·&nbsp; **[→ Código](https://github.com/SandroFCR/sisap-precios)**

---

## El problema

SISAP ([sistemas.midagri.gob.pe](http://sistemas.midagri.gob.pe/sisap/portal2/ciudades/)) es el sistema oficial donde el Ministerio de Agricultura y Riego de Perú publica precios mayoristas y minoristas de 51 productos agropecuarios en 28 regiones del país, con historia desde 2021. Es información pública y valiosa — un agricultor decidiendo cuándo vender su cosecha, un comerciante comparando mercados, un investigador estudiando inflación de alimentos — todos se benefician de poder ver esa data.

El problema es que el sistema es una interfaz de formularios de los años 2000: para ver una serie histórica hay que elegir producto, región y variable **una combinación a la vez**, sin poder exportar, sin API, sin gráficos, sin forma de comparar regiones ni detectar cuándo un precio "raro" es una tendencia real del mercado o un error de tipeo de quien lo reportó. Los datos existen, pero son prácticamente inutilizables a escala.

Este proyecto resuelve eso: reconstruye el dataset completo por fuera del sitio, lo modela como un data warehouse dimensional, lo audita con reglas explícitas para separar señal de ruido, y lo expone en un dashboard que cualquiera puede usar sin saber SQL.

---

## Qué hace, en una frase

**Scraper respetuoso → validación con Pydantic → Parquet (staging) → modelo en estrella en DuckDB → dashboard en Streamlit**, con una corrida diaria automática vía GitHub Actions.

```
┌──────────────┐    ┌──────────────┐    ┌───────────────┐    ┌──────────────────┐    ┌─────────────┐
│  SISAP        │───▶│  scraper.py  │───▶│  parser.py    │───▶│  storage.py       │───▶│  precios.   │
│  (HTML crudo) │    │  httpx +     │    │  BeautifulSoup│    │  Pydantic +       │    │  parquet    │
│               │    │  reintentos  │    │  3 parsers    │    │  dedup por unidad │    │  (staging)  │
└──────────────┘    └──────────────┘    └───────────────┘    └──────────────────┘    └──────┬──────┘
                                                                                              │
┌──────────────────┐    ┌───────────────────────────────────────────────────────────────────┘
│  dashboard.py     │◀───│  warehouse.py: modelo en estrella + reglas de outliers (DuckDB)
│  Streamlit +      │    └───────────────────────────────────────────────────────────────────
│  Plotly           │
└──────────────────┘

GitHub Actions (runner autohospedado, cron diario) ejecuta scraper → warehouse → commit,
sin intervención manual.
```

---

## Por qué las decisiones de ingeniería importan más que el código

Cualquiera puede escribir un `requests.get()` y un `for` loop. Lo que hace a este proyecto un ejercicio real de ingeniería de datos es lo que pasó **después** de que el pipeline "funcionaba": los datos se veían mal, y encontrar por qué fue el trabajo de verdad.

### 1. El sitio no tiene API — hubo que reconstruirla

Inspeccionando las peticiones del navegador se encontró el endpoint real detrás del formulario:

```
GET /sisap/portal2/ciudades/resumenes/filtrar
    ?region=150000&productos[]=0104&variables[]=may_precio_prom
    &periodicidad=mensual&__ajax_carga_final=consulta
```

Sin documentación, sin Swagger, sin nada — reversa pura leyendo el HTML de respuesta. El endpoint tiene **tres modos de periodicidad**, y cada uno devuelve una tabla con una forma distinta (filas por producto, filas por fecha, filas por año), así que **no hay un parser único**: `parser.py` tiene tres funciones dedicadas (`parse_resumen_dia`, `parse_resumen_intervalo`, `parse_resumen_mensual`), cada una resolviendo un problema de layout distinto (columnas de unidad/equivalencia que se repiten cuando se mezclan variables mayorista/minorista, celdas vacías que igual traen `class="numero"` sin el atributo `rel` que marca datos reales, etc.).

El modo mensual además acepta **varios productos en una sola petición**. Eso cambia la complejidad de la carga histórica de "miles de requests" a **168** (28 regiones × 6 variables, con los 51 productos agrupados en cada una) — la diferencia entre un scraper que tarda minutos y uno que tarda horas y probablemente termina bloqueado.

### 2. Bug de datos real #1 — colisión silenciosa de unidades

SISAP a veces reporta el **mismo** producto/región/fecha/variable **dos veces en la misma respuesta**, bajo unidades distintas (ej. "Kilogramo" y "Bandeja" para huevos el mismo mes). La clave natural de deduplicación no incluye la unidad, así que solo una fila puede sobrevivir — y la regla original (quedarse con el precio numéricamente más alto) elegía la unidad equivocada cada vez que su número crudo era mayor, sin importar cuál era la unidad *habitual* de esa serie. El error resultante era sutil (10-20% de diferencia), demasiado pequeño para disparar cualquier regla de outliers, y estuvo corrompiendo datos en silencio.

**La corrección** (`storage.py`): antes de deduplicar, se calcula qué unidad es la dominante históricamente para cada serie (`region + producto + variable`), y esa gana ante una colisión — no el precio más alto. Verificado con un caso real (Naranja washington naval/Andahuaylas) donde 5 de 12 meses tenían la unidad equivocada.

### 3. Bug de datos real #2 — outliers que no son outliers

Separar "typo de digitación" de "el mercado realmente cambió de precio" con una regla estadística simple casi siempre genera falsos positivos. La regla final en `warehouse.py` (con SQL puro, `LAG`/`LEAD` sobre una ventana por serie temporal) anula un precio solo si:

- sus dos vecinos temporales concuerdan entre sí (evidencia de que representan el nivel real, no están en medio de su propia transición), **y**
- el valor se aleja fuerte de ese nivel compartido (menos del 40% o más del 250%), bajo su propia unidad **o** bajo la unidad de los vecinos (para atrapar el caso de la colisión de unidades sin re-introducir falsos positivos).

Esto pasó por tres iteraciones reales: la primera versión era demasiado laxa (dejaba pasar típos moderados), la segunda demasiado agresiva (433 falsos positivos, todos por el bug de colisión de unidades sin corregir todavía), la tercera es la que quedó — **67 outliers genuinos** detectados sobre ~700K filas, cada uno con su propio test de regresión que documenta el caso real que lo motivó.

El valor crudo **nunca se borra** — se anula solo en la capa de hechos (`fact_precios`), y sigue disponible íntegro en `staging_precios` para auditoría.

### 4. Bug de datos real #3 — fallas silenciosas del propio servidor

Pedir el catálogo completo (51 productos × 6 años) para una región con mucho historial (Lima) hace que el **propio SISAP devuelva una página de error de timeout** en vez de la tabla — y el parser, al no encontrar filas que parsear, simplemente devolvía una lista vacía sin ningún aviso. Lima se quedó con 14 de ~85 productos minoristas esperados durante meses sin que nada lo señalara.

**La corrección** tiene dos capas: el parser ahora **falla fuerte** (`ValueError`) si detecta la página de error en vez de silenciarlo, y `poblar-historico` reintenta automáticamente año por año cuando el pedido completo falla — resiliencia que no dependía de que alguien notara el problema a simple vista.

### 5. Integridad referencial, no fe

`construir_modelo_dimensional()` reconstruye el esquema en estrella completo desde cero en cada corrida (idempotente, sin migraciones a mano) y termina con una aserción explícita: **el conteo de filas de `fact_precios` debe ser exactamente igual al de `staging_precios`**. Si un `JOIN` pierde filas (un nombre con espacio de más) o las multiplica (una dimensión con duplicados), la corrida falla ruidosamente en vez de publicar un esquema roto en silencio.

### 6. Probar el pipeline no es probar la app

Un `curl /_stcore/health` confirma que el servidor de Streamlit está vivo — **no** que el script corre sin excepciones, porque Streamlit solo ejecuta el script de verdad cuando una sesión real se conecta. Un bug real (`StreamlitWidgetAlreadyInstantiatedError`, un formulario y `session_state` compartiendo la misma clave) pasó exactamente por ese hueco: los health checks lo veían todo verde en producción mientras el dashboard tiraba una excepción a cada usuario.

Los tests de `test_dashboard.py` usan `streamlit.testing.v1.AppTest`, que simula una sesión real (incluyendo clics en el mapa, envíos de formulario y cambios de widget) y verifica que no haya excepciones — la única forma de atrapar esta clase de bug antes de que un usuario lo vea.

---

## El dashboard

- **Mapa interactivo de Perú** (Plotly + MapLibre, GeoJSON de [geoBoundaries](https://www.geoboundaries.org/), dominio público) — clic para elegir región. Estático a propósito: tras varios intentos de arreglar un parpadeo de zoom que resultó vivir en el renderizado WebGL del navegador (fuera de lo que este código controla), se desactivó el zoom/arrastre en vez de seguir persiguiendo un síntoma que no era arreglable desde Python.
- **KPIs con sparklines**: precio actual, promedio, mínimo y máximo del periodo, con indicador de tendencia (ícono + color + texto, nunca solo color, para accesibilidad).
- **Evolución histórica**: la línea y el relleno se **cortan** en huecos reales de más de 45 días (no dibuja una tendencia inventada) y se conectan con una línea punteada tenue — visualmente continuo, honesto sobre qué es dato real.
- **Variación mensual**: % de cambio contra el último dato real disponible, no contra el mes calendario anterior (un hueco de datos no debe esconder el cambio más grande de la serie).
- **Comparación regional** y tabla paginada con exportación a CSV.
- Paleta de colores validada por contraste WCAG y separación para daltonismo (ΔE en espacio OKLab), no elegida a ojo.

---

## Modelo de datos

Esquema en estrella en DuckDB, reconstruido completo en cada corrida desde `precios.parquet` (la única fuente de verdad):

```
                    ┌────────────────┐
                    │  dim_fecha      │
                    │  fecha (PK)     │
                    │  anio, mes, dia │
                    │  trimestre      │
                    └────────┬────────┘
                             │
┌──────────────┐   ┌────────▼────────┐   ┌──────────────┐
│ dim_producto  │──▶│  fact_precios    │◀──│ dim_region    │
│ producto_id   │   │  fecha           │   │ region_id     │
│ nombre        │   │  region_id (FK)  │   │ nombre_region │
└──────────────┘   │  producto_id (FK)│   └──────────────┘
                    │  variable_id (FK)│
                    │  unidad          │   ┌──────────────────┐
                    │  equivalencia_kg │──▶│  dim_variable     │
                    │  precio          │   │  variable_id      │
                    └──────────────────┘   │  tipo_mercado     │
                                            │  tipo_precio      │
                                            └──────────────────┘
```

`staging_precios` vive aparte, como capa cruda sin modelar (el parquet tal cual) — separarla del esquema en estrella deja claro qué es dato de origen y qué es resultado de negocio (como la anulación de outliers).

---

## Stack

| Capa | Herramienta | Por qué |
|---|---|---|
| Scraping | `httpx` + `tenacity` | Cliente HTTP async-ready con reintentos declarativos |
| Parsing | `BeautifulSoup4` | HTML mal formado, sin JSON — necesita tolerancia a errores |
| Validación | `Pydantic` | Un `PriceRecord` inválido falla en el borde, no 3 pasos después |
| Almacenamiento crudo | `Parquet` (pandas/pyarrow) | Columnar, comprimido, versionable en git sin pesar demasiado |
| Warehouse analítico | `DuckDB` | SQL completo (window functions) sin levantar un servidor de base de datos |
| Dashboard | `Streamlit` + `Plotly` | Iteración rápida en Python puro, sin escribir HTML/JS a mano |
| Testing | `pytest` + `AppTest` | Fixtures de HTML real capturado del sitio, no mocks inventados |
| Automatización | GitHub Actions (runner **autohospedado**) | MIDAGRI bloquea IPs de datacenter en la nube (confirmado: `ubuntu-latest` de GitHub da `ConnectTimeout`) — la única opción es correr desde una IP residencial |
| Lint | `ruff` | Rápido, sin config ceremoniosa |

---

## Estructura del proyecto

```
src/sisap/
├── scraper.py      # HTTP: arma las peticiones a los 3 endpoints de SISAP
├── parser.py       # HTML → PriceRecord: un parser por cada forma de tabla
├── models.py       # PriceRecord (Pydantic) — el contrato de datos del pipeline
├── storage.py       # Persistencia idempotente a Parquet + deduplicación por unidad habitual
├── warehouse.py     # Parquet → esquema en estrella en DuckDB + reglas de outliers
├── cli.py           # Comandos: hoy, historico, consultar, poblar-historico, construir-dwh
├── dashboard.py      # Streamlit + Plotly
└── assets/
    └── peru_regiones.geojson

tests/
├── test_parser.py      # Fixtures de HTML real capturado del sitio
├── test_storage.py     # Casos de colisión de unidades con datos reales
├── test_warehouse.py   # Cada regla de outliers con el caso real que la motivó
└── test_dashboard.py   # AppTest: simula sesiones reales, no solo health checks

.github/workflows/
└── actualizar_datos.yml   # Cron diario en runner autohospedado
```

---

## Correrlo localmente

```bash
git clone https://github.com/SandroFCR/sisap-precios.git
cd sisap-precios
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e ".[dev]"

# el repo ya trae data/processed/precios.parquet y sisap.duckdb poblados —
# el dashboard funciona sin scrapear nada primero
streamlit run src/sisap/dashboard.py
```

Para reconstruir el pipeline desde cero:

```bash
python -m sisap.cli hoy                                    # snapshot del dia
python -m sisap.cli poblar-historico --desde-anio 2021 --hasta-anio 2026  # catalogo completo
python -m sisap.cli construir-dwh                           # reconstruye el modelo en estrella
```

```bash
pytest -q            # 20 tests
ruff check src/ tests/
```

---

## Limitaciones conocidas (documentadas a propósito, no escondidas)

- El mapa muestra 25 de 28 regiones como polígonos clicables — Andahuaylas, Chota y Jaén son provincias que SISAP reporta aparte de su departamento, pero no existen como polígono propio en el GeoJSON a nivel departamental; siguen siendo elegibles por el dropdown.
- Lima y Callao no tienen mercado **mayorista** para varios productos (carnes en particular) — confirmado contra el sitio en vivo, es una característica real de la fuente, no un hueco de scraping.
- El histórico completo cubre 2021–2026, limitado por lo que SISAP expone en su interfaz.

---

## Autor

**Sandro Cusihuaman** — Ingeniero de Datos
[LinkedIn](https://www.linkedin.com/in/sandro-cusihuaman) · [GitHub](https://github.com/SandroFCR) · cusihuamanrojassandrofelipe@gmail.com
