# IGAC — Migración de rutas de mosaicos raster

## 1. Objetivo

Herramientas para identificar y actualizar rutas de archivos raster utilizadas por **Mosaic Datasets publicados en ArcGIS Server**.

El proceso trabaja sobre mosaicos almacenados en una **Enterprise Geodatabase** accesible mediante una conexión `.sde`, identifica sus servicios asociados y reemplaza las rutas de los archivos raster desde `old_root` hacia `new_root`.

No republica servicios ni modifica la conexión SDE del servicio.

---

## 2. Archivos

| Archivo | Uso |
|---|---|
| `migrar_mosaicos.py` | Script principal para inventario, diagnóstico y migración masiva de mosaicos. |
| `migrar_mosaicos_filtrado.py` | Variante para trabajar directamente sobre un servicio específico y reducir el inventario durante pruebas controladas. |
| `migrar_mosaicos_old.py` | Versión anterior conservada como referencia. No se recomienda para ejecución productiva. |
| `config_lab.json` | Configuración del sitio, conexión SDE y rutas origen/destino. |

### Script recomendado

Para ejecución normal y por lotes:

```text
migrar_mosaicos.py
```

Para pruebas sobre un único servicio:

```text
migrar_mosaicos_filtrado.py
```

---

## 3. Requisitos

Ejecutar desde **Python Command Prompt de ArcGIS Pro**.

Requiere:

- ArcGIS Pro con ArcPy.
- Python del entorno de ArcGIS Pro.
- Paquete `requests`.
- Conexión `.sde` con acceso a los Mosaic Dataset.
- Cuenta administradora de ArcGIS Server.
- Acceso al repositorio raster origen y destino.
- Copia previa de las imágenes y overviews al nuevo repositorio.
- Respaldo recuperable de la Enterprise Geodatabase antes de usar `--apply`.

Instalar `requests` si es necesario (ArcGIS Pro 3.3.x ya lo trae instalado):

```bat
python -m pip install requests
```

---

## 4. Configuración

El archivo JSON debe contener como mínimo:

```json
{
    "server_url": "https://servidor/arcgis",
    "sde_connection": "C:\\ruta\\conexion.sde",
    "old_root": "\\\\repositorio_anterior\\ArcGIS\\cemdatos",
    "new_root": "\\\\repositorio_nuevo\\ArcGIS\\cemdatos",
    "manifest_match": {
        "DATABASE": "raster",
        "SERVER": "servidor"
    },
    "output_dir": "resultados_mosaicos"
}
```

`manifest_match` identifica únicamente los servicios cuya conexión del manifiesto corresponde a la Enterprise Geodatabase configurada.

---

## 5. Formatos soportados

### Sí procesa

| Servicio | Fuente del servicio | ¿Lo procesa? |
|---|---|---|
| ImageServer | Mosaic Dataset en Enterprise Geodatabase | Sí |
| MapServer | Mosaic Dataset en la misma Enterprise Geodatabase | Sí, únicamente como servicio dependiente del mosaico |

El script identifica el Mosaic Dataset a partir del manifiesto del servicio y verifica mediante ArcPy que el dataset corresponda al tipo:

```text
MosaicDataset
```

La actualización de las rutas se realiza mediante:

```text
arcpy.management.RepairMosaicDatasetPaths
```

**Nota:** Los archivos raster asociados al Mosaic Dataset deben haber sido copiados previamente al nuevo repositorio. El script no copia imágenes ni archivos raster; únicamente actualiza dentro del Mosaic Dataset las rutas que apuntan al repositorio anterior.

### No procesa

| Servicio / fuente | Motivo |
|---|---|
| ImageServer con Mosaic Dataset en FileGDB | El flujo actual trabaja con Mosaic Dataset almacenados en la Enterprise Geodatabase configurada mediante la conexión `.sde` |
| ImageServer basado directamente en un Raster Dataset | No corresponde a un Mosaic Dataset |
| ImageServer cuya Enterprise Geodatabase no coincide con `manifest_match` | Está fuera de la conexión configurada |
| Datos raster copiados al servidor | No corresponden al patrón de Mosaic Dataset referenciado |
| Hosted Services | No aplica |
| FeatureServer | No aplica |
| Servicios sin Mosaic Dataset identificable en el manifiesto | Se reportan como `SIN_MOSAICO_COINCIDENTE` |
| Rutas que no estén bajo `old_root` | No se modifican |
---

## 6. Inventario IGAC identificado

Según el inventario de servicios de Mapas 2:

| Tipo de servicio | Fuente | Cantidad | ¿Este script lo migra? |
|---|---|---:|---|
| ImageServer | Enterprise Geodatabase PostgreSQL | 1.110 | Sí, si el dataset es Mosaic Dataset y coincide con la conexión configurada |
| ImageServer | File Geodatabase | 2 | No |
| MapServer | Enterprise Geodatabase PostgreSQL | 49 | Solo si referencia uno de los Mosaic Dataset identificados |
| MapServer | FileGDB | 1.542 | No |
| MapServer | Shapefile | 4 | No |
| FeatureServer Hosted | ArcGIS Data Store | 15 | No |

### Universo principal del proceso raster

El universo principal corresponde a:

```text
1.110 ImageServer
```

con fuente registrada mediante Enterprise Geodatabase PostgreSQL.

Sin embargo, un servicio solo entra al proceso cuando cumple todas estas condiciones:

