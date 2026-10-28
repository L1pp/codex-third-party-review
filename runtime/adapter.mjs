/** Bailian wire adaptation around official pi-ai parsing and DSH stream/replay. */
import { performance } from 'node:perf_hooks';
import { setImmediate as nextTurn } from 'node:timers/promises';
import { LlmAdapter, LlmError, BlockAssembler, ReasoningEffortId, attributionHeaders, isContextWindowExceededError, isQuotaExceededError, resolveRetryPolicy, createUserMessage, errorChain, projectToolUpdates } from '@deepseek-ai/dsh-llm';
import { idleWatchdog, timeoutOf } from '@deepseek-ai/dsh-timeout';
import { stream as piStream, convertMessages } from '@earendil-works/pi-ai/api/openai-completions';
import { normalizeContext } from '@earendil-works/pi-ai/utils/transcript';
import { createParser } from 'eventsource-parser';
import { toPiContext } from './vendor/dsh-pi-replay.mjs';
import { toStreamChunks } from './vendor/dsh-pi-stream.mjs';

const REDACTED='[REDACTED]';
function redactTree(value,secret) { if(!secret)return value;if(typeof value==='string')return value.replaceAll(secret,REDACTED);if(Array.isArray(value))return value.map(item=>redactTree(item,secret));if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value).map(([key,item])=>[key.replaceAll(secret,REDACTED),redactTree(item,secret)]));return value; }
class StreamingKeyMask {
  constructor(secret){this.secret=secret;this.pending='';}
  feed(text){if(!this.secret)return text;this.pending+=text;const end=Math.max(0,this.pending.length-this.secret.length+1);let out='',pos=0;while(pos<end){if(this.pending.startsWith(this.secret,pos)){out+=REDACTED;pos+=this.secret.length;}else out+=this.pending[pos++];}this.pending=this.pending.slice(pos);return out;}
  finish(){const out=redactTree(this.pending,this.secret);this.pending='';return out;}
}
async function* protectedChunks(source,secret,onRawChunk) {
  const pending=new Map();
  function flush(index){const entry=pending.get(index);if(!entry)return null;const text=entry.mask.finish();pending.delete(index);return text?{...entry.template,[entry.field]:text}:null;}
  try { for await(const chunk of source){onRawChunk(chunk);const field=chunk.type==='text-delta'||chunk.type==='reasoning-delta'?'text':chunk.type==='tool-call-delta'?'argumentsDelta':null;
    if(field){let entry=pending.get(chunk.index);if(!entry){entry={mask:new StreamingKeyMask(secret),template:redactTree(chunk,secret),field};pending.set(chunk.index,entry);}const text=entry.mask.feed(chunk[field]);if(text)yield {...redactTree(chunk,secret),[field]:text};continue;}
    if(chunk.type==='block-end'){const tail=flush(chunk.index);if(tail)yield tail;}
    if(chunk.type==='finish')for(const index of [...pending.keys()]){const tail=flush(index);if(tail)yield tail;}
    yield redactTree(chunk,secret);
  }}catch(error){for(const index of [...pending.keys()]){const tail=flush(index);if(tail)yield tail;}throw error;}
}

