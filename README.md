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

**Scraper respetuoso → validación con Pydantic → Parquet (staging) → modelo en estrella en DuckDB → dashboard en Streamlit**, con una corrida diaria automática vía GitHub Actions que además recupera los días perdidos, refresca el agregado mensual de las 28 regiones cada semana y avisa si los datos se quedan viejos.

```
┌──────────────┐    ┌──────────────┐    ┌───────────────┐    ┌──────────────────┐    ┌─────────────┐
│  SISAP        │───▶│  scraper.py  │───▶│  parser.py    │───▶│  storage.py       │───▶│  precios.   │
│  (HTML crudo) │    │  httpx +     │    │  BeautifulSoup│    │  Pydantic +       │    │  parquet    │
│               │    │  reintentos  │    │  3 parsers    │    │  dedup por unidad │    │  (staging)  │
└──────────────┘    └──────────────┘    └───────────────┘    └──────────────────┘    └──────┬──────┘
                                                                                              │
┌──────────────────┐    ┌───────────────────────────────────────────────────────────────────┘
│  dashboard.py     │◀───│  warehouse.py: modelo en estrella + reglas de outliers (DuckDB,
│  Streamlit +      │    │  armado en memoria al arrancar el dashboard, ~1 s)
│  Plotly           │    └───────────────────────────────────────────────────────────────────
└──────────────────┘

GitHub Actions (runner autohospedado, cron diario): dato diario de la última semana, hoy
incluido → refresco del agregado mensual (al empezar cada mes y cada 6+ días) →
validación del warehouse → commit del parquet → alarma de frescura. Sin intervención manual.
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

Esto pasó por tres iteraciones reales: la primera versión era demasiado laxa (dejaba pasar típos moderados), la segunda demasiado agresiva (433 falsos positivos, todos por el bug de colisión de unidades sin corregir todavía), la tercera es la que quedó — **67 outliers genuinos** detectados sobre ~700K filas en su momento (con el dataset re-scrapeado completo en octubre de 2026: 95 sobre ~671K filas, 93 mensuales y 2 diarios), cada regla con su propio test de regresión que documenta el caso real que la motivó.

El valor crudo **nunca se borra** — se anula solo en la capa de hechos (`fact_precios`), y sigue disponible íntegro en `staging_precios` para auditoría.

### 4. Bug de datos real #3 — fallas silenciosas del propio servidor

Pedir el catálogo completo (51 productos × 6 años) para una región con mucho historial (Lima) hace que el **propio SISAP devuelva una página de error de timeout** en vez de la tabla — y el parser, al no encontrar filas que parsear, simplemente devolvía una lista vacía sin ningún aviso. Lima se quedó con 14 de ~85 productos minoristas esperados durante meses sin que nada lo señalara.

**La corrección** tiene dos capas: el parser ahora **falla fuerte** (`ValueError`) si detecta la página de error en vez de silenciarlo, y `poblar-historico` reintenta automáticamente año por año cuando el pedido completo falla — resiliencia que no dependía de que alguien notara el problema a simple vista.

### 5. Integridad referencial, no fe

`construir_modelo_dimensional()` reconstruye el esquema en estrella completo desde cero en cada corrida (idempotente, sin migraciones a mano) y termina con una aserción explícita: **el conteo de filas de `fact_precios` debe ser exactamente igual al de `staging_precios`**. Si un `JOIN` pierde filas (un nombre con espacio de más) o las multiplica (una dimensión con duplicados), la corrida falla ruidosamente en vez de publicar un esquema roto en silencio.

### 6. Probar el pipeline no es probar la app

Un `curl /_stcore/health` confirma que el servidor de Streamlit está vivo — **no** que el script corre sin excepciones, porque Streamlit solo ejecuta el script de verdad cuando una sesión real se conecta. Un bug real (`StreamlitWidgetAlreadyInstantiatedError`, un formulario y `session_state` compartiendo la misma clave) pasó exactamente por ese hueco: los health checks lo veían todo verde en producción mientras el dashboard tiraba una excepción a cada usuario.

Los tests de `test_dashboard.py` usan `streamlit.testing.v1.AppTest`, que simula una sesión real (incluyendo clics en el mapa, envíos de formulario y cambios de widget) y verifica que no haya excepciones — la única forma de atrapar esta clase de bug antes de que un usuario lo vea.

### 7. Expandir la automatización sin asumir que "todas las regiones son iguales"

La automatización diaria (`cmd_hoy`/`cmd_historico`) arrancó pensada para un puñado fijo de productos, solo en Lima — una limitación de nuestro alcance, no del sitio. Al expandirla al catálogo completo en varias regiones, pedir el modo "Intervalo de Tiempo" para Cusco devolvió la misma página de error silenciosa del bug #4, pero por una causa distinta: no era "pedimos demasiado" (particionar productos no cambiaba nada, ni con 1 solo producto), sino que **esa región no tiene ese tipo de reporte en SISAP en absoluto** (el modo "Mensual" sí funciona ahí con normalidad). El reintento ahora diagnostica antes de insistir: prueba con un solo producto primero, y si eso también falla, se rinde para esa región en vez de particionar el catálogo entero contra un error que ninguna partición iba a arreglar.

Un efecto secundario de la misma expansión: el gráfico "Evolución histórica" empezó a anclar cada punto mensual en fechas distintas según si el mes tenía grano diario o no (día 1 vs. día 30), lo que inflaba artificialmente la distancia entre meses consecutivos y activaba el corte de huecos de más de 45 días (pensado para huecos *reales* de reporte, ver sección "El dashboard") donde no correspondía — un mes con dato diario terminaba viéndose desconectado del anterior aunque hubo datos continuos. La fecha de cada punto ahora se ancla siempre al día 1 del mes, sin importar la granularidad real detrás — consistente con cómo ya se interpretaban los meses puramente mensuales.

### 8. Datos que se veían bien pero estaban congelados

Una auditoría del dataset (2026-10-17), seis semanas después de dejar la automatización corriendo sola, encontró cinco problemas que ningún test ni health check marcaba, porque el dashboard seguía mostrando números creíbles:

- **25 de 28 regiones congeladas.** La corrida diaria solo pedía el dato diario de 3 regiones; nada volvía a pedir el agregado mensual después de la carga inicial (5 de septiembre). Esas regiones se quedaron con un septiembre parcial (5 días) y sin octubre, mientras "Comparación entre regiones" ponía a Lima del 17 de octubre al lado de ellas. **Fix:** `refrescar-mensual` pide solo los meses recientes (el parámetro `meses[]` acepta cualquier subconjunto: 2 meses × 51 productos = 1 pedido de ~5 s) y se salta la corrida si el último refresco tiene menos de N días. Así el costo (168 pedidos) se paga una vez por semana aunque el runner haya estado apagado justo el día "que tocaba".
- **Un refresco que no podía refrescar.** Ante dos lecturas de la misma clave, la deduplicación se quedaba con el precio más alto: el promedio parcial viejo le ganaba al mes completo cada vez que era mayor. **Fix:** cada fila guarda su `fecha_captura` y gana la más reciente (la unidad habitual sigue teniendo prioridad, ver bug #2).
- **El día 1 significaba dos cosas.** El agregado mensual se fecha el día 1, y el dato diario del día 1 tenía la misma clave natural: uno pisaba al otro en silencio. **Fix:** columna `periodicidad` (`dia`/`mensual`) en el contrato de datos y en la clave. Las dos series se evalúan por separado en la regla de outliers, y una tabla `fact_precios_mensual` da un valor por mes y región: el agregado oficial de SISAP cuando existe (la misma metodología en las 28 regiones) y el promedio de los días solo para los meses que todavía no lo tienen.
- **Días perdidos para siempre, y congelados en su versión de la mañana.** `hoy` solo pide la fecha de hoy: 6 días entre septiembre y octubre quedaron sin dato (runner apagado o SISAP sin responder). Peor: SISAP sigue corrigiendo el dato de un día después de publicarlo (165 de 402 precios de Lima del 14 de septiembre cambiaron), y la captura de la mañana quedaba fija. **Fix:** cada corrida vuelve a pedir la última semana (`historico --ultimos-dias 7`), hoy incluido.
- **Dos modos del sitio, dos respuestas.** Para el mismo día, el modo "Día" y el modo "Intervalo" de SISAP no coinciden en el precio máximo, y el promedio del modo "Intervalo" es el que cuadra con el agregado mensual oficial (error mediano 0,0 % contra 1,8 %). Además, el parser del modo "Día" usaba la unidad del grupo mayorista para los precios minoristas: Ají rocoto (mayorista en "Cajón" de 18 kg, minorista por kilo) salía 18 veces más barato por kg, y un producto sin mayorista quedaba con unidad vacía. **Fix:** el parser toma la unidad de cada grupo (test con el HTML real), y la automatización usa solo el modo "Intervalo", así que todo el dato diario sale de una única fuente consistente.
- **Corridas "verdes" sin datos nuevos.** **Fix:** `verificar-frescura` pone el job en rojo si el dato diario o el mensual pasan su antigüedad máxima, y una región que falla ya no corta la corrida de las demás: se registra, se commitea lo que sí llegó y el job termina en rojo.

De paso: el 14% del parquet eran filas sin precio (celdas vacías de productos sin minorista y meses futuros del año en curso) que ya no se guardan. El `.duckdb` (19 MB, distinto byte a byte en cada corrida, ~90% del crecimiento diario del repo) dejó de versionarse: el dashboard arma el modelo en memoria desde el parquet en ~1 s, y nunca puede quedar leyendo un warehouse desfasado.

### 9. Seguridad con un modelo de amenaza concreto

No hay usuarios, secretos ni datos personales, pero sí tres superficies reales, cada una cubierta por un test en `test_seguridad.py`:

1. **Texto externo, sin TLS.** SISAP no completa un handshake TLS moderno, así que se baja por HTTP (riesgo aceptado: datos públicos; el scraper no sigue redirecciones y todo pasa por la validación de `PriceRecord`, que rechaza precios ≤0, NaN o infinitos). Ese texto termina en un dashboard público, así que nombres de producto y unidades se escapan antes de llegar a cualquier widget que interprete Markdown o HTML. El CSV descargable neutraliza celdas que una planilla ejecutaría como fórmula (`=`, `+`, `-`, `@`), y los nombres de archivo se sanean.
2. **SQL y rutas.** Toda consulta va con parámetros (ninguna interpola strings; lo verifican ruff `S608` y un test estático). Los argumentos de la CLI que terminan en URLs y en nombres de archivo bajo `data/raw/` se validan contra su formato (`../../x` no pasa).
3. **Runner autohospedado en un repo público.** Un workflow con `pull_request` que corriera en ese runner ejecutaría código de cualquier fork en la PC del autor: un test lo prohíbe. Además, las actions están fijadas por SHA de commit (un tag se puede mover), cada workflow declara permisos mínimos, las dependencias de producción están fijadas (Dependabot propone las actualizaciones) y CI corre `pip-audit` en cada push, incluidos los commits diarios del bot. Streamlit ya no muestra tracebacks completos a los visitantes (`showErrorDetails = "type"`).

Los tests de seguridad se validaron por mutación: con la defensa quitada, el test correspondiente falla.

---

## El dashboard

- **Mapa interactivo de Perú** (Plotly + MapLibre, GeoJSON de [geoBoundaries](https://www.geoboundaries.org/), dominio público) — clic para elegir región. Estático a propósito: tras varios intentos de arreglar un parpadeo de zoom que resultó vivir en el renderizado WebGL del navegador (fuera de lo que este código controla), se desactivó el zoom/arrastre en vez de seguir persiguiendo un síntoma que no era arreglable desde Python.
- **Selector visual de productos** (fotos reales) debajo del mapa, como atajo a los productos más buscados — no reemplaza el dropdown completo (~125 productos), lo complementa.
- **Vista "Todo el Perú"**: opción del selector de región que promedia entre todas las regiones que reportan cada fecha, en vez de mirar una sola — con aviso explícito de que no todas reportan el mismo mes.
- **KPIs con sparklines**: precio actual, promedio, mínimo y máximo del periodo, con indicador de tendencia (ícono + color + texto — nunca solo color, para accesibilidad — y un tercer estado neutro explícito para "sin cambio", no una flecha de baja con signo de subida).
- **Evolución histórica** (un punto por mes, con barra de rango para navegar arrastrando): la línea y el relleno se **cortan** en huecos reales de más de 45 días (no dibuja una tendencia inventada) y se conectan con una línea punteada tenue — visualmente continuo, honesto sobre qué es dato real. Cada punto mensual es el **promedio** de ese mes, no el último dato — importa para los productos con automatización diaria (ver abajo), donde tomar solo el último día convertía una suba gradual real en un salto vertical falso entre un mes y el siguiente.
- **Últimos días**: gráfico de grano diario/sub-mensual real (no agregado), para los productos y regiones donde la automatización lo captura así — se detecta automáticamente si existe esa granularidad antes de dibujar, en vez de mostrar un gráfico vacío para el resto del catálogo.
- **Comparación entre regiones**: cada región muestra su **propio** dato más reciente (no exige que coincida con la fecha global más nueva de la tabla) — necesario desde que algunas regiones reportan a diario y otras solo mensual.
- Tabla paginada con exportación a CSV.
- Paleta de colores validada por contraste WCAG y separación para daltonismo (ΔE en espacio OKLab), no elegida a ojo.

---

## Modelo de datos

Esquema en estrella en DuckDB, reconstruido completo desde `precios.parquet` (la única fuente de verdad versionada): en memoria cada vez que arranca el dashboard, y como archivo (`sisap.duckdb`, fuera de git) con `construir-dwh` para consultas SQL locales:

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
                    │  periodicidad    │   ┌──────────────────┐
                    │  unidad          │──▶│  dim_variable     │
                    │  equivalencia_kg │   │  variable_id      │
                    │  precio          │   │  tipo_mercado     │
                    └────────┬─────────┘   │  tipo_precio      │
                             │             └──────────────────┘
                    ┌────────▼──────────────┐
                    │  fact_precios_mensual  │  1 valor por mes x región x producto
                    │  mes, *_id, precio_kg  │  x variable: el agregado oficial de
                    │  fuente, n_datos       │  SISAP, o el promedio de los días
                    └───────────────────────┘  si el mes todavía no lo tiene
```

