const form = document.getElementById('qa-form');
    function policyValidityLabel(policy) {
      const now=new Date();
      const today=`${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;
      if(policy.expiry_date && policy.expiry_date<=today)return '已失效';
      if(policy.validity_status==='invalid' && !policy.expiry_date)return '已失效';
      if(policy.effective_date && policy.effective_date>today)return '尚未生效';
      if(policy.validity_status==='unknown')return '效力待核实';
      if(policy.validity_status==='invalid')return '默认有效（已确认未来废止）';
      return policy.validity_status==='current'?'默认有效':'效力待核实';
    }
    const question = document.getElementById('question');
    const asOf = document.getElementById('as-of');
    const submit = document.getElementById('submit');
    const status = document.getElementById('status');
    const panel = document.getElementById('answer-panel');
    const labels = {
      compliant:'符合', non_compliant:'不符合',
      conditionally_compliant:'有条件符合', undetermined:'无法确定'
    };
    let controller=null, conversationId=null, messageId=null, libraryOffset=0, libraryTotal=0, generation=0;
    let activeCitations=[], showAllCitations=false;
    async function api(path,options={}) {
      const response=await fetch(path,options);
      if(response.status===204)return null;
      const data=await response.json();
      if(!response.ok)throw new Error(data.detail||`请求失败（${response.status}）`);
      return data;
    }
    function showTab(tab) {
      const searching=tab==='search';
      const library=tab==='library';
      document.getElementById('tab-qa').setAttribute('aria-selected',String(!searching&&!library));
      document.getElementById('tab-search').setAttribute('aria-selected',String(searching));
      document.getElementById('tab-library').setAttribute('aria-selected',String(library));
      document.getElementById('qa-form').hidden=searching||library;
      document.getElementById('answer-panel').hidden=searching || library || !document.getElementById('answer-panel').dataset.ready;
      document.getElementById('welcome').hidden=searching||library||Boolean(panel.dataset.ready)||Boolean(document.getElementById('loading-panel').dataset.pending);
      document.getElementById('loading-panel').hidden=searching||library||!document.getElementById('loading-panel').dataset.pending;
      document.getElementById('evidence-panel').hidden=searching||library;
      document.getElementById('search-panel').hidden=!searching;
      document.getElementById('library-panel').hidden=!library;
    }
    document.getElementById('tab-qa').addEventListener('click',()=>showTab('qa'));
    document.getElementById('tab-search').addEventListener('click',()=>showTab('search'));
    document.getElementById('tab-library').addEventListener('click',()=>{showTab('library');loadLibrary();});
    async function renderHistory() {
      const host=document.getElementById('history'); host.replaceChildren();
      try { const data=await api('/api/conversations');(data.items||[]).forEach(item=>{
        const li=document.createElement('li'), button=document.createElement('button');
        button.type='button'; button.textContent=item.title||'新对话';
        button.addEventListener('click',()=>loadConversation(item.conversation_id));
        const actions=document.createElement('div');actions.className='small-actions';
        const rename=document.createElement('button');rename.type='button';rename.className='secondary';rename.textContent='重命名';
        rename.addEventListener('click',async()=>{const title=prompt('会话名称',item.title||'');if(!title?.trim())return;try{await api('/api/conversations/'+encodeURIComponent(item.conversation_id),{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:title.trim()})});renderHistory();}catch(error){status.textContent=error.message;}});
        const remove=document.createElement('button');remove.type='button';remove.className='secondary';remove.textContent='删除';
        remove.addEventListener('click',async()=>{if(!confirm('删除这条会话及全部消息？'))return;try{await api('/api/conversations/'+encodeURIComponent(item.conversation_id),{method:'DELETE'});if(conversationId===item.conversation_id)newConversation();renderHistory();}catch(error){status.textContent=error.message;}});
        actions.append(rename,remove);li.append(button,actions);host.appendChild(li);
      });}catch(error){status.textContent=error.message;}
    }
    function newConversation() {generation++;conversationId=null;messageId=null;question.value='';activeCitations=[];document.getElementById('transcript').replaceChildren();document.getElementById('thread-history').hidden=true;document.getElementById('citations').replaceChildren();document.getElementById('citation-detail').hidden=true;document.getElementById('show-all-citations').hidden=true;delete document.getElementById('loading-panel').dataset.pending;panel.hidden=true;delete panel.dataset.ready;showTab('qa');question.focus();}
    document.getElementById('new-conversation').addEventListener('click',newConversation);
    document.querySelectorAll('.examples button').forEach(button=>button.addEventListener('click',()=>{question.value=button.textContent;question.focus();}));
    async function loadConversation(id, fromSubmit=false) {
      if(!fromSubmit)generation++;
      const run=generation;
      try {const data=await api('/api/conversations/'+encodeURIComponent(id));if(run!==generation)return;conversationId=id;messageId=null;showTab('qa');
        const transcript=document.getElementById('transcript');transcript.replaceChildren();
        const messages=data.messages||[];
        const history=document.getElementById('thread-history');history.hidden=messages.length<=1;history.open=false;
        messages.forEach(item=>{const row=document.createElement('article');row.tabIndex=0;row.setAttribute('role','button');row.setAttribute('aria-label','查看回答：'+item.question);const q=document.createElement('strong');q.textContent=item.question;const p=document.createElement('p');p.textContent=item.answer?.answer||'';row.append(q,p);const select=()=>{messageId=item.message_id;question.value=item.question;render(item.answer);};row.addEventListener('click',select);row.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();select();}});transcript.appendChild(row);});
        const last=messages.at(-1);if(last){question.value=last.question;messageId=last.message_id;render(last.answer);}else{panel.hidden=true;delete panel.dataset.ready;showTab('qa');}
      }catch(error){if(run===generation){status.textContent=error.message;delete document.getElementById('loading-panel').dataset.pending;document.getElementById('loading-panel').hidden=true;}}
    }
    renderHistory();

    function setText(id, value) { document.getElementById(id).textContent = value || ''; }
    function fillList(id, sectionId, values) {
      const list = document.getElementById(id); list.replaceChildren();
      const items = Array.isArray(values) ? values : [];
      document.getElementById(sectionId).hidden = items.length === 0;
      items.forEach(value => { const li = document.createElement('li'); li.textContent = value; list.appendChild(li); });
    }
    function renderCitations(citations) {
      const host = document.getElementById('citations'); host.replaceChildren();
      activeCitations=Array.isArray(citations)?citations:[];
      const more=document.getElementById('show-all-citations');
      more.hidden=activeCitations.length<=3||showAllCitations;
      if (!activeCitations.length) { const p=document.createElement('p');p.className='hint';p.textContent='本次没有可引用的制度条款。';host.appendChild(p);document.getElementById('citation-detail').hidden=true;return; }
      (showAllCitations?activeCitations:activeCitations.slice(0,3)).forEach(item=>{
        const row=document.createElement('button');row.type='button';row.className='source-item';row.dataset.evidenceId=item.evidence_id||'';
        const number=document.createElement('small');number.textContent=item.evidence_id||'依据';
        const title=document.createElement('strong');title.textContent=item.title||'未命名制度';
        const meta=document.createElement('small');meta.textContent=item.clause_no||'条款编号未识别';
        row.append(number,title,meta);row.addEventListener('click',()=>selectCitation(item.evidence_id));host.appendChild(row);
      });
      document.getElementById('citation-detail').hidden=true;
    }
    function selectCitation(id) {
      const item=activeCitations.find(value=>value.evidence_id===id);if(!item)return;
      if(activeCitations.indexOf(item)>=3&&!showAllCitations){showAllCitations=true;renderCitations(activeCitations);}
      document.querySelectorAll('.source-item').forEach(row=>row.setAttribute('aria-current',String(row.dataset.evidenceId===id)));
      const host=document.getElementById('citation-detail');host.replaceChildren();host.hidden=false;
      const title=document.createElement('h4');title.textContent=item.title||'未命名制度';
      const meta=document.createElement('p');meta.className='citation-meta';
      const page=item.page_start?(item.page_end&&item.page_end!==item.page_start?`第 ${item.page_start}–${item.page_end} 页`:`第 ${item.page_start} 页`):'页码未知';
      const temporal={applicable:'适用',expired:'已失效',future:'尚未生效',unknown:'效力待核实',unfiltered:'未按日期筛选'};
      meta.textContent=[...(item.chapter_path||[]),item.clause_no||'条款编号未识别',page,temporal[item.temporal_status]||'效力待核实'].join(' · ');
      const quote=document.createElement('blockquote');quote.textContent=item.raw_text||'';
      host.append(title,meta,quote);
      if(item.policy_id){const open=document.createElement('button');open.type='button';open.className='secondary';open.textContent='查看完整制度';open.addEventListener('click',()=>openPolicy(item.policy_id,item.clause_id));host.appendChild(open);}
      if(window.innerWidth<=1100)host.scrollIntoView({behavior:'smooth',block:'nearest'});
    }
    document.getElementById('show-all-citations').addEventListener('click',()=>{showAllCitations=true;renderCitations(activeCitations);});
    function renderAnswerText(value,citations) {
      const host=document.getElementById('answer-text');host.replaceChildren();
      const items=Array.isArray(citations)?citations:[];
      const valid=new Set(items.map(item=>item.evidence_id));
      String(value||'当前没有可展示的回答。').split(/(\[(?:E\d+|\d+)\])/g).forEach(part=>{
        const marker=/^\[(E\d+|\d+)\]$/.exec(part)?.[1];
        const id=marker&&/^\d+$/.test(marker)?'E'+marker:marker;
        if(id && valid.has(id)) { const link=document.createElement('a');link.href='#citation-detail';link.textContent=part;link.addEventListener('click',event=>{event.preventDefault();selectCitation(id);});host.appendChild(link); }
        else host.appendChild(document.createTextNode(part));
      });
    }
    function render(data) {
      panel.hidden=false; panel.dataset.ready='true';
      delete document.getElementById('loading-panel').dataset.pending;document.getElementById('loading-panel').hidden=true;
      panel.scrollTop=0;document.getElementById('evidence-panel').scrollTop=0;
      document.getElementById('welcome').hidden=true;
      setText('conclusion', labels[data.conclusion] || data.conclusion || '无法确定');
      renderAnswerText(data.answer,data.citations);
      const answer=document.getElementById('answer-text');answer.classList.remove('expanded');
      const expand=document.getElementById('expand-answer');expand.textContent='展开完整回答';
      requestAnimationFrame(updateExpandButton);
      const degraded=document.getElementById('degraded'); degraded.hidden=!data.degraded;
      degraded.textContent=data.degraded ? (data.degraded_reason || '当前证据不足，请核对原文或补充问题信息。') : '';
      degraded.className=data.degraded ? 'notice error' : 'notice';
      fillList('conditions','conditions-section',data.conditions);
      fillList('warnings','warnings-section',data.warnings);
      document.getElementById('conditions-section').open=false;
      document.getElementById('warnings-section').open=false;
      showAllCitations=false;
      renderCitations(data.citations);
    }
    function updateExpandButton() {
      const answer=document.getElementById('answer-text');
      document.getElementById('expand-answer').hidden=!answer.classList.contains('expanded')&&answer.scrollHeight<=answer.clientHeight+2;
    }
    window.addEventListener('resize',updateExpandButton);
    document.getElementById('expand-answer').addEventListener('click',()=>{const answer=document.getElementById('answer-text');const expanded=answer.classList.toggle('expanded');document.getElementById('expand-answer').textContent=expanded?'收起回答':'展开完整回答';});
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if(submit.disabled)return;
      const value=question.value.trim();
      if (!value) { status.textContent='请输入问题。'; question.focus(); return; }
      const run=++generation;
      const hadAnswer=Boolean(panel.dataset.ready);
      submit.disabled=true; status.textContent='正在检索制度并生成回答…';
      if(!hadAnswer){const loading=document.getElementById('loading-panel');loading.dataset.pending='true';loading.hidden=false;document.getElementById('welcome').hidden=true;}
      try {
        controller=new AbortController();
        if(!conversationId){const created=await api('/api/conversations',{method:'POST',signal:controller.signal});if(run!==generation)return;conversationId=created.conversation_id;}
        const targetId=conversationId;
        const message=await api('/api/conversations/'+encodeURIComponent(targetId)+'/messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:value,as_of:asOf.value||null}),signal:controller.signal});
        if(run!==generation)return;
        messageId=message.message_id;await loadConversation(targetId,true);
        if(run!==generation)return;
        status.textContent=`适用日期：${message.answer.as_of || '需要补充'}。`;renderHistory();
      } catch (error) {
        if(run===generation)status.textContent=error instanceof Error ? error.message : '请求失败，请稍后重试。';
      } finally { submit.disabled=false; controller=null;if(run===generation){delete document.getElementById('loading-panel').dataset.pending;document.getElementById('loading-panel').hidden=true;if(!panel.dataset.ready)document.getElementById('welcome').hidden=false;} }
    });
    question.addEventListener('keydown', event => {
      if (event.key==='Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); form.requestSubmit(); }
    });
    document.getElementById('retry').addEventListener('click',()=>form.requestSubmit());
    document.getElementById('copy-answer').addEventListener('click',async()=>{
      try { await navigator.clipboard.writeText(document.getElementById('answer-text').textContent); status.textContent='回答已复制。'; }
      catch { status.textContent='复制失败，请手动选择回答文本。'; }
    });
    for(const rating of ['helpful','unhelpful'])document.getElementById(rating).addEventListener('click',async()=>{
      if(!messageId)return;try{await api('/api/messages/'+encodeURIComponent(messageId)+'/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rating})});for(const value of ['helpful','unhelpful'])document.getElementById(value).setAttribute('aria-pressed',String(value===rating));status.textContent='反馈已保存。';}catch(error){status.textContent=error.message;}
    });
    document.getElementById('search-form').addEventListener('submit',async event=>{
      event.preventDefault(); const query=document.getElementById('search-query').value.trim(); if(!query)return;
      const host=document.getElementById('search-results'), state=document.getElementById('search-status');
      host.replaceChildren(); state.textContent='正在检索条款…';
      const params=new URLSearchParams({q:query,top_k:'10'}), date=document.getElementById('search-date').value;
      if(date)params.set('as_of',date);
      try {
        const response=await fetch('/policy-search?'+params);
        const data=await response.json(); if(!response.ok)throw new Error(data.detail||`检索失败（${response.status}）`);
        state.textContent=`找到 ${data.result_count||0} 条结果`;
        (data.results||[]).forEach(item=>{
          const row=document.createElement('article');row.className='search-result';
          const title=document.createElement('h3');title.textContent=item.policy?.title||'未命名制度';
          const meta=document.createElement('small');meta.textContent=[...(item.clause?.chapter_path||[]),item.clause?.clause_no||'条款编号未识别',item.clause?.page_start?`第 ${item.clause.page_start} 页`:''].filter(Boolean).join(' · ');
          const raw=document.createElement('p');raw.textContent=item.clause?.raw_text||'';
          row.append(title,meta,raw);
          if(item.policy?.policy_id){const open=document.createElement('button');open.type='button';open.className='secondary';open.textContent='查看完整制度';open.addEventListener('click',()=>openPolicy(item.policy.policy_id,item.clause?.clause_id));row.appendChild(open);}
          host.appendChild(row);
        });
      } catch(error) { state.textContent=error instanceof Error?error.message:'检索失败，请稍后重试。'; }
    });
    async function loadLibrary() {
      const host=document.getElementById('library-list'), state=document.getElementById('library-status-text');
      const params=new URLSearchParams({q:document.getElementById('library-query').value.trim(),status:document.getElementById('library-status').value,limit:'30',offset:String(libraryOffset)});
      state.textContent='正在加载制度…';host.replaceChildren();document.getElementById('policy-detail').hidden=true;
      try {const data=await api('/api/policies?'+params);libraryTotal=data.total;state.textContent=`共 ${data.total} 份制度`;
        (data.items||[]).forEach(item=>{const row=document.createElement('button');row.type='button';row.className='policy-row';const name=document.createElement('span');name.textContent=item.title||'未命名制度';const meta=document.createElement('small');meta.textContent=[item.issuing_department,policyValidityLabel(item)].filter(Boolean).join(' · ');row.append(name,meta);row.addEventListener('click',()=>openPolicy(item.policy_id));host.appendChild(row);});
        document.getElementById('library-prev').disabled=libraryOffset===0;document.getElementById('library-next').disabled=libraryOffset+30>=libraryTotal;
      }catch(error){state.textContent=error.message;}
    }
    document.getElementById('library-form').addEventListener('submit',event=>{event.preventDefault();libraryOffset=0;loadLibrary();});
    document.getElementById('library-prev').addEventListener('click',()=>{libraryOffset=Math.max(0,libraryOffset-30);loadLibrary();});
    document.getElementById('library-next').addEventListener('click',()=>{if(libraryOffset+30<libraryTotal){libraryOffset+=30;loadLibrary();}});
    async function openPolicy(policyId,clauseId) {
      showTab('library');const host=document.getElementById('policy-detail'), state=document.getElementById('library-status-text');
      host.replaceChildren();host.hidden=false;state.textContent='正在加载制度原文…';
      try {const data=await api('/api/policies/'+encodeURIComponent(policyId));state.textContent='';
        const title=document.createElement('h2');title.textContent=data.policy.title||'未命名制度';host.appendChild(title);
        const meta=document.createElement('p');meta.className='citation-meta';meta.textContent=[data.policy.doc_number,data.policy.issuing_department,data.policy.issue_date?`发布于 ${data.policy.issue_date}`:'',data.policy.effective_date?`生效于 ${data.policy.effective_date}`:'',policyValidityLabel(data.policy)].filter(Boolean).join(' · ');host.appendChild(meta);
        const toc=document.createElement('nav');toc.className='policy-toc';toc.setAttribute('aria-label','制度章节目录');const chapters=new Set();
        (data.clauses||[]).forEach(item=>{const chapter=item.chapter_path?.[0];if(!chapter||chapters.has(chapter))return;chapters.add(chapter);const button=document.createElement('button');button.type='button';button.textContent=chapter;button.addEventListener('click',()=>document.getElementById('clause-'+item.clause_id)?.scrollIntoView({behavior:'smooth',block:'start'}));toc.appendChild(button);});
        if(toc.childElementCount)host.appendChild(toc);
        function renderClause(node, container) {
          const item=node.item, row=document.createElement('article');row.className='policy-clause';row.id='clause-'+item.clause_id;
          const h=document.createElement('h3');h.textContent=node.path.filter(Boolean).join(' · ')||'条款';
          const p=document.createElement('p');p.textContent=item.raw_text||'';row.append(h,p);container.appendChild(row);
          if(node.children.length){const children=document.createElement('div');children.className='policy-clause-children';row.appendChild(children);node.children.forEach(child=>renderClause(child,children));}
        }
        buildPolicyClauseTree(data.clauses||[]).forEach(node=>renderClause(node,host));
        const target=clauseId&&document.getElementById('clause-'+clauseId);if(target)target.classList.add('highlight');(target||host).scrollIntoView({behavior:'smooth',block:'start'});
      }catch(error){state.textContent=error.message;}
    }
