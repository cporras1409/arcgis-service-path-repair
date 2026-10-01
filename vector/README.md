# IGAC — Migrador vectorial de FileGDB

## 1. Objetivo y alcance

Herramienta para ajustar referencias de rutas en artefactos de servicios vectoriales de ArcGIS Server 11.3. Procesa directorios de servicios MapServer y FeatureServer con workspaces FileGDB referenciados bajo `old_root`.

El proceso conserva la lógica de la versión 1.2 suministrada. No importa mapas en ArcGIS Pro ni republica servicios: modifica los artefactos existentes, con copia de seguridad, detención, inicio y validación REST.

Los Registered Folders se consultan y se relacionan con los servicios; el script no cambia sus rutas. La copia de los datos al repositorio de destino se realiza previamente, fuera de esta herramienta.

## 2. Archivos

| Archivo | Contenido |
| --- | --- |
| `IGAC_Vector_FileGDB_Migrator_11_3.py` | Código de auditoría y aplicación de cambios. |
| `config_vector_11_3.json` | Configuración del laboratorio suministrado. |
| `README.md` | Procedimiento, parámetros, pseudocódigo y resultados. |

## 3. Requisitos y preparación

- Python 3.8 o superior y paquete `requests`.
- Cuenta administradora del sitio para la autenticación solicitada en consola.
- Acceso a las URL de administración y REST configuradas.
- Permisos de lectura en los directorios de servicios; escritura para aplicar cambios y restaurar copias.
- Espacio disponible en `output_root` para copias de seguridad y evidencias.
- FileGDB copiadas al destino y permisos comprobados con la cuenta de ArcGIS Server desde todos los nodos.
- Registered Folders revisados antes de aplicar los cambios.

Instalación de la dependencia:

```powershell
python -m pip install requests
```

## 4. Configuración

Los valores del laboratorio se conservaron. Ajuste las rutas y URL antes de usar otro sitio. Las credenciales se solicitan durante la ejecución y no se incluyen en el JSON.

| Parámetro | Uso |
| --- | --- |
| `admin_url` | URL de ArcGIS Server Admin. |
| `rest_url` | URL base de los servicios REST. |
| `arcgisinput_root` | Directorio que se recorre para descubrir servicios. |
| `configstore_services_root` | Directorio de servicios del config-store. |
| `old_root` | Prefijo de ruta que se busca y reemplaza. |
| `new_root` | Prefijo de ruta de destino. |
| `output_root` | Directorio de inventarios, copias y evidencias. |
| `verify_ssl` | Verificación TLS de las solicitudes HTTP. |
| `http_timeout_seconds` | Tiempo límite de las solicitudes HTTP. |
| `stop_timeout_seconds` | Espera máxima del estado detenido. |
| `start_timeout_seconds` | Espera máxima del estado iniciado. |
| `exclude_regex` | Exclusión de directorios y servicios; el valor suministrado excluye Hosted. |
| `include_regex` | Conservado en el JSON; no se utiliza en esta versión. |
| `delay_between_services_seconds` | Conservado en el JSON; no introduce pausas en esta versión. |

Las rutas Windows deben usar barras invertidas escapadas en JSON. `old_root` y `new_root` deben corresponder al mismo nivel de la estructura de carpetas.

## 5. Selección y validaciones

Un servicio es elegible cuando cumple simultáneamente estas condiciones:

1. Su clasificación es `FILEGDB_REFERENCED`.
2. Sus artefactos contienen al menos una referencia a `old_root`.
3. La evaluación de Registered Folders devuelve `ready = true`.

La clasificación examina los workspaces FileGDB bajo `old_root`. Considera referenciados aquellos cuyo atributo `byReference` no es el booleano `false`; no exige que el atributo sea explícitamente `true`.

Para cada workspace, la asociación selecciona el Registered Folder candidato con mayor especificidad de ruta. El estado READY admite que la ruta de servidor **o** la de publicación contenga la ubicación esperada y esté bajo `new_root`. Esta evaluación compara rutas; no sustituye la comprobación de acceso efectivo de todos los nodos al repositorio.

