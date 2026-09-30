import React, {useEffect, useState} from 'react';
import {ArrowRight, Check, Monitor, Moon, ShieldCheck, Sun} from 'lucide-react';
import {previewMode} from './api';

export type Appearance = 'system' | 'light' | 'dark';
export const previewScene = previewMode ? new URLSearchParams(location.search).get('preview') || '' : '';
function saved(key:string) {try{return localStorage.getItem(key);}catch{return null;}}
function save(key:string,value:string) {try{localStorage.setItem(key,value);}catch{/* Storage can be unavailable in private browser previews. */}}
export function useAppearance() {
  const [appearance,setAppearance]=useState<Appearance>(()=>{
    const choice=(previewMode ? new URLSearchParams(location.search).get('theme') : null) || saved('nb.appearance');
    return choice==='light' || choice==='dark' ? choice : 'system';
  });
  const [dark,setDark]=useState(()=>matchMedia('(prefers-color-scheme: dark)').matches);
  useEffect(()=>{
    const media=matchMedia('(prefers-color-scheme: dark)');
    const update=()=>setDark(media.matches);media.addEventListener('change',update);
    return()=>media.removeEventListener('change',update);
  },[]);
  const resolved=appearance==='system' ? dark ? 'dark' : 'light' : appearance;
  useEffect(()=>{document.documentElement.dataset.theme=resolved;save('nb.appearance',appearance);},[appearance,resolved]);
  return {appearance,setAppearance,resolved};
}
export function AppearanceControl({value,onChange}:{value:Appearance;onChange:(v:Appearance)=>void}) {
  return <div className="appearance-control" role="group" aria-label="Appearance">
    {([['light',Sun,'Light'],['dark',Moon,'Dark'],['system',Monitor,'Auto']] as const).map(([v,Icon,label])=><button key={v} type="button" title={`${label} appearance`} aria-label={`${label} appearance`} aria-pressed={value===v} onClick={()=>onChange(v)}><Icon size={14}/><span>{label}</span></button>)}
  </div>;
}
export function BrandMark({large=false}:{large?:boolean}) {
  return <svg aria-hidden="true" className={`nb-symbol ${large?'large':''}`} viewBox="0 0 48 48" fill="none"><path d="M10 36V12h5l18 24h5V12" stroke="currentColor" strokeWidth="4.5" strokeLinecap="round" strokeLinejoin="round"/><circle cx="10" cy="12" r="3" fill="currentColor"/><circle cx="38" cy="36" r="3" fill="currentColor"/></svg>;
}
export function Spinner({label}:{label?:string}) {
  return <span className="nb-spinner" role={label?'status':undefined} aria-label={label} aria-hidden={label?undefined:true}>{Array.from({length:8},(_,i)=><i key={i} style={{'--i':i} as React.CSSProperties}/>)}</span>;
}
export function ConnectionArt({connected=false}:{connected?:boolean}) {
  return <div className={`connection-art ${connected?'connected':''}`} aria-hidden="true">
    <div className="connection-halo"/>
    <div className="computer computer-left"><div className="computer-screen"><div className="computer-face"><i/><i/><span/></div></div><div className="computer-foot"/><small>Your workspace</small></div>
    <div className="connection-link"><span/><span/><span/>{connected && <b><Check size={18}/></b>}</div>
    <div className="computer computer-right"><div className="computer-screen"><div className="computer-face"><i/><i/><span/></div></div><div className="computer-foot"/><small>Your room</small></div>
  </div>;
}
export function PreviewScenes() {
  if(!previewMode)return null;
  return <label className="preview-scenes"><span>DESIGN PREVIEW</span><select aria-label="Preview screen" value={previewScene} onChange={e=>{const url=new URL(location.href);url.searchParams.set('preview',e.target.value);location.href=url.toString();}}>{[['splash','Launch'],['signin','Sign in'],['name','Your name'],['connecting','Connecting'],['welcome','Welcome'],['studio','Studio']].map(([v,label])=><option value={v} key={v}>{label}</option>)}</select></label>;
}
export function ExperienceFrame({children,appearance,onAppearance}:{children:React.ReactNode;appearance:Appearance;onAppearance:(v:Appearance)=>void}) {
  return <div className="experience-shell"><header className="experience-header"><div className="auth-brand"><BrandMark/><span>NetBridge <b>Studio</b></span></div><div className="experience-tools"><PreviewScenes/><AppearanceControl value={appearance} onChange={onAppearance}/></div></header><div className="experience-content">{children}</div><footer className="experience-footer"><span><ShieldCheck size={13}/> Your space to connect.</span><span>{previewMode?'Preview only · No media is transmitted':'NetBridge Studio · Mac & Windows'}</span></footer></div>;
}
export function Splash() {
  return <section className="splash-content" role="status"><div className="splash-mark"><BrandMark large/></div><h1>NetBridge<span>Studio</span></h1><p>A little closer. From anywhere.</p><Spinner label="Starting NetBridge Studio"/></section>;
}
export function WorkspaceLoading({error,retry,slow=false}:{error?:string;retry:()=>void;slow?:boolean}) {
  return <section className="connection-content"><ConnectionArt/><span className="experience-kicker">YOUR WORKSPACE</span><h1>{error?'Let’s try that again.':'Making the connection.'}</h1><p>{error || (slow?'Fleet is taking a little longer to respond. You can retry safely.':'Finding your bridges and preparing your devices.')}</p>{error || slow ? <button className="primary" onClick={retry}>Try again <ArrowRight size={16}/></button> : <div className="connection-status" role="status"><Spinner/> Preparing your workspace</div>}<div className="connection-privacy"><ShieldCheck size={14}/> Camera and microphone remain off.</div></section>;
}
export type Profile={complete:boolean;name:string};
export function profileKey(email:string,control:string) {return 'nb.profile.'+JSON.stringify([control.replace(/\/$/,''),email.toLowerCase()]);}
export function readProfile(key:string):Profile {try{const v=JSON.parse(saved(key)||'{}');return {complete:v.complete===true,name:typeof v.name==='string'?v.name.slice(0,40):''};}catch{return {complete:false,name:''};}}
export function storeProfile(key:string,name:string) {save(key,JSON.stringify({complete:true,name:name.trim().slice(0,40)}));}
export function NameEntry({onComplete}:{onComplete:(name:string)=>void}) {
  const [name,setName]=useState('');
  return <section className="name-card"><div className="welcome-avatar"><BrandMark/></div><span className="experience-kicker">NICE TO MEET YOU</span><h1>What should we call you?</h1><p>A familiar hello makes this space yours.</p><form onSubmit={e=>{e.preventDefault();if(name.trim())onComplete(name);}}><label>Your name<input value={name} autoFocus autoComplete="given-name" maxLength={40} placeholder="First name" onChange={e=>setName(e.target.value)}/></label><p className="name-note">Saved for this account on this computer only.</p><button className="primary wide" disabled={!name.trim()}>Continue <ArrowRight size={17}/></button></form><button className="text-button" onClick={()=>onComplete('')}>Skip for now</button></section>;
}
export function Welcome({name,onContinue}:{name:string;onContinue:()=>void}) {
  return <section className="welcome-content"><ConnectionArt connected/><span className="experience-kicker">ALL SET</span><h1>{name?`Hi, ${name}!`:'Welcome aboard.'}</h1><p>Your workspace is ready.<br/>Let’s bring you into the room.</p><button className="primary" autoFocus onClick={onContinue}>Open Studio <ArrowRight size={17}/></button></section>;
}
