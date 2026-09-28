import {test,expect} from '@playwright/test';
import {readFileSync} from 'node:fs';
const html=readFileSync(new URL('../../../control-plane/panel-dist/index.html',import.meta.url),'utf8');
test('Fleet health stays read-only and maintenance saves explicit choices',async({page})=>{
 const writes:{path:string,body:any}[]=[];const errors:string[]=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('http://fleet.test/**',async route=>{
  const req=route.request(),path=new URL(req.url()).pathname;
  if(path==='/')return route.fulfill({contentType:'text/html',body:html});
  if(req.method()==='POST')writes.push({path,body:req.postDataJSON()});
  const result=path.endsWith('/health')?{checked_at:new Date().toISOString(),checks:[{label:'Power',status:'warning',detail:'Active undervoltage detected.'},{label:'Receiver',status:'unknown',detail:'Rendered picture not measured.'}]}:{};
  return route.fulfill({json:result});
 });
 await page.goto('http://fleet.test/');
 await page.evaluate(()=> (window as any).workflowHealth({id:'a',name:'Room A'}));
 await expect(page.getByText('Active undervoltage detected.',{exact:true})).toBeVisible();
 await expect(page.getByText('Rendered picture not measured.',{exact:true})).toBeVisible();
 expect(writes).toEqual([]);
 await page.getByRole('button',{name:'Close',exact:true}).last().click();
 await page.evaluate(()=> (window as any).workflowSettings({id:'a',name:'Room A'}));
 await page.getByLabel('Site or room').fill('Conference room');
 await page.getByLabel('Installation notes').fill('Left USB port');
 await page.getByLabel('Maintenance duration').selectOption('30');
 await page.getByLabel('Maintenance reason').fill('Planned inspection');
 await page.getByRole('button',{name:'Save',exact:true}).click();
 await expect(page.getByText('Saved',{exact:true})).toBeVisible();
 expect(writes).toEqual([{path:'/admin/devices/a/operations',body:{site:'Conference room',notes:'Left USB port',maintenance_minutes:30,reason:'Planned inspection'}}]);
 expect(errors).toEqual([]);
});

test('Cancelling a baseline dialog never saves a baseline',async({page})=>{
 const writes:string[]=[];
 await page.route('http://fleet.test/**',async route=>{
  const req=route.request();
  if(new URL(req.url()).pathname==='/')return route.fulfill({contentType:'text/html',body:html});
  if(req.method()==='POST')writes.push(req.url());
  return route.fulfill({json:{}});
 });
 await page.goto('http://fleet.test/');
 await page.evaluate(()=> (window as any).goldenSaveDialog({id:'a',name:'Room A'}));
 const dialog=page.getByRole('dialog');
 await expect(dialog.getByRole('button',{name:'Save verified baseline',exact:true})).toBeVisible();
 await expect(dialog.getByRole('button',{name:'Save unconfirmed',exact:true})).toBeVisible();
 await dialog.getByRole('button',{name:'Cancel',exact:true}).click();
 await expect(dialog).toHaveCount(0);
 expect(writes).toEqual([]);
});


test('Unconfirmed baseline requires its explicit save action',async({page})=>{
 const writes:any[]=[];
 await page.route('http://fleet.test/**',async route=>{
  const req=route.request();
  if(new URL(req.url()).pathname==='/')return route.fulfill({contentType:'text/html',body:html});
  if(req.method()==='POST')writes.push(req.postDataJSON());
  return route.fulfill({json:{id:123,status:'done',timeout_s:1}});
 });
 await page.goto('http://fleet.test/');
 await page.evaluate(()=> (window as any).goldenSaveDialog({id:'a',name:'Room A',online:true}));
 await page.getByRole('dialog').getByRole('button',{name:'Save unconfirmed',exact:true}).click();
 await expect.poll(()=>writes.length).toBe(1);
 expect(writes[0].type).toBe('golden-save');
 expect(writes[0].args.confirmed).toBe(false);
 expect(writes[0].args.verified).toBe('');
 expect(writes[0].args.note).toMatch(/^UNCONFIRMED /);
});