1. Es `ImageServer` o `MapServer`.
2. La fuente está publicada **por referencia**.
3. La conexión del manifiesto coincide con `manifest_match`.
4. El dataset existe en la conexión `.sde`.
5. ArcPy identifica el dataset como `MosaicDataset`.
6. El grupo contiene al menos un `ImageServer`.
7. Existen rutas bajo `old_root` que deban cambiar.
8. Los archivos esperados en `new_root` existen antes de ejecutar la reparación.

---

## 7. Servicios que se migran

Se migran los servicios cuyo manifiesto permita identificar un Mosaic Dataset válido en la Enterprise Geodatabase configurada.

Ejemplo:

```text
ImageServer
    ↓
Enterprise GDB PostgreSQL
    ↓
Mosaic Dataset
    ↓
Raster físicos en recurso compartido
```

El cambio se realiza sobre las rutas internas del Mosaic Dataset:

```text
\\repositorio_anterior\...\imagen.tif
```

a:

```text
\\repositorio_nuevo\...\imagen.tif
```

El servicio no se republica.

---

## 8. Servicios que no se migran

### ImageServer + FileGDB

Cantidad identificada:

```text
2
```

No se procesan porque el script busca los Mosaic Dataset mediante la conexión `.sde` configurada.

---

### ImageServer sin Mosaic Dataset coincidente

Estado:

```text
SIN_MOSAICO_COINCIDENTE
```

Puede ocurrir cuando:

- la base no coincide con `manifest_match`;
- el dataset no existe en la conexión SDE;
- el dataset no es un Mosaic Dataset;
- el manifiesto no contiene la referencia esperada.

No se modifica.

---

### Servicio con error de inventario

Estado:

```text
ERROR_INVENTARIO
```

En modo `--apply`, cualquier error de inventario bloquea la ejecución masiva para evitar modificar mosaicos con dependencias incompletas.

---

### Raster de destino inexistente

Estado:

```text
BLOQUEADO_DESTINO
```

El script comprueba previamente que cada archivo raster esperado exista en `new_root`.

Si falta alguno, el mosaico no se modifica.

---

### Mosaicos ya migrados

Estado:

```text
SIN_CAMBIOS
```

Si el Mosaic Dataset ya utiliza las rutas nuevas, no se vuelve a modificar ni es necesario reiniciar sus servicios.

---

## 9. Uso

### Inventario y diagnóstico

No modifica datos ni detiene servicios:

```bat
python migrar_mosaicos.py --config config_lab.json
```

---

### Probar un solo servicio

Con la versión filtrada:

```bat
python migrar_mosaicos_filtrado.py --config config_lab.json --service Raster/Servicio.ImageServer
```

Aplicar el cambio:

```bat
python migrar_mosaicos_filtrado.py --config config_lab.json --service Raster/Servicio.ImageServer --apply --backup-confirmed
```

---

### Migrar los primeros 10 mosaicos

```bat
python migrar_mosaicos.py --config config_lab.json --apply --backup-confirmed --limit 10
```

`--limit 10` limita el procesamiento a los primeros diez mosaicos identificados.

Los mosaicos que ya estén migrados aparecerán como:

```text
SIN_CAMBIOS
```

y no se vuelven a reparar.

---

### Migrar todos los mosaicos elegibles

```bat
python migrar_mosaicos.py --config config_lab.json --apply --backup-confirmed
```

---

## 10. Validaciones realizadas

Antes de modificar un mosaico, el script:

1. Identifica el servicio mediante ArcGIS Server Admin.
2. Consulta su `manifest.json`.
3. Verifica que la fuente esté publicada por referencia.
4. Compara la conexión con `manifest_match`.
5. Verifica que el dataset exista mediante ArcPy.
6. Verifica que sea un `MosaicDataset`.
7. Exporta las rutas actuales.
8. Calcula las rutas nuevas.
9. Verifica que los archivos destino existan.
10. Identifica todos los servicios que utilizan el mismo mosaico.

Durante la aplicación:

1. Guarda el estado inicial de los servicios.
2. Detiene los servicios activos.
3. Ejecuta `RepairMosaicDatasetPaths`.
4. Exporta nuevamente las rutas.
5. Compara el resultado con el reemplazo esperado.
6. Restablece los servicios que estaban activos.

---

## 11. Resultados

Cada ejecución genera una carpeta con:

```text
inventario.json
plan.json
resultados.json
resumen.csv
mosaico_N.json
auditoria.gdb
```

`auditoria.gdb` contiene las rutas encontradas antes y después de cada reparación.

Estados principales:

| Estado | Significado |
|---|---|
| `ACTUALIZADO` | Rutas actualizadas correctamente |
| `SIN_CAMBIOS` | El mosaico no tiene rutas pendientes |
| `BLOQUEADO_DESTINO` | Faltan archivos en el nuevo repositorio |
| `SIN_MOSAICO_COINCIDENTE` | Servicio fuera del patrón soportado |
| `ERROR_INVENTARIO` | No fue posible determinar correctamente la fuente |
| `REVISION_REQUERIDA` | La reparación inició pero no pudo validarse completamente |
| `ERROR_REINICIO` | Error al restaurar el estado de uno o más servicios |
| `ERROR_ANTES_CAMBIO` | Fallo antes de ejecutar la modificación |

---

## 12. Consideración importante

El script **no realiza rollback automático del Mosaic Dataset**.

Antes de ejecutar:

```text
--apply --backup-confirmed
```

debe existir un respaldo recuperable de la Enterprise Geodatabase.

Después de cada lote se debe validar al menos:

- visualización del Image Service;
- imágenes a escalas de detalle;
- overviews;
- logs de ArcGIS Server.