El escaneo y la actualización cubren `manifest.json`, `manifest.xml`, `serviceconfiguration.json`, archivos MAPX y contenido JSON/XML de archivos MSD. `metadata.xml` queda fuera de la actualización.

La validación REST consulta la raíz del servicio y ejecuta `query` con `where=1=1` y `returnCountOnly=true` sobre la primera capa anunciada; si no hay capas, utiliza la primera tabla. No recorre todas las capas ni valida representación cartográfica, edición o rendimiento.

## 6. Ejecución

Ejecute los comandos desde la carpeta donde guardó los tres archivos. El modo predeterminado es `audit`.

### Auditoría

```powershell
python IGAC_Vector_FileGDB_Migrator_11_3.py `
    --config config_vector_11_3.json `
    --mode audit
```

Revise `inventory.csv`, `inventory.json` y `datastores_audit.json`. La auditoría genera archivos de salida, pero no modifica servicios ni Registered Folders. La ejecución en modo `apply` vuelve a generar el inventario; el programa no exige un archivo de auditoría previo.

### Aplicación sobre un servicio

```powershell
python IGAC_Vector_FileGDB_Migrator_11_3.py `
    --config config_vector_11_3.json `
    --mode apply `
    --only-service "Vector/Departamentos_Resumen.MapServer"
```

Utilice el valor exacto de `admin_path` del inventario. La comparación no distingue mayúsculas y minúsculas.

### Lote de diez servicios elegibles

```powershell
python IGAC_Vector_FileGDB_Migrator_11_3.py `
    --config config_vector_11_3.json `
    --mode apply `
    --limit 10
```

### Todos los servicios elegibles

```powershell
python IGAC_Vector_FileGDB_Migrator_11_3.py `
    --config config_vector_11_3.json `
    --mode apply
```

Antes de modificar servicios, la consola solicita escribir `APLICAR-VECTOR-IGAC`. El límite predeterminado es cero, sin restricción de cantidad; utilice valores positivos para limitar el lote.

Las únicas opciones son `--config`, `--mode`, `--only-service`, `--limit` y la ayuda `--help`. No existen opciones para actualizar Registered Folders ni un modo de restauración manual desde la línea de comandos.

## 7. Pseudocódigo

```text
INICIO
    Leer argumentos y cargar configuración
    Solicitar usuario y contraseña
    Crear cliente de administración y autenticar
    Crear directorio de ejecución

    Consultar Registered Folders y guardar datastores_audit.json
    Recorrer arcgisinput y descubrir servicios MapServer / FeatureServer
    Aplicar exclusiones y filtro de servicio, si corresponde

    PARA CADA servicio descubierto
        Localizar manifest.json
        SI no existe
            Clasificar como NO_MANIFEST
        SINO
            Leer manifest y clasificar workspaces FileGDB bajo OLD_ROOT
            SI la lectura o clasificación falla
                Clasificar como MANIFEST_ERROR
            FIN SI
        FIN SI

        Escanear artefactos y contar referencias a OLD_ROOT
        Evaluar asociación con Registered Folders
        Incorporar servicio, clasificación y evaluación al inventario

        SI clasificación = FILEGDB_REFERENCED
           Y existen referencias a OLD_ROOT
           Y todos los workspaces evaluados están READY
            Añadir servicio a la lista de elegibles
        FIN SI
    FIN PARA

    Guardar inventory.json e inventory.csv

    SI modo = AUDIT O no hay servicios elegibles
        FINALIZAR
    FIN SI

    Aplicar límite de servicios, si es positivo
    Solicitar confirmación APLICAR-VECTOR-IGAC
    SI la confirmación no coincide
        FINALIZAR
    FIN SI

    PARA CADA servicio elegible
        Validar raíz REST y consulta de conteo en primera capa o tabla
        SI la validación devuelve un resultado no satisfactorio
            Registrar SKIPPED_BASELINE_REST_FAILED y su evidencia
            CONTINUAR
        FIN SI

        Recontar referencias a OLD_ROOT
        SI no quedan referencias
            Registrar NO_OLD_ROOT_FOUND
            CONTINUAR
        FIN SI

        Respaldar directorios del servicio en arcgisinput y config-store

        INTENTAR
            Detener servicio y esperar STOPPED / STOPPED
            Reemplazar OLD_ROOT por NEW_ROOT en artefactos aplicables
            Exigir al menos un reemplazo y cero referencias residuales
            Iniciar servicio y esperar STARTED / STARTED
            Validar nuevamente raíz REST y consulta de conteo
            Registrar SUCCESS
        SI OCURRE UN ERROR EN ESTE BLOQUE
            INTENTAR
                Restaurar copia de seguridad
                Solicitar inicio y esperar STARTED / STARTED
                Validar REST después de restaurar
                SI estado y validación son correctos
                    Registrar FAILED_ROLLBACK_OK
                SINO
                    Registrar FAILED_ROLLBACK_ATTENTION_REQUIRED
                FIN SI
            SI FALLA LA RESTAURACIÓN
                Registrar FAILED_ROLLBACK_ATTENTION_REQUIRED
            FIN INTENTAR
        FIN INTENTAR

        Guardar evidencia del servicio y actualizar report.json
        SI resultado = FAILED_ROLLBACK_ATTENTION_REQUIRED
            DETENER LOTE
        FIN SI
    FIN PARA

    Mostrar resultados y ubicación de evidencias
FIN
```

