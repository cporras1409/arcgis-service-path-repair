# -*- coding: utf-8 -*-
"""
Cambia el servidor de un fileshare (\\\\HOST_ANTERIOR\\... -> \\\\HOST_NUEVO\\...) en los servicios
de ArcGIS Server / Image Server que se elijan (uno, varios, una carpeta o todos).

  - Solo toca archivos de servicios que contengan "\\\\HOST_ANTERIOR\\" (con la barra final,
    para no tocar hosts que empiecen igual). Los demás archivos no se abren para escritura.
  - Reemplaza a nivel de bytes: no re-serializa el JSON, así que formato, tipos y orden quedan intactos.
  - Incluye los .msd y .mapx de map services (el .msd se reescribe entrada por entrada).
  - Antes de escribir NADA valida TODOS los archivos: cada JSON debe seguir siendo válido y la única
    diferencia deben ser los strings con la ruta; cada XML debe seguir bien formado.
    Si un solo archivo no cuadra, aborta sin modificar ninguno.
  - MODO 'simular' (por defecto): muestra qué cambiaría, no escribe nada.
  - MODO 'aplicar': exige ArcGIS Server detenido, respalda cada archivo y luego escribe.
  - MODO 'restaurar': devuelve los archivos desde un respaldo hecho por este script.

Ejecutar como administrador (o con la cuenta de ArcGIS Server) con cualquier Python 3; no necesita arcpy.
No modifica el .sd original de arcgisinput ni el contenido de los datos (p. ej. rutas internas de
mosaic datasets: eso se repara aparte con RepairMosaicDatasetPaths).
"""
import datetime
import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET

# ============================ CONFIGURACIÓN ============================
CONFIG_STORE = r'D:\arcgisserver\config-store'
ARCGIS_INPUT = r'D:\arcgisserver\directories\arcgissystem\arcgisinput'

# Qué servicios procesar. Se combinan (un servicio debe cumplir los tres filtros).
SERVICIOS = '*'      # '*' = todos, o lista: ['PILOTO_IA/2021__RGB_8bits_tif.ImageServer', 'MiMapa.MapServer']
CARPETAS = None      # None = todas, o lista: ['PILOTO_IA', ''] ('' = carpeta raíz)
TIPOS = None         # None = todos, o lista: ['ImageServer', 'MapServer']
EXCLUIR_CARPETAS = ['System', 'Utilities']   # servicios internos de ArcGIS Server

HOST_ANTERIOR = 'nbgwags17'
HOST_NUEVO = 'bgwags4'

MODO = 'aplicar'     # 'simular' | 'aplicar' | 'restaurar'
BACKUP_DIR = r'C:\temp\backup_cambio_share'
RESTAURAR_DESDE = None   # para MODO='restaurar': la carpeta de respaldo que imprime 'aplicar'

NOMBRE_SERVICIO_WINDOWS = 'ArcGIS Server'
IGNORAR_SERVIDOR_ACTIVO = False       # dejar en False: editar con el servidor encendido no es seguro
EXIGIR_QUE_EXISTA_NUEVA_RUTA = False  # la comprobación usa TU cuenta, no la cuenta de servicio
MOSTRAR_DETALLE = True                # imprimir cada valor antes/después
# =======================================================================

EXT_TEXTO = ('.json', '.xml', '.mapx', '.lyrx')
EXT_JSON = ('.json', '.mapx', '.lyrx')
EXT_ZIP = ('.msd',)
REEMPLAZO = b'\\\\' + HOST_NUEVO.encode('ascii')
# "\\host" seguido de "\". En JSON la ruta viene escapada (\\\\host\\...) y el mismo patrón
# encaja con las dos últimas barras, así que el resultado queda bien escapado.
PATRON = re.compile(re.escape(b'\\\\' + HOST_ANTERIOR.encode('ascii')) + b'(?=\\\\)', re.IGNORECASE)
PATRON_STR = re.compile(re.escape('\\\\' + HOST_ANTERIOR) + r'(?=\\)', re.IGNORECASE)
RESIDUO = re.compile(re.escape(HOST_ANTERIOR.encode('ascii')), re.IGNORECASE)
RE_UNC = re.compile(r'\\\\[^\\;"<>|]+\\[^;"<>|\r\n]*')
RE_SERVICIO_JSON = re.compile(r'^(?P<nombre>.+)\.(?P<tipo>[A-Za-z]+Server)\.json$', re.IGNORECASE)


class Abortar(Exception):
    pass


def sustituir(b):
    return PATRON.sub(lambda m: REEMPLAZO, b)


