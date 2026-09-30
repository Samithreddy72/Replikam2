import {test,expect} from '@playwright/test';
test('appearance switches, persists and follows the operating system',async({page})=>{
 await page.goto('/?preview=studio');
 await expect(page.getByLabel('Meeting bridge',{exact:true})).toBeVisible();
 await page.getByRole('button',{name:'Light appearance',exact:true}).click();
 await expect(page.locator('html')).toHaveAttribute('data-theme','light');
 await page.getByRole('button',{name:'Dark appearance',exact:true}).click();
 await expect(page.locator('html')).toHaveAttribute('data-theme','dark');
 await page.reload();await expect(page.locator('html')).toHaveAttribute('data-theme','dark');
 await page.getByRole('button',{name:'Auto appearance',exact:true}).click();
 await page.emulateMedia({colorScheme:'light'});await expect(page.locator('html')).toHaveAttribute('data-theme','light');
 await page.emulateMedia({colorScheme:'dark'});await expect(page.locator('html')).toHaveAttribute('data-theme','dark');
});
test('first sign-in asks a name, greets the user, and keeps capture off',async({page})=>{
 await page.goto('/?preview=signin&theme=light');
 await page.getByLabel('Work email',{exact:true}).fill('presenter@example.test');
 await page.getByRole('button',{name:'Send sign-in code',exact:true}).click();
 await page.getByLabel('Sign-in code',{exact:true}).fill('123456');
 await page.getByRole('button',{name:'Sign in',exact:true}).click();
 await expect(page.getByRole('heading',{name:'What should we call you?'})).toBeVisible();
 await page.getByLabel('Your name',{exact:true}).fill('Alex');await page.getByRole('button',{name:'Continue',exact:true}).click();
 await expect(page.getByRole('heading',{name:'Hi, Alex!'})).toBeVisible();
 await page.getByRole('button',{name:'Open Studio',exact:true}).click();
 await expect(page.getByLabel('Meeting bridge',{exact:true})).toBeVisible();
 await expect(page.getByRole('heading',{name:'Hi, Alex!'})).toBeVisible();
 await expect(page.getByText('Not broadcasting',{exact:true})).toBeVisible();
 await page.reload();await page.getByLabel('Work email',{exact:true}).fill('presenter@example.test');
 await page.getByRole('button',{name:'Send sign-in code',exact:true}).click();await page.getByLabel('Sign-in code',{exact:true}).fill('123456');await page.getByRole('button',{name:'Sign in',exact:true}).click();
 await expect(page.getByLabel('Meeting bridge',{exact:true})).toBeVisible();
 await expect(page.getByRole('heading',{name:'What should we call you?'})).toHaveCount(0);
});
test('screen review in both themes with reduced motion and no horizontal overflow',async({page})=>{
 await page.emulateMedia({reducedMotion:'reduce'});await page.setViewportSize({width:1360,height:900});
 for(const theme of ['light','dark']) for(const scene of ['splash','signin','name','connecting','welcome','studio']){
  await page.goto(`/?preview=${scene}&theme=${theme}`);
  if(scene==='studio')await expect(page.getByLabel('Meeting bridge',{exact:true})).toBeVisible();
  if(scene==='name')await expect(page.getByLabel('Your name',{exact:true})).toBeVisible();
  if(scene==='welcome')await expect(page.getByRole('button',{name:'Open Studio'})).toBeVisible();
  if(scene==='connecting')await expect(page.getByRole('heading',{name:'Making the connection.'})).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('data-theme',theme);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
  if(await page.locator('.nb-spinner i').count())expect(await page.locator('.nb-spinner i').first().evaluate(e=>getComputedStyle(e).animationName)).toBe('none');
  await page.screenshot({path:`test-results/redesign-${scene}-${theme}.png`,fullPage:false});
 }
});
test('workspace failure is recoverable and does not reveal a half-loaded Studio',async({page})=>{
 await page.addInitScript(()=>{
  const w=window as any;w.isTauri=true;w.bridgeAttempts=0;w.deviceAttempts=0;
  localStorage.setItem('nb.profile.'+JSON.stringify(['https://fleet.example.test','loading@example.test']),JSON.stringify({complete:true,name:'Alex'}));
  w.__TAURI_INTERNALS__={metadata:{currentWindow:{label:'main'},currentWebview:{label:'main'}},transformCallback:()=>1,unregisterCallback:()=>{},invoke:async(cmd:string,args:any)=>{
   if(cmd!=='engine_request')return 1;
   if(args.path==='/api/state')return {signed_in:true,email:'loading@example.test',control_url:'https://fleet.example.test',live:false};
   if(args.path==='/api/bridges'){w.bridgeAttempts++;return w.bridgeAttempts===1 ? {_error:'Fleet could not be reached. Check your connection.'} : [{id:'room',name:'Test room',online:true,ip:'192.0.2.1'}];}
   if(args.path==='/api/devices'){w.deviceAttempts++;return {video:[],audio:[]};}
   return {};
  }};
 });
 await page.goto('/');
 await expect(page.getByRole('heading',{name:'Let’s try that again.'})).toBeVisible();
 await expect(page.getByRole('button',{name:'Go live',exact:true})).toHaveCount(0);
 await page.getByRole('button',{name:'Try again',exact:true}).click();
 await expect(page.getByLabel('Meeting bridge',{exact:true})).toHaveValue('room');
 expect(await page.evaluate(()=>(window as any).bridgeAttempts)).toBe(2);
});
