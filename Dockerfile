FROM python:3.11-slim

# install only what's needed
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
    sqlite3 procps net-tools iptables gosu && \
    rm -rf /var/lib/apt/lists/*

# create non-root user
RUN useradd -r -s /bin/bash mirai && \
    mkdir -p /data /home/mirai

# copy CNC + entrypoint
COPY cnc_railway.py /home/mirai/cnc.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /home/mirai/cnc.py /entrypoint.sh

# Railway $PORT
ENV PORT=8080
ENV DB_PATH=/data/mirai.db
ENV ADMIN_USER=root
ENV ADMIN_PASS=mirai

WORKDIR /home/mirai

# health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/')" || exit 1

EXPOSE ${PORT}

ENTRYPOINT ["/entrypoint.sh"]
