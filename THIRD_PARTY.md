# Dependencies and provenance

LL2S's independently implemented Python code is MIT-licensed. No FRR source is
copied, linked or vendored. Cisco-style command vocabulary does not imply Cisco
software, affiliation or full CLI compatibility.

The current Alpine image installs [Open vSwitch](https://www.openvswitch.org/)
through Alpine's package repository. The tested package is `openvswitch-3.7.1-r0`.
The `0.2.0-ovs-rstp1` release overrides only `ovs-vswitchd` with a source-built
OVS 3.7.1 daemon carrying the [root-vector patch](packaging/ovs-rstp-test/root-vector-order.patch).
Its exact source, build metadata and override hashes accompany the image;
the other OVS executables retain their Alpine package identity.
OVS user-space software is primarily Apache-2.0 licensed; its upstream tree and
packaged components retain their own notices and applicable licenses. LL2S calls
its separate `ovs-vsctl` and `ovs-appctl` programs. The host supplies Linux's
Open vSwitch kernel module.

Python, iproute2, Alpine and their dependencies retain their respective licenses.
The image also installs Alpine's `lldpd` package (tested `1.0.22-r0`, ISC license)
for LLDP discovery. LL2S invokes its separate daemon and `lldpcli` executable;
no lldpd source is copied into LL2S. See [upstream](https://lldpd.github.io/).
The Dockerfile records package installation and the base image series. Packages
are not yet pinned to exact revisions. Published images include an exact package inventory and source instructions at
`/usr/share/ll2s/`. Each binary release is accompanied by a dependency source
bundle containing Alpine recipes, patches and checksum-verified upstream sources
with their notices. See [container sources](packaging/SOURCES.md).

The earlier mstpd prototype and its local control-socket patch are no longer
included in this image or used by LL2S. The current backend uses OVS for both STP
and RSTP.

Basic SNMP monitoring uses Alpine's Net-SNMP packages (`net-snmp` and
`net-snmp-tools`, tested 5.9.5.2), invoked as separate executables. Net-SNMP carries
BSD-style and other component-specific notices; see the upstream
[COPYING file](https://github.com/net-snmp/net-snmp/blob/master/COPYING).
No Net-SNMP source is copied into LL2S.
