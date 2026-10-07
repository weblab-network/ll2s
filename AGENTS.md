# Contributing to LL2S

Read [README.md](README.md), [architecture](docs/architecture.md),
[validation](docs/validation.md), and [third-party notices](THIRD_PARTY.md)
before changing related behavior.

- Keep the Python CLI independently implemented, using the standard library.
  Preserve dependency notices. Invoke system tools with argument arrays, never
  evaluate interface names or configuration using a shell.
- Preserve OVS bridge/port ownership checks and interface identity journals.
  Preflight rejection must not disturb forwarding. Failed rollback blocks saving.
- Keep candidate, committed intent and saved startup state separate. `end` and
  Ctrl+Z commit; `write memory` saves. Preserve abbreviation, help and Tab behavior.
- LLDP and SNMP failures must remain independent of critical OVS supervision.
  Sanitize neighbor strings. Never log SNMP communities in service status.
- Use disposable containers/namespaces for tests. Do not operate an active lab
  solely for testing. Run native suites sequentially and verify cleanup.
- Run `python3 -m unittest discover -s tests -v` and relevant native checks in the
  validation guide. Distinguish mocked behavior from actual switching tests.
- Keep user docs consistent with implemented behavior and limitations. Do not
  claim MSTP, BPDU guard, SNMPv3 or other unimplemented features from syntax alone.
- Do not commit credentials, live configuration captures, runtime data or images.
  Building a local image does not authorize deployment or publication.
