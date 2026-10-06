"""Inventario y reparación por lotes de mosaicos publicados en ArcGIS Server."""

import argparse
import csv
import getpass
import json
import ntpath
import os
from pathlib import Path
import time
from urllib.parse import quote

import arcpy
import requests


def save(path, value):
    temporary = Path(str(path) + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def remap(path, old, new):
    path, old, new = [ntpath.normpath(p) for p in (path, old, new)]
    if path.casefold() == old.casefold():
        return new
    if path.casefold().startswith(old.casefold() + '\\'):
        return new + path[len(old):]
    return path


class Server:
    def __init__(self, config):
        self.base = config['server_url'].rstrip('/')
        self.session = requests.Session()
        self.session.verify = config.get('ca_bundle') or True
        self.user = input('Usuario administrador de ArcGIS Server: ')
        self.password = getpass.getpass('Contraseña: ')
        self.token = None
        self.expires = 0

    def authenticate(self):
        r = self.session.post(self.base + '/admin/generateToken', data={
            'f': 'json', 'username': self.user, 'password': self.password,
            'client': 'requestip', 'expiration': 60}, timeout=60)
        r.raise_for_status()
        data = r.json()
        if 'token' not in data:
            raise RuntimeError('No se pudo autenticar con Server Admin. Use una cuenta del sitio.')
        self.token = data['token']
        expires = data.get('expires')
        try:
            self.expires = int(expires) / 1000
        except (TypeError, ValueError):
            self.expires = time.time() + 3000

    def call(self, path, parameters=None, post=False):
        for attempt in range(2):
            if time.time() > self.expires - 120:
                self.authenticate()
            data = {'f': 'json', 'token': self.token, **(parameters or {})}
            method = self.session.post if post else self.session.get
            kwargs = {'data' if post else 'params': data}
            try:
                response = method(self.base + path, timeout=120, **kwargs)
                response.raise_for_status()
                result = response.json()
            except (requests.RequestException, ValueError):
                raise RuntimeError('Fallo HTTP/JSON en ' + path) from None
            if result.get('error', {}).get('code') in (498, 499) and attempt == 0:
                self.expires = 0
                continue
            if 'error' in result or result.get('status') == 'error':
                raise RuntimeError('Error REST en ' + path + ': ' + str(result.get('error', result)))
            return result
        raise RuntimeError('Token rechazado')

    def resource(self, service):
        return '/admin/services/' + quote(service, safe='/.')

    def state(self, service):
        return self.call(self.resource(service) + '/status')

    def transition(self, service, target):
        operation = 'start' if target == 'STARTED' else 'stop'
        self.call(self.resource(service) + '/' + operation, post=True)
        deadline = time.time() + 180
        while time.time() < deadline:
            state = self.state(service)
            if state.get('realTimeState') == target and state.get('configuredState') == target:
                return
            time.sleep(3)
        raise RuntimeError('No se confirmó ' + target + ': ' + service)

    def services(self):
        root = self.call('/admin/services')
        for folder in [''] + root.get('folders', []):
            if folder in ('System', 'Utilities'):
                continue
            data = root if not folder else self.call('/admin/services/' + quote(folder, safe=''))
            for item in data.get('services', []):
                yield (folder + '/' if folder else '') + item['serviceName'] + '.' + item['type']


def inventory(server, config, only_service=None):
    groups, rows, failures = {}, [], []
    for service in server.services():
        if only_service and service.casefold() != only_service.casefold():
            continue
        print('Inventario:', service, flush=True)
        if not service.endswith(('.ImageServer', '.MapServer')):
            rows.append({'servicio': service, 'estado': 'TIPO_NO_APLICA'})
            continue
        matched = False
        try:
            manifest = server.call(server.resource(service) + '/iteminfo/manifest/manifest.json')
            for database in manifest.get('databases', []):
                if database.get('byReference') is not True:
                    continue
                connection = dict(part.split('=', 1) for part in database.get(
                    'onServerConnectionString', '').split(';') if '=' in part)
                connection = {k.strip().upper(): v.strip() for k, v in connection.items()}
                expected = config['manifest_match']
                if not all(connection.get(k.upper(), '').casefold() == v.casefold()
                           for k, v in expected.items()):
                    continue
                for dataset in database.get('datasets', []):
                    name = dataset.get('onServerName', '')
                    # Solo nombres exactos; nunca deducir el mosaico del nombre del servicio.
                    if not name or '/' in name or '\\' in name:
                        continue
                    mosaic = os.path.join(config['sde_connection'], name)
                    if not arcpy.Exists(mosaic):
                        raise RuntimeError('Dataset de manifest no accesible: ' + name)
                    if arcpy.Describe(mosaic).dataType != 'MosaicDataset':
                        continue
                    key = mosaic.casefold()
                    group = groups.setdefault(key, {'mosaico': mosaic, 'servicios': []})
                    if service not in group['servicios']:
                        group['servicios'].append(service)
                    rows.append({'servicio': service, 'estado': 'MOSAICO', 'mosaico': mosaic})
                    matched = True
            if not matched:
                rows.append({'servicio': service, 'estado': 'SIN_MOSAICO_COINCIDENTE'})
        except Exception as exc:
            failures.append(service)
            rows.append({'servicio': service, 'estado': 'ERROR_INVENTARIO', 'error': str(exc)})
    return [g for g in groups.values() if any(s.endswith('.ImageServer') for s in g['servicios'])], rows, failures


def export_paths(mosaic, gdb, name):
    table = os.path.join(str(gdb), name)
    arcpy.management.ExportMosaicDatasetPaths(mosaic, table, export_mode='ALL')
    fields = {f.name.lower(): f.name for f in arcpy.ListFields(table)}
    if 'path' not in fields:
        raise RuntimeError('La tabla exportada no contiene el campo Path')
    with arcpy.da.SearchCursor(table, [fields['path']]) as cursor:
        return [str(row[0]) for row in cursor if row[0]]


def process(server, config, group, gdb, index, apply, journal):
    result = {**group, 'estado': 'DIAGNOSTICO', 'estados_originales': {}}
    prefix = 'm' + str(index)
    old, new = config['old_root'], config['new_root']
    before = export_paths(group['mosaico'], gdb, prefix + '_antes')
    changes = sorted({(p, remap(p, old, new)) for p in before if remap(p, old, new) != ntpath.normpath(p)})
    result['cambios'] = changes
    result['destinos_no_accesibles'] = [new_path for _, new_path in changes if not os.path.isfile(new_path)]
    save(journal, result)
    if not changes:
        result['estado'] = 'SIN_CAMBIOS'
    elif result['destinos_no_accesibles']:
        result['estado'] = 'BLOQUEADO_DESTINO'
    elif apply:
        # Capturar todos los estados antes de detener el primer servicio.
        for service in group['servicios']:
            state = server.state(service)
            if state.get('configuredState') != state.get('realTimeState') or state.get('realTimeState') not in ('STARTED', 'STOPPED'):
                raise RuntimeError('Estado inestable: ' + service)
            result['estados_originales'][service] = state['realTimeState']
        result['estado'] = 'PREPARADO'
        save(journal, result)
        mutation_started = False
        try:
            for service, state in result['estados_originales'].items():
                if state == 'STARTED':
                    server.transition(service, 'STOPPED')
            result['estado'] = 'REPARANDO'
            save(journal, result)
            mutation_started = True
            arcpy.management.RepairMosaicDatasetPaths(group['mosaico'], [[old, new]])
            after = export_paths(group['mosaico'], gdb, prefix + '_despues')
            expected = sorted(ntpath.normcase(remap(p, old, new)) for p in before)
            actual = sorted(ntpath.normcase(ntpath.normpath(p)) for p in after)
            if expected != actual:
                raise RuntimeError('Las rutas finales no coinciden con el reemplazo esperado')
            result['estado'] = 'ACTUALIZADO'
        except Exception as exc:
            result['error'] = str(exc)
            # No invertir toda la raíz: podría alterar rutas nuevas preexistentes.
            result['estado'] = 'REVISION_REQUERIDA' if mutation_started else 'ERROR_ANTES_CAMBIO'
        finally:
            save(journal, result)
            result['errores_reinicio'] = []
            # Si la reparación pudo quedar parcial, mantener detenidos para revisar.
            if result['estado'] != 'REVISION_REQUERIDA':
                for service, state in result['estados_originales'].items():
                    if state == 'STARTED':
                        try:
                            server.transition(service, 'STARTED')
                        except Exception as exc:
                            result['errores_reinicio'].append({'servicio': service, 'error': str(exc)})
                if result['errores_reinicio']:
                    result['estado'] = 'ERROR_REINICIO'
    save(journal, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--limit', type=int, help='Máximo de mosaicos por ejecución')
    parser.add_argument('--service', help='Restringir mosaicos a un servicio; incluye sus dependientes')
    parser.add_argument('--backup-confirmed', action='store_true')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    if args.limit is not None and args.limit < 1:
        parser.error('--limit debe ser positivo')
    old, new = config['old_root'], config['new_root']
    if not old.startswith('\\\\') or not new.startswith('\\\\'):
        parser.error('Las raíces deben ser rutas UNC')
    if remap(new, old, old) == ntpath.normpath(old) or ntpath.normcase(old) == ntpath.normcase(new):
        parser.error('Las raíces deben ser distintas')
    if not config.get('manifest_match') or 'DATABASE' not in config['manifest_match'] or not any(k in config['manifest_match'] for k in ('SERVER', 'INSTANCE')):
        parser.error('manifest_match necesita DATABASE y SERVER o INSTANCE exactos')
    if args.apply and not args.backup_confirmed:
        parser.error('Para aplicar, indique --backup-confirmed tras disponer de respaldo recuperable')
    if not arcpy.Exists(config['sde_connection']):
        parser.error('Conexión SDE inaccesible')
    out = Path(config.get('output_dir', 'resultados')) / time.strftime('%Y%m%d_%H%M%S')
    out.mkdir(parents=True, exist_ok=False)
    gdb = arcpy.management.CreateFileGDB(str(out), 'auditoria.gdb').getOutput(0)
    server = Server(config)
    groups, rows, failures = inventory(
        server,
        config,
        only_service=args.service
    )
    save(out / 'inventario.json', rows)
    if failures and args.apply:
        raise RuntimeError('Inventario incompleto; no se aplicaron cambios. Revise inventario.json')
    if args.limit:
        groups = groups[:args.limit]
    save(out / 'plan.json', groups)
    results = []
    for index, group in enumerate(groups, 1):
        print(f"[{index}/{len(groups)}] {group['mosaico']}", flush=True)
        try:
            result = process(server, config, group, gdb, index, args.apply, out / f'mosaico_{index}.json')
        except Exception as exc:
            result = {**group, 'estado': 'ERROR', 'error': str(exc)}
        results.append(result)
        save(out / 'resultados.json', results)
        print(result['estado'], flush=True)
        if args.apply and result['estado'] in ('ERROR', 'REVISION_REQUERIDA', 'ERROR_REINICIO', 'ERROR_ANTES_CAMBIO'):
            print('Lote detenido. Consulte el diario del mosaico antes de reanudar.')
            break
    with (out / 'resumen.csv').open('w', newline='', encoding='utf-8-sig') as file:
        writer = csv.writer(file)
        writer.writerow(['mosaico', 'servicios', 'estado', 'rutas_a_cambiar', 'error'])
        for r in results:
            writer.writerow([r['mosaico'], ';'.join(r['servicios']), r['estado'], len(r.get('cambios', [])), r.get('error', '')])
    print('Resultados:', out.resolve())
    print('El estado STARTED no certifica el despliegue. Validar imágenes y overviews a varias escalas.')


if __name__ == '__main__':
    main()