# ----------------------------- localizar -----------------------------
def listar_servicios():
    """Devuelve {(carpeta, nombre, tipo): [jsons]} para cada servicio en config-store\\services.
    Admite services\\<carpeta>\\<s>.<T>.json y services\\<carpeta>\\<s>.<T>\\<s>.<T>.json"""
    raiz = os.path.join(CONFIG_STORE, 'services')
    if not os.path.isdir(raiz):
        raise Abortar(f'No existe {raiz}. CONFIG_STORE debe ser la raíz del config-store (sin \\services).')
    servicios = {}
    for root, dirs, files in os.walk(raiz):
        for f in files:
            m = RE_SERVICIO_JSON.match(f)
            if not m or '.rlock' in f.lower():
                continue
            partes = [p for p in os.path.relpath(root, raiz).split(os.sep) if p not in ('.', '')]
            base = f[:-5]  # sin .json
            if partes and partes[-1].lower() == base.lower():
                partes = partes[:-1]
            elif partes and RE_SERVICIO_JSON.match(partes[-1] + '.json'):
                continue  # json auxiliar dentro de la carpeta de OTRO servicio
            clave = ('/'.join(partes), m.group('nombre'), m.group('tipo'))
            servicios.setdefault(clave, []).append(os.path.join(root, f))
    return servicios


def filtrar(servicios):
    elegidos = {}
    lista = None if SERVICIOS == '*' else {s.strip('\\/').replace('\\', '/').lower() for s in SERVICIOS}
    carpetas = None if CARPETAS is None else {c.strip('\\/').lower() for c in CARPETAS}
    tipos = None if TIPOS is None else {t.lower() for t in TIPOS}
    excluir = {c.lower() for c in EXCLUIR_CARPETAS}
    for (carpeta, nombre, tipo), jsons in servicios.items():
        cl = carpeta.lower()
        completo = (f'{carpeta}/' if carpeta else '') + f'{nombre}.{tipo}'
        if cl.split('/')[0] in excluir:
            continue
        if carpetas is not None and cl not in carpetas:
            continue
        if tipos is not None and tipo.lower() not in tipos:
            continue
        if lista is not None and completo.lower() not in lista:
            continue
        elegidos[(carpeta, nombre, tipo)] = jsons
    if lista is not None:
        encontrados = {((c + '/') if c else '') .lower() + f'{n}.{t}'.lower() for c, n, t in elegidos}
        faltan = lista - encontrados
        if faltan:
            raise Abortar(f'No se encontraron estos servicios en config-store: {sorted(faltan)}')
    if not elegidos:
        raise Abortar('Ningún servicio cumple los filtros SERVICIOS / CARPETAS / TIPOS.')
    return elegidos


def archivos_del_servicio(carpeta, nombre, tipo, jsons):
    svc = f'{nombre}.{tipo}'
    sub = carpeta.replace('/', os.sep)
    cs = os.path.join(CONFIG_STORE, 'services', sub) if carpeta else os.path.join(CONFIG_STORE, 'services')
    ai = os.path.join(ARCGIS_INPUT, sub) if carpeta else ARCGIS_INPUT
    lista, vistos = [], set()

    def agregar(ruta):
        if os.path.normcase(ruta) not in vistos:
            vistos.add(os.path.normcase(ruta))
            lista.append(ruta)

    for j in jsons:
        agregar(j)
    for base in (os.path.join(cs, svc), os.path.join(ai, svc)):
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.lower().endswith('.gdb')]  # datos copiados al servidor
            for f in files:
                fl = f.lower()
                if (fl.endswith(EXT_TEXTO) or fl.endswith(EXT_ZIP)) and '.rlock' not in fl:
                    agregar(os.path.join(root, f))
    return lista


# ----------------------------- validación -----------------------------
def comparar_json(a, b, clave=''):
    """Devuelve [(clave, antes, despues)] y lanza Abortar si hay algo distinto a la ruta esperada."""
    if type(a) is not type(b):
        raise Abortar(f'cambio de tipo en {clave}: {type(a).__name__} -> {type(b).__name__}')
    difs = []
    if isinstance(a, dict):
        if list(a.keys()) != list(b.keys()):
            raise Abortar(f'cambiaron las claves en {clave or "(raíz)"}')
        for k in a:
            difs += comparar_json(a[k], b[k], f'{clave}.{k}' if clave else k)
    elif isinstance(a, list):
        if len(a) != len(b):
            raise Abortar(f'cambió la longitud de la lista {clave}')
        for i, (x, y) in enumerate(zip(a, b)):
            difs += comparar_json(x, y, f'{clave}[{i}]')
    elif isinstance(a, str):
        if a != b:
            if b != PATRON_STR.sub(lambda m: '\\\\' + HOST_NUEVO, a):
                raise Abortar(f'cambio inesperado en {clave}')
            difs.append((clave, a, b))
    elif a != b:
        raise Abortar(f'cambio de valor inesperado en {clave}')
    return difs


