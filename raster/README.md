# Actualización masiva de rutas de mosaicos

## Alcance

Recorre la raíz y las carpetas de un sitio ArcGIS Server, excepto System y Utilities. Identifica los mosaicos referenciados por ImageServer mediante el manifiesto administrativo, verifica que sean MosaicDataset en la conexión SDE configurada y actualiza cada mosaico una sola vez. Incluye MapServer del mismo sitio que referencien ese mosaico en el grupo de parada e inicio. Otros tipos de servicio se reportan sin cambios.

Este paquete está configurado para el laboratorio spwebgis113. No se ha ejecutado desde este entorno contra el servidor ni con ArcPy. La reparación individual ya fue validada por el operador; la detección masiva y el control de servicios necesitan su prueba de integración.

## Requisitos

- Windows y entorno Python de ArcGIS Pro con ArcPy, requests y licencia Standard o Advanced.
- Conexión SDE con permisos sobre los mosaicos, preferiblemente su propietario.
- Cuenta administradora del sitio para /admin; usuario y contraseña se solicitan sin guardarlos.
- El Web Adaptor debe permitir acceso administrativo; alternativamente use la URL HTTPS directa del sitio en server_url.
- Esta versión usa generateToken de ArcGIS Server, con cuenta del sitio. No implementa OAuth, SAML ni inicio de sesión de Portal.
- Copia completa de imágenes y overviews al destino y permisos SMB/NTFS para la cuenta de ArcGIS Server, en todos los nodos.
- Respaldo recuperable de la geodatabase y ventana sin edición/publicación concurrente.

## Configuración

manifest_match compara valores EXACTOS del onServerConnectionString del manifiesto, sin distinguir mayúsculas. DATABASE=raster y SERVER=11326server son valores iniciales que deben validarse con el diagnóstico. Si el manifiesto utiliza FQDN, puerto u otro identificador, ajuste al valor real; puede usar INSTANCE en lugar de SERVER. La conexión SDE debe apuntar a esa misma base e instancia. No deduce coincidencias por nombres de servicio ni por sufijos de dataset.

Los ImageServer sin mosaico coincidente aparecen como SIN_MOSAICO_COINCIDENTE; deben revisarse antes de la ejecución masiva. Un inventario con errores de lectura bloquea --apply para no omitir dependencias conocidas. Los mosaicos de otras bases requieren otra configuración y conexión. No se modifican rutas de funciones ráster, caché del servicio, Registered Folders ni conexiones del servicio.

Si el certificado requiere una CA corporativa, indique la ruta PEM en ca_bundle. La verificación TLS permanece activa.

## Ejecución

Abra Python Command Prompt de ArcGIS Pro y cambie al directorio del paquete.

Diagnóstico de todo el sitio, sin detener servicios ni modificar mosaicos:

```bat
python migrar_mosaicos.py --config config_lab.json
```

Prueba limitada al servicio conocido:

```bat
python migrar_mosaicos.py --config config_lab.json --service Raster/raster_rasteruser_orto10.ImageServer
python migrar_mosaicos.py --config config_lab.json --service Raster/raster_rasteruser_orto10.ImageServer --apply --backup-confirmed
```

Si Orto10 ya está reparado, aparecerá SIN_CAMBIOS y no se reiniciará. Para una nueva prueba use un mosaico que conserve la ruta antigua o restablezca manualmente el mosaico de laboratorio.

Primer lote de hasta diez mosaicos:

```bat
python migrar_mosaicos.py --config config_lab.json --apply --backup-confirmed --limit 10
```

Aplicar a todos los mosaicos identificados:

```bat
python migrar_mosaicos.py --config config_lab.json --apply --backup-confirmed
```

--limit selecciona los primeros N mosaicos del inventario, incluidos los que ya no necesitan cambios. Para continuar el conjunto completo, retire --limit; los ya reparados se omiten por sus rutas actuales. Cada invocación reconstruye el inventario.

## Procedimiento

```text
Leer configuración y autenticar en el sitio
Recorrer servicios de todas las carpetas de usuario
    Consultar manifiesto para ImageServer y MapServer
    Comparar base e instancia con la conexión configurada
    Identificar MosaicDataset por nombre exacto
Agrupar servicios por mosaico
Conservar grupos que incluyan ImageServer
Para cada mosaico seleccionado
    Exportar todas las rutas (imágenes, overviews y caché de elementos)
    Calcular reemplazos respetando límites de carpetas
    Comprobar existencia de todos los archivos de destino afectados
    Si es diagnóstico: registrar y seguir
    Si no hay cambios: omitir
    Si faltan archivos: bloquear ese mosaico y seguir
    Guardar estados originales de todos sus servicios
    Detener servicios activos y esperar confirmación
    Reparar rutas
    Exportar rutas finales y compararlas con las esperadas
    Si son correctas: restaurar servicios originalmente activos
    Si la reparación falla: mantener detenidos y detener lote
    Guardar resultado
Emitir resumen CSV y diarios JSON
```

## Resultados y recuperación

Cada ejecución crea una carpeta fechada con inventario.json, plan.json, resultados.json, resumen.csv, diarios mosaico_N.json y auditoria.gdb. Esta última contiene tablas de rutas antes y después. Los diarios se guardan antes de detener servicios y antes de modificar datos.

La exportación de rutas es evidencia, no un respaldo de la geodatabase. No hay rollback automático: invertir la raíz podría modificar rutas de destino que ya existían antes. En REVISION_REQUERIDA, revise las tablas y el respaldo y repare/restaure con el administrador SIG antes de arrancar servicios. Los estados originales están en el diario.

Si falla únicamente la parada, el script intenta restaurar los servicios originalmente activos. Si el proceso se termina abruptamente o se apaga la máquina, la restauración no está garantizada: consulte el diario y restablezca los estados manualmente. No existe transacción global para los 1.027 mosaicos.

## Validación de aceptación

El script comprueba archivos desde la cuenta ejecutora, compara rutas completas antes/después y confirma configuredState/realTimeState de servicios. No realiza exportImage ni certifica la lectura desde la cuenta de ArcGIS Server. Después de cada lote, probar visualización a escalas generales y de detalle, revisar overviews y logs del servidor. Rutas ya rotas fuera de la raíz elegida no se corrigen con esta operación.

El inventario de dependencias se limita al sitio configurado y a sus ImageServer/MapServer. Si otros sitios, aplicaciones o procesos consumen los mismos mosaicos, deben coordinarse dentro de la ventana. El programa no modifica la definición del servicio ni republica.

## Referencias

- https://pro.arcgis.com/en/pro-app/3.4/tool-reference/data-management/repair-mosaic-dataset-paths.htm
- https://pro.arcgis.com/en/pro-app/3.4/tool-reference/data-management/export-mosaic-dataset-paths.htm
- https://developers.arcgis.com/rest/enterprise-administration/server/servicemanifest/
- https://developers.arcgis.com/rest/enterprise-administration/server/service-status/