const TLS_CERTIFICATE_CODES=new Set(['ERR_TLS_CERT_ALTNAME_INVALID','CERT_HAS_EXPIRED','DEPTH_ZERO_SELF_SIGNED_CERT','UNABLE_TO_VERIFY_LEAF_SIGNATURE','UNABLE_TO_GET_ISSUER_CERT_LOCALLY']);
function transportDiagnostic(error) {
  // fetch exposes Node's certificate code on the direct Error or its cause.
  // Official errorChain owns arbitrary nesting, AggregateError and safe rendering.
  const message=errorChain(error);
  const code=[error?.code,error?.cause?.code].find(value=>typeof value==='string'&&TLS_CERTIFICATE_CODES.has(value))
    ?? [...TLS_CERTIFICATE_CODES].find(value=>message.includes(value));
  const tls=code!==undefined||/certificate has expired|self[- ]signed certificate|unable to verify (?:the first|leaf) certificate|unable to get (?:local )?issuer certificate|hostname\/IP does not match certificate/i.test(message);
  return {error_type:error?.name??'Error',...(code?{code}:{}),message,tls};
}
const SAFE_HEADERS = new Set(['content-type','request-id','x-request-id','x-acs-request-id','x-dashscope-request-id','x-aliyun-request-id','trace-id','x-trace-id','retry-after','retry-after-ms','x-ratelimit-limit-requests','x-ratelimit-remaining-requests','x-ratelimit-reset-requests','x-ratelimit-limit-tokens','x-ratelimit-remaining-tokens','x-ratelimit-reset-tokens']);
const compat = {supportsFinishReason:true,supportsUsageInStreaming:true,supportsStore:false,supportsDeveloperRole:false,supportsReasoningEffort:true,requiresReasoningContentOnAssistantMessages:true,maxTokensField:'max_tokens',supportsStrictMode:false};
function modelOf(options,route) { return {id:options.model??route.model,name:route.model,api:'openai-completions',provider:options.provider??'review',baseUrl:'https://unused.invalid/v1',reasoning:true,input:['text'],contextWindow:route.context_window_tokens??route.context_window??1000000,maxTokens:route.max_tokens??256000,cost:{input:0,output:0,cacheRead:0,cacheWrite:0},thinkingLevelMap:{off:'none',none:'none',max:'max'},compat}; }
/** Shared exact message projection for both the token meter and transport. */
function projectOptions(options) {
  const projected=projectToolUpdates(options.messages,options.tools,undefined,options.toolHistory);
  return {...options,messages:projected.messages,tools:projected.tools};
}
export function toOpenAIMessages(options,route) {
  const projected=projectOptions(options);
  return convertMessages(modelOf(projected,route),normalizeContext(toPiContext(projected)),compat);
}
/** Project the text review's declared tools; runtime-only fields never reach the API. */
export function toOpenAITools(tools=[]) { return tools.filter(tool=>!tool.deferLoading).map(tool=>({type:'function',function:{name:tool.name,description:tool.description,parameters:tool.parameters}})); }
/** Build only the gateway's declared wire fields, preserving Core's current history. */
export function buildPayload(options,route) {
  options=projectOptions(options);
  const payload={model:options.model??route.model,messages:toOpenAIMessages(options,route),stream:true,stream_options:{include_usage:true}};
  const tools=toOpenAITools(options.tools);if(tools.length)payload.tools=tools;
  const compact=options.purpose==='compaction';const effort=options.reasoningEffort??route.reasoning_effort;
  const off=effort==='none'||effort==='off';
  if(route.reasoning_effort!==undefined&&route.reasoning_effort!==null)payload.reasoning_effort=off?'none':effort;
  if(route.enable_thinking!==undefined&&route.enable_thinking!==null)payload.enable_thinking=off?false:route.enable_thinking;
  if(compact)payload.enable_thinking=false;
  const parameter=route.output_limit_parameter??'max_tokens';const combined=route.combined_output_limit_parameter;
  if(!['max_tokens','max_completion_tokens'].includes(parameter)||combined!==undefined&&combined!==null&&combined!=='max_completion_tokens')throw new LlmError('Unsupported output-limit parameter','INVALID_REQUEST');
  let cap=route.max_tokens??undefined;
  if(cap!==undefined&&combined&&parameter!==combined&&!off&&!compact&&route.enable_thinking!==false)cap+=(route.thinking_budget??0);
  // A request may narrow the selected route's positive allowance, never expand
  // it. Non-thinking compaction uses the body allowance, not auto's full sum.
  if(options.maxTokens!==undefined)cap=Number.isFinite(cap)&&cap>0?Math.min(options.maxTokens,cap):options.maxTokens;
  if(cap!==undefined)payload[combined??parameter]=cap;
  if(!combined&&!compact&&!off&&route.thinking_budget!==undefined&&route.thinking_budget!==null&&route.thinking_budget_parameter!==null)payload[route.thinking_budget_parameter??'thinking_budget']=route.thinking_budget;
  return payload;
}
/** One authoritative selected generation allowance, derived from the actual wire mapping. */
export function generationAllowance(route) {
  const payload=buildPayload({provider:'review',model:route.model,messages:[],tools:[],reasoningEffort:route.reasoning_effort},route);
  let value=payload[route.combined_output_limit_parameter??route.output_limit_parameter??'max_tokens'];
  // A separate thinking_budget is an additional allowance only when the
  // actual wire uses a body-only max_tokens field. Combined caps include it.
  const thinking=payload[route.thinking_budget_parameter??'thinking_budget'];
  if(payload.max_tokens!==undefined&&Number.isSafeInteger(thinking)&&thinking>0)value+=thinking;
  return Number.isSafeInteger(value)&&value>0?value:undefined;
}
/** Provider-error normalization adapted from DSH llm-deepseek/transport.ts (MIT). */
export function normalizeProviderFailure(raw,status,headers={}) {
  const error=raw&&typeof raw.error==='object'&&raw.error!==null?raw.error:raw??{};
  const fields={};for(const key of ['code','type','message'])if(error[key]!==undefined)fields[key]=String(error[key]);
  const message=fields.message??`Model request failed (${status??'stream error'})`;const detail=[fields.type,fields.code,message].join(' ');let code;
  if(status===401||status===403||/authentication_error|permission_error|invalid_api_key/i.test(detail))code='AUTH';
  else if(status===402||isQuotaExceededError(detail))code='QUOTA';
  else if(isContextWindowExceededError(detail))code='CONTEXT_WINDOW_EXCEEDED';
  // Only the structured provider error reaches this classifier. Policy and
  // invalid-request failures stay terminal even when wrapped in HTTP 200.
  else if(/data[\s_-]?inspection(?:failed)?|content[\s_-]?filter|safety[\s_-]?violation|policy[\s_-]?violation/i.test(detail))code='CONTENT_FILTER';
  else if(status===400||status===413||fields.type==='invalid_request_error'||/invalid[\s_-]?parameter|invalid[\s_-]?model|model[\s_-]?not[\s_-]?(?:found|exist)|unsupported[\s_-]?model/i.test(detail))code='INVALID_REQUEST';
  else if(status===429||/rate.?limit|throttl|too_many_requests/i.test(detail))code='RATE_LIMIT';
  else if(status===408||/gateway.?timeout|deadline.?exceeded/i.test(detail))code='TIMEOUT';
  else if(status===409||status===425)code='SERVER';
  else if(status>=500||/RESOURCE_EXHAUSTED|internal.?server.?error|inference engine abort|overloaded_error|api_error/i.test(detail))code='SERVER';
  else code=status===undefined||status===200?'SERVER':`HTTP_${status}`;
  const retry=headers['retry-after-ms']??headers['retry-after'];const value=retry==null?NaN:/^\d+(?:\.\d+)?$/.test(retry)?Number(retry)*(headers['retry-after-ms']!==undefined?1:1000):Date.parse(retry)-Date.now();
  return {message,code,...Number.isFinite(value)&&value>0?{providerRetryAfterMs:value}:{},...status===undefined?{}:{status},provider_error:fields};
}
function cleanHeaders(headers) { return Object.fromEntries([...headers].filter(([name])=>SAFE_HEADERS.has(name.toLowerCase())).map(([name,value])=>[name.toLowerCase(),value])); }
function resultStatus(finish,hasTools) { if(finish?.kind==='stop')return 'completed';if(finish?.kind==='tool-calls')return hasTools?'tool_calls':'incomplete';if(finish?.kind==='max-tokens')return 'incomplete';if(finish?.kind==='aborted')return 'interrupted';return 'error'; }
function finishReason(finish) { return ({stop:'stop','tool-calls':'tool_calls','max-tokens':'length',aborted:'aborted'})[finish?.kind]??null; }