La clave natural es `(fecha, region, producto, variable, periodicidad)`: el agregado mensual (fechado el día 1) y el dato diario del día 1 conviven. `staging_precios` vive aparte, como capa cruda sin modelar (el parquet tal cual, con su `fecha_captura`) — separarla del esquema en estrella deja claro qué es dato de origen y qué es resultado de negocio (como la anulación de outliers).

---

## Stack

| Capa | Herramienta | Por qué |
|---|---|---|
| Scraping | `httpx` + `tenacity` | Cliente HTTP async-ready con reintentos declarativos |
| Parsing | `BeautifulSoup4` | HTML mal formado, sin JSON — necesita tolerancia a errores |
| Validación | `Pydantic` | Un `PriceRecord` inválido falla en el borde, no 3 pasos después |
| Almacenamiento crudo | `Parquet` (pandas/pyarrow) | Columnar, comprimido, versionable en git sin pesar demasiado |
| Warehouse analítico | `DuckDB` | SQL completo (window functions) sin levantar un servidor de base de datos; el dashboard lo arma en memoria en ~1 s |
| Dashboard | `Streamlit` + `Plotly` | Iteración rápida en Python puro, sin escribir HTML/JS a mano |
| Testing | `pytest` + `AppTest` | Fixtures de HTML real capturado del sitio, no mocks inventados |
| Automatización | GitHub Actions (runner **autohospedado**) | MIDAGRI bloquea IPs de datacenter en la nube (confirmado: `ubuntu-latest` de GitHub da `ConnectTimeout`) — la única opción es correr desde una IP residencial |
| CI | GitHub Actions (`ubuntu-latest`) | `pytest` + `ruff` + `pip-audit` en cada push/PR — a diferencia del scraper, los tests solo leen el parquet ya versionado, no le pegan a MIDAGRI, así que no necesitan el runner autohospedado (y nunca deben usarlo: corre código de PRs de forks) |
| Lint | `ruff` (+ reglas de seguridad `S`) | Rápido, sin config ceremoniosa; las reglas `S` (equivalentes a bandit) atrapan SQL armado con strings, `assert` usado como validación, etc. |
| Dependencias | versiones fijas + Dependabot + `pip-audit` | Una release nueva no puede romper producción sin pasar antes por un PR con CI |

