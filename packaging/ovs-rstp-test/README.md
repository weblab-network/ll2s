# Local LL2S image with the OVS RSTP root-vector correction

This optional test build replaces only `ovs-vswitchd` in the pinned LL2S 0.2.0
image. It compiles OVS 3.7.1 from a checksum-verified upstream archive with the
supplied root-vector patch. LL2S Python, OVSDB and the other runtime tools remain
from the released image. The normal Dockerfile and published tags are unchanged.

From the LL2S source root:

```sh
docker build -t ll2s:ovs-rstp1 -f packaging/ovs-rstp-test/Dockerfile packaging/ovs-rstp-test
```

The small explicit build context contains only this recipe, source patch and
metadata recorder. Build tools and the downloaded source remain in the build
stage; the runtime also retains the verified OVS source archive and patch with
their notices so the override can be inspected and rebuilt. The upstream source
and patch are pinned; builder APK revisions can change, so this is not a claim
of a bit-for-bit reproducible build. Exact builder APK metadata is retained.

The runtime records the override at
`/usr/share/ll2s/ovs-rstp-test/override.json`. The inherited `packages.json` still
describes the base APKs; the override record explicitly identifies the replaced
file and its source/binary hashes. SSL/libcapng/DPDK are disabled in this local
daemon build; LL2S uses its existing local Unix sockets and host OVS datapath.

Verify the marker:

```sh
docker image inspect ll2s:ovs-rstp1 --format '{{index .Config.Labels "network.weblab.ll2s.ovs.rstp-root-vector"}}'
docker run --rm --network none --entrypoint cat ll2s:ovs-rstp1 /usr/share/ll2s/ovs-rstp-test/override.json
```

## Selecting the image in Weblab

Weblab supports `ll2s:dev` as its local development selector. Preserve the old
development tag first if needed, and ensure no active test depends on its tag
before changing it. Then:

```sh
docker tag ll2s:ovs-rstp1 ll2s:dev
```

For a **JSON + saved configs** import, make a separate copy of the JSON and change
the intended LL2S switch's `image` to `ll2s:dev`. Preserve its `startup_config`,
interfaces, links and other settings. Importing the copy then makes `ll2s:dev`
available in the Inspector. It does not require rebuilding Weblab.

Saved storage is separate for each node ID and image selector; existing storage
still wins over snippets. For a fresh comparison, use fresh persistent node IDs
and update their link references in the copied JSON, preserving configuration
text and all cable ports. Do not delete old storage to force snippets. Keep the
original JSON unchanged for returning to the published image.

A saved-state ZIP has additional exact image-identity requirements: do not
retag `ghcr.io/weblab-network/ll2s:0.2.0` to bypass them. Before sharing a binary
image or publishing a release, complete its package/source review and test the
actual image with the intended topology. This build adds no MSTP support.
