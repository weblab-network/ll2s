#!/usr/bin/env python3
"""Collect exact Alpine package recipes, patches and verified upstream sources.

Run in a disposable Alpine container. Snapshot installed APK metadata BEFORE
installing build tools. The output accompanies the binary image release.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile


def inventory(text):
    rows = []
    for block in text.strip().split('\n\n'):
        fields = {}
        for line in block.splitlines():
            if ':' in line:
                key, value = line.split(':', 1)
                if key in {'P', 'V', 'o', 'c', 'L', 'U'}:
                    fields[key] = value
        if not fields:
            continue
        if not all(fields.get(k) for k in ('P', 'V', 'o', 'c', 'L')):
            raise ValueError('Package metadata missing source identity')
        if not re.fullmatch(r'[a-zA-Z0-9+_.-]+', fields['o']) or not re.fullmatch(r'[0-9a-f]{7,40}', fields['c']):
            raise ValueError('Invalid package source identity')
        rows.append(dict(name=fields['P'], version=fields['V'], origin=fields['o'],
                         commit=fields['c'], license=fields['L'], upstream=fields.get('U', '')))
    if not rows:
        raise ValueError('Empty package inventory')
    return sorted(rows, key=lambda r: r['name'])


def run(*args, **kwargs):
    return subprocess.check_output(args, **kwargs)


def collect(database, output):
    rows = inventory(database.read_text())
    output.mkdir(parents=True, exist_ok=True)
    (output / 'packages.json').write_text(json.dumps(rows, indent=2) + '\n')
    (output / 'apk-installed.txt').write_bytes(database.read_bytes())
    repo = Path('/tmp/ll2s-aports')
    repo.mkdir()
    run('git', 'init', '-q', str(repo))
    run('git', '-C', str(repo), 'remote', 'add', 'origin', 'https://github.com/alpinelinux/aports.git')
    for origin, commit in sorted({(r['origin'], r['commit']) for r in rows}):
        print('Collecting sources:', origin, commit, flush=True)
        run('git', '-C', str(repo), 'fetch', '-q', '--depth=1', '--filter=blob:none', 'origin', commit)
        entries = run('git', '-C', str(repo), 'ls-tree', '-d', '--name-only', 'FETCH_HEAD',
                      'main/' + origin, 'community/' + origin).decode().splitlines()
        if len(entries) != 1:
            raise RuntimeError('Cannot identify recipe for ' + origin)
        destination = output / 'aports' / origin / commit
        destination.mkdir(parents=True, exist_ok=True)
        archive = Path('/tmp/ll2s-recipe.tar')
        archive.write_bytes(run('git', '-C', str(repo), 'archive', 'FETCH_HEAD:' + entries[0]))
        with tarfile.open(archive) as bundle:
            bundle.extractall(destination, filter='data')
        series = '.'.join(Path('/etc/alpine-release').read_text().strip().split('.')[:2])
        had_src = (destination / 'src').exists()
        env = dict(os.environ, SRCDEST=str(destination / 'distfiles'),
                   DISTFILES_MIRROR='https://distfiles.alpinelinux.org/distfiles/v' + series)
        # abuild validates each fetched source against the recipe's checksums.
        subprocess.run(['abuild', '-F', 'fetch', 'verify'], cwd=destination, env=env, check=True)
        # abuild creates scratch symlinks with container-absolute paths.
        # Keep the actual recipes, patches and distfiles; omit this scratch tree.
        if not had_src and (destination / 'src').is_dir():
            shutil.rmtree(destination / 'src')
    (output / 'README.txt').write_text(
        'Sources accompanying the LL2S container. packages.json identifies exact installed binaries.\n'
        'aports/<origin>/<commit>/ contains Alpine build recipes, patches and verified distfiles.\n'
        'Build with the matching Alpine release and abuild toolchain; see APKBUILD and upstream\n'
        'license files inside each source archive. LL2S source is in the release Git tag.\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    collect(args.database, args.output.resolve())
