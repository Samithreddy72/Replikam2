"""Execute the real UI poll on lost diagnostics; stale green must not survive."""
import pathlib,re,subprocess,shutil,unittest,json
ROOT=pathlib.Path(__file__).resolve().parents[1]
class Freshness(unittest.TestCase):
 def test_errors_clear_readiness_without_stopping_media(self):
  node=shutil.which('node');self.assertIsNotNone(node,'Node is required to exercise shipped UI behavior')
  src=(ROOT/'app/netbridge-source/source_app.py').read_text()
  helper=re.search(r'function checksUnavailable\(message\)\{.*?\n\}',src,re.S)
  poll=re.search(r'async function poll\(\)\{.*?(?=\nboot\(\);)',src,re.S)
  tone=re.search(r'function setCheckTone\(ready\)\{.*?\n\}',src,re.S)
  self.assertIsNotNone(helper);self.assertIsNotNone(poll);self.assertIsNotNone(tone)
  harness=r'''
const assert=require('assert');
const nodes={};const $=id=>nodes[id]||(nodes[id]={className:'ok',textContent:'Ready to present',style:{},hidden:false});
let greenSince=1,liveHost='bridge',pinSnooze=Date.now(),pinBlockedUntil=0,pinBlockedMessage='';
const host=()=>liveHost;let mode='error';
const j=async()=>{if(mode==='throw')throw Error('offline');if(mode==='locked')return {pin:{locked:true,protocol:2}};return {_error:'timeout'};};
'''+helper.group(0)+'\n'+tone.group(0)+'\n'+poll.group(0)+r'''
(async()=>{
for(mode of ['error','throw','locked']){
 greenSince=1;for(const id of ['c1','c2','c3','c4','c5'])$(id).className='ok';
 await poll();assert.strictEqual(greenSince,null);
 for(const id of ['c1','c2','c3','c4','c5'])assert.notStrictEqual($(id).className,'ok');
 assert.notStrictEqual($('m2').textContent,'Ready to present');
}
console.log('freshness checked');
})().catch(e=>{console.error(e);process.exit(1)});
'''
  r=subprocess.run([node,'-e',harness],capture_output=True,text=True)
  self.assertEqual(r.returncode,0,r.stderr);self.assertIn('freshness checked',r.stdout)
if __name__=='__main__':unittest.main()
