# Future directions

The current release implements VLAN access/trunk switching, STP/RSTP, LLDP,
a single management IPv4 endpoint and read-only SNMPv2c. See the
[README](../README.md) for supported commands and limitations.

Potential extensions, without a delivery schedule:

- Incremental commits that preserve unaffected forwarding and neighbor caches.
- SNMPv3 and switch-specific monitoring through BRIDGE-MIB/Q-BRIDGE-MIB.
- LLDP timers, statistics and management-address controls.
- BPDU guard and LACP with tested enforcement and recovery.
- MSTP, after validating a suitable protocol engine and per-instance dataplane.
- IPv6 management and additional management options.

OVS currently provides one STP/RSTP tree across VLANs. MSTP requires separate
backend and interoperability work; it cannot be enabled by adding CLI syntax.
Each extension must preserve ownership checks, rollback, saved-state semantics
and failure handling described in the [architecture](architecture.md).
