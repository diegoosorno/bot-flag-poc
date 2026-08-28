# Bot Flag POC — Data Warehouse → Classification Upload

Procesa los 3 exports de Adobe Data Warehouse (uno por nivel de confianza
de bot) y genera un único CSV listo para subir como **Classification**
de `eVar23` (ECID) en Adobe Analytics.

## Qué hace

1. Lee los 3 archivos (`.zip` o `.csv`) de la carpeta `input/`.
2. Detecta y remueve las filas con ECID vacío (solo ceros) — de lo
   contrario se etiquetaría como bot a todos los visitantes sin ECID real.
3. Reconcilia los ECID que aparecen en más de un tier: gana el nivel
   más alto (`high` > `medium` > `low`).
4. Genera un CSV final con columnas `Key`, `Bot Flag`, dividido en
   partes si supera el tamaño máximo configurado.
5. Imprime un resumen de filas leídas, duplicados resueltos y conteo final.

## Estructura del proyecto

```
bot-flag-poc/
├── config.py              # todos los parámetros ajustables
├── process_bot_flags.py   # lógica de procesamiento
├── requirements.txt
├── input/                 # coloca aquí los 3 archivos de Data Warehouse
└── output/                # aquí queda el CSV final
```

## Requisitos de los archivos de entrada

- Nombres de archivo que empiecen con `Bots_Tier_1`, `Bots_Tier_2` y
  `Bots_Tier_3` (mapeados a `high`, `medium`, `low` respectivamente —
  ajustable en `config.py`).
- Formato: CSV con encabezados en la primera fila, columna
  `Marketing Cloud Visitor ID` con el ECID (nombre ajustable en `config.py`).
- Pueden venir comprimidos en `.zip` o como `.csv` directo.

## Instalación

```bash
git clone <url-del-repo>
cd bot-flag-poc
python -m venv venv
source venv/bin/activate   # en Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Uso

1. Copia los 3 archivos de Data Warehouse dentro de `input/`.
2. Corre:

```bash
python process_bot_flags.py
```

3. El resultado queda en `output/bot_flag_upload.csv` (o
   `bot_flag_upload_part1.csv`, `part2.csv`... si el archivo es grande).

## Configuración

Todos los parámetros específicos del caso están en `config.py`:
nombres de columnas, mapeo de archivo → nivel de confianza, tamaño
máximo del archivo de salida, tamaño de chunk de lectura. No es
necesario tocar `process_bot_flags.py` para ajustar estos valores.

## Importante — datos sensibles

Este repositorio **no** debe contener datos reales de ECID. Las
carpetas `input/` y `output/` están excluidas en `.gitignore` (solo se
versiona la estructura de carpetas vía `.gitkeep`). No remuevas esa
regla del `.gitignore` al clonar/replicar el proyecto en otra máquina.

## Siguiente paso — subida a Classifications

El archivo de salida (`Key`, `Bot Flag`) se puede subir de dos formas:

**Manual:** Admin > Classifications > eVar23 > Import File (o vía FTP si
el archivo es grande), mapeando `Key` al ECID y `Bot Flag` a la
clasificación creada para ese propósito.

**Automático (recomendado):** `upload_classifications.py` sube los CSV de
`output/` vía la **Classifications API 2.0** (flujo createApiJob →
uploadFile → commitApiJob → polling de estado). Autenticación con OAuth
Server-to-Server (JWT está deprecado).

### Credenciales (nunca se guardan en el repo)

El script lee las credenciales solo de variables de entorno. La forma
recomendada es un archivo `.env` local (ignorado por git):

```bash
cp .env.example .env
# edita .env y pon tus valores reales
```

`.env` está en `.gitignore` y **nunca** debe subirse (el repo es público).
El script lo carga solo si tienes `python-dotenv` instalado
(`pip install -r requirements.txt`).

Variables:

```bash
ADOBE_CLIENT_ID=...        # client_id del proyecto de Developer Console
ADOBE_CLIENT_SECRET=...    # client_secret — NUNCA en el código
ADOBE_DATASET_ID=...       # dataset de clasificación de eVar23
# Opcionales:
ADOBE_RSID=...             # report suite ID; lo usa --list-datasets (o --rsid)
ADOBE_COMPANY_ID=...       # si se omite, se descubre vía Discovery API
ADOBE_SCOPES=...           # si se omite, usa el default de config.py
```

Alternativa sin `.env` (exportarlas a mano en la terminal):

```bash
export ADOBE_CLIENT_ID=...
export ADOBE_CLIENT_SECRET=...
export ADOBE_DATASET_ID=...
```

### Cómo hallar el DATASET_ID (una sola vez)

El listado de datasets de la Classifications API 2.0 es **por report
suite**, así que necesitas indicar el RSID (con `--rsid` o la variable
`ADOBE_RSID`):

```bash
python upload_classifications.py --list-datasets --rsid tu_report_suite
# o, si ya definiste ADOBE_RSID en .env:
python upload_classifications.py --list-datasets
```

En la salida busca la entrada cuyo `id` sea `evar23`: el valor de
`datasets` es el id que debes copiar a `ADOBE_DATASET_ID`.

> El `DATASET_ID` de esta API es el id de un *Classification Set* de
> Adobe Analytics (Components > Classification Sets), no de un dataset de
> Experience Platform.

### Subir

```bash
# Sube todos los output/bot_flag_upload*.csv:
python upload_classifications.py

