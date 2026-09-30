import React, {useEffect, useRef} from 'react';
import {X} from 'lucide-react';
export function Dialog({title, close, children}: {title:string; close:()=>void; children:React.ReactNode}) {
  const ref=useRef<HTMLDivElement>(null), closer=useRef(close); closer.current=close;
  useEffect(()=>{
    const previous=document.activeElement as HTMLElement | null;
    const old=document.body.style.overflow; document.body.style.overflow='hidden';
    ref.current?.focus();
    const key=(event:KeyboardEvent)=>{
      if(event.key==='Escape'){event.stopImmediatePropagation();closer.current();}
      if(event.key==='Tab'){
        const items=Array.from(ref.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input, select, summary, [href], [tabindex="0"]') || []).filter(e=>e.getClientRects().length);
        const first=items[0],last=items.at(-1);
        if(event.shiftKey && (document.activeElement===first || document.activeElement===ref.current)){event.preventDefault();last?.focus();}
        else if(!event.shiftKey && (document.activeElement===last || document.activeElement===ref.current)){event.preventDefault();first?.focus();}
      }
    };
    document.addEventListener('keydown',key,true);
    return()=>{document.body.style.overflow=old;document.removeEventListener('keydown',key,true);previous?.focus();};
  },[]);
  return <div className="modal-backdrop" onClick={close}><div className="studio-dialog" role="dialog" aria-modal="true" aria-label={title} ref={ref} tabIndex={-1} onClick={e=>e.stopPropagation()}>
    <header><div><span className="eyebrow">NETBRIDGE STUDIO</span><h2>{title}</h2></div><button aria-label={`Close ${title}`} className="icon-button" onClick={close}><X size={20}/></button></header>
    {children}
  </div></div>;
}
