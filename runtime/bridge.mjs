/** Private stdio host boundary for the official Harness services. */
import readline from 'node:readline';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const root=path.dirname(fileURLToPath(import.meta.url));
const manifest=JSON.parse(await readFile(path.join(root,'package.json'),'utf8'));
if(process.argv.includes('--runtime-info')){
  const resolved={};
  for(const [name,expected] of Object.entries(manifest.dependencies)){
    const filename=path.join(root,'node_modules',...name.split('/'),'package.json');
    const pkg=JSON.parse(await readFile(filename,'utf8'));
    if(pkg.version!==expected) throw new Error('Runtime dependency version mismatch: '+name);
    resolved[name]=pkg.version;
  }
  console.log(JSON.stringify({node:process.version,officialHarness:resolved['@deepseek-ai/dsh-agent-loop'],dependencies:resolved}));
}else{
  const input=readline.createInterface({input:process.stdin,crlfDelay:Infinity,terminal:false});
  const pending=new Map();let nextId=0;let started=false;let completed=false;
  const controller=new AbortController();let apiKey='';
  function redact(value){
    if(typeof value==='string') return apiKey?value.split(apiKey).join('[REDACTED]'):value;
    if(Array.isArray(value)) return value.map(redact);
    if(value&&typeof value==='object') return Object.fromEntries(Object.entries(value).map(([k,v])=>[redact(k),redact(v)]));
    return value;
  }
  function send(value){process.stdout.write(JSON.stringify(redact(value))+'\n');}
  function request(method,params){
    const id=String(++nextId);
    return new Promise((resolve,reject)=>{
      pending.set(id,{resolve,reject});send({request:{id,method,params}});
    });
  }
  async function run(message){
    started=true;
    const spec=message.spec;
    apiKey=message.secret?.apiKey??'';
    const host={apiKey,endpoint:message.secret?.endpoint,signal:controller.signal,request,
      notify:(type,data)=>send({event:type,data}),
      executeTool:params=>request('executeTool',params)};
    try{
      let result;
      if(message.method==='single'){
        const {runSingleRequest}=await import('./adapter.mjs');
        result=await runSingleRequest(spec,host,message.payload);
      }else{
        const [{runEngine},{createReviewAdapter},{default:persistencePlugin},{mountContext}]=await Promise.all([
          import('./engine.mjs'),import('./adapter.mjs'),import('./persistence.mjs'),import('./context.mjs')]);
        const adapter=createReviewAdapter(spec,host);
        result=await runEngine(spec,host,{adapter,persistencePlugin,contextPlugin:mountContext});
      }
      send({event:'engine.complete',data:result});
    }catch(error){
      send({event:'engine.complete',data:{status:controller.signal.aborted?'interrupted':'error',
        error_type:error?.code??error?.name??'RuntimeError',
        error_message:String(error?.message??'The review runtime failed'),finalContent:'',finishReason:null}});
    }finally{
      completed=true;apiKey='';host.apiKey='';input.close();
      for(const item of pending.values()) item.reject(new Error('Review runtime closed'));
      pending.clear();
    }
  }
  input.on('line',line=>{
    try{
      const message=JSON.parse(line);
      if(message.reply!==undefined){
        const item=pending.get(String(message.reply));if(!item)return;
        pending.delete(String(message.reply));
        if(message.error){const err=new Error(String(message.error.message??'Host operation failed'));err.code=message.error.code??'HOST_IO_ERROR';item.reject(err);}
        else item.resolve(message.result);
      }else if(message.cancel){controller.abort(new Error('Operator cancelled'));}
      else if(['run','single'].includes(message.method)&&!started){void run(message);}
      else if(!started){throw new Error('Expected one run initialization');}
    }catch(error){
      send({event:'engine.protocol_error',data:{error_type:'ProtocolError',error_message:String(error.message)}});
      controller.abort(error);
    }
  });
  input.on('close',()=>{if(started&&!completed)controller.abort(new Error('Host input closed'));});
}
