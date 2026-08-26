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

## Siguiente paso

El archivo de salida (`Key`, `Bot Flag`) se sube directamente en
Admin > Classifications > eVar23 > Import File (o vía FTP si el
archivo es grande), mapeando `Key` al ECID y `Bot Flag` a la
clasificación creada para ese propósito.