# Sube uno específico:
python upload_classifications.py --file output/bot_flag_upload_part2.csv

# Sube y confirma todo, pero NO espera el procesamiento (fire-and-forget):
python upload_classifications.py --no-wait
```

El script imprime el estado de cada job y sale con código distinto de 0
si alguno falló, para poder encadenarlo en CI/cron.

### Rendimiento con muchas partes

Con decenas o cientos de partes, el flujo **no** procesa un archivo y se
queda esperando a que Adobe lo termine antes de pasar al siguiente. En su
lugar trabaja en dos fases paralelas:

1. **Subida:** `create + upload + commit` de todos los archivos en
   paralelo. El procesamiento del import corre en los servidores de Adobe,
   así que no hace falta esperarlo para lanzar el siguiente.
2. **Polling:** consulta el estado de todos los jobs confirmados en
   paralelo, en rondas, hasta que terminan.

Así el tiempo total queda acotado por la fase más lenta y no por la suma
de los tiempos de procesamiento de cada job. Si solo quieres subir y
verificar después, usa `--no-wait` (hace únicamente la fase 1).

La API 2.0 aplica un límite de ~120 requests/minuto por usuario y devuelve
HTTP 429 al pasarse
([FAQ de la API 2.0](https://dev.adobe.com/analytics-apis/docs/2.0/guides/faq)).
Por eso la concurrencia por defecto es baja y cada request reintenta con
backoff exponencial ante 429 o errores 5xx. Los parámetros están en
`config.py` y puedes ajustarlos según el límite de tu cuenta:

```python
UPLOAD_CONCURRENCY = 4     # subidas simultáneas
POLL_CONCURRENCY   = 4     # jobs consultados a la vez en el polling
RETRY_MAX_ATTEMPTS = 5     # reintentos por request ante 429/5xx
RETRY_BACKOFF_BASE = 2.0   # segundos; backoff = BASE * 2**(intento-1)
RETRY_BACKOFF_MAX  = 30.0  # tope del backoff por intento
```

> Nota: cada archivo consume 3 requests en la fase de subida
> (create+upload+commit) más 1 por ronda de polling. Con 100 partes son
> ~300 requests solo en la subida; si subes la concurrencia demasiado,
> puedes toparte con el rate limit. Contenido reformulado para cumplir con
> restricciones de licencia respecto a la documentación de Adobe.

> Nota: los nombres exactos de campos de la API 2.0 (p. ej. el campo del
> multipart en `uploadFile` y las claves del JSON de respuesta) pueden
> variar según la versión; el script contempla variantes comunes, pero si
> Adobe cambia el contrato conviene validar contra la
> [doc oficial](https://developer.adobe.com/analytics-apis/docs/2.0/guides/endpoints/classifications/import-file).
