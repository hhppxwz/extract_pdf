"""制度问答单页界面。"""


def render_policy_qa_page() -> str:
    """返回无需构建工具即可使用的制度问答页面。"""
    return r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>制度问答</title>
  <style>
    :root { --ink:#15171a; --muted:#62676f; --line:#d9dde3; --paper:#fff; --wash:#f7f7f8; --blue:#002fa7; }
    * { box-sizing:border-box; }
    body { margin:0; color:var(--ink); background:var(--paper); font-family:"Helvetica Neue",Arial,"Microsoft YaHei",sans-serif; }
    body::before { content:""; position:fixed; inset:0; pointer-events:none; opacity:.32;
      background-image:linear-gradient(to right,transparent calc(25% - 1px),var(--line) 25%,transparent calc(25% + 1px));
      background-size:100% 100%; }
    .shell { position:relative; width:min(1180px,calc(100% - 40px)); margin:0 auto; }
    header { padding:38px 0 24px; border-bottom:1px solid var(--ink); display:grid; grid-template-columns:3fr 1fr; gap:24px; }
    h1 { margin:0; font-size:clamp(38px,7vw,84px); line-height:.92; letter-spacing:-.055em; font-weight:700; }
    .intro { align-self:end; margin:0; max-width:28rem; color:var(--muted); line-height:1.55; font-size:14px; }
    main { display:grid; grid-template-columns:minmax(0,3fr) minmax(210px,1fr); gap:24px; padding:28px 0 64px; }
    .query { border-top:6px solid var(--blue); padding-top:18px; }
    label { display:block; margin-bottom:8px; font-size:13px; font-weight:700; }
    textarea, input { width:100%; border:1px solid var(--ink); border-radius:0; color:var(--ink); background:var(--paper); font:inherit; }
    textarea { min-height:150px; resize:vertical; padding:16px; font-size:18px; line-height:1.55; }
    input { min-height:46px; padding:10px 12px; }
    textarea:focus, input:focus { outline:3px solid rgba(0,47,167,.18); outline-offset:0; border-color:var(--blue); }
    .form-row { display:grid; grid-template-columns:minmax(180px,240px) 1fr; align-items:end; gap:16px; margin-top:18px; }
    button { min-height:46px; padding:10px 24px; border:1px solid var(--blue); border-radius:0; color:#fff; background:var(--blue); font:700 15px inherit; cursor:pointer; }
    button:hover { background:#001f70; }
    button:disabled { cursor:wait; opacity:.55; }
    .hint, #status { color:var(--muted); font-size:13px; line-height:1.5; }
    #status { min-height:24px; margin:14px 0 0; }
    aside { padding-top:24px; border-top:1px solid var(--ink); }
    aside strong { display:block; margin-bottom:10px; font-size:13px; }
    aside p { margin:0; color:var(--muted); font-size:13px; line-height:1.6; }
    #answer-panel { grid-column:1 / -1; margin-top:26px; border-top:1px solid var(--ink); }
    #answer-panel[hidden] { display:none; }
    .answer-head { display:grid; grid-template-columns:1fr auto; gap:18px; padding:22px 0; border-bottom:1px solid var(--line); }
    .answer-head h2 { margin:0; font-size:28px; letter-spacing:-.025em; }
    .badges { display:flex; flex-wrap:wrap; justify-content:flex-end; gap:8px; }
    .badge { display:inline-flex; align-items:center; min-height:28px; padding:4px 9px; border:1px solid var(--ink); font-size:12px; font-weight:700; }
    .badge.primary { border-color:var(--blue); color:var(--blue); }
    #answer-text { max-width:820px; margin:24px 0; font-size:19px; line-height:1.7; white-space:pre-wrap; }
    .section { padding:20px 0; border-top:1px solid var(--line); }
    .section h3 { margin:0 0 14px; font-size:14px; }
    .section ul { margin:0; padding-left:22px; line-height:1.7; }
    .citation { display:grid; grid-template-columns:70px minmax(0,1fr); gap:18px; padding:20px 0; border-top:1px solid var(--line); }
    .citation-index { color:var(--blue); font-size:28px; font-weight:700; font-variant-numeric:tabular-nums; }
    .citation h4 { margin:0 0 6px; font-size:16px; }
    .citation-meta { margin:0 0 12px; color:var(--muted); font-size:12px; }
    .citation blockquote { margin:0; padding-left:14px; border-left:3px solid var(--blue); line-height:1.65; white-space:pre-wrap; }
    .notice { padding:12px 14px; border-left:4px solid var(--blue); background:var(--wash); line-height:1.55; }
    .notice.error { border-left-color:#b42318; color:#7a271a; }
    @media (max-width:760px) {
      .shell { width:min(100% - 24px,1180px); }
      header, main { grid-template-columns:1fr; }
      .form-row { grid-template-columns:1fr; }
      .answer-head { grid-template-columns:1fr; }
      .badges { justify-content:flex-start; }
      .citation { grid-template-columns:44px minmax(0,1fr); }
      aside { padding-top:0; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <h1>制度问答</h1>
      <p class="intro">根据已入库制度条款形成初步回答，并保留制度名称、条款编号、页码和原文证据。</p>
    </header>
    <main>
      <form id="qa-form" class="query">
        <label for="question">问题</label>
        <textarea id="question" required placeholder="请输入需要查询的制度问题"></textarea>
        <div class="form-row">
          <div>
            <label for="as-of">适用日期（可选）</label>
            <input id="as-of" type="date">
          </div>
          <button id="submit" type="submit">开始查询</button>
        </div>
        <p class="hint">不选择日期时，系统会识别问题中的完整日期；没有时间表达则按当天查询。</p>
        <p id="status" role="status" aria-live="polite"></p>
      </form>
      <aside>
        <strong>使用说明</strong>
        <p>回答是基于当前制度库的初步判断，不替代正式审批。涉及报销、学籍、处分等事项时，请结合引用原文进行人工复核。</p>
      </aside>
      <section id="answer-panel" hidden>
        <div class="answer-head">
          <h2>回答</h2>
          <div class="badges">
            <span id="conclusion" class="badge primary"></span>
            <span id="confidence" class="badge"></span>
            <span id="review" class="badge"></span>
          </div>
        </div>
        <p id="answer-text"></p>
        <div id="degraded" class="notice" hidden></div>
        <div id="conditions-section" class="section" hidden><h3>条件与待确认事项</h3><ul id="conditions"></ul></div>
        <div id="warnings-section" class="section" hidden><h3>提示</h3><ul id="warnings"></ul></div>
        <div class="section"><h3>制度依据</h3><div id="citations"></div></div>
      </section>
    </main>
  </div>
  <script>
    const form = document.getElementById('qa-form');
    const question = document.getElementById('question');
    const asOf = document.getElementById('as-of');
    const submit = document.getElementById('submit');
    const status = document.getElementById('status');
    const panel = document.getElementById('answer-panel');
    const labels = {
      compliant:'符合', non_compliant:'不符合',
      conditionally_compliant:'有条件符合', undetermined:'无法确定'
    };

    function setText(id, value) { document.getElementById(id).textContent = value || ''; }
    function fillList(id, sectionId, values) {
      const list = document.getElementById(id); list.replaceChildren();
      const items = Array.isArray(values) ? values : [];
      document.getElementById(sectionId).hidden = items.length === 0;
      items.forEach(value => { const li = document.createElement('li'); li.textContent = value; list.appendChild(li); });
    }
    function renderCitations(citations) {
      const host = document.getElementById('citations'); host.replaceChildren();
      const items = Array.isArray(citations) ? citations : [];
      if (!items.length) { const p = document.createElement('p'); p.className='hint'; p.textContent='本次没有可引用的制度条款。'; host.appendChild(p); return; }
      items.forEach((item, index) => {
        const row=document.createElement('article'); row.className='citation';
        const number=document.createElement('div'); number.className='citation-index'; number.textContent=String(index+1).padStart(2,'0');
        const body=document.createElement('div');
        const title=document.createElement('h4'); title.textContent=item.title || item.policy_id || '未命名制度';
        const meta=document.createElement('p'); meta.className='citation-meta';
        const range=item.page_end && item.page_end!==item.page_start ? `${item.page_start}–${item.page_end}` : (item.page_start || '未知');
        meta.textContent=[item.clause_no || '条款编号未识别', `第 ${range} 页`, item.temporal_status || 'unknown'].join(' · ');
        const quote=document.createElement('blockquote'); quote.textContent=item.raw_text || '';
        body.append(title,meta,quote); row.append(number,body); host.appendChild(row);
      });
    }
    function render(data) {
      panel.hidden=false;
      setText('conclusion', labels[data.conclusion] || data.conclusion || '无法确定');
      setText('confidence', `置信度：${data.confidence || '未知'}`);
      setText('review', data.requires_human_review ? '需要人工复核' : '无需人工复核');
      setText('answer-text', data.answer || '当前没有可展示的回答。');
      const degraded=document.getElementById('degraded'); degraded.hidden=!data.degraded;
      degraded.textContent=data.degraded ? `降级结果：${data.degraded_reason || '回答生成未完成'}` : '';
      degraded.className=data.degraded ? 'notice error' : 'notice';
      fillList('conditions','conditions-section',data.conditions);
      fillList('warnings','warnings-section',data.warnings);
      renderCitations(data.citations);
    }
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const value=question.value.trim();
      if (!value) { status.textContent='请输入问题。'; question.focus(); return; }
      submit.disabled=true; panel.hidden=true; status.textContent='正在检索制度并生成回答…';
      const payload=new FormData(); payload.append('question',value); if (asOf.value) payload.append('as_of',asOf.value);
      try {
        const response=await fetch('/policy-answer',{method:'POST',body:payload});
        const data=await response.json();
        if (!response.ok) throw new Error(data.detail || `请求失败（${response.status}）`);
        render(data); status.textContent=`适用日期：${data.as_of || '需要补充'}。`;
      } catch (error) {
        status.textContent=error instanceof Error ? error.message : '请求失败，请稍后重试。';
      } finally { submit.disabled=false; }
    });
    question.addEventListener('keydown', event => {
      if ((event.ctrlKey || event.metaKey) && event.key==='Enter') form.requestSubmit();
    });
  </script>
</body>
</html>'''
