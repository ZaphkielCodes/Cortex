import {test} from 'node:test';
import assert from 'node:assert/strict';
import {applyEnvelope,emptyWorkspace,validateItem,actionRequest,progressMode} from '../agent/ui/workspace-state.js';
const task = {kind:'task',id:'task:t',title:'A task',provenance:{source:'task_db',task_id:'t'},status:'RUNNING',description:'',result:'',
  actions:[{kind:'cancel_task',task_id:'t',action_id:'a'.repeat(32),label:'Cancel task'}]};
const message = (items=[task]) => ({protocol_version:1,epoch:'epoch',revision:1,mode:'snapshot',items,removed:[]});
test('snapshot and incremental update preserve unrelated items',()=>{
  const initial=applyEnvelope(emptyWorkspace(),message());
  const next=applyEnvelope(initial,{...message([{...task,status:'CANCELLED',actions:[]}]),mode:'delta',base_revision:1,revision:2});
  assert.equal(next.items[0].status,'CANCELLED');
  const removed=applyEnvelope(next,{...message([]),mode:'delta',base_revision:2,revision:3,removed:['task:t']});
  assert.deepEqual(removed.items,[]);
});
test('restart snapshot recovers while stale or mismatched delta is rejected',()=>{
  const view=applyEnvelope(emptyWorkspace(),message());
  assert.throws(()=>applyEnvelope(view,{...message(),mode:'delta',base_revision:0}));
  assert.throws(()=>applyEnvelope(view,{...message(),revision:0}));
  assert.equal(applyEnvelope(view,{...message(),epoch:'restart'}).epoch,'restart');
});
test('executable components and malformed payloads are rejected',()=>{
  for(const kind of ['html','script','iframe','form']) assert.throws(()=>applyEnvelope(emptyWorkspace(),message([{...task,kind}])));
  assert.throws(()=>applyEnvelope(emptyWorkspace(),{...message(),protocol_version:2}));
  assert.throws(()=>applyEnvelope(emptyWorkspace(),message([task,task])));
  assert.throws(()=>applyEnvelope(emptyWorkspace(),message([{...task,title:'x'.repeat(241)}])));
});
test('text markup stays data and action payload cannot carry executable fields',()=>{
  const injected={...task,title:'<img src=x onerror=evil()>',command:'run_shell'};
  assert.equal(validateItem(injected),true);
  const result=actionRequest(injected,injected.actions[0]);
  assert.deepEqual(Object.keys(result).sort(),['action_id','kind','task_id']);
  assert.equal(result.kind,'cancel_task');
  assert.throws(()=>actionRequest({...task,status:'COMPLETED'},task.actions[0]));
  assert.throws(()=>actionRequest({...task,actions:[{...task.actions[0],kind:'run_shell'}]},task.actions[0]));
});
test('invalid progress and unbounded timelines do not render',()=>{
  const base={id:'p',title:'Progress',provenance:task.provenance};
  assert.equal(validateItem({...base,kind:'progress',status:'RUNNING',value:null,label:'Unknown total'}),true);
  assert.equal(validateItem({...base,kind:'progress',status:'RUNNING',value:2,label:'bad'}),false);
  assert.equal(validateItem({...base,kind:'timeline',entries:Array(33).fill({at:'',stage:'x',detail:''})}),false);
});

test('new tasks in deltas and updated tasks stay first with grouped details',()=>{
  const older={...task,provenance:{...task.provenance,updated_at:'2026-10-21T10:00:00Z'}};
  const fresh={...task,id:'task:new',provenance:{source:'task_db',task_id:'new',updated_at:'2026-10-22T10:00:00Z'},actions:[]};
  const card={kind:'card',id:'answer:new',title:'Response',text:'Recorded',provenance:fresh.provenance};
  const initial=applyEnvelope(emptyWorkspace(),message([older]));
  const next=applyEnvelope(initial,{...message([fresh,card]),mode:'delta',base_revision:1,revision:2});
  assert.deepEqual(next.items.map(i=>i.id),['task:new','answer:new','task:t']);
  const updated={...older,provenance:{...older.provenance,updated_at:'2026-10-23T10:00:00Z'}};
  const last=applyEnvelope(next,{...message([updated]),mode:'delta',base_revision:2,revision:3});
  assert.deepEqual(last.items.map(i=>i.id),['task:t','task:new','answer:new']);
  assert.deepEqual(applyEnvelope(emptyWorkspace(),{...message(last.items)}).items,last.items);
});

test('failed cancelled paused and unknown tasks never animate as running',()=>{
  for(const status of ['FAILED','CANCELLED','DENIED','PAUSED','UNKNOWN']) assert.equal(progressMode(status),'stopped');
  for(const status of ['PENDING','RUNNING','WAITING']) assert.equal(progressMode(status),'active');
  assert.equal(progressMode('COMPLETED'),'complete');
});
