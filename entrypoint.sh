#!/bin/bash
# fix volume permissions at runtime — volume mounts after build, owned by root
chown -R mirai:mirai /data 2>/dev/null || true
# drop to mirai user, run CNC
exec gosu mirai python3 /home/mirai/cnc.py