def difs_texto(antes):
    texto = antes.decode('utf-8', errors='replace')
    return [('(texto)', m, PATRON_STR.sub(lambda x: '\\\\' + HOST_NUEVO, m))
            for m in sorted({m.group(0) for m in RE_UNC.finditer(texto) if PATRON_STR.search(m.group(0))})]


def validar(nombre, antes, despues, avisos):
    """Valida un contenido (archivo o entrada de .msd). Devuelve las diferencias."""
    nl = nombre.lower()
    if nl.endswith(EXT_JSON):
        try:
            a = json.loads(antes.decode('utf-8-sig'))
        except ValueError:
            a = None
        if a is not None:
            try:
                b = json.loads(despues.decode('utf-8-sig'))
            except ValueError as e:
                raise Abortar(f'{nombre} quedaría con JSON inválido: {e}')
            return comparar_json(a, b)
        avisos.append(f'{nombre}: no era JSON válido antes del cambio; se trató como texto')
    else:
        try:
            ET.fromstring(antes)
            valido_antes = True
        except ET.ParseError:
            valido_antes = False
            avisos.append(f'{nombre}: no era XML bien formado antes del cambio; se trató como texto')
        if valido_antes:
            try:
                ET.fromstring(despues)
            except ET.ParseError as e:
                raise Abortar(f'{nombre} quedaría con XML inválido: {e}')
    # Texto: solo se permite que cambie la ruta
    if despues != sustituir(antes):
        raise Abortar(f'{nombre}: diferencia inesperada')
    return difs_texto(antes)


def procesar_msd(ruta, antes, avisos):
    """Reescribe en memoria un .msd (zip) cambiando solo las entradas que contienen la ruta."""
    difs, n = [], 0
    salida = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(antes)) as zin, zipfile.ZipFile(salida, 'w') as zout:
        for info in zin.infolist():
            datos = zin.read(info)
            if not info.is_dir() and PATRON.search(datos):
                nuevo = sustituir(datos)
                n += len(PATRON.findall(datos))
                nombre_entrada = info.filename if info.filename.lower().endswith(EXT_TEXTO) else info.filename + '.xml'
                difs += [(f'msd:{info.filename} {c}', a, b)
                         for c, a, b in validar(f'{ruta}!{nombre_entrada}', datos, nuevo, avisos)]
                datos = nuevo
            elif RESIDUO.search(datos):
                avisos.append(f'{ruta}!{info.filename}: menciona "{HOST_ANTERIOR}" pero no como \\\\{HOST_ANTERIOR}\\')
            zout.writestr(info, datos)
    despues = salida.getvalue()
    with zipfile.ZipFile(io.BytesIO(despues)) as z:
        if z.testzip() is not None:
            raise Abortar(f'{ruta}: el .msd reconstruido no pasa la verificación')
    return n, despues, difs


def servidor_activo():
    if os.name != 'nt':
        return None
    try:
        r = subprocess.run(['sc', 'query', NOMBRE_SERVICIO_WINDOWS], capture_output=True, text=True)
        if 'RUNNING' in r.stdout or 'START_PENDING' in r.stdout or 'STOP_PENDING' in r.stdout:
            return True
        if 'STOPPED' in r.stdout:
            return False
    except Exception:
        pass
    return None


# ------------------------------- modos -------------------------------
def analizar():
    servicios = filtrar(listar_servicios())
    print(f'Cambio: \\\\{HOST_ANTERIOR}\\...  ->  \\\\{HOST_NUEVO}\\...')
    print(f'Servicios revisados: {len(servicios)}\n')
    cambios, avisos, afectados = [], [], []
    for (carpeta, nombre, tipo), jsons in sorted(servicios.items()):
        etiqueta = (f'{carpeta}/' if carpeta else '') + f'{nombre}.{tipo}'
        cambios_svc = []
        for ruta in archivos_del_servicio(carpeta, nombre, tipo, jsons):
            with open(ruta, 'rb') as f:
                antes = f.read()
            if ruta.lower().endswith(EXT_ZIP):
                try:
                    n, despues, difs = procesar_msd(ruta, antes, avisos)
                except zipfile.BadZipFile:
                    avisos.append(f'{ruta}: no es un zip válido; se omite')
                    continue
                if n == 0:
                    continue
            else:
                n = len(PATRON.findall(antes))
                if n == 0:
                    if RESIDUO.search(antes):
                        avisos.append(f'{ruta}: menciona "{HOST_ANTERIOR}" pero no como \\\\{HOST_ANTERIOR}\\ ; no se toca')
                    continue
                despues = sustituir(antes)
                difs = validar(ruta, antes, despues, avisos)
            cambios_svc.append({'ruta': ruta, 'despues': despues, 'difs': difs, 'n': n, 'servicio': etiqueta})
        if not cambios_svc:
            continue
        afectados.append(etiqueta)
        print(f'== {etiqueta}  ({len(cambios_svc)} archivo(s))')
        for c in cambios_svc:
            print(f'   {c["n"]:>3} cambio(s)  {c["ruta"]}')
            if MOSTRAR_DETALLE:
                for clave, a, b in c['difs']:
                    print(f'          {clave}\n            antes:   {a}\n            después: {b}')
        cambios += cambios_svc
    return cambios, avisos, afectados


