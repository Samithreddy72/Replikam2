"""Meeting-safe decisions and honest readiness, shared by API and command delivery."""
import datetime as dt
from .models import utcnow

DISRUPTIVE = frozenset({'reboot','restart','start','stop','profile','reset-clock',
    'golden-restore','jitter-fix','jitter-reset','update','deploy-script','revert-script','unquarantine'})

def utc(value):
    if isinstance(value, str):
        try: value = dt.datetime.fromisoformat(value.replace('Z','+00:00'))
        except ValueError: return None
    if not isinstance(value, dt.datetime): return None
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value

def maintenance(dev, now=None):
    info = (getattr(dev,"operations",None) or {}).get('maintenance') or {}
    until = utc(info.get('until'))
    return info if until and until > (now or utcnow()) else None

def activity(dev, now=None):
    now = now or utcnow()
    seen = utc(dev.last_seen)
    t = dev.latest if isinstance(dev.latest, dict) else {}
    if not seen or (now-seen).total_seconds() > 45:
        return {'busy': True, 'unknown': True, 'reason': 'Fresh bridge status is unavailable.'}
    streams = t.get('streams') or {}
    pin = t.get('pin') or {}
    if not isinstance(streams,dict) or not isinstance(pin,dict):
        return {'busy': True, 'unknown': True, 'reason': 'Bridge status cannot be verified.'}
    session = pin.get('session') or {}
    live = bool(streams.get('video') or streams.get('voice') or
                (isinstance(session,dict) and session.get('active')))
    laptop = t.get('udc') in ('configured','suspended')
    # Configured can remain stale after unplugging; retain the conservative busy guard.
    unknown = laptop or t.get('udc') not in ('not attached','attached','powered','default','addressed','configured','suspended') or not all(isinstance(streams.get(k),bool) for k in ('video','voice'))
    return {'busy': live or laptop or unknown, 'unknown': unknown, 'live':live,
            'laptop':laptop, 'attachment_verified':False, 'reason': 'A presenter is live.' if live else
            'USB attachment is unverified; treat the bridge as busy.' if laptop else
            'USB/session status is unavailable.' if unknown else 'Bridge is idle.'}

def power_detail(power):
    """Electrical evidence never proves audibility or prescribes audio tuning."""
    import math
    if not isinstance(power,dict): return 'Power measurement unavailable.'
    parts=[]
    if power.get('live') is True: parts.append('Undervoltage or throttling is reported now.')
    rate=power.get('rate') if isinstance(power.get('rate'),dict) else {}
    pct=rate.get('pct');samples=rate.get('samples')
    if isinstance(pct,(int,float)) and not isinstance(pct,bool) and math.isfinite(pct) and 0<=pct<=100:
        text='Undervoltage in %.1f%% of recent samples' % pct
        if isinstance(samples,int) and not isinstance(samples,bool) and samples>0: text+=' (%d samples)' % samples
        parts.append(text+'.')
    if power.get('live') is True or power.get('ok') is False:
        parts.append('Inspect the power path during maintenance; firmware flags do not verify audio quality.')
    elif power.get('ok') is True: parts.append('No current power warning is reported.')
    return ' '.join(parts) or 'Power measurement unavailable.'


