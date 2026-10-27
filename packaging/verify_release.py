#!/usr/bin/env python3
"""Verify the frozen binary/source pair before publishing the tested variant."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[1]


def verify(directory, *, installed=False):
    lock = json.loads((ROOT / 'packaging/ovs-rstp-test/release.json').read_text())
    for name, expected in lock['files'].items():
        if Path(name).name != name:
            raise ValueError('Invalid release filename')
        with (directory / name).open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != expected:
            raise ValueError('Release checksum mismatch: ' + name)
    identity = json.loads((directory / 'image.json').read_text())
    if (identity['Id'] != lock['image_id'] or identity['image'] != lock['image']
            or identity['Architecture'] != 'amd64' or identity['Os'] != 'linux'):
        raise ValueError('Image identity mismatch')
    with tarfile.open(directory / 'll2s-dependency-sources.tar.gz') as archive:
        for name in ('Dockerfile', 'README.md', 'record_override.py', 'root-vector-order.patch'):
            if archive.extractfile('ovs-override/' + name).read() != (
                    ROOT / 'packaging/ovs-rstp-test' / name).read_bytes():
                raise ValueError('Bundled recipe differs from source tag: ' + name)
        override = json.load(archive.extractfile('ovs-override/override.json'))
        if override != identity['ovs_override']:
            raise ValueError('Override identity mismatch')
        source = archive.extractfile('ovs-override/openvswitch-3.7.1.tar.gz')
        if hashlib.file_digest(source, 'sha256').hexdigest() != override['source_archive_sha256']:
            raise ValueError('Upstream source checksum mismatch')
        patch = archive.extractfile('ovs-override/root-vector-order.patch')
        if hashlib.file_digest(patch, 'sha256').hexdigest() != override['patch_sha256']:
            raise ValueError('Patch checksum mismatch')
        inventory = json.load(archive.extractfile('dependencies/packages.json'))
        if override['base_apk_package'] not in inventory:
            raise ValueError('Missing inherited OVS package identity')
    if installed:
        def run(*args):
            return subprocess.check_output(args).decode().strip()
        if run('docker', 'image', 'inspect', lock['image'], '--format', '{{.Id}}') != lock['image_id']:
            raise ValueError('Loaded image differs from tested image')
        actual = run('docker', 'run', '--rm', '--network', 'none', '--entrypoint',
                     'sha256sum', lock['image_id'], '/usr/sbin/ovs-vswitchd').split()[0]
        if actual != override['overridden_files']['/usr/sbin/ovs-vswitchd']:
            raise ValueError('Installed daemon differs from tested daemon')
        actual_inventory = json.loads(run('docker', 'run', '--rm', '--network', 'none',
                                         '--entrypoint', 'cat', lock['image_id'],
                                         '/usr/share/ll2s/packages.json'))
        if actual_inventory != inventory:
            raise ValueError('Runtime APK inventory differs from source bundle')
    print('Release binary, corresponding sources and reviewed lock agree')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--installed', action='store_true')
    args = parser.parse_args()
    verify(args.directory, installed=args.installed)
