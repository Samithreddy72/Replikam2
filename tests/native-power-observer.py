"""Register/unregister actual OS notifications; never suspend the test machine."""
import pathlib,sys,time
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'app/netbridge-source'))
from power_observer import PowerObserver
observer=PowerObserver(lambda:None,lambda:None)
assert observer.active,observer.error
observer.close()
if hasattr(observer,'thread'):
 observer.thread.join(timeout=2)
 assert not observer.thread.is_alive(), 'observer did not unregister'
print('Native sleep notification registration and cleanup passed')
