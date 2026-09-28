"""Bound request bytes before FastAPI buffers/parses JSON or multipart bodies."""
import asyncio

class RequestLimits:
    def __init__(self, app, default_limit=1024*1024, upload_limit=8*1024*1024, timeout=15):
        self.app, self.default_limit, self.upload_limit, self.timeout = app, default_limit, upload_limit, timeout

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        limit = self.upload_limit if scope.get('path') in ('/v1/diagnostics','/admin/payloads') else self.default_limit
        async def reject(status, message):
            body = ('{"detail":"'+message+'"}').encode()
            await send({'type':'http.response.start','status':status,'headers':[
                (b'content-type',b'application/json'),(b'content-length',str(len(body)).encode())]})
            await send({'type':'http.response.body','body':body})
        lengths = [v for k,v in scope.get('headers',[]) if k.lower()==b'content-length']
        if lengths:
            if len(lengths)!=1 or not lengths[0].isdigit():
                return await reject(400,'Invalid request length')
            length = lengths[0].lstrip(b'0') or b'0'
            if len(length) > len(str(limit)) or int(length) > limit:
                return await reject(413,'Request is too large')
        async def collect():
            chunks, size = [], 0
            while True:
                event = await receive()
                if event['type']=='http.disconnect': return None
                chunk = event.get('body',b'')
                size += len(chunk)
                if size > limit: raise OverflowError
                chunks.append(chunk)
                if not event.get('more_body',False): return b''.join(chunks)
        try:
            body = await asyncio.wait_for(collect(), timeout=self.timeout)
        except OverflowError:
            return await reject(413,'Request is too large')
        except asyncio.TimeoutError:
            return await reject(408,'Request body timed out')
        if body is None: return
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type':'http.request','body':body,'more_body':False}
            return await receive()
        await self.app(scope,replay,send)