---

## Estructura del proyecto

```
src/sisap/
├── scraper.py      # HTTP: arma las peticiones a los 3 endpoints de SISAP
├── parser.py       # HTML → PriceRecord: un parser por cada forma de tabla
├── models.py       # PriceRecord (Pydantic) — el contrato de datos del pipeline
├── storage.py       # Persistencia idempotente a Parquet + deduplicación (unidad habitual, captura más reciente)
├── warehouse.py     # Parquet → esquema en estrella en DuckDB + reglas de outliers + vista mensual
├── cli.py           # Comandos: hoy, historico, consultar, poblar-historico, refrescar-mensual,
│                    #           construir-dwh, verificar-frescura
├── sanitizar.py     # Texto de SISAP antes de mostrarlo/exportarlo (Markdown, CSV, nombres de archivo)
├── dashboard.py      # Streamlit + Plotly
└── assets/
    ├── peru_regiones.geojson
    └── productos/       # fotos del selector visual + CREDITOS.md (licencias por imagen)

tests/
├── test_parser.py      # Fixtures de HTML real capturado del sitio
├── test_storage.py     # Casos de colisión de unidades con datos reales
├── test_warehouse.py   # Cada regla de outliers con el caso real que la motivó + vista mensual
├── test_cli.py         # Refresco mensual, recuperación de días, fallas aisladas por región, frescura
├── test_seguridad.py   # Una prueba por defensa (ver sección 9)
└── test_dashboard.py   # AppTest: simula sesiones reales, no solo health checks

.github/
├── workflows/
│   ├── actualizar_datos.yml   # Cron diario en runner autohospedado (necesita IP residencial)
│   └── ci.yml                 # pytest + ruff + pip-audit en cada push/PR, en runner normal de GitHub
└── dependabot.yml             # PRs semanales con actualizaciones de dependencias y actions
```

