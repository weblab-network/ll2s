# Container dependencies and sources

LL2S is MIT-licensed; the operating system and installed packages retain their
own licenses. `/usr/share/ll2s/packages.json` records exact installed package
versions, license identifiers, source origins and Alpine aports commits.

For published version tags, download `ll2s-dependency-sources.tar.gz` from the
[matching GitHub release](https://github.com/weblab-network/ll2s/releases).
It contains those exact Alpine packaging recipes and patches, plus upstream
source archives verified against the recipes' checksums, including their notices.
LL2S source and build instructions are in that release's Git tag. The release
also provides a SHA-256 checksum for the dependency archive. Retain this source
bundle alongside any redistributed image. The bundle covers runtime packages,
including dependencies inherited from the base image.

Published images target Linux amd64. The host supplies the Open vSwitch kernel
module; it is not part of the container image. Locally rebuilt images can resolve
different Alpine revisions: regenerate their package inventory and source bundle
rather than assuming a published bundle matches them.
