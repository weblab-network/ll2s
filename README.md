# LL2S — Linux Layer 2 Switch

A standalone Cisco-style shell for Linux switching, using **Open vSwitch (OVS)**
for forwarding, VLANs and spanning tree. No FRR dependency or fork is required.
The local CLI is `ll2sh`; LL2S itself uses Python's standard library.

**RSTP works on the tested Linux 6.1 host.** This backend does not require the
new Linux bridge `stp_mode` API. The host's Open vSwitch kernel module supplies
the datapath; OVS runs the STP/RSTP protocol engine in the switch container.

## Features

- Global/interface configuration modes, `?` help, Tab completion and `-c` commands.
- One explicitly owned OVS bridge, `br0`, with access and trunk ports.
- Allowed trunk VLAN lists, native VLANs and optional VLAN names.
- STP and RSTP, bridge priority, path cost, RSTP edge ports and port shutdown.
- Candidate configuration, `commit`, rollback attempts and `write memory`.
- Startup replay, including a configurable wait for lab interfaces to arrive.
- Live `show spanning-tree`, `show vlan`, `show interfaces`, and `show mac address-table`.
- LLDP neighbor discovery, detailed displays and per-port transmit/receive controls.
- Management IPv4 on one VLAN and read-only SNMPv2c (system, interfaces, LLDP).
- Supervised private OVS daemons; daemon loss triggers data-port shutdown.

This is a lab prototype. **BPDU guard is unsupported by this OVS backend**:
`spanning-tree bpduguard enable` is recognized for configuration compatibility,
but a commit containing it is rejected before networking changes. Use
`no spanning-tree bpduguard` to remove it. Portfast does not provide BPDU guard.
MSTP, PVST+, Rapid-PVST, LACP configuration and hardware offload are not implemented.

## Build and run

Use a Linux amd64 Docker host with the Open vSwitch kernel module. Prebuilt
images are published at `ghcr.io/weblab-network/ll2s:0.2.0`; `latest` follows
the latest released build.

### Prebuilt image

```sh
sudo modprobe openvswitch
docker pull ghcr.io/weblab-network/ll2s:0.2.0
docker run -d --name ll2s --network none \
  --cap-add NET_ADMIN --cap-add NET_RAW \
  -v ll2s-config:/etc/ll2s ghcr.io/weblab-network/ll2s:0.2.0
docker exec -it ll2s ll2sh
```

Pull on the **Docker host used by Weblab**. New Weblab integrations use the
versioned GHCR image directly. If your Weblab image selector still lists only
`ll2s:dev`, add that compatibility tag:

```sh
docker pull ghcr.io/weblab-network/ll2s:0.2.0
docker tag ghcr.io/weblab-network/ll2s:0.2.0 ll2s:dev
docker pull quay.io/frrouting/frr:10.7.1
docker pull alpine:latest
```

Then select LL2S switches, FRR routers and Alpine PCs in Weblab. LL2S needs no KVM
or vendor image. Weblab attaches the data interfaces; standalone containers start
without them. Older Weblab releases without an LL2S profile require an update.
The `ll2s:dev` name is a compatibility tag, not a registry address. Updating the
image/tag does not change running nodes: save with `wr`, then Stop/Start them.

### Build from source

On the Docker host:

```sh
git clone https://github.com/weblab-network/ll2s.git
cd ll2s
sudo modprobe openvswitch
docker build -t ll2s:dev .
docker run -d --name ll2s --network none \
  --cap-add NET_ADMIN --cap-add NET_RAW \
  -v ll2s-config:/etc/ll2s ll2s:dev
docker exec -it ll2s ll2sh
```

The host needs an installed `openvswitch` module. Load it after reboot or configure
your normal module-loading mechanism. The image deliberately does not load host modules
or need SYS_MODULE, host networking, a Docker socket, or privileged mode.

The switch starts without data ports. Your lab harness must attach dedicated
Ethernet/veth interfaces to its network namespace. LL2S refuses addressed ports,
ports owned by other bridges/bonds, an unowned OVS `br0`, and an existing Linux
bridge named `br0`. It never flushes a port's IP addresses to take it over.

