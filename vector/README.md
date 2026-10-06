# IGAC - Migración de servicios vectoriales FileGDB

Herramientas para identificar, validar y migrar referencias de datos de servicios vectoriales de ArcGIS Server 11.3 desde un repositorio compartido anterior (`old_root`) hacia un nuevo repositorio (`new_root`).

El alcance de estos scripts es **MapServer / FeatureServer con File Geodatabase (.gdb) referenciada**. No realizan republicación desde ArcGIS Pro y no migran fuentes Enterprise GDB, Shapefile, Hosted ni servicios de imágenes.

## 1. Archivos

| Archivo | Uso |
| --- | --- |
| `IGAC_Vector_Diagnostico.py` | Script de diagnóstico. Modifica los artefactos del servicio por etapas y valida el servicio después de cada cambio para identificar qué archivo genera una falla. Usar únicamente para pruebas controladas. |
| `IGAC_Vector_Migrator.py` | Script de migración. Valida la fuente destino, realiza backup, cambia las referencias de ruta, reinicia el servicio, valida REST y ejecuta rollback si ocurre un error. |
| `config_vector_11_3.json` | Configuración del sitio, rutas origen/destino y directorios de ArcGIS Server. |

## 2. Requisitos

- ArcGIS Server 11.3.
- Python 3.x.
- Paquete `requests`.
- Para `IGAC_Vector_Migrator.py`, ejecutar con el entorno Python de ArcGIS Pro porque la validación de la FileGDB destino utiliza ArcPy.
- Usuario administrador del sitio de ArcGIS Server.
- Permisos de lectura/escritura sobre `arcgisinput`, `config-store` y la carpeta de resultados.
- Acceso desde ArcGIS Server al nuevo repositorio compartido.
- Las FileGDB deben estar copiadas previamente en el nuevo repositorio.
- El Registered Folder correspondiente al nuevo repositorio debe estar configurado antes de ejecutar la migración.

Instalar `requests`solo si es necesario (ArcGIS Pro 3.3.x ya lo tra instalado):

```powershell
python -m pip install requests
```

Para el migrador se recomienda ejecutarlo desde **Python Command Prompt de ArcGIS Pro**.


## 3. Configuración

Los parámetros principales del archivo JSON son:

| Parámetro | Uso |
| --- | --- |
| `admin_url` | URL de ArcGIS Server Admin. |
| `rest_url` | URL REST del sitio. |
| `arcgisinput_root` | Ruta de `arcgisinput`. |
| `configstore_services_root` | Ruta de servicios dentro del `config-store`. |
| `old_root` | Repositorio actual que se reemplazará. |
| `new_root` | Nuevo repositorio. |
| `output_root` | Carpeta para inventarios, backup y evidencias. |
| `exclude_regex` | Servicios o carpetas que no deben procesarse. |

Ejemplo conceptual:

```json
{
  "old_root": "\\\\repositorio_anterior\\ArcGIS\\cemdatos\\Vector",
  "new_root": "\\\\repositorio\\ArcGIS\\cemdatos\\Vector"
}
```

## 4. Formato vectorial que migra

El migrador procesa únicamente servicios que cumplan todas estas condiciones:

| Condición | Requerido |
| --- | --- |
| Tipo de servicio | `MapServer` o `FeatureServer` |
| Workspace | `FileGDBWorkspaceFactory` |
| Fuente | File Geodatabase `.gdb` |
| Publicación | Datos referenciados (`byReference`) |
| Ruta actual | Debe estar bajo `old_root` |
| Registered Folder destino | Debe estar disponible y asociado con `new_root` |
| Fuente destino | La `.gdb` debe existir y ser accesible |
| Datasets | Los datasets requeridos por el servicio deben existir en la FileGDB destino |

La clasificación utilizada por el script para estos servicios es:

```text
FILEGDB_REFERENCED
```

Los artefactos donde puede actualizar referencias son:

- `manifest.json`
- `manifest.xml`
- `serviceconfiguration.json`
- `.mapx`
- `.msd`

## 5. Formatos encontrados en el inventario IGAC

En el inventario analizado se identificaron **2.722 servicios con información de fuente de datos**.

| Servicio | Fuente | Referencia | Cantidad | Migración con estos scripts |
| --- | --- | --- | ---: | --- |
| MapServer | FileGDB | Referenciada | **1.513** | **Sí** |
| MapServer | Enterprise GDB - PostgreSQL | Referenciada | **49** | No |
| MapServer | FileGDB | Copiada al servidor | **29** | No |
| MapServer | Shapefile | Referenciada | **3** | No |
| MapServer | Shapefile | Copiada al servidor | **1** | No |
| FeatureServer | ArcGIS Data Store / Hosted | Copiada | **15** | No |
| ImageServer | Enterprise GDB - PostgreSQL | Referenciada | **1.110** | No, corresponde al proceso raster |
| ImageServer | FileGDB | Referenciada | **2** | No, corresponde al proceso raster |

### Servicios objetivo de la migración vectorial

El universo principal identificado corresponde a:

