import {test,expect,Page} from '@playwright/test';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../../netbridge-source/source_app.py',import.meta.url),'utf8');
const html=source.split('UI = r"""')[1].split('"""')[0];
async function setup(page:Page,live=true){
 await page.clock.install();
 await page.addInitScript(({live})=>{
  const w=window as any; w.requests=[];w.checkError=false;w.pinLocked=false;
  w.state={signed_in:true,live,wanted:live,live_host:'100.1.2.2',live_bridge_id:'b',last_bridge:'a',pin:{host:'100.1.2.2'},return_on:true};
 },{live});
 await page.route('http://source.test/**',async route=>{
  const url=new URL(route.request().url());
  if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:html});
  const state=await page.evaluate(()=>{const w=window as any;return {state:w.state,error:w.checkError,locked:w.pinLocked};});
  await page.evaluate(path=>(window as any).requests.push(path),url.pathname+url.search);
  let body:any={};
  if(url.pathname==='/api/state')body=state.state;
  if(url.pathname==='/api/bridges')body=[{id:'a',name:'Room A',online:true,tailscale_ip:'100.1.2.1'},{id:'b',name:'Room B',online:true,tailscale_ip:'100.1.2.2'}];
  if(url.pathname==='/api/devices')body={video:[{name:'Camera'}],audio:[{name:'Mic'}]};
  if(url.pathname==='/api/checks')body=state.error?{_error:'Mesh helper unavailable'}:{...Object.fromEntries(['online','video_arriving','voice_arriving','client_sees_camera','return_audio'].map(k=>[k,{ok:true,detail:'Measured'}])),pin:{protocol:2,locked:state.locked},_mesh_path:{via:'direct'},_legs:{ok:true}};
  if(url.pathname==='/api/golive')body={_error:'Locked for 60 minutes',reason:'locked_out',retry_in:3600};
  return route.fulfill({json:body});
 });
 await page.goto('http://source.test/');
 await expect(page.locator('#bridge')).toHaveValue('b');
}
test('Reload retains the active bridge and failures clear green readiness',async({page})=>{
 await setup(page);
 await expect(page.locator('#bridge')).toBeDisabled();
 await page.evaluate(()=> (window as any).poll());
 await page.clock.fastForward(40000);
 await page.evaluate(()=> (window as any).poll());
 await expect(page.locator('#ckfix')).toHaveText('Ready to present');
 await expect(page.locator('#ckfix')).toHaveCSS('color','rgb(23, 122, 76)');
 const checks=await page.evaluate(()=>(window as any).requests.filter((x:string)=>x.startsWith('/api/checks')));
 expect(checks.length).toBeGreaterThan(0);expect(checks.every((x:string)=>x.includes('100.1.2.2'))).toBeTruthy();
 await page.locator('#m2').evaluate(el=>el.textContent='NO ROOM AUDIO');
 await page.evaluate(()=> (window as any).poll());
 await expect(page.locator('#m2')).toHaveText('NO ROOM AUDIO');
 await page.evaluate(()=>{(window as any).checkError=true;});
 await page.evaluate(()=> (window as any).poll());
 await expect(page.locator('#ckfix')).toHaveText('Mesh helper unavailable');
 await expect(page.locator('#l1')).toHaveText('Not currently verified');
});
test('PIN lockout survives subsequent status polls without reopening the dialog',async({page})=>{
 await setup(page);
 await page.evaluate(()=> (window as any).startSession({pin:'1234'}));
 await page.evaluate(()=>{(window as any).pinLocked=true;});
 await page.evaluate(()=> (window as any).poll());
 await expect(page.locator('#pinbox')).toBeHidden();
 await expect(page.locator('#m2')).toHaveText('Locked for 60 minutes');
});
test('Advanced audio diagnostics are hidden and not polled while idle',async({page})=>{
 await setup(page,false);
 await expect(page.locator('#audioDiagnostics')).toBeHidden();
 await page.clock.fastForward(5000);
 expect(await page.evaluate(()=>(window as any).requests.some((x:string)=>x==='/api/audio/diagnostics'))).toBeFalsy();
});
