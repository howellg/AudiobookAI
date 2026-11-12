"use strict";
// No third-party libraries, inline event handlers, or HTML injection.
const $ = id => document.getElementById(id);
const getSavedKey = () => sessionStorage.getItem('audiobookai_key') || localStorage.getItem('audiobookai_key') || '';
let key = getSavedKey(), current = null, bookSections = [], busy = false;
const toast = message => { const el = $('toast'); el.textContent = message; el.classList.add('show'); setTimeout(() => el.classList.remove('show'), 4500); };
function setConnected(yes) { $('login').hidden = yes; $('dashboard').hidden = !yes; $('connected').textContent = yes ? 'Connected' : 'Locked'; $('connected').classList.toggle('muted', !yes); }
async function api(path, options = {}) {
  const headers = {Authorization:'Bearer ' + key, ...(options.headers || {})};
  const r = await fetch(path,{...options,headers,cache:'no-store'});
  const out = await r.json();
  if (!r.ok) { if (r.status === 401) setConnected(false); throw Error(out.error || 'Request failed'); }
  return out;
}
async function action(command, extra={}) {
  if (busy) return;
  busy = true;
  try {
    let result = await api('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({command,...extra})});
    toast(result.result?.message || 'Updated successfully');
    await refresh();
  } catch(e) { toast(e.message); }
  finally { busy = false; }
}
function text(tag, value, className) { const el = document.createElement(tag); el.textContent = String(value ?? ''); if(className)el.className=className;return el; }
function btn(label, callback, disabled=false) {const b=text('button',label);b.disabled=disabled;b.addEventListener('click',callback);return b;}
function displayStatus(data) {
  current=data;
  $('active-title').textContent = data.active ? data.active.book + ' — ' + data.active.name : 'Nothing running';
  const total = Number(data.active?.total || 0), done=Number(data.active?.done || 0);
  $('active-detail').textContent = data.active?.section == null ? '' : 'Section ' + data.active.section;
  const exporting = data.export_percent != null;
  const percent = exporting ? data.export_percent : (total > 0 ? Math.min(100, 100 * done / total) : 0);
  $('progress-fill').style.width = percent + '%';
  $('progress-text').textContent = exporting ? percent + '% encoded' :
    (data.active ? done + ' / ' + total + ' chunks' : 'Idle');
  $('eta').textContent = data.active ? data.eta : '—';
  $('queue-mode').textContent = data.active?.status === 'running' ?
    (data.run_queue ? 'Automatic continuation enabled' : 'Finishing current part, then pausing') : '';
  $('queue-status').textContent = data.queue_status || '';
  $('queue-activity').textContent = (data.queue_activity || []).slice().reverse().join('\n');
  $('queue-count').textContent = data.jobs.filter(j=>j.status==='queued').length + ' waiting';
  $('storage').textContent = data.storage;
  $('logs').textContent = (data.log || []).slice(-15).join('\n');
  const container=$('jobs');container.replaceChildren();
  data.jobs.forEach((job,index)=>{
    let row=text('div','','job'), info=text('div','','info');
    info.append(text('b',job.book+' — '+job.name),text('small','#'+job.id+' · '+job.status+' · '+job.progress));
    row.append(info);
    const actions=text('div','','buttons');
    if(job.status==='queued'){
      actions.append(btn('↑',()=>action('move',{job_id:job.id,index:index-1}),index===0),
                     btn('↓',()=>action('move',{job_id:job.id,index:index+1}),index===data.jobs.length-1),
                     btn('Run',()=>{if(confirm('Run only this part?')) action('run_selected',{job_id:job.id});}));
    }else if(['failed','interrupted'].includes(job.status)){
      actions.append(btn('Retry',()=>action('retry',{job_id:job.id})));
    }
    row.append(actions);container.append(row);
  });
  if(!data.jobs.length)container.append(text('p','No queued jobs. Add sections below.','subtle'));
  const bookSelect=$('books'), currentBook=bookSelect.value;
  const wantedBooks=data.books.map(b=>String(b.id)).join('|');
  if(bookSelect.dataset.options!==wantedBooks){
    bookSelect.replaceChildren();
    data.books.forEach(b=>{let o=text('option',b.title+' — '+b.author);o.value=String(b.id);bookSelect.append(o);});
    bookSelect.dataset.options=wantedBooks;
    if(data.books.some(b=>String(b.id)===currentBook))bookSelect.value=currentBook;
  }
  const narrator=$('voice'), old=narrator.value;
  const wantedVoices=data.voices.map(v=>v.id+'|'+v.name).join('\n');
  if(narrator.dataset.options!==wantedVoices){
    narrator.replaceChildren();
    data.voices.forEach(v=>{let o=text('option',v.name);o.value=v.id;narrator.append(o);});
    narrator.dataset.options=wantedVoices;
    if(data.voices.some(v=>v.id===old))narrator.value=old;
  }
}
async function refresh() {
  if (!key) {setConnected(false);return;}
  try {const data=await api('/api/status');setConnected(true);displayStatus(data);}
  catch(e){if(e.message==='Access key required')return; $('connected').textContent='Disconnected';$('connected').classList.add('muted');}
}
$('connect').addEventListener('click', async ()=>{
  key=$('access-key').value.trim();
  try{
    let data=await api('/api/status');
    sessionStorage.setItem('audiobookai_key',key);
    if($('remember').checked)localStorage.setItem('audiobookai_key',key);
    else localStorage.removeItem('audiobookai_key');
    $('login-error').textContent='';setConnected(true);displayStatus(data);
  }catch(e){$('login-error').textContent=e.message;}
});
$('signout').addEventListener('click',()=>{sessionStorage.removeItem('audiobookai_key');localStorage.removeItem('audiobookai_key');key='';setConnected(false);});
$('start').addEventListener('click',()=>action('start'));
$('pause').addEventListener('click',()=>action('pause'));
$('stop').addEventListener('click',()=>{
  if(confirm('Stop the currently generating part? The current chunk may be interrupted, but previously cached audio is kept.'))action('stop_safely');
});
$('clear-all').addEventListener('click',()=>{
  if(confirm('Clear ALL queue entries including unfinished parts? Your saved audio and permanent completion history will remain.'))
    if(prompt('Type CLEAR QUEUE to confirm:')==='CLEAR QUEUE')action('clear_all',{confirmation:'CLEAR QUEUE'});
});
$('clear-completed').addEventListener('click',()=>{if(confirm('Remove completed queue rows? Saved audio and completion history will remain.'))action('clear_completed',{confirmation:true});});
$('storage-check').addEventListener('click',()=>action('storage_check'));
$('add').addEventListener('click',()=>action('add',{book_id:Number($('books').value),ranges:$('ranges').value,
  all_unqueued:$('all-unqueued').checked,batch_size:Number($('batch-size').value),
  acting_mode:$('acting').value,voice:$('voice').value}));
$('load-sections').addEventListener('click',async()=>{
  try {
    const result=await api('/api/book?id='+encodeURIComponent($('books').value));
    bookSections=result.sections;
    $('sections-preview').textContent=bookSections.map(s=>s.index+' · '+s.title+(s.claimed?' [already claimed]':'')).join('\n');
  }catch(e){toast(e.message);}
});
$('upload').addEventListener('click',async()=>{
  const file=$('epub').files[0];
  if(!file){toast('Choose an EPUB first');return;}
  if(file.size>24*1024*1024){toast('Maximum EPUB upload size is 24 MiB');return;}
  try {
    const result=await api('/api/upload',{method:'POST',
      headers:{'Content-Type':'application/epub+zip','X-Filename':encodeURIComponent(file.name)},
      body:file});
    toast(result.result.message); await refresh();
    $('books').value=String(result.result.book_id);
    $('epub').value='';
  }catch(e){toast(e.message);}
});
$('books').addEventListener('change',()=>{$('sections-preview').textContent='';});
window.addEventListener('focus',refresh);
setInterval(()=>{if(document.visibilityState==='visible')refresh();},3500);
setConnected(false);if(key)refresh();