Once `eth1` and `eth2` exist:

```text
ll2s# configure terminal
ll2s(config)# hostname SW1
SW1(config)# spanning-tree mode rstp
SW1(config)# spanning-tree priority 4096
SW1(config)# vlan 10,20
SW1(config)# vlan 10 name USERS
SW1(config)# vlan 20 name SERVERS
SW1(config)# interface eth1
SW1(config-if)# switchport mode trunk
SW1(config-if)# switchport trunk allowed vlan 10,20
SW1(config-if)# exit
SW1(config)# interface eth2
SW1(config-if)# switchport mode access
SW1(config-if)# switchport access vlan 10
SW1(config-if)# spanning-tree portfast
SW1(config-if)# exit
SW1(config)# commit
SW1(config)# end
SW1# show spanning-tree
SW1# show vlan
SW1# write memory
```

The default protocol remains STP for existing configuration compatibility;
select `spanning-tree mode rstp` explicitly. All VLANs share one spanning tree.
Priorities are 0–61440 in increments of 4096. Path cost 0 means automatic;
manual STP costs are 1–65535 and RSTP costs are 1–200000000.

Ports default to access VLAN 1, administratively enabled after commit. Trunks
default to allowed/native VLAN 1. When the native VLAN is allowed, it is untagged;
when absent from the allowed list, the trunk accepts only its permitted tagged
VLANs. `shutdown` / `no shutdown` controls administrative state. `no interface`
detaches a switch port and leaves it down.

RSTP edge behavior is explicit through `spanning-tree portfast`. Automatic edge
detection is disabled, so links without portfast remain ordinary spanning-tree
ports. Configure portfast on host-facing ports, not the switch triangle's trunks.

## Configuration and lifecycle

Changes remain in the candidate until `commit` or `end`. `show candidate-config`
displays pending changes; `abort` discards them. `end` commits pending changes and
returns to exec mode; Ctrl+Z does the same in the container's interactive console,
discarding any partially typed, unsubmitted line. A failed commit leaves you in
configuration mode with the candidate available to fix or abort. With no pending
changes and healthy runtime state, `end` simply exits without reapplying ports.
`exit` still moves up one mode without committing. `write memory`
saves committed configuration and refuses to silently discard pending changes.
Bare `write` (or `wr`) is a shorthand for `write memory`, with the same save checks.
Unambiguous keyword abbreviations work in commands, Tab completion and help:
`conf t`, `sh runn`, and `do sh cand` are accepted. Ambiguous input such as `sh s`
is rejected; use `?` to see matching commands.

Name a VLAN with `vlan 100 name USERS`, or quote a name containing spaces:
`vlan 200 name "Guest LAN"`. Names contain 1–32 printable characters and are
local labels; they do not change VLAN tags or spanning-tree behavior. `show vlan`
lists committed VLAN IDs and names, followed by live OVS port settings. Names
follow the normal candidate/commit/save workflow. `no vlan 100 name` removes only
the label; `no vlan 100` deletes the VLAN and its name. Existing ID-only startup
files and runtime state remain compatible.

In the container's interactive console, press `?` for immediate context help;
Enter is not required. For example, `sh ru?` matches `show running-config`, `show ?` lists show commands and
`switchport ?` lists interface-mode commands. Your partially typed command and
cursor position are preserved. Tab completion, arrow-key history and editing
remain available. `help` and `-c 'show ?'` also work. Native Python builds without
GNU Readline retain `?` followed by Enter; the startup banner states which mode
is available. The Ctrl+Z binding also requires GNU Readline; use `end` otherwise.

## LLDP discovery

LLDP is disabled globally by default, including when loading older configurations.
Enable it on the switch with:

```text
configure terminal
lldp run
end
wr
show lldp neighbors
show lldp neighbors detail
show lldp neighbors interface eth0
show lldp neighbors interface eth0 detail
show lldp interface
show lldp interface eth0
```

