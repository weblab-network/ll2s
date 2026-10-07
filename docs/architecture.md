# Architecture

```text
ll2sh → validated candidate → OVSDB transaction → ovs-vswitchd
                                                ├─ STP / RSTP engine
                                                └─ Linux Open vSwitch datapath
```

LL2S remains independent of FRR. The CLI and configuration model do not implement
Ethernet forwarding or spanning tree themselves. They invoke separate OVS tools
with argument arrays and never evaluate configuration through a shell.

## Why Open vSwitch

The first prototype used Linux bridges and mstpd. Linux 6.1 cannot select
userspace STP inside an ordinary container namespace: its helper runs only in
the initial network namespace. The explicit `stp_mode user` API requires newer
kernel and iproute2 support. This made the original container backend unsuitable
for RSTP on that kernel.

OVS supplies its own STP/RSTP engine and programs its kernel datapath, so LL2S now
works with the host's existing 6.1 Open vSwitch module. OVS uses one tree across
all VLANs. No kernel patches, FRR integration or mstpd build are needed.

## Policy mapping

| LL2S | OVS |
| --- | --- |
| spanning-tree mode rstp | Bridge `rstp_enable=true`, `stp_enable=false` |
| spanning-tree mode stp | Bridge `stp_enable=true`, `rstp_enable=false` |
| spanning-tree priority | Bridge `other_config:rstp-priority` or `stp-priority` |
| spanning-tree cost | Port `other_config:rstp-path-cost` or `stp-path-cost`; omitted for auto |
| spanning-tree portfast | Port `other_config:rstp-port-admin-edge=true` |
| access VLAN | Port `vlan_mode=access`, `tag=VID` |
| trunk without an allowed native VLAN | Port `vlan_mode=trunk`, explicit `trunks` |
| trunk with an allowed native VLAN | Port `vlan_mode=native-untagged`, `tag=VID`, explicit `trunks` |
| shutdown | Linux interface administratively down |

Automatic RSTP edge detection is disabled. BPDU guard has no equivalent OVS
setting and is rejected by this backend before mutation; no unsupported key is
written to OVSDB. See the [OVS schema reference](https://www.openvswitch.org/support/dist-docs/ovs-vswitchd.conf.db.5.html).

## Ownership and apply ordering

LL2S owns only OVS `br0` marked `external_ids:ll2s-owner=ll2s` and similarly marked
data ports. Preflight checks prevent takeover of other bridges, OVS memberships,
Linux masters, or addressed interfaces. An existing Linux bridge requires explicit
migration. Unmanaged ports added to the owned bridge cause a preflight rejection.

Every commit currently interrupts all managed data ports. LL2S journals their
ifindex/MAC identities, takes ports down, then applies memberships, VLAN policy,
and mutually exclusive protocol flags in one OVSDB transaction. It checks that
OVS created real OpenFlow ports without Interface errors before bringing them up.
A successful ovs-vsctl exit alone is not sufficient evidence of port attachment.

The shell stores successful running intent and attempts rollback after a mutation
failure. Preflight failures do not trigger disruptive rollback. If rollback fails,
a persistent uncertain-state marker blocks saving. Existing startup text is never
overwritten by a failed apply.

## Service lifecycle

The Python supervisor starts a private ovsdb-server and ovs-vswitchd in the same
network namespace. Each instance has explicit control/database socket paths. It
recreates runtime OVS configuration from saved LL2S startup text, optionally
waiting for ports attached later by the lab harness.

If either daemon exits, the supervisor attempts to take every journaled data
port down before exiting. The journal includes interface identities to avoid
shutting down a replacement interface with a reused name. This path does not need
a working OVSDB. The internal bridge interface alone is not a forwarding gate.

The per-session configuration lock and per-service lifecycle lock serialize LL2S
operations, not external network managers. Daemon failure shutdown is best effort;
continuous reconciliation, incremental changes and hardware watchdog behavior
remain future work. Keep this prototype in a dedicated switch namespace.

## LLDP discovery

`ll2s/lldp.py` supervises a separate, non-critical `lldpd` process. Global enable
and per-port TX/RX settings live in the ordinary Config/Port model and startup
text. Defaults are global off and port TX/RX on. The supervisor reads committed
`running.json` every half second; it never reads the CLI candidate. A failed
switching commit therefore cannot publish its candidate discovery settings.

The service initially excludes all interfaces. It installs hostname, description,
Bridge capability and ethN port IDs, with a disabled default port policy, before
enabling allowed managed ports. Chassis identification uses managed-port MACs.
Shut ports and ports with both directions disabled are excluded. Neighbor output
uses `lldpcli -f json0`; remote strings are sanitized before terminal rendering.

Changes to discovery policy restart this process and clear its neighbor cache.
No changes to OVS policy are made by the discovery service. Its private socket,
PID file and status file are `lldp.sock`, `lldp.pid` and `lldp-status.json` under
the runtime directory. The status includes a hash of the reconciled policy so
CLI displays can identify pending settings instead of presenting stale results.
LLDP daemon failures retry after five seconds and do not affect the critical OVS
daemon list or switching readiness. An uncertain switching state disables LLDP.
The daemon starts in a separate process session and is stopped during shutdown.

## Management and monitoring

One optional owned internal port, `ll2s-mgmt`, maps an IPv4 endpoint to an access
VLAN. Global management fields and SNMP settings share Config validation, candidate
commit/rollback and startup replay. Physical addressed-port rejection is unchanged.
Management names are reserved; preflight refuses foreign interfaces or default
routes. The management port is excluded from physical membership comparisons but
its ownership and internal type are checked. OVS attachment readback includes it.

When management is enabled, the implicit br0 local port is assigned VLAN 4095,
which cannot be selected by LL2S data-port configuration. This prevents Linux's
weak-host model from answering for the management address through br0 on VLAN 1.
The management MAC is a locally administered address derived from its IPv4
address, stable across recreation. Only the owned management interface has its
IPv4 addresses/routes replaced. LL2S
does not configure multiple SVIs, forwarding sysctls or general L3 routing.

`ll2s/snmp.py` supervises Net-SNMP separately from OVS. It reconciles committed
settings, binds UDP to the management address, and installs a v2c-only VACM group
with a read view limited to system, interfaces/ifXTable and LLDP-MIB. No v1 group,
v3 users or write view is installed. Generated daemon config is mode 0600; status
contains a policy hash and process state, never the community. Configuration
changes restart snmpd; process failure retries after five seconds. Shutdown closes
data ports before stopping monitoring/discovery.

When SNMP is enabled, lldpd starts with AgentX on `<runtime>/agentx.sock` (0600).
The subagent reconnects after master restarts. SNMP settings are replayable startup
text; v2c needs no persistent user database or stable engine ID. Temporary daemon
protocol state lives in `<runtime>/snmp-state`. SNMPv3 requires a separate state
and backup design. Basic SNMP exposes Linux interface metrics, not OVS-specific
BRIDGE-MIB/Q-BRIDGE-MIB tables.

## Future work

See the [roadmap](roadmap.md) for further monitoring work, MSTP backend alternatives,
proof-of-feasibility milestones and other nice-to-have work.

Incremental configuration, BPDU guard with tested enforcement, LACP,
and multiple management/SVI interfaces. Native Weblab integration is implemented in the
Weblab repository. Full MSTP and hardware
offload require separate capability and interoperability validation.
