# IGAC — Migración de rutas de Mosaic Dataset

## 1. Objetivo

Script para identificar y actualizar las rutas de archivos raster utilizadas por **Mosaic Dataset publicados en ArcGIS Server**.

El proceso trabaja sobre Mosaic Dataset almacenados en una **Enterprise Geodatabase** accesible mediante una conexión `.sde`.

Actualiza las rutas internas desde:

```text
\\172.26.0.20\cemdatos
```

hacia:

```text
\\repositorio\ArcGIS\cemdatos
```

El script:

- No republica servicios.
- No modifica la conexión SDE del servicio.
- No copia archivos raster.
- No modifica Mosaic Dataset almacenados en FileGDB.
- Utiliza `RepairMosaicDatasetPaths` para actualizar las rutas internas del mosaico.

---

## 2. Archivo principal

```text
migrar_mosaicos_lotes.py
```

Esta versión permite tres formas de ejecución:

1. Un servicio específico.
2. Un lote controlado mediante archivo TXT.
3. Inventario global del sitio.

Para producción se recomienda utilizar **`--services-file`**.

---

## 3. Requisitos

Ejecutar desde:

```text
Python Command Prompt de ArcGIS Pro
```

Requiere:

- ArcGIS Pro.
- ArcPy.
- `requests`.
- Acceso administrativo a ArcGIS Server.
- Conexión `.sde` a la Enterprise Geodatabase.
- Acceso al repositorio raster origen.
- Acceso al repositorio raster destino.
- Imágenes y archivos raster previamente copiados al nuevo repositorio.
- Respaldo recuperable de la Enterprise Geodatabase antes de utilizar `--apply`.

---

## 4. Configuración

Ejemplo:

```json
{
    "server_url": "https://mapas2.igac.gov.co/server",
    "sde_connection": "C:\\ruta\\conexion.sde",
    "old_root": "\\\\172.26.0.20\\cemdatos",
    "new_root": "\\\\repositorio\\ArcGIS\\cemdatos",
    "manifest_match": {
        "DATABASE": "raster",
        "SERVER": "servidor"
    },
    "output_dir": "C:\\migracion\\resultados_mosaicos"
}
```

`manifest_match` permite procesar únicamente servicios cuya conexión corresponda a la Enterprise Geodatabase definida.

---

## 5. Servicios soportados

### Sí procesa

| Servicio | Fuente |
|---|---|
| ImageServer | Mosaic Dataset en Enterprise Geodatabase |
| MapServer | Mosaic Dataset en la misma Enterprise Geodatabase |

El dataset debe ser identificado por ArcPy como:

```text
MosaicDataset
```

### No procesa

- ImageServer con Mosaic Dataset en FileGDB.
- Raster Dataset publicado directamente.
- Hosted Services.
- FeatureServer.
- Datos copiados al servidor.
- Servicios cuya Enterprise Geodatabase no coincida con `manifest_match`.
- Servicios sin un Mosaic Dataset identificable en el manifiesto.

---

# 6. Ejecución recomendada por lotes

## Crear archivo TXT

Ejemplo:

```text
C:\migracion\raster_lote_01.txt
```

Contenido:

```text
md/mdt05002abejorral.ImageServer
md/mdt05021alejandria.ImageServer
md/mdt05031amalfi.ImageServer
md/mdt05034andes.ImageServer
md/mdt05038angostura.ImageServer
```

Una ruta de servicio por línea.

También se permiten líneas vacías y comentarios iniciados por:

```text
#
```

---

## Diagnóstico del lote

No modifica datos:

```bat
python migrar_mosaicos_lotes.py ^
 --config config_IGAC.json ^
 --services-file C:\migracion\raster_lote_01.txt
```

En este modo el script:

```text
lee el TXT
→ consulta únicamente esos servicios
→ analiza sus manifest
→ identifica Mosaic Dataset
→ valida rutas
```

No se realiza inventario global del sitio.

---

## Aplicar migración del lote

```bat
python migrar_mosaicos_lotes.py ^
 --config config_IGAC.json ^
 --services-file C:\migracion\raster_lote_01.txt ^
 --apply ^
 --backup-confirmed
```

`--backup-confirmed` indica que existe un respaldo recuperable de la Enterprise Geodatabase.

---

# 7. Ejecutar un único servicio

Diagnóstico:

```bat
python migrar_mosaicos_lotes.py ^
 --config config_IGAC.json ^
 --service md/mdt05002abejorral.ImageServer
```

Aplicar:

```bat
python migrar_mosaicos_lotes.py ^
 --config config_IGAC.json ^
 --service md/mdt05002abejorral.ImageServer ^
 --apply ^
 --backup-confirmed
```

Este modo tampoco realiza inventario global.

---

# 8. Inventario global

Si no se especifica:

```text
--service
```

ni:

```text
--services-file
```

