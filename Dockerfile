FROM alpine:3.24
RUN apk add --no-cache python3 iproute2 openvswitch lldpd net-snmp net-snmp-tools \
    && mkdir -p /etc/ll2s /run/ll2s /usr/share/ll2s
COPY ll2s /usr/share/ll2s/ll2s
COPY LICENSE THIRD_PARTY.md /usr/share/ll2s/
COPY packaging/SOURCES.md /usr/share/ll2s/SOURCES.md
COPY packaging/collect_sources.py /tmp/collect_sources.py
RUN python3 -c 'import sys,json,pathlib; sys.path.insert(0,"/tmp"); from collect_sources import inventory; pathlib.Path("/usr/share/ll2s/packages.json").write_text(json.dumps(inventory(pathlib.Path("/lib/apk/db/installed").read_text()),indent=2)+"\n")' \
    && rm /tmp/collect_sources.py
LABEL org.opencontainers.image.source="https://github.com/weblab-network/ll2s" \
      org.opencontainers.image.title="LL2S — Linux Layer 2 Switch"
COPY scripts/ll2sh /usr/local/bin/ll2sh
COPY scripts/entrypoint /usr/local/bin/ll2s-entrypoint
RUN chmod +x /usr/local/bin/ll2sh /usr/local/bin/ll2s-entrypoint
ENV PYTHONPATH=/usr/share/ll2s
ENTRYPOINT ["ll2s-entrypoint"]
CMD ["sleep", "infinity"]
