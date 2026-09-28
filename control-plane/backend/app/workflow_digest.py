"""Opt-in daily digest. Addresses come from the organisation's admin accounts."""
import datetime as dt
from zoneinfo import ZoneInfo
from sqlalchemy import select
from .models import Device, User, WorkflowSettings, utcnow
from . import notifier, workflow
from .alerts import device_alerts, bridge_title


def summary(db,org):
    lines=['NetBridge daily fleet summary']
    for dev in db.scalars(select(Device).where(Device.org_id==org,Device.claimed_at.is_not(None))).all():
        try: alerts=device_alerts(dev,db)
        except Exception: alerts=[{'kind':'status unavailable'}]
        state=', '.join(a['kind'].replace('_',' ') for a in alerts) or 'No active alert detected'
        if workflow.maintenance(dev):state+='; maintenance active'
        lines.append(bridge_title(dev)+': '+state)
    return '\n'.join(lines)


def tick(db,now=None):
    now=now or utcnow();local=now.astimezone(ZoneInfo('Asia/Kolkata'))
    for row in db.scalars(select(WorkflowSettings)).all():
        data=dict(row.data or {})
        if not data.get('daily_digest') or local.hour<9 or data.get('digest_day')==local.date().isoformat():continue
        last=workflow.utc(data.get('digest_attempt'))
        if last and (now-last).total_seconds()<600:continue
        data['digest_attempt']=now.isoformat();row.data=data;db.commit()
        recipients=db.scalars(select(User.email).where(User.org_id==row.org_id,User.role=='admin')).all()
        if not recipients or not notifier.email_configured():continue
        # Mark delivered recipients individually so a partial failure does not resend to everyone.
        sent=dict(data.get('digest_sent') or {})
        body=summary(db,row.org_id)
        db.commit()  # no database transaction held during SMTP
        for email in recipients:
            if sent.get(email)==local.date().isoformat():continue
            if notifier.send_mail(email,'NetBridge daily summary',body):
                sent[email]=local.date().isoformat();data['digest_sent']=sent;row.data=dict(data);db.commit()
        if all(sent.get(e)==local.date().isoformat() for e in recipients):
            data['digest_day']=local.date().isoformat();row.data=dict(data);db.commit()