Once enabled globally, managed ports default to both transmit and receive.
Interface commands `no lldp transmit` and `no lldp receive` control each direction;
`lldp transmit` and `lldp receive` restore them. `no lldp run` disables discovery
globally. Unmanaged ports, internal `br0` and administratively shut ports do not
participate. A link discarded by RSTP can still discover its direct neighbor.

The summary shows remote device name, local interface, enabled capabilities and
remote port ID. Details include chassis ID, descriptions, advertised management
addresses, advertised TTL and neighbor age. Missing fields appear as `-`. LL2S
advertises its configured hostname, Bridge capability, and Linux interface names
as port IDs. These are real LLDP advertisements supplied by the separate
[`lldpd` service](https://lldpd.github.io/usage.html); FRR routers need their own
LLDP agent to appear as neighbors.

LLDP settings follow candidate/commit/save semantics. The supervisor reconciles
committed LLDP settings asynchronously; displays report pending/starting status
until ready. A configuration change affecting LLDP restarts its daemon and clears
the local neighbor cache. Allow discovery to refill (default advertisements every
30 seconds, advertised lifetime 120 seconds). Other switches may retain old
information until a withdrawal or expiry. Startup on isolated networks can take
several seconds. LLDP failures report unavailable status and are retried without
stopping OVS or data forwarding. Configuration commits themselves still have the
normal port interruption described below.

## Management IPv4 and SNMPv2c

Configure one management endpoint on a declared VLAN. The address belongs to
LL2S's owned internal OVS interface `ll2s-mgmt`; physical ports keep their normal
access/trunk configuration. Carry the management VLAN to your monitoring PC or
router using those ports. This does not add general SVI/inter-VLAN routing.

```text
configure terminal
vlan 100 name MANAGEMENT
management vlan 100
management ip address 10.100.0.11/24
ip default-gateway 10.100.0.1
snmp-server community labpublic ro
snmp-server contact "Lab operator"
snmp-server location "Rack 1"
lldp run
end
wr
show management
show snmp
```

Use a unique address for each switch. The optional default gateway must be another
address in the management subnet; omit it for local-subnet polling. Existing
foreign default routes cause preflight rejection rather than being overwritten.
`management vlan` defaults to VLAN 1. Address syntax is IPv4/prefix (no DHCP,
IPv6 management or multiple management interfaces in this release).

SNMP is disabled until a community is configured and committed. Net-SNMP listens
on **that management IPv4 address, UDP 161**, with a v2c-only read view and no SET
access. Optional `snmp-server source 10.0.0.0/8` limits query sources;
`no snmp-server source` restores the default of any IPv4 source that can reach
management. A community contains 1–64 letters, digits or `_.@+-` characters.
Contact/location accept quoted spaces. `no snmp-server contact` and
`no snmp-server location` clear those fields.

From a monitoring host with Net-SNMP tools:

```sh
snmpget -v2c -c labpublic 10.100.0.11 .1.3.6.1.2.1.1.5.0
snmpwalk -v2c -c labpublic 10.100.0.11 .1.3.6.1.2.1.2
snmpwalk -v2c -c labpublic 10.100.0.11 .1.3.6.1.2.1.31
snmpwalk -v2c -c labpublic 10.100.0.11 .1.0.8802.1.1.2
```

These cover system identity/agent uptime, IF-MIB including 64-bit counters,
and lldpd's LLDP-MIB subset through a private AgentX socket. LLDP tables require
`lldp run` and discovered neighbors. Interface counters are Linux interface
counters, including internal/loopback interfaces; they are not ASIC counters or
an implementation of OVS VLAN/FDB/STP MIBs. ifIndex can change when containers or
interfaces are recreated: rediscover indices using ifName. Agent uptime resets
when snmpd restarts.

`show snmp` reports pending/starting/running/unavailable without displaying the
community. Changes follow normal candidate/commit/save semantics and currently
interrupt managed ports. Monitoring failures retry independently of forwarding.
Allow polling/discovery to recover after commits. To disable SNMP and remove the
management endpoint:

```text
configure terminal
no snmp-server community
no management ip address
end
wr
```

Removing the address also clears its gateway. Disable SNMP first, or the commit
is rejected. `no ip default-gateway` removes only the gateway.

SNMPv2c communities are plaintext on the wire and in replayable running/startup
configuration and exported Weblab backups. Local state files are written with
mode 0600. This lab implementation has no SNMPv3, traps/informs or writable MIBs.
No additional saved-state files are required: Weblab's existing `wr` then
Stop/Start preserves management and v2c settings. Rebuild `ll2s:dev` and recreate
existing switches to install this feature; saving first preserves their settings.

## Commit and service behavior

**Commits interrupt traffic on all managed ports.** LL2S brings affected ports
administratively down, applies VLAN and protocol policy in an OVSDB transaction,
checks port attachment, then enables configured ports. A failed apply attempts to
restore the previous configuration. If rollback fails, LL2S attempts to leave data
ports down and marks state uncertain; saving is blocked until a successful commit.
The complete operation is not an atomic transaction across OVS and Linux.

One CLI session at a time holds the local configuration lock. External `ip`,
`ovs-vsctl` and OpenFlow changes are not reconciled into saved configuration.
Use a dedicated switch namespace; do not share management with another controller.
`show running-config` is the last successful LL2S commit; operational show commands
read the current OVS/kernel state.

```sh
# No privileges/network changes: check file syntax and model references
python3 -m ll2s.cli --check -f examples/switch.conf

# Inside a switch: apply a file, then save it
ll2sh -f /path/to/switch.conf
ll2sh -c 'write memory'
```

`--check` validates the configuration model, not runtime/backend capabilities.
Backend checks (including unsupported BPDU guard) run before a commit.

Mount `/etc/ll2s` persistently. The supervisor replays `startup.conf` when present,
waiting up to `LL2S_RESTORE_WAIT` seconds (default 30) for its interfaces. Increase
that environment variable when the orchestrator attaches interfaces slowly.
The managed OVS bridge is rebuilt on service start; **`write memory`
is the persistence interface**, not direct edits to OVSDB. Runtime database
ownership metadata is retained so an old kernel datapath can be cleaned up safely.

`LL2S_RUNTIME_DIR` defaults to `/run/ll2s`; `LL2S_STARTUP` defaults to
`/etc/ll2s/startup.conf`. They are useful when running separate instances in
separate network namespaces. Each namespace needs its own runtime directory.
The file `<runtime>/ready` appears only after daemon startup and configuration
restoration succeed. Missing startup interfaces cause an explicit startup failure.

The supervisor checks both OVS daemons. On daemon loss it attempts to shut down
the physical data ports using a journal of interface identities, independently
of OVSDB. Taking only the OVS internal `br0` interface down would not suffice.
This provides best-effort software shutdown, not instantaneous hardware protection.

## Tests and migration

```sh
python3 -m unittest discover -s tests -v
./scripts/test-integration
```

The tests use disposable containers. The capability test uses NET_ADMIN/NET_RAW;
the triangle test needs privileged mode to create nested network namespaces,
but has no host network or writable host mount. It checks actual RSTP BPDUs,
root election, alternate-port discarding, tagged VLAN forwarding/isolation,
MAC learning, measured link-failure recovery, startup replay and daemon loss.
See [validation results](docs/validation.md).

For an earlier LL2S/mstpd container, save its text configuration and recreate the
container with this image. Do not reuse an existing Linux `br0` in place. Remove
any `spanning-tree bpduguard enable` lines before applying the configuration.
Weblab integration uses a dedicated `ll2s:dev` switch profile when available
in your Weblab version;
this image should not be retagged as an FRR router. Rebuild locally, save each
switch with `wr`, then Stop/Start it in Weblab to use the new image. Existing
saved configurations need `lldp run` to enable discovery.

See [architecture](docs/architecture.md) and [third-party notices](THIRD_PARTY.md).
Possible future extensions are in the [roadmap](docs/roadmap.md).
LL2S source is MIT-licensed; dependencies retain their own licenses.