def comprobar_rutas_nuevas(cambios):
    """Comprueba (con la cuenta actual) que exista la nueva ruta de datos de cada servicio."""
    faltan, vistas = [], set()
    for c in cambios:
        for clave, _, b in c['difs']:
            if clave != 'properties.path':
                continue
            for unc in RE_UNC.findall(b):
                unc = unc.rstrip('\\ ')
                if unc.lower() in vistas:
                    continue
                vistas.add(unc.lower())
                existe = os.path.exists(unc)
                print(f'  {"OK       " if existe else "NO EXISTE"}  {unc}')
                if not existe:
                    faltan.append(unc)
    if not vistas:
        print('  (ningún properties.path cambia; nada que comprobar)')
    return faltan


def aplicar(cambios):
    activo = servidor_activo()
    if activo and not IGNORAR_SERVIDOR_ACTIVO:
        raise Abortar(f'El servicio de Windows "{NOMBRE_SERVICIO_WINDOWS}" está en ejecución. Detenlo y vuelve a correr.')
    if activo is None:
        print(f'AVISO: no se pudo comprobar el estado de "{NOMBRE_SERVICIO_WINDOWS}". Asegúrate de que esté detenido.')

    destino = os.path.join(BACKUP_DIR, datetime.datetime.now().strftime('%Y%m%d_%H%M%S') + '_cambio_share')
    os.makedirs(destino)
    indice = []
    for i, c in enumerate(cambios):
        copia = os.path.join(destino, f'{i:04d}_{os.path.basename(c["ruta"])}')
        shutil.copy2(c['ruta'], copia)
        indice.append({'original': c['ruta'], 'respaldo': copia, 'servicio': c['servicio']})
    with open(os.path.join(destino, 'indice.json'), 'w', encoding='utf-8') as f:
        json.dump(indice, f, indent=2, ensure_ascii=False)
    print(f'\nRespaldo de {len(indice)} archivo(s) en: {destino}')

    for c in cambios:
        tmp = c['ruta'] + '.tmp_cambio_share'
        with open(tmp, 'wb') as f:
            f.write(c['despues'])
        shutil.copystat(c['ruta'], tmp)
        os.replace(tmp, c['ruta'])
    print(f'Escritos {len(cambios)} archivo(s).')
    print(f'\nPara deshacer: MODO = "restaurar" y RESTAURAR_DESDE = r"{destino}"')


def restaurar():
    if not RESTAURAR_DESDE:
        raise Abortar('Define RESTAURAR_DESDE con la carpeta del respaldo.')
    activo = servidor_activo()
    if activo and not IGNORAR_SERVIDOR_ACTIVO:
        raise Abortar(f'Detén "{NOMBRE_SERVICIO_WINDOWS}" antes de restaurar.')
    with open(os.path.join(RESTAURAR_DESDE, 'indice.json'), encoding='utf-8') as f:
        indice = json.load(f)
    for e in indice:
        shutil.copy2(e['respaldo'], e['original'])
        print(f'  restaurado {e["original"]}')
    print(f'Restauración completa: {len(indice)} archivo(s).')


def main():
    try:
        if MODO == 'restaurar':
            restaurar()
            return
        if MODO not in ('simular', 'aplicar'):
            raise Abortar(f'MODO desconocido: {MODO}')
        cambios, avisos, afectados = analizar()
        if avisos:
            print('\nAVISOS:')
            for a in avisos:
                print('  ' + a)
        if not cambios:
            print('\nNingún servicio contiene la ruta anterior. No hay nada que cambiar.')
            return
        print('\nComprobando la nueva ruta de datos de cada servicio (con TU cuenta, no la de servicio):')
        faltan = comprobar_rutas_nuevas(cambios)
        if faltan and EXIGIR_QUE_EXISTA_NUEVA_RUTA:
            raise Abortar(f'{len(faltan)} ruta(s) nueva(s) no existen o no son accesibles.')
        print(f'\nResumen: {len(afectados)} servicio(s), {len(cambios)} archivo(s) a cambiar.')
        if MODO == 'simular':
            print('SIMULACIÓN: no se escribió nada.')
            print('Si todo se ve bien: detén ArcGIS Server, pon MODO = "aplicar" y vuelve a correr.')
            return
        aplicar(cambios)
    except Abortar as e:
        print(f'\nABORTADO, no se modificó nada: {e}')
        sys.exit(1)


if __name__ == '__main__':
    main()