```text
1.513 MapServer -> FileGDB referenciada
```

Estos servicios pueden ser candidatos al migrador, pero el número final procesable lo determina la auditoría porque cada servicio debe cumplir además con:

- referencia a `old_root`;
- Registered Folder destino en estado `READY`;
- FileGDB destino existente;
- datasets destino disponibles;
- servicio REST funcional antes de la modificación.

### Servicios que no migra este script

| Fuente / condición | Motivo |
| --- | --- |
| Enterprise GDB / SDE PostgreSQL | No utiliza `FileGDBWorkspaceFactory`. |
| Shapefile | El script está limitado a FileGDB. |
| FileGDB copiada al servidor | No es una fuente referenciada; requiere tratamiento/republicación diferente. |
| Hosted Feature Service | Los datos están administrados por ArcGIS Data Store. |
| ImageServer | Fuera del alcance vectorial; se procesa con el flujo raster. |
| FileGDB fuera de `old_root` | No corresponde a la ruta que se está migrando. |
| Registered Folder no preparado | El servicio queda fuera de la lista `READY`. |
| FileGDB destino inexistente o sin datasets | El migrador bloquea el cambio antes de detener el servicio. |

## 6. Uso de `IGAC_Vector_Diagnostico.py`

Utilizar este script cuando se necesite identificar en qué artefacto del servicio ocurre una falla durante el cambio de ruta.

### Auditoría

```powershell
python IGAC_Vector_Diagnostico.py --config config_vector_11_3.json --mode audit
```

### Diagnóstico de un servicio

```powershell
python IGAC_Vector_Diagnostico.py `
  --config config_vector_11_3.json `
  --mode apply `
  --only-service "Carpeta/Servicio.MapServer"
```

El script cambia y valida secuencialmente:

```text
config-store manifest.json
config-store manifest.xml
arcgisinput manifest.json
arcgisinput manifest.xml
MAPX
MSD
serviceconfiguration.json
```

Si una etapa causa la falla del servicio, restaura el backup.

## 7. Uso de `IGAC_Vector_Migrator.py`

Este es el script que debe utilizarse para la migración controlada de servicios elegibles.

### 1. Auditoría

```powershell
python IGAC_Vector_Migrator.py --config config_vector_11_3.json --mode audit
```

La auditoría no modifica servicios. Genera el inventario y determina cuáles cumplen las condiciones iniciales.

### 2. Migrar un servicio

```powershell
python IGAC_Vector_Migrator.py `
  --config config_vector_11_3.json `
  --mode apply `
  --only-service "Carpeta/Servicio.MapServer"
```

### 3. Migrar un lote de 5 servicios

```powershell
python IGAC_Vector_Migrator.py `
  --config config_vector_11_3.json `
  --mode apply `
  --limit 5
```

### 4. Migrar un lote de 10 servicios

```powershell
python IGAC_Vector_Migrator.py `
  --config config_vector_11_3.json `
  --mode apply `
  --limit 10
```

### 5. Migrar todos los servicios elegibles

```powershell
python IGAC_Vector_Migrator.py --config config_vector_11_3.json --mode apply
```

Antes de comenzar la modificación el script solicita:

```text
APLICAR-VECTOR-IGAC
```

## 8. Qué hace el migrador por servicio

```text
1. Valida que el servicio REST funcione.
2. Verifica que todavía existan referencias a old_root.
3. Valida que la FileGDB exista en new_root.
4. Valida la FileGDB con ArcPy.
5. Valida que existan los datasets requeridos.
6. Crea backup de arcgisinput y config-store.
7. Detiene el servicio.
8. Cambia old_root por new_root en los artefactos del servicio.
9. Inicia el servicio.
10. Valida el manifest y el servicio REST.
11. Si ocurre una falla, ejecuta rollback.
```

## 9. Resultados

Cada ejecución crea una carpeta dentro de `output_root`.

Los archivos principales son:

| Archivo / carpeta | Contenido |
| --- | --- |
| `inventory.json` | Inventario detallado de servicios. |
| `inventory.csv` | Resumen del inventario. |
| `datastores_audit.json` | Estado de los Registered Folders. |
| `backups/` | Copias de seguridad de los servicios procesados. |
| `evidence/` | Evidencia de las validaciones y cambios. |
| `report.json` | Resultado final de cada servicio procesado. |

Estados principales:

| Estado | Significado |
| --- | --- |
| `SUCCESS` | Servicio migrado y validado. |
| `SKIPPED_BASELINE_REST_FAILED` | El servicio ya presentaba problemas antes de la migración. |
| `SKIPPED_DESTINATION_SOURCE_INVALID` | La FileGDB o los datasets destino no están disponibles. |
| `NO_OLD_ROOT_FOUND` | El servicio ya no contiene referencias a la ruta anterior. |
| `FAILED_ROLLBACK_OK` | La migración falló y el servicio fue restaurado correctamente. |
| `FAILED_ROLLBACK_ATTENTION_REQUIRED` | El rollback no pudo validarse y requiere revisión manual. |
