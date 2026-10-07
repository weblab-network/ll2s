# Validation and development

Use disposable containers and namespaces. Run native suites sequentially; they
create their own networks and must never be pointed at an active lab.

## Unit and terminal tests

```sh
python3 -m unittest discover -s tests -v
python3 -m ll2s.cli --check -f examples/switch.conf
git diff --check
```

These cover configuration parsing/replay, candidate isolation, ownership,
rollback/save protection, abbreviations, completion, controlling-terminal
keystrokes, LLDP display sanitization and monitoring service behavior. System
operations are mocked here; these checks alone do not prove forwarding.

## Native switching tests

A Linux Docker host with its `openvswitch` module is required:

```sh
sudo modprobe openvswitch
./scripts/test-integration
```

The script builds the local `ll2s:dev` image, then runs:

- `tests/capabilities.py` with NET_ADMIN/NET_RAW: actual OVS attachment, VLAN policy,
  STP/RSTP, ownership, saved restore, management and rejection of unsupported policy.
- `tests/integration.py` in a disposable privileged container: three-switch RSTP
  election, actual BPDUs, alternate-port discarding, tagged forwarding/isolation,
  MAC learning, link failure, startup replay and critical OVS daemon loss/recovery.
  `tests/lldp_checks.py` adds direct neighbors, TX/RX controls, expiry and recovery.
- `tests/management.py` in a disposable privileged container: management VLAN
  isolation, routed polling, read-only SNMPv2c access controls, interface counters,
  LLDP AgentX, service restarts and saved-state restoration.

The privileged containers need nested namespaces; they have no host network,
Docker socket or writable host mount. The capability suite uses the normal
runtime capability set. Initial/service-restart convergence allows 45 seconds;
this is a test budget, not a recovery guarantee.

Test the installed Python package independently of the source checkout:

```sh
docker run --rm --network none \
  -v "$PWD/tests:/tests:ro" -w /tmp --entrypoint python3 \
  ll2s:dev -m unittest discover -s /tests -v
```

## Optional Weblab integration

`tests/weblab_lldp.py` requires a Weblab checkout containing its LL2S profile,
root, Docker, host TAP access, Weblab's native dependencies and a built `ll2s:dev`.
Set `WEBLAB_SOURCE` to that checkout and explicitly set `LL2S_WEBLAB_TEST=1`:

```sh
sudo env WEBLAB_SOURCE=/path/to/weblab LL2S_WEBLAB_TEST=1 \
  python3 tests/weblab_lldp.py
```

It creates a disposable Lab directory and reserves device IDs 920–922. Keep those
IDs free and run other native node suites separately. It checks TAP/macvlan
forwarding, LLDP, shared consoles, SNMP and saved Stop/Start behavior. This suite
is optional for standalone LL2S and does not replace Weblab's own regression tests.

## Verified scope and limits

The October 2026 baseline was tested on x86-64 Linux 6.1 with Alpine 3.24,
OVS 3.7.1, lldpd 1.0.22 and Net-SNMP 5.9.5.2. The 42 unit/terminal tests and the
native suites above passed during development, including Weblab integration.
Package revisions are not pinned; rerun the relevant suites after changes.

These checks establish lab behavior. Physical-switch interoperability, prolonged
stress, production resilience and throughput are not established. Commits disrupt
managed ports and rollback remains best effort. BPDU guard, MSTP, PVST+, LACP,
SNMPv3 and hardware offload are not implemented.