El pseudocódigo resume la implementación. Las excepciones de la validación inicial, del escaneo previo o de la copia de seguridad ocurren antes del bloque de recuperación por servicio: pueden terminar la ejecución mediante el manejador general sin generar un resultado para ese servicio.

## 8. Salidas y estados

Cada ejecución crea una carpeta `YYYYMMDD_HHMMSS_audit` o `YYYYMMDD_HHMMSS_apply` dentro de `output_root`.

| Salida | Contenido |
| --- | --- |
| `datastores_audit.json` | Auditoría de Registered Folders. |
| `inventory.json` | Servicios, artefactos, referencias y asociaciones. |
| `inventory.csv` | Resumen tabular del inventario. |
| `backups/` | Copias creadas antes de modificar servicios. |
| `evidence/` | Evidencia por servicio procesado en modo apply. |
| `report.json` | Resultados acumulados del procesamiento en modo apply. |

`report.json` se guarda después de cada servicio procesado; no se genera en una auditoría ni cuando se cancela antes del procesamiento. Las copias se crean solo cuando el servicio supera la validación inicial y conserva referencias a la ruta anterior.

| Resultado | Interpretación |
| --- | --- |
| `SUCCESS` | Reemplazo, inicio y validación REST satisfactorios. |
| `SKIPPED_BASELINE_REST_FAILED` | Validación inicial no satisfactoria; servicio omitido. |
| `NO_OLD_ROOT_FOUND` | El escaneo previo al respaldo no encontró referencias. |
| `FAILED_ROLLBACK_OK` | Falló la aplicación; restauración e inicio validados. |
| `FAILED_ROLLBACK_ATTENTION_REQUIRED` | Recuperación no confirmada; se detiene el lote. |

## 9. Secuencia operativa

1. Copiar los datos al repositorio de destino y comprobar permisos.
2. Revisar los Registered Folders y ejecutar la auditoría.
3. Verificar los servicios elegibles y sus artefactos.
4. Aplicar el cambio a un servicio piloto.
5. Comprobar capas, tablas y aplicaciones consumidoras del piloto.
6. Continuar con lotes pequeños y revisar sus evidencias.
7. Ejecutar una auditoría final y revisar las referencias residuales a `old_root`.

## 10. Organización del código y revisión

El script agrupa constantes, utilidades, cliente Admin/REST, auditoría de Registered Folders, asociación de rutas, descubrimiento, escaneo, actualización, respaldo, validación REST y procesamiento. `main()` conserva la generación del inventario, los reportes y la coordinación del lote.

Esta revisión normaliza el formato, la indentación y los comentarios, y actualiza la documentación. Se conserva la estructura ejecutable del código original mediante comparación de árboles sintácticos. La comprobación local de sintaxis no sustituye una ejecución sobre el laboratorio de ArcGIS Server.
