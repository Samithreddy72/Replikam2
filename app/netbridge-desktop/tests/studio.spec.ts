import {test,expect} from '@playwright/test';
test('signed-out landing only offers sign-in',async({page})=>{
 await page.goto('/');
 await expect(page.getByRole('heading',{name:'Welcome to NetBridge'})).toBeVisible();
 await expect(page.getByRole('button',{name:'Go live',exact:true})).toHaveCount(0);
 await expect(page.getByRole('button',{name:'Sessions',exact:true})).toHaveCount(0);
 await expect(page.getByRole('button',{name:'Send sign-in code',exact:true})).toBeDisabled();
 await page.screenshot({path:'test-results/signin.png',fullPage:true});
});

test('desktop UI unlocks before streaming, handles mute, and ends session',async({page})=>{
 await page.addInitScript(()=>{
  const w=window as any;w.isTauri=true;w.calls=[];w.rejectStart=true;
  const state={signed_in:false,email:'developer@example.test',control_url:'https://fleet.example.test',last_bridge:'room',last_camera:'Test camera',last_mic:'Disconnected microphone',live:false,wanted:false,voice_muted:false,return_on:true,return_gain:'1.0',return_jitter_ms:'250',version:'test'};
  w.__TAURI_INTERNALS__={metadata:{currentWindow:{label:'main'},currentWebview:{label:'main'}},transformCallback:()=>1,unregisterCallback:()=>{},invoke:async(cmd:string,args:any)=>{
   if(cmd!=='engine_request')return 1;
   w.calls.push({path:args.path,body:args.body});
   switch(args.path){
    case '/api/signin-request':return {note:'Check your email for the code.'};
    case '/api/signin-redeem':state.signed_in=true;return {ok:true};
    case '/api/preflight':return {checks:[{label:'Camera',status:'pass',detail:'Listed, capture not yet tested.'},{label:'Receiver',status:'unknown',detail:'Displayed picture not verified.'}]};
    case '/api/support-report':return args.body.submit?{reference:'NB-SUPPORT-42'}:{report:{live:state.live,app_version:'test'},notice:'No recordings or raw logs are uploaded.'};
    case '/api/state':return {...state,bridge_checks:{age_s:1,reachable:true,checks:Object.fromEntries(['video_arriving','voice_arriving','return_audio','client_sees_camera'].map(key=>[key,{ok:true,detail:'Fresh test measurement'}]))}};
    case '/api/bridges':return [{id:'room',name:'Test room',online:true,tailscale_ip:'100.1.2.3'}];
    case '/api/devices':return {video:[{name:'Test camera',index:'0'}],audio:[{name:'Test mic',index:'0'}]};
    case '/api/unlock':return {ok:args.body.pin==='123456',message:'Wrong PIN'};
    case '/api/golive':if(w.rejectStart){w.rejectStart=false;return {_error:'Session expired. Enter the bridge PIN again.'}}state.live=true;state.wanted=true;return {ok:true};
    case '/api/microphone':state.voice_muted=args.body.muted;return {ok:true};
    case '/api/return':state.return_on=args.body.on;return {ok:true};
    case '/api/stop':state.live=false;state.wanted=false;return {ok:true};
    default:return {ok:true};
   }
  }};
 });
 await page.goto('/');
 await page.getByLabel('Work email',{exact:true}).fill('developer@example.test');
 await page.getByRole('button',{name:'Send sign-in code',exact:true}).click();
 await page.getByLabel('Sign-in code',{exact:true}).fill('123456');
 await page.getByRole('button',{name:'Sign in',exact:true}).click();
 await page.getByRole('button',{name:'Skip for now',exact:true}).click();
 await page.getByRole('button',{name:'Open Studio',exact:true}).click();
 await expect(page.getByRole('button',{name:'Go live',exact:true})).toBeEnabled();
 await expect(page.getByRole('heading',{name:'Room & devices'})).toBeVisible();
 await expect(page.getByRole('button',{name:'Go live',exact:true})).toHaveCount(1);
 await page.screenshot({path:'test-results/studio-ready.png',fullPage:true});
 await page.getByPlaceholder('Enter your bridge PIN').fill('000000');
 await page.getByRole('button',{name:'Go live',exact:true}).click();
 await expect(page.getByRole('alert')).toContainText('Wrong PIN');
 expect(await page.evaluate(()=>(window as any).calls.some((c:any)=>c.path==='/api/golive'))).toBeFalsy();
 await page.getByRole('button',{name:'Check my setup',exact:true}).click();
 await expect(page.getByText('Displayed picture not verified.',{exact:false})).toBeVisible();
 await page.getByRole('button',{name:'Close setup results'}).click();
 await page.getByRole('button',{name:'Get help',exact:true}).click();
 await expect(page.getByRole('heading',{name:'Send a report to your fleet administrator'})).toBeVisible();
 expect(await page.evaluate(()=>(window as any).calls.some((c:any)=>c.path==='/api/support-report'&&c.body.submit))).toBeFalsy();
 await page.getByRole('button',{name:'Send report',exact:true}).click();
 await expect(page.getByText('Sent: NB-SUPPORT-42',{exact:true})).toBeVisible();
 await page.getByPlaceholder('Enter your bridge PIN').fill('123456');
 await page.getByRole('button',{name:'Go live',exact:true}).click();
 await expect(page.getByText('Session expired. Enter the bridge PIN again.',{exact:true})).toBeVisible();
 await page.getByPlaceholder('Enter your bridge PIN').fill('123456');
 await page.getByRole('button',{name:'Go live',exact:true}).click();
 expect(await page.evaluate(()=>(window as any).calls.filter((c:any)=>c.path==='/api/unlock').length)).toBe(3);
 expect(await page.evaluate(()=>(window as any).calls.filter((c:any)=>c.path==='/api/golive').at(-1).body.mic_name)).toBe('Test mic');
 await expect(page.getByRole('button',{name:'End session',exact:true})).toBeEnabled();
 await expect(page.getByText('3/4 confirmed',{exact:true})).toBeVisible();
 await expect(page.getByRole('button',{name:'Meeting laptop sees the camera: USB reported configured'})).toBeVisible();
 await page.screenshot({path:'test-results/studio-live.png',fullPage:true});
 await expect(page.getByRole('region',{name:'Meeting checks'})).toBeVisible();
 await page.getByRole('button',{name:'Mute microphone',exact:true}).click();
 await expect(page.getByRole('button',{name:'Unmute microphone',exact:true})).toBeEnabled();
 await page.getByRole('button',{name:'Mute meeting audio',exact:true}).click();
 await expect(page.getByRole('button',{name:'Enable meeting audio',exact:true})).toBeEnabled();
 await page.getByRole('button',{name:'End session',exact:true}).click();
 await expect(page.getByRole('button',{name:'Go live',exact:true})).toBeEnabled();
 await page.getByRole('button',{name:'Sessions',exact:true}).click();
 await expect(page.getByText('Test room',{exact:true})).toBeVisible();
});
