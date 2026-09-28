"""Optional read-only node-expiry watch; never mints or rotates a credential."""
import json, os, time, urllib.request, urllib.parse, datetime as dt
from sqlalchemy import select
from .models import Device,utcnow
from .config import settings
from .workflow import utc
_last_attempt=0.0

def refresh(db):
    global _last_attempt
    token=os.environ.get('TS_DEVICES_READ_TOKEN','')
    if not token or time.monotonic()-_last_attempt<3600:return
    _last_attempt=time.monotonic()
    url='https://api.tailscale.com/api/v2/tailnet/'+urllib.parse.quote(settings.ts_tailnet or '-',safe='')+'/devices?fields=all'
    req=urllib.request.Request(url,headers={'Authorization':'Bearer '+token})
    with urllib.request.urlopen(req,timeout=10) as response:
        raw=response.read(2_000_001)
    if len(raw)>2_000_000:raise ValueError('Device inventory exceeds safe response limit')
    payload=json.loads(raw)
    nodes=payload.get('devices') if isinstance(payload,dict) else None
    if not isinstance(nodes,list):raise ValueError('Invalid device inventory')
    by_ip={ip:node for node in nodes if isinstance(node,dict) for ip in (node.get('addresses') if isinstance(node.get('addresses'),list) else []) if isinstance(ip,str)}
    now=utcnow()
    for dev in db.scalars(select(Device)).all():
        node=by_ip.get(dev.tailscale_ip)
        data=dict(dev.operations or {})
        data['mesh_expiry']={'checked_at':now.isoformat(),'known':bool(node),
            'disabled':bool(node and node.get('keyExpiryDisabled') is True),
            'expires':node.get('expires') if node else None}
        dev.operations=data
    db.commit()

def expiry_warning(dev,now=None):
    now=now or utcnow()
    data=(getattr(dev,'operations',None) or {}).get('mesh_expiry') or {}
    checked=utc(data.get('checked_at'))
    if not checked or (now-checked).total_seconds()>7200 or not data.get('known'):return None
    expires=utc(data.get('expires'))
    if data.get('disabled') or not expires:return None
    days=(expires-now).total_seconds()/86400
    if days<=30:return 'Bridge mesh identity expires in %.1f days. Schedule renewal while idle.' % max(0,days)
    return None
