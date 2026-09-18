"""Run a frozen engine in an empty profile; never touch cameras or the fleet."""
import json,os,pathlib,secrets,socket,subprocess,sys,tempfile,time,urllib.request,urllib.error
root=pathlib.Path(sys.argv[1]).resolve()
binary=root/('NetBridgeEngine.exe' if sys.platform=='win32' else 'NetBridgeEngine')
with tempfile.TemporaryDirectory() as profile:
 with socket.socket() as sock:
  sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
 token=secrets.token_hex(32)
 env={**os.environ,'NB_DESKTOP_PORT':str(port),'NB_DESKTOP_TOKEN':token,'NB_DESKTOP_STATE_DIR':profile,'NB_MEDIA_DIR':str(root/'runtime')}
 p=subprocess.Popen([str(binary)],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
 def request(path,body=None,authorized=True):
  req=urllib.request.Request('http://127.0.0.1:%d%s'%(port,path),headers={'Content-Type':'application/json',**({'X-NetBridge-Token':token} if authorized else {})},data=json.dumps(body).encode() if body is not None else None)
  with urllib.request.urlopen(req,timeout=2) as r:return json.load(r)
 try:
  for attempt in range(450):
   if p.poll() is not None:raise RuntimeError(p.stdout.read().decode())
   try:state=request('/api/state');break
   except (OSError,urllib.error.URLError) as error:
    last_error=error;time.sleep(.1)
  else:raise RuntimeError('Engine did not start: %s'%last_error)
  assert state['signed_in'] is False and state['live'] is False
  try:request('/api/state',authorized=False);raise AssertionError('Unauthenticated read accepted')
  except urllib.error.HTTPError as e:assert e.code==403
  assert request('/api/desktop/status')['desktop'] is True
  assert request('/api/desktop/quit',{})['ok'] is True
  assert p.wait(timeout=15)==0
  print('PASS: bundled engine starts, denies unauthenticated access, and shuts down cleanly in an isolated profile.')
 finally:
  if p.poll() is None:p.terminate();p.wait(timeout=15)