def health(dev, alerts=(), now=None):
    now = now or utcnow()
    t = dev.latest if isinstance(dev.latest,dict) else {}
    seen = utc(dev.last_seen)
    age = max(0,(now-seen).total_seconds()) if seen else None
    fresh = age is not None and age <= 45
    rows=[]
    def row(key,label,status,detail):
        rows.append(dict(key=key,label=label,status=status,detail=detail))
    row('freshness','Recent bridge status','pass' if fresh else 'unknown',
        'Updated %.0f seconds ago.' % age if age is not None else 'No telemetry received.')
    row('claimed','Fleet ownership','pass' if dev.claimed_at else 'fail',
        'Claimed by this organisation.' if dev.claimed_at else 'Claim this bridge before use.')
    row('mesh','Private connection','pass' if fresh and dev.tailscale_ip else 'unknown',
        'Bridge reports a mesh address; this is not an end-to-end connection test.' if dev.tailscale_ip else 'No mesh address reported.')
    power=t.get('power') if isinstance(t.get('power'),dict) else {}
    row('power','Power','unknown' if not fresh or not isinstance(power.get('ok'),bool) else 'pass' if power.get('ok') is True else 'warn',
        power_detail(power))
    row('usb','USB connection','unknown' if not fresh or 'udc' not in t or t.get('udc') in ('configured','suspended') else 'warn',
        'USB reports configured or suspended; this reading can persist after unplugging. Attachment and displayed picture are unverified.' if t.get('udc') in ('configured','suspended') else 'Check the meeting laptop USB connection.')
    misses=t.get('usb_misses_per_s')
    known=isinstance(misses,(int,float)) and not isinstance(misses,bool)
    row('usb_misses','USB delivery','unknown' if not fresh or not known else 'warn' if misses>0 else 'pass',
        '%s transfer misses/s; not a rendered-frame measurement.' % misses if known else 'USB transfer measurement unavailable.')
    pin=t.get('pin') if isinstance(t.get('pin'),dict) else {}
    row('pin','Presenter PIN','unknown' if not fresh or 'pin_set' not in pin else 'pass' if pin.get('pin_set') else 'fail',
        'PIN configured; each presenter still needs to unlock their session.' if pin.get('pin_set') else 'Ask the administrator to configure a PIN.')
    if pin.get('lockout'):
        row('lockout','PIN lockout','fail','Wait for lockout expiry or ask the administrator to review access attempts.')
    services=t.get('services')
    if isinstance(services,list):
        services={item[0]:item[1] for item in services if isinstance(item,(list,tuple)) and len(item)==2 and isinstance(item[0],str) and isinstance(item[1],str)}
    bad=[name for name,state in services.items() if state in ('failed','inactive','dead')] if isinstance(services,dict) else []
    row('services','Bridge services','unknown' if not fresh or not isinstance(services,dict) or not services else 'warn' if bad else 'pass',
        'Inspect services: '+', '.join(bad) if bad else 'Reported service states available.' if services else 'Service states unavailable.')
    disk=t.get('data_free_mb')
    row('disk','Free storage','unknown' if not fresh or not isinstance(disk,(int,float)) else 'warn' if disk<500 else 'pass',
        str(disk)+' MB free.' if isinstance(disk,(int,float)) else 'Storage measurement unavailable.')
    import re
    temp=re.search(r'[-+]?[0-9]+(?:\.[0-9]+)?',str(t.get('temp','')))
    row('temperature','Temperature','unknown' if not fresh or not temp else 'warn' if float(temp[0])>=75 else 'pass',
        temp[0]+' °C.' if temp else 'Temperature measurement unavailable.')
    mesh=(getattr(dev,'operations',None) or {}).get('mesh_expiry') or {}
    checked=utc(mesh.get('checked_at'))
    known=checked and (now-checked).total_seconds()<=7200 and mesh.get('known')
    expiry=utc(mesh.get('expires'))
    row('mesh_expiry','Mesh credential expiry','unknown' if not known or (not expiry and not mesh.get('disabled')) else 'warn' if expiry and not mesh.get('disabled') and (expiry-now).days<=30 else 'pass',
        'Expiry disabled for this node.' if known and mesh.get('disabled') else 'Expires '+str(mesh.get('expires')) if known and expiry else 'Expiry monitoring is not configured or its evidence is stale.')
    row('receiver','Displayed picture','unknown','The meeting laptop must confirm the picture; bridge counters cannot verify it.')
    row('maintenance','Maintenance','warn' if maintenance(dev,now) else 'pass',
        'A maintenance window is active.' if maintenance(dev,now) else 'No active maintenance window.')
    for a in alerts:
        row('alert:'+a['kind'],a['kind'].replace('_',' ').capitalize(),'warn',a.get('detail') or 'Inspect the bridge alert.')
    return {'device_id':dev.id,'checked_at':now.isoformat(),'age_s':age,
        'status':'needs_attention' if any(r['status'] in ('warn','fail') for r in rows) else
                 'unverified' if any(r['status']=='unknown' for r in rows) else 'ready',
        'checks':rows,'activity':activity(dev,now),'receiver_picture_verified':False}


def observe_session(db, dev, now=None):
    """Record observed media intervals. A telemetry gap is explicitly an unknown interval."""
    from sqlalchemy import select
    from .models import ObservedSession
    now=now or utcnow()
    t=dev.latest if isinstance(dev.latest,dict) else {}
    streams=t.get('streams')
    if not isinstance(streams,dict) or not any(k in streams for k in ('video','voice')):
        return
    opened=db.scalar(select(ObservedSession).where(ObservedSession.device_id==dev.id,
        ObservedSession.ended_at.is_(None)).order_by(ObservedSession.id.desc()).limit(1))
    if opened and (now-utc(opened.last_seen)).total_seconds()>60:
        opened.ended_at=opened.last_seen;opened.end_reason='Telemetry gap; actual session end unknown'
        opened=None
    live=bool(streams.get('video') or streams.get('voice'))
    if live:
        if not opened:
            opened=ObservedSession(device_id=dev.id,started_at=now,last_seen=now,samples=0,warning_samples=0)
            db.add(opened)
        opened.last_seen=now;opened.samples+=1
        power=t.get('power') or {}
        misses=t.get('usb_misses_per_s')
        if (isinstance(power,dict) and power.get('live')) or (isinstance(misses,(int,float)) and misses>0):
            opened.warning_samples+=1
    elif opened:
        opened.ended_at=now;opened.end_reason='Bridge reported no forward media'


def notification_muted(event, now=None):
    until=utc((event.handling or {}).get('snoozed_until'))
    return bool(until and until>(now or utcnow()))