export function createReviewAdapter(spec,host) {
  const route=spec.route;const endpoint=new URL(host.endpoint??spec.endpoint??`${spec.base_url.replace(/\/$/,'')}/chat/completions`);
  if(endpoint.protocol!=='https:'||endpoint.username||endpoint.password||endpoint.hash||endpoint.search||!endpoint.pathname.endsWith('/chat/completions'))throw new LlmError('Review endpoint must be an explicit HTTPS chat/completions URL','INVALID_REQUEST');
  let counter=0;let latest=null;
  class ReviewAdapter extends LlmAdapter {
    lastCallId=null;
    getLatestResult() { return latest===null?null:structuredClone(latest); }
    providerRetryPolicy() { const seconds=spec.apiRecovery?.retry_window_seconds??spec.api_recovery?.retry_window_seconds??120;return resolveRetryPolicy({mode:'always',backoff:{maxDelayMs:Math.max(10000,seconds*1000)}},'review.retryPolicy'); }
    resolveModel(provider,model) { return Promise.resolve({provider,id:model,name:model,inputModalities:['text'],context:{contextWindow:route.context_window_tokens??route.context_window??spec.context_window_tokens??1000000},defaultMaxTokens:generationAllowance(route),reasoning:{efforts:['none','off','low','medium','high','max'].map(id=>({id:ReasoningEffortId(id),name:id}))}}); }
    async *stream(options) {
      const start=performance.now();const startedUtc=new Date().toISOString();const owner=new AbortController();const signal=options.signal?AbortSignal.any([options.signal,owner.signal]):owner.signal;
      const watchdog=idleWatchdog(signal,(spec.readTimeoutSeconds??spec.read_timeout_seconds??300)*1000,'REVIEW_IDLE');
      let requestId=null;let httpStatus;let headers={};let providerError;let failure;let rawDone=false;let rawComplete=false;let rawTask;let rawReader;let firstEvent=null;let firstContent=null;let text='';let reasoning='';let rawUsage;let reportedModel;let wirePayload;let terminalChunk;let rawText='';let rawReasoning='';let rawReasoningChars=0;let transportDetail;let nonSseText='';let isSse=true;let drainRaw;
      const assembler=new BlockAssembler();latest=null;this.lastCallId=null;
      const emit=(type,data)=>host.notify?.(type,redactTree({id:requestId,requestId,...data},host.apiKey));
      const acknowledge=async(method,data)=>{try { if(host.request)await host.request(method,redactTree({id:requestId,requestId,...data},host.apiKey));else await emit(method==='requestStart'?'request.start':'request.end',data); }catch(cause){failure={code:'HOST_IO_ERROR',message:'Host could not persist request metadata'};if(latest&&method==='requestEnd'){latest.status='error';latest.error_code='HOST_IO_ERROR';latest.error_type='HostIOError';}throw new LlmError(failure.message,failure.code,{cause});}};
      const parser=createParser({onEvent:(event)=>{if(firstEvent===null)firstEvent=(performance.now()-start)/1000;if(event.data==='[DONE]'){rawDone=true;return;}try{const data=JSON.parse(event.data);if(data?.error)providerError=data;}catch{/* The SDK owns malformed JSON validation. */}},onComment:()=>watchdog.pulse(),onError:()=>{/* Raw bytes remain available; SDK parsing is authoritative. */}});
      const observeFetch=async(input,init)=>{
        if(String(input)!==endpoint.href)throw new LlmError('SDK attempted an undeclared endpoint','INVALID_REQUEST');
        try { await host.beforeRequest?.(options); }catch(cause){
          if(cause instanceof LlmError || ['REVIEW_CONTEXT_CHANGED','REVIEW_MODEL_CALL_LIMIT','REVIEW_COMPACTION_LIMIT'].includes(cause?.code)) failure={code:cause.code,message:cause.message};
          else failure={code:'HOST_IO_ERROR',message:'Local review preflight failed'};
          await host.notify?.('runtime.preflight_error',redactTree({code:failure.code,message:errorChain(cause)},host.apiKey));
          throw new LlmError(failure.message,failure.code,{cause});
        }

        requestId=`call-${String(++counter).padStart(4,'0')}`;this.lastCallId=requestId;wirePayload=JSON.parse(init.body);
        await acknowledge('requestStart',{requestId,number:counter,payload:wirePayload,route,purpose:options.purpose??'review',started_utc:startedUtc});
        let response;
        try { response=await (host.fetch??globalThis.fetch)(input,{...init,redirect:'error',signal:init.signal?AbortSignal.any([init.signal,watchdog.signal]):watchdog.signal}); }
        catch(cause){transportDetail=transportDiagnostic(cause);await emit('response.transport-error',transportDetail);failure={code:signal.aborted?'ABORTED':init.signal?.aborted&&!watchdog.signal.aborted?'TIMEOUT':transportDetail.tls?'TLS_ERROR':'TRANSPORT',message:transportDetail.tls?'Provider TLS certificate validation failed':'Provider connection failed'};throw new LlmError(failure.message,failure.code,{cause});}
        httpStatus=response.status;headers=cleanHeaders(response.headers);await emit('response.http',{status:httpStatus,headers});
        if(!response.body)return response;
        rawReader=response.body.getReader();isSse=(headers['content-type']??'').toLowerCase().includes('text/event-stream');const decoder=new TextDecoder();
        const readRaw=async()=>{const item=await rawReader.read();if(item.done){rawComplete=true;const tail=decoder.decode();if(isSse)parser.feed(tail);else nonSseText+=tail;if(!isSse){try{providerError=JSON.parse(nonSseText);}catch{/* HTTP status still identifies non-JSON gateway failures. */}}return item;}watchdog.pulse();const received=new Date().toISOString();const text=decoder.decode(item.value,{stream:true});if(isSse)parser.feed(text);else nonSseText+=text;await emit('response.raw',{base64:Buffer.from(item.value).toString('base64'),received_utc:received});return item;};
        // One reader observes bytes before the SDK receives them. SDK completion
        // can cancel at [DONE]; buffered tail bytes are drained at settlement.
        const sdkBody=new ReadableStream({async pull(controller){try{const item=await readRaw();if(item.done)controller.close();else controller.enqueue(item.value);}catch(error){controller.error(error);}},cancel(){/* Request owner handles the shared source after semantic completion. */}},{highWaterMark:0});
        drainRaw=async()=>{try{while(!rawComplete){const item=await readRaw();if(item.done)return;}}catch(error){if(!owner.signal.aborted&&!signal.aborted)await emit('response.capture-warning',{error_type:error?.name??'Error'});}};
        return new Response(sdkBody,{status:response.status,statusText:response.statusText,headers:response.headers});
      };
      try {
        const model={...modelOf(options,route),baseUrl:endpoint.href.slice(0,-'/chat/completions'.length)};
        const events=piStream(model,normalizeContext(toPiContext(options)),{apiKey:host.apiKey,maxRetries:0,cacheRetention:'none',signal:watchdog.signal,timeoutMs:(spec.connectTimeoutSeconds??spec.connect_timeout_seconds??30)*1000,fetch:observeFetch,headers:attributionHeaders(),maxTokens:options.maxTokens??route.max_tokens,onPayload:()=>redactTree(spec.singleRequestPayload??buildPayload(options,route),host.apiKey),onProviderStreamEvent:async(data)=>{if(firstEvent===null)firstEvent=(performance.now()-start)/1000;if(data?.model)reportedModel=data.model;if(data?.usage&&Object.keys(data.usage).length)rawUsage=data.usage;await emit('response.provider',{data});}});
        const safe=protectedChunks(toStreamChunks(events,model.contextWindow,options.signal,options.model),host.apiKey,item=>{if(item.type==='text-delta'){if(firstContent===null)firstContent=(performance.now()-start)/1000;rawText+=item.text;}if(item.type==='reasoning-delta'){rawReasoningChars+=item.text.length;rawReasoning+=item.text;}});const iterator=safe[Symbol.asyncIterator]();
        try { while(true){const next=await watchdog.next(iterator);if(next.done)break;let item=next.value;
          if(item.type==='finish'&&item.reason.kind==='error'&&(providerError||failure)){const {provider_error,...canonical}=failure??normalizeProviderFailure(providerError,httpStatus,headers);item={...item,reason:{kind:'error',failure:canonical}};}
          item=redactTree(item,host.apiKey);assembler.push(item);
          if(item.type==='reasoning-delta'){reasoning+=item.text;await emit('response.reasoning',{text:item.text});}
          if(item.type==='text-delta'){if(firstContent===null)firstContent=(performance.now()-start)/1000;text+=item.text;}
          if(item.type==='finish')terminalChunk=item;else yield item;
        }}finally{void iterator.return?.().catch(()=>{});}
      } catch(error) {
        if(timeoutOf(watchdog.signal,'REVIEW_IDLE')!==undefined)failure={code:'TIMEOUT',message:'Provider stream idle timeout'};
        else if(options.signal?.aborted)failure={code:'ABORTED',message:'Request cancelled'};
        else failure??={code:error?.code??'TRANSPORT',message:error?.message??'Provider stream failed'};
        throw new LlmError(redactTree(failure.message,host.apiKey),failure.code);
      } finally {
        if(drainRaw&&!rawComplete){rawTask=drainRaw();await Promise.race([rawTask,nextTurn()]);}
        owner.abort();void rawReader?.cancel().catch(()=>{});watchdog[Symbol.dispose]();
        // The SDK can settle at a terminal line before the observer consumes
        // its trailing CRLF. Normalize only after buffered raw capture drains.
        if(terminalChunk?.reason.kind==='error'&&(providerError||failure)){const {provider_error,...canonical}=failure??normalizeProviderFailure(providerError,httpStatus,headers);terminalChunk=redactTree({...terminalChunk,reason:{kind:'error',failure:canonical}},host.apiKey);}
        const safeReasoning=redactTree(rawReasoning,host.apiKey);if(safeReasoning.startsWith(reasoning)&&safeReasoning.length>reasoning.length)await emit('response.reasoning',{text:safeReasoning.slice(reasoning.length)});reasoning=safeReasoning;text=redactTree(rawText,host.apiKey);
        const finish=failure?{kind:failure.code==='ABORTED'?'aborted':'error',failure}:terminalChunk?.reason??assembler.finish;
        const blocks=assembler.blocks();const tools=blocks.filter(block=>block.type==='tool-call').map(block=>({id:block.id,type:'function',function:{name:block.name,arguments:block.arguments}}));
        latest={id:requestId,requestId,status:resultStatus(finish,tools.length>0),finish_reason:finishReason(finish),content:text,reasoning_text:reasoning,reasoning_chars:rawReasoningChars,tool_calls:tools,usage:rawUsage??{},usage_missing:rawUsage===undefined,reported_model:reportedModel??null,http_status:httpStatus??null,stream_done:rawDone,raw_capture_complete:rawComplete,seconds:(performance.now()-start)/1000,first_event_seconds:firstEvent,first_content_seconds:firstContent,started_utc:startedUtc,ended_utc:new Date().toISOString(),route};
        if(transportDetail){latest.transport_error_category=transportDetail.tls?'tls':'connection';if(transportDetail.code)latest.transport_error_code=transportDetail.code;latest.transport_error_message=transportDetail.message;}
        const terminalFailure=finish?.failure;
        if(terminalFailure){latest.error_type=terminalFailure.code==='HOST_IO_ERROR'?'HostIOError':'ProviderError';latest.error_code=terminalFailure.code;latest.provider_error=providerError?.error??terminalFailure.provider_error??{code:terminalFailure.code,message:terminalFailure.message};latest.error_message=terminalFailure.message;}
        latest=redactTree(latest,host.apiKey);if(requestId){const {reasoning_text,...record}=latest;await acknowledge('requestEnd',record);}
      }
      if(terminalChunk)yield terminalChunk;
    }
  }
  return new ReviewAdapter();
}

/** One compatibility request through the same mature transport, with no loop or tool dispatch. */
export async function runSingleRequest(spec,host,payload) {
  const adapter=createReviewAdapter({...spec,singleRequestPayload:payload},host);
  const options={provider:'review',model:payload.model,messages:[createUserMessage({content:[{type:'text',text:'single request payload'}]})],maxTokens:payload.max_completion_tokens??payload.max_tokens??spec.route.max_tokens,reasoningEffort:ReasoningEffortId(spec.route.reasoning_effort??'none')};
  try {for await(const _item of adapter.stream(options)){/* The adapter records all canonical and raw output. */}}catch(error){const result=adapter.getLatestResult();if(result)return result;throw error;}
  return adapter.getLatestResult();
}