---

## Correrlo localmente

```bash
git clone https://github.com/SandroFCR/sisap-precios.git
cd sisap-precios
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e ".[dev]"

# el repo ya trae data/processed/precios.parquet poblado — el dashboard arma
# el modelo en memoria al arrancar, funciona sin scrapear nada primero
streamlit run src/sisap/dashboard.py
```

Para reconstruir el pipeline desde cero:

```bash
python -m sisap.cli poblar-historico --desde-anio 2021 --hasta-anio 2026  # agregado mensual, catalogo completo
python -m sisap.cli historico --desde 2026-08-01 --hasta 2026-10-17       # dato diario de un rango
python -m sisap.cli hoy                                    # snapshot del dia
python -m sisap.cli historico --ultimos-dias 7             # recupera la ultima semana
python -m sisap.cli refrescar-mensual --meses 2            # re-pide el agregado de los ultimos 2 meses
python -m sisap.cli construir-dwh                          # sisap.duckdb local, para consultas SQL
python -m sisap.cli verificar-frescura                     # codigo 1 si los datos estan viejos
```

```bash
pytest -q            # 91 tests
ruff check .
pip-audit --skip-editable
```

---

## Limitaciones conocidas (documentadas a propósito, no escondidas)

- El mapa muestra 25 de 28 regiones como polígonos clicables — Andahuaylas, Chota y Jaén son provincias que SISAP reporta aparte de su departamento, pero no existen como polígono propio en el GeoJSON a nivel departamental; siguen siendo elegibles por el dropdown.
- Callao no tiene mercado **mayorista** en SISAP en absoluto (ningún producto, ningún año: el sitio devuelve su página de error), y Lima no lo tiene para varios productos (carnes en particular) — confirmado contra el sitio en vivo, es una característica real de la fuente, no un hueco de scraping.
- El histórico completo cubre 2021–2026, limitado por lo que SISAP expone en su interfaz.
- 4 de los 51 géneros del catálogo de SISAP (Aceituna botija, Cerezas, Fresa, Gallo) están listados en el checkbox de productos del sitio pero nunca tienen precio reportado, en ninguna región ni año — confirmado pidiéndolos en vivo contra el sitio (devuelve la página de "sin datos" de SISAP, no un timeout ni un error nuestro). No aparecen en el dropdown de Producto del dashboard porque nunca hay una fila que agregar a `dim_producto`.
- El dato diario/sub-mensual real (catálogo completo, no solo un puñado de productos) se recolecta en Lima, Arequipa y Piura — **Cusco no tiene reporte "Día" ni "Intervalo de Tiempo" en SISAP en absoluto** (confirmado: falla hasta con 1 solo producto, 1 solo día; el modo "Mensual" sí funciona ahí con normalidad). El resto de las 28 regiones solo tiene el agregado mensual porque todavía no se agregaron a la automatización, no por una limitación de la fuente.
- El agregado mensual se refresca una vez por semana: para las regiones sin dato diario, el mes en curso puede tener hasta ~6 días de atraso.
- SISAP se baja por HTTP: el servidor no completa un handshake TLS moderno (ver sección 9).
- El historial de git todavía carga ~40 versiones del `.duckdb` de cuando se versionaba (~200 MB); dejar de versionarlo frena el crecimiento, pero achicar el historial requiere reescribirlo (`git filter-repo`) y un force push.

---

## Autor

**Sandro Cusihuaman** — Ingeniero de Datos
[LinkedIn](https://www.linkedin.com/in/sandro-cusihuaman) · [GitHub](https://github.com/SandroFCR) · cusihuamanrojassandrofelipe@gmail.com
