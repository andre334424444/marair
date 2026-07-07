FROM python:3.11-slim

RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
    sqlite3 && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p /data /home/mirai

COPY cnc_railway.py /home/mirai/cnc.py
RUN chmod +x /home/mirai/cnc.py

ENV PORT=8080
ENV DB_PATH=/data/mirai.db
ENV ADMIN_USER=root
ENV ADMIN_PASS=mirai

WORKDIR /home/mirai

EXPOSE 8080

CMD ["python3", "/home/mirai/cnc.py"]
