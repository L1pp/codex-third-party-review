/** Offline-only preloader: every fetch is answered from the caller's fixture. */
import {readFileSync} from 'node:fs';
const filename=process.env.CODEX_REVIEW_TEST_SCENARIO;
if(!filename)throw new Error('An explicit offline scenario is required');
const fixture=JSON.parse(readFileSync(filename,'utf8'));let position=0;
globalThis.fetch=async(input,init={})=>{
  const step=fixture.steps[position++];
  if(!step)throw new Error('Unexpected extra request in the offline scenario');
  if(step.expectedPurpose){
    const body=JSON.parse(init.body);
    if(step.expectedPurpose==='compaction'&&body.enable_thinking!==false)throw new Error('Compaction did not disable thinking');
  }
  if(step.throw){const error=new Error(step.throw);error.name=step.errorName??'TypeError';throw error;}
  let text=step.body;
  if(text===undefined){
    text=(step.events??[]).map(event=>event==='[DONE]'?'data: [DONE]\n\n':'data: '+JSON.stringify(event)+'\n\n').join('');
  }
  const bytes=new TextEncoder().encode(text);
  let offset=0;
  const stream=new ReadableStream({
    pull(controller){
      if(init.signal?.aborted){controller.error(init.signal.reason);return;}
      if(offset>=bytes.length){controller.close();return;}
      const size=step.chunkSize??bytes.length;
      controller.enqueue(bytes.slice(offset,offset+size));offset+=size;
    },
  });
  return new Response(stream,{status:step.status??200,headers:{'content-type':'text/event-stream; charset=utf-8',...(step.headers??{})}});
};