el script recorre todos los servicios visibles del sitio.

Ejemplo:

```bat
python migrar_mosaicos_lotes.py ^
 --config config_IGAC.json
```

---

## Procesar máximo 10 mosaicos del inventario global

```bat
python migrar_mosaicos_lotes.py ^
 --config config_IGAC.json ^
 --apply ^
 --backup-confirmed ^
 --limit 10
```

Importante:

```text
--limit 10
```

no limita el inventario.

El flujo es:

```text
inventario completo del sitio
→ identificación de mosaicos
→ selección de máximo 10
→ procesamiento
```

Por esta razón, en servidores con gran cantidad de servicios se recomienda utilizar:

```text
--services-file
```

---

# 9. Parámetros

| Parámetro | Uso |
|---|---|
| `--config` | Archivo JSON de configuración |
| `--service` | Procesa un servicio específico |
| `--services-file` | Procesa únicamente los servicios definidos en un TXT |
| `--limit` | Máximo de mosaicos después de un inventario global |
| `--apply` | Ejecuta la modificación |
| `--backup-confirmed` | Confirma existencia de respaldo recuperable |

No se permite utilizar simultáneamente:

```text
--service + --services-file
```

Tampoco:

```text
--services-file + --limit
```

o:

```text
--service + --limit
```

---

# 10. Flujo de migración

Para cada servicio seleccionado:

```text
Servicio ArcGIS Server
        ↓
manifest.json
        ↓
Enterprise Geodatabase
        ↓
Mosaic Dataset
        ↓
ExportMosaicDatasetPaths
        ↓
validación de rutas destino
        ↓
STOP de servicios activos
        ↓
RepairMosaicDatasetPaths
        ↓
ExportMosaicDatasetPaths
        ↓
validación
        ↓
START de servicios que estaban activos
```

---

# 11. Validaciones

Antes de modificar un mosaico se valida:

1. El servicio existe.
2. El manifiesto puede consultarse.
3. La fuente está publicada por referencia.
4. La conexión coincide con `manifest_match`.
5. El dataset existe en la conexión SDE.
6. ArcPy identifica el dataset como `MosaicDataset`.
7. Existen rutas bajo `old_root`.
8. Los archivos esperados existen bajo `new_root`.
9. El estado del servicio es estable antes de detenerlo.

---

# 12. Estados principales

| Estado | Significado |
|---|---|
| `ACTUALIZADO` | Rutas actualizadas correctamente |
| `SIN_CAMBIOS` | No existen rutas pendientes |
| `BLOQUEADO_DESTINO` | Faltan archivos en el nuevo repositorio |
| `SIN_MOSAICO_COINCIDENTE` | No se identificó Mosaic Dataset válido |
| `ERROR_INVENTARIO` | Error al determinar la fuente |
| `ERROR_ANTES_CAMBIO` | Error antes de modificar el mosaico |
| `REVISION_REQUERIDA` | La reparación inició pero no pudo validarse completamente |
| `ERROR_REINICIO` | Error al restaurar el estado de un servicio |

---

# 13. Resultados generados

Cada ejecución crea una carpeta con:

```text
inventario.json
plan.json
resultados.json
resumen.csv
mosaico_1.json
mosaico_2.json
...
auditoria.gdb
```

`auditoria.gdb` contiene las rutas exportadas antes y después de la reparación.

---

# 14. Consideraciones operativas

Para Mapas2 se recomienda trabajar con lotes pequeños:

```text
10 a 20 ImageServer por ejecución
```

utilizando:

```text
--services-file
```

Esto evita realizar un inventario profundo de todos los servicios del servidor en cada ejecución.

Después de cada lote validar:

- Estado de los servicios.
- Visualización del Image Service.
- Imágenes en diferentes escalas.
- Overviews.
- Logs de ArcGIS Server.
- Rutas del Mosaic Dataset.

Importante: un estado `STARTED` únicamente confirma que ArcGIS Server inició el servicio; no garantiza que las imágenes estén funcionando correctamente.

---

# 15. Dependencias entre servicios

Un mismo Mosaic Dataset puede ser utilizado por varios servicios.

Cuando se utilice `--services-file`, deben incluirse en el lote los servicios conocidos que utilicen el mismo Mosaic Dataset.

El modo filtrado prioriza rendimiento y no realiza un inventario global de todos los servicios del sitio para descubrir dependencias adicionales.

---

# 16. Recomendación

Para producción utilizar principalmente:

```text
--services-file
```

Ejemplo definitivo:

```bat
python "C:\arcgis-service-path-repair-main\raster\migrar_mosaicos_lotes.py" ^
 --config "C:\arcgis-service-path-repair-main\raster\config_IGAC.json" ^
 --services-file "C:\arcgis-service-path-repair-main\raster\lote_01.txt" ^
 --apply ^
 --backup-confirmed
```