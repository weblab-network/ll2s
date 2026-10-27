"""Record the explicit daemon override; preserve the inherited APK inventory."""
import hashlib
import json
import os
from pathlib import Path


root = Path('/usr/share/ll2s/ovs-rstp-test')
binary = Path('/usr/sbin/ovs-vswitchd')
packages = json.loads(Path('/usr/share/ll2s/packages.json').read_text())
package = next(row for row in packages if row['name'] == 'openvswitch')
if package['version'] != '3.7.1-r0':
    raise SystemExit('This test variant requires the reviewed OVS 3.7.1-r0 base package')


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


record = {
    'variant': 'rstp-root-vector-1',
    'base_image': os.environ['LL2S_BASE'],
    'base_apk_package': package,
    'overridden_files': {str(binary): sha256(binary)},
    'source_version': '3.7.1',
    'source_archive_sha256': sha256(root / 'openvswitch-3.7.1.tar.gz'),
    'patch_sha256': sha256(root / 'root-vector-order.patch'),
    'configure_options': ['--disable-ssl', '--disable-libcapng', '--without-dpdk'],
    'notice': 'packages.json identifies inherited APKs; this record identifies the '
              'source-built override of ovs-vswitchd. Other OVS binaries are unchanged. '
              'Local test variant; not an LL2S release or an unmodified Alpine package.',
}
(root / 'override.json').write_text(json.dumps(record, indent=2) + '\n')
with Path('/usr/share/ll2s/SOURCES.md').open('a') as stream:
    stream.write('\n## Local OVS root-vector test variant\n\n'
                 'The ovs-vswitchd binary is source-built and overrides the inherited '
                 'Alpine package file. See ovs-rstp-test/override.json for exact hashes '
                 'and build options. This directory contains the verified upstream '
                 'source archive, applied patch, OVS license, compiler identity and '
                 'builder APK metadata. Other runtime dependencies retain the base '
                 'image inventory and source arrangements described above.\n')
