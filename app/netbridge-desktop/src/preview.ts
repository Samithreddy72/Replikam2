/** Development-only fixture. Imported only by Vite dev, never a live Fleet/media path. */
import type {EngineState} from './api';
const state:EngineState={signed_in:new URLSearchParams(location.search).get('preview')!=='signin',email:'presenter@example.test',control_url:'https://fleet.example.test',last_bridge:'demo-room',last_camera:'Built-in camera',last_mic:'System microphone',live:false,wanted:false,video_muted:false,voice_muted:false,return_on:true,return_gain:'1',return_jitter_ms:'250',version:'local preview'};
export async function previewRequest(path:string, raw?:unknown):Promise<unknown> {
 const body=(raw || {}) as Record<string,any>;
 switch(path){
 case '/api/state':return {...state,bridge_checks:{age_s:1,reachable:true,checks:Object.fromEntries(['video_arriving','voice_arriving','return_audio','client_sees_camera'].map(k=>[k,{ok:true,detail:'Simulated measurement for layout review only.'}]))}};
 case '/api/signin-request':return {note:'Preview only: use 123456. No email was sent.'};
 case '/api/signin-redeem':if(body.code!=='123456') throw Error('Code not accepted. Use 123456 in this local preview.');state.signed_in=true;return {ok:true};
 case '/api/signout':state.signed_in=false;state.live=false;state.wanted=false;return {ok:true};
 case '/api/bridges':await new Promise(r=>setTimeout(r,700));return [{id:'demo-room',name:'Conference room · Demo',online:true,ip:'192.0.2.10'}];
 case '/api/devices':return {video:[{name:'Built-in camera',index:'0'}],audio:[{name:'System microphone',index:'0'},{name:'USB headset',index:'1'}]};
 case '/api/unlock':if(body.pin!=='123456') throw Error('Incorrect bridge PIN. Check the PIN and try again.');return {ok:true};
 case '/api/golive':state.live=true;state.wanted=true;state.video_muted=body.video_muted;state.voice_muted=body.voice_muted;return {ok:true,return_note:'Simulation only. No camera, microphone, or network stream was started.'};
 case '/api/stop':state.live=false;state.wanted=false;return {ok:true};
 case '/api/video':state.video_muted=body.muted;return {ok:true};
 case '/api/microphone':state.voice_muted=body.muted;return {ok:true};
 case '/api/return':state.return_on=body.on;return {ok:true};
 case '/api/audio/diagnostics':return {backend:'Preview fixture',running:false};
 case '/api/preflight':return {checks:[{label:'Fleet account',status:'pass',detail:'Example account is signed in.'},{label:'Bridge',status:'pass',detail:'Conference room is available in this example.'},{label:'Camera & microphone',status:'unknown',detail:'Preview only. No devices were opened.'},{label:'Meeting laptop',status:'unknown',detail:'Verify the picture and sound in your meeting app.'}]};
 case '/api/support-report':return body.submit ? {reference:'DEMO-LOCAL-ONLY'} : {notice:'This local preview uses sample data. In the app, reports share account identity and bridge health with your fleet administrator. No recordings are included.',report:{mode:'LOCAL UI PREVIEW',bridge:'Conference room · Demo',live:state.live}};
 case '/api/remember':return {ok:true};
 default:throw Error('This action is unavailable in the local interface preview.');
 }
}
