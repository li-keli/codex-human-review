const app = document.querySelector('#app');
const token = location.hash.slice(1);
let signature = '', current = null, busy = false;
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// 支持安全的 Markdown 子集：段落、标题、列表、粗体、行内代码及代码块；不执行原始 HTML。
function inline(s) { return esc(s).replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>'); }
function md(s) {
  return String(s ?? '').split(/(```[\s\S]*?```)/g).map(part => {
    if (part.startsWith('```')) return '<pre><code>' + esc(part.replace(/^```[^\n]*\n?/, '').replace(/```$/, '')) + '</code></pre>';
    return part.split(/\n\s*\n/).filter(Boolean).map(block => {
      if (/^#{1,3} /.test(block)) return '<h3>' + inline(block.replace(/^#{1,3} /, '')) + '</h3>';
      if (block.split('\n').every(l => /^[-*] /.test(l))) return '<ul>' + block.split('\n').map(l=>'<li>'+inline(l.slice(2))+'</li>').join('') + '</ul>';
      return '<p>'+inline(block).replace(/\n/g,'<br>')+'</p>';
    }).join('');
  }).join('');
}
async function api(path, body) {
  const r = await fetch('/api/' + path, {signal: AbortSignal.timeout(10000), method: body ? 'POST' : 'GET', headers: {'Authorization':'Bearer '+token,'Content-Type':'application/json'}, body: body ? JSON.stringify(body) : undefined});
  const result = await r.json();
  if (!r.ok) throw Error(result.error || '请求失败');
  return result;
}
function history(answers) {
  return answers.length ? '<details class="history"><summary>最近提交记录 · '+answers.length+' 项</summary>'+answers.map(a=>'<div class="record"><strong>'+esc(a.question)+'</strong><p>'+esc(a.cancelled ? '已暂缓' : (a.labels.join('、') || '自由意见'))+'</p>'+(a.note?'<p>'+esc(a.note)+'</p>':'')+'</div>').join('')+'</details>' : '';
}
function render(d) {
  const q = d.question;
  if (d.status !== 'pending') {
    const messages = {idle:['准备开始','等待 Codex 发布第一个复核事项。'],answered:['决定已提交','答案已保存，Codex 收到结果后会自动关闭此审核标签页。'],paused:['复核已暂缓','当前事项仍未解决。Codex 收到结果后会关闭此审核标签页。'],timed_out:['尚未抉择','无操作倒计时已结束。未提交任何决定，Codex 将关闭此标签页并暂停本轮，等待你处理。'],completed:['本轮复核完成',d.summary || '所有已知事项均已处理。']};
    const [title, text] = messages[d.status] || messages.idle;
    app.innerHTML = '<section class="done"><h1>'+esc(title)+'</h1>'+md(text)+'</section>'+history(d.answers);
    return;
  }
  const table = q.table ? '<section><h2>方案对比</h2><div class="table-wrap"><table><thead><tr>'+q.table.headers.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+q.table.rows.map(row=>'<tr>'+row.map(cell=>'<td>'+inline(cell)+'</td>').join('')+'</tr>').join('')+'</tbody></table></div></section>' : '';
  app.innerHTML = `<header class="review-header"><h1>人工复核</h1><div class="progress">第 ${esc(q.round || 1)} 轮 <span>·</span> 剩余 ${esc(q.remaining || 1)} 项（含本题）</div><p class="subtitle">${esc(q.description || '结合事实与选项说明，作出本轮决定。')}</p></header>
  <article class="question-card"><h2 class="question-title">${esc(q.question)}</h2>
  <div class="context"><section><h2>当前情况</h2>${md(q.context)}</section>
  ${q.impact?'<section><h2>使用影响</h2>'+md(q.impact)+'</section>':''}
  ${q.scenario?'<section class="scenario"><h2>示例场景</h2>'+md(q.scenario)+'</section>':''}
  ${table}${q.evidence?'<details class="evidence"><summary>核对依据</summary>'+md(q.evidence)+'</details>':''}</div>
  <form id="review-form"><h2 class="decision-title">你的决定</h2>
  ${q.recommendation?'<div class="recommendation">'+md(q.recommendation)+'</div>':''}
  <div class="options">${(q.options || []).map((o,i)=>`<div class="option-row" data-option="${esc(o.id)}">
    <div class="option-main"><label class="option"><input type="${q.type==='multi'?'checkbox':'radio'}" name="choice" value="${esc(o.id)}"><div><div class="option-title">${esc(o.label)}${o.recommended?'<span class="badge">推荐</span>':''}</div><div class="option-detail">${md(o.description)}</div></div></label><button class="ask-toggle" type="button" aria-label="追问：${esc(o.label)}" aria-expanded="false">Ask ↗</button></div>
    <div class="ask-panel" hidden><div class="ask-meta">当前会话模型 <span>· 保留上下文继续追问</span></div><div class="ask-history"></div><label class="ask-label" for="ask-${i}">关于「${esc(o.label)}」，你想了解什么？</label><textarea id="ask-${i}" class="ask-input" maxlength="6000" placeholder="例如：这个选择有什么风险？适合哪些场景？"></textarea><div class="ask-bottom"><span class="ask-status" role="status"></span><button type="button" class="ask-send">发送追问 →</button></div></div></div>`).join('')}</div>
  <label class="note-label" for="note">${q.type==='text'?'你的意见':'补充意见（可选，也可直接填写其他决定）'}</label><textarea id="note" maxlength="12000" placeholder="说明你的考虑或需要补充的信息…"></textarea><p id="error" role="alert"></p><div class="form-actions"><button class="secondary" type="button" id="pause">暂缓本项</button><button class="primary" type="submit">提交决定 →</button></div><p class="hint">推荐不预选 · Ask 不代表选择 · 提交后关页 · 无操作 10 分钟自动暂停</p></form></article>${history(d.answers)}`;
  document.querySelector('#review-form').onsubmit = e => {e.preventDefault(); submit(false);};
  document.querySelector('#pause').onclick = () => submit(true);
  document.querySelectorAll('.option-row').forEach(row => {
    row.querySelector('.ask-toggle').onclick = () => {
      const panel = row.querySelector('.ask-panel');
      panel.hidden = !panel.hidden;
      row.classList.toggle('ask-open', !panel.hidden);
      row.querySelector('.ask-toggle').setAttribute('aria-expanded', String(!panel.hidden));
      if(!panel.hidden) row.querySelector('.ask-input').focus();
    };
    row.querySelector('.ask-send').onclick = () => ask(row);
  });
}
function renderAsks(d) {
  const pending = (d.asks || []).some(a=>a.status==='pending');
  document.querySelectorAll('.option-row').forEach(row => {
    const asks = (d.asks || []).filter(a=>a.revision===d.revision && a.option_id===row.dataset.option);
    const target = row.querySelector('.ask-history');
    const key = JSON.stringify(asks);
    if(target.dataset.key!==key){
      target.innerHTML = asks.map(a=>`<div class="ask-message"><div class="ask-user">你 · ${esc(a.prompt)}</div><div class="ask-response">${a.status==='answered'?md(a.response):a.status==='cancelled'?'本题已提交，追问已停止。':'等待当前会话回答…'}</div></div>`).join('');
      target.dataset.key = key;
    }
    row.querySelector('.ask-send').disabled = pending;
  });
}
async function ask(row) {
  const input = row.querySelector('.ask-input');
  const status = row.querySelector('.ask-status');
  if(!input.value.trim()){status.textContent='请先填写追问。';return;}
  row.querySelector('.ask-send').disabled = true;
  try {
    await api('ask',{revision:current.revision,option_id:row.dataset.option,prompt:input.value});
    input.value='';status.textContent='';await refresh(true);
  } catch(e){status.textContent=e.message;row.querySelector('.ask-send').disabled=false;}
}
async function submit(cancelled) {
  if(busy) return;
  const choices = [...document.querySelectorAll('input[name=choice]:checked')].map(x=>x.value);
  const note = document.querySelector('#note').value;
  if(!cancelled && !choices.length && !note.trim()) {document.querySelector('#error').textContent='请选择一个选项，或填写你的意见。'; return;}
  busy = true;
  document.querySelectorAll('.form-actions button').forEach(b=>b.disabled=true);
  try { await api('answer',{revision:current.revision,choices,note,cancelled}); signature=''; await refresh(true); }
  catch(e) { const error = document.querySelector('#error'); if(error)error.textContent=e.message; }
  finally {busy=false;document.querySelectorAll('.form-actions button').forEach(b=>b.disabled=false);}
}
let refreshFlight = null;
function refresh(force = false) {
  if (refreshFlight) return force ? refreshFlight.then(() => refresh(true)) : refreshFlight;
  refreshFlight = loadState().finally(() => { refreshFlight = null; });
  return refreshFlight;
}
async function loadState() {
  try {
    let d = await api('state?since=' + (current?.version ?? -1));
    if(d.unchanged) d = {...current, deadline:d.deadline};
    if(current?.revision===d.revision && current.status==='pending' && d.status==='pending') d.deadline = Math.max(d.deadline, current.deadline || 0);
    current = d;
    document.querySelector('#connection').textContent='● 本地会话已连接';
    // Ask 更新仅替换回答区域，不重建输入框，保留选中项、草稿和光标。
    const key = JSON.stringify([d.revision,d.status,d.question,d.answers,d.summary]);
    if(key!==signature){render(d);signature=key;}
    renderAsks(d);
    countdown();
  } catch(e) {document.querySelector('#connection').textContent='连接中断 · '+e.message;}
}
async function poll() {
  if(!busy) await refresh();
  setTimeout(poll, 1200);
}
poll();

let activityTimer = null, lastActivitySent = 0, activityBusy = false;
function countdown() {
  const badge = document.querySelector('#idle-timer');
  if(!current || current.status !== 'pending'){badge.hidden=true;return;}
  badge.hidden=false;
  const left = Math.max(0, Math.ceil(current.deadline - Date.now()/1000));
  badge.textContent = `${String(Math.floor(left/60)).padStart(2,'0')}:${String(left%60).padStart(2,'0')}`;
  badge.classList.toggle('urgent',left<=30);
  badge.setAttribute('aria-label',`无操作剩余 ${left} 秒`);
}
async function sendActivity() {
  activityTimer=null;
  if(!current || current.status!=='pending' || activityBusy)return;
  activityBusy=true;lastActivitySent=Date.now();
  try {
    const result=await api('activity',{revision:current.revision});
    if(current.status==='pending' && result.revision===current.revision) current.deadline=Math.max(current.deadline,result.deadline);
  } catch(e) {await refresh(true);}
  finally {activityBusy=false;}
}
function onActivity(event) {
  if(!event.isTrusted || !current || current.status!=='pending' || document.hidden)return;
  const urgent = current.deadline-Date.now()/1000 < 2;
  current.deadline=Date.now()/1000+current.idle_seconds;
  countdown();
  clearTimeout(activityTimer);
  if(!activityBusy && (urgent || Date.now()-lastActivitySent>=1000)) sendActivity();
  else activityTimer=setTimeout(sendActivity, Math.max(50,1000-(Date.now()-lastActivitySent)));
}
['pointermove','pointerdown','keydown','input','change','wheel','scroll'].forEach(name=>document.addEventListener(name,onActivity,{passive:true,capture:true}));
setInterval(countdown,250);
