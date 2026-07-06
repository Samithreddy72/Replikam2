#!/bin/bash
# Fleet alert notifier: polls the local control plane and pushes NEW alerts
# to ntfy.sh (free push notifications on the admin's phone).
# Config: /etc/default/fleet-notifier  (ADMIN_KEY=..., NTFY_TOPIC=...)
[ -f /etc/default/fleet-notifier ] && . /etc/default/fleet-notifier
STATE=/tmp/fleet-notifier.state; touch $STATE
while true; do
  ALERTS=$(curl -s -m 8 -H "Authorization: Bearer $ADMIN_KEY" http://127.0.0.1:8000/admin/alerts 2>/dev/null)
  echo "$ALERTS" | python3 -c "
import json,sys,hashlib
try: alerts=json.load(sys.stdin)
except Exception: alerts=[]
seen=set(open('$STATE').read().split())
now=set()
for a in alerts:
    k=hashlib.md5((str(a.get('device_id'))+a.get('kind','')).encode()).hexdigest()[:12]
    now.add(k)
    if k not in seen:
        print(f\"{a.get('name') or a.get('device_id')}: {a.get('kind')} — {a.get('detail','')}\")
open('$STATE','w').write(' '.join(now))
" | while read -r MSG; do
      [ -n "$MSG" ] && curl -s -m 8 -H "Title: RepliKam Fleet Alert" -H "Priority: high" -H "Tags: rotating_light"         -d "$MSG" "https://ntfy.sh/$NTFY_TOPIC" >/dev/null
    done
  sleep 60
done
