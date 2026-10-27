# Rebuild the patched OVS daemon and install the current LL2S source.
ARG LL2S_BASE=ghcr.io/weblab-network/ll2s:0.2.0@sha256:90092fe3fa9486a85726f19bed6d0187f16be3a4581f88e450c9ec3fe2e68ccb
FROM alpine:3.24 AS ovs-build
RUN apk add --no-cache build-base python3 linux-headers autoconf automake libtool
WORKDIR /source
RUN wget -O openvswitch-3.7.1.tar.gz https://www.openvswitch.org/releases/openvswitch-3.7.1.tar.gz \
    && echo 'b8936c2e95a024d37123536ca843648bc2f1d2520921f991dd3d06248859b70f  openvswitch-3.7.1.tar.gz' | sha256sum -c - \
    && tar -xzf openvswitch-3.7.1.tar.gz \
    && mv openvswitch-3.7.1 ovs
COPY packaging/ovs-rstp-test/root-vector-order.patch /source/root-vector-order.patch
WORKDIR /source/ovs
RUN patch -p1 < /source/root-vector-order.patch \
    && ./configure --disable-ssl --disable-libcapng --without-dpdk \
    && make -j2
RUN mkdir /source/provenance \
    && cp /lib/apk/db/installed /source/provenance/build-apk-installed.txt \
    && cp LICENSE /source/provenance/OVS-LICENSE \
    && cp NOTICE /source/provenance/OVS-NOTICE \
    && gcc --version > /source/provenance/compiler.txt

FROM ${LL2S_BASE}
ARG LL2S_BASE
COPY ll2s /usr/share/ll2s/ll2s
COPY LICENSE THIRD_PARTY.md /usr/share/ll2s/
COPY packaging/SOURCES.md /usr/share/ll2s/SOURCES.md
COPY scripts/ll2sh /usr/local/bin/ll2sh
COPY scripts/entrypoint /usr/local/bin/ll2s-entrypoint
RUN chmod +x /usr/local/bin/ll2sh /usr/local/bin/ll2s-entrypoint
ENV PYTHONPATH=/usr/share/ll2s
ENV LL2S_OVS_VARIANT=rstp-root-vector-1
LABEL network.weblab.ll2s.ovs.rstp-root-vector="1" \
      org.opencontainers.image.description="LL2S with the OVS 3.7.1 RSTP root-vector ordering correction"
COPY --from=ovs-build /source/ovs/vswitchd/ovs-vswitchd /usr/sbin/ovs-vswitchd
COPY --from=ovs-build /source/openvswitch-3.7.1.tar.gz /usr/share/ll2s/ovs-rstp-test/
COPY --from=ovs-build /source/root-vector-order.patch /usr/share/ll2s/ovs-rstp-test/
COPY --from=ovs-build /source/provenance/ /usr/share/ll2s/ovs-rstp-test/
COPY packaging/ovs-rstp-test/Dockerfile packaging/ovs-rstp-test/README.md packaging/ovs-rstp-test/record_override.py /usr/share/ll2s/ovs-rstp-test/
COPY packaging/ovs-rstp-test/record_override.py /tmp/record_override.py
RUN LL2S_BASE="$LL2S_BASE" python3 /tmp/record_override.py \
    && rm /tmp/record_override.py
ENTRYPOINT ["ll2s-entrypoint"]
CMD ["sleep", "infinity"]
