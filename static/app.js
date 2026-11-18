/* Same-origin browser client. Tokens use tab-scoped sessionStorage; untrusted content uses textContent. */
const $ = (id) => document.getElementById(id);
const state = { seenUpdates: new Set(), token: null, employee: null, session: null, busy: false, tickets: [], selectedTicket: null, selectedEmployee: null };
let toastTimer;
async function enterWorkspace() {
    state.employee = await api('/me');
    $('account-name').textContent = state.employee.role === 'it_support' ? 'IT support' : 'Employee';
    const support = state.employee.role === 'it_support';
    $('chat-role').textContent = support ? 'IT SUPPORT' : 'EMPLOYEE SUPPORT';
    $('ticket-nav-label').textContent = support ? 'Ticket queue' : 'My tickets';
    $('tickets-heading').textContent = support ? 'Ticket queue' : 'My tickets';
    $('tickets-description').textContent = support ? 'Review employee issues and update their status.' : 'Your submitted and automatically escalated issues.';
    $('login-screen').hidden = true; $('workspace').hidden = false; newConversation(false);
    await Promise.all([refreshSessions(),refreshTickets()]);
    const saved = sessionStorage.getItem(`active-session:${state.employee.id}`);
    if (saved) await loadSession(saved);
}


function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function statusLabel(value) { return ({open: 'Open', in_progress: 'In progress', resolved: 'Resolved'})[value] || value; }
function dateLabel(value) {
  const date = new Date(value.replace(' ', 'T') + 'Z');
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString(undefined, {month:'short', day:'numeric', year:'numeric'});
}
function notify(message) {
  $('toast').textContent = message; $('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('toast').hidden = true; }, 5000);
}
function showError(id, message) { $(id).textContent = message; $(id).hidden = !message; }
async function api(path, options = {}) {
  const headers = { ...(options.body ? {'Content-Type':'application/json'} : {}), ...(state.token ? {Authorization:`Bearer ${state.token}`} : {}), ...options.headers };
  const response = await fetch(path, {...options, headers});
  const data = response.status === 204 ? null : await response.json();
  if (!response.ok) {
    if (response.status === 401 && state.token) signOutLocal();
    const detail = typeof data?.detail === 'string' ? data.detail : `Request failed (${response.status}). Check the entered values.`;
    throw new Error(detail);
  }
  return data;
}
function signOutLocal() {
  if (state.employee) sessionStorage.removeItem(`active-session:${state.employee.id}`);
  sessionStorage.removeItem("support-token");
  state.token = null; state.employee = null; state.session = null;
  $('workspace').hidden = true; $('login-screen').hidden = false;
  if ($('ticket-dialog').open) $('ticket-dialog').close();
  $('password').value = ''; $('messages').replaceChildren(); $('session-list').replaceChildren();
  $('draft-list').replaceChildren(); $('tool-list').replaceChildren(); $('source-list').replaceChildren();
  $('ticket-rows').replaceChildren(); state.tickets = [];
}
function setBusy(value) {
  state.busy = value;
  for (const id of ['send','new-chat','logout','message-input']) $(id).disabled = value;
  $('send').textContent = value ? 'Working…' : 'Send message ↑';
  document.querySelectorAll('.session-button, .draft-card button, [data-prompt]').forEach(button => { button.disabled = value; });
}
function showView(view) {
  const chat = view === 'chat';
  $('chat-view').hidden = !chat; $('inspector').hidden = !chat; $('tickets-view').hidden = chat;
  $('nav-chat').classList.toggle('active', chat); $('nav-tickets').classList.toggle('active', !chat);
  $('page-title').textContent = chat ? 'Assistant' : (state.employee?.role === 'it_support' ? 'Ticket queue' : 'My tickets');
}
const welcomeTemplate = $('welcome').cloneNode(true);
function newConversation(clearSaved = true) {
  if (state.busy) return;
  state.session = null;
  if (clearSaved && state.employee) sessionStorage.removeItem(`active-session:${state.employee.id}`);
  $('messages').replaceChildren(welcomeTemplate.cloneNode(true));
  $('draft-list').replaceChildren(); renderTools([], []); $('run-summary').textContent = 'No request yet';
  $('message-input').value = ''; showError('chat-error',''); showView('chat');
  showDemoSymptoms();
  document.querySelectorAll('.session-button').forEach(button => button.classList.remove('selected'));
  $('message-input').focus();
}
function addMessage(role, text, meta='') {
  $('welcome')?.remove();
  const block = element('article', `message ${role}`);
  block.append(element('div','message-label',role === 'user' ? 'YOU' : role === 'update' ? 'TICKET UPDATE' : role === 'system' ? 'APPLICATION CONFIRMATION' : 'SUPPORT AGENT'));
  // Source limitations are shown beside the relevant evidence.
  const clean = text.replace(/^\[SIMULATED DEMO — not live company data\]\s*/, '');
  block.append(element('div','message-body',clean));
  const resetLink = role === 'assistant' ? clean.match(/\/corporate-password#token=[\w-]+/) : null;
  if (resetLink) {
    // One-time link: shown on this response only; the saved transcript stores a redacted copy.
    const link = element('a', 'primary', 'Open my password reset link ↗'); link.href = resetLink[0]; link.target = '_blank'; link.rel = 'noopener noreferrer'; block.append(link);
  } else if (role === 'assistant' && text.includes('/reset-password')) {
    const link = element('a', 'secondary', 'Open password page'); link.href = '/reset-password'; link.target = '_blank'; link.rel = 'noopener noreferrer'; block.append(link);
  }
  if (meta) block.append(element('div','message-meta',meta));
  $('messages').append(block); $('messages').scrollTop = $('messages').scrollHeight;
  return block;
}
async function refreshSessions() {
  const sessions = await api('/sessions');
  $('session-list').replaceChildren();
  if (!sessions.length) $('session-list').append(element('p','empty-copy','Your conversations will appear here.'));
  for (const session of sessions) {
    const button = element('button',`session-button${session.id === state.session ? ' selected' : ''}`,session.title);
    button.title = session.title; button.disabled = state.busy;
    button.addEventListener('click', () => loadSession(session.id));
    $('session-list').append(button);
  }
}
async function loadSession(id) {
  if (state.busy) return;
  setBusy(true); showError('chat-error','');
  try {
    const data = await api(`/sessions/${encodeURIComponent(id)}`);
    state.session = id; sessionStorage.setItem(`active-session:${state.employee.id}`,id);
    if ($('history-dialog').open) $('history-dialog').close();
    $('messages').replaceChildren(); showView('chat');
    const tools = []; const sources = new Set();
    for (const message of data.messages) {
      if (message.role === 'user') addMessage('user',message.content || '');
      if (message.kind === 'ticket_update') { state.seenUpdates.add(message.uid); addMessage('update',message.content); continue; }
      if (message.role === 'assistant' && message.content && !message.tool_calls?.length) {
        addMessage(message.content.startsWith('Application confirmation:') ? 'system' : 'assistant',message.content);
      }
      if (message.role === 'tool') {
        let result;
        try { result = JSON.parse(message.content); } catch { result = {content:message.content}; }
        tools.push({tool:message.tool_name, result});
        if (message.tool_name === 'search_knowledge_base') (result.results || []).forEach(doc => sources.add(doc.source));
      }
    }
    renderTools(tools,[...sources]); renderDrafts(data.pending_tickets || []); renderReceipts(data.created_tickets || []);
    $('run-summary').textContent = `Saved conversation · ${tools.length} recorded tool call${tools.length === 1 ? '' : 's'}`;
    await refreshSessions();
  } catch (error) { showError('chat-error',error.message); }
  finally { setBusy(false); $('message-input').focus(); }
}
function renderTools(tools, sources) {
  $('tool-list').replaceChildren(); $('source-list').replaceChildren();
  if (!tools.length) $('tool-list').append(element('p','empty-copy','No tool calls recorded for this response. The answer has no new tool evidence.'));
  for (const item of tools) {
    const card = element('details','tool-card');
    if (item.tool === 'check_service_status') card.append(element('p','small muted','Source: sample status records; live monitoring is not connected.'));
    const argText = item.arguments && Object.keys(item.arguments).length ? ' ' + JSON.stringify(item.arguments).slice(0, 70) : '';
    card.append(element('summary','',`${item.result.error ? '!' : '✓'}  ${item.tool}${argText}`));
    card.append(element('pre','',JSON.stringify(item.result,null,2)));
    $('tool-list').append(card);
  }
  if (!sources.length) $('source-list').append(element('p','empty-copy','No documents retrieved for this response.'));
  sources.forEach(source => $('source-list').append(element('div','source-chip',source)));
}
function renderDrafts(drafts) {
  $('draft-list').replaceChildren();
  for (const draft of drafts) {
    const card = element('section','draft-card');
    card.append(element('span','pill','DRAFT · NOT SUBMITTED'));
    card.append(element('h4','',draft.title)); card.append(element('p','',draft.description));
    const button = element('button','primary','Confirm & create ticket');
    button.type = 'button'; button.disabled = state.busy;
    const errorBox = element('p','error');
    button.addEventListener('click',async () => {
      if (state.busy) return;
      setBusy(true); button.textContent = 'Creating…'; errorBox.textContent = '';
      try {
        const ticket = await api(`/ticket-drafts/${encodeURIComponent(draft.id)}/confirm`,{method:'POST'});
        card.replaceChildren(element('span','pill','TICKET CREATED'),element('h4','',`#${ticket.id} · ${ticket.title}`),element('p','',`Status: ${statusLabel(ticket.status)}. Saved to the local support queue.`));
        addMessage('system',`Ticket #${ticket.id} was created: ${ticket.title}. Status: ${statusLabel(ticket.status)}.`);
        notify(`Ticket #${ticket.id} created. Find it in ${state.employee.role === 'it_support' ? 'Ticket queue' : 'My tickets'}.`);
        await refreshTickets();
      } catch(error) { errorBox.textContent = error.message; button.textContent = 'Confirm & create ticket'; }
      finally { setBusy(false); }
    });
    card.append(button,errorBox,element('small','', 'Review the title and description above. Only these details will be saved. To change them, ask the assistant for a corrected draft.'));
    $('draft-list').append(card);
  }
}
async function refreshTickets() {
  state.tickets = await api('/tickets');
  $('ticket-count').textContent = state.tickets.length;
  renderTickets();
}
function renderTickets() {
  $('ticket-stats').replaceChildren();
  for (const status of ['open','in_progress','resolved']) {
    const card = element('div','stat-card');
    card.append(element('span','stat-label',statusLabel(status)),element('span','stat-number',state.tickets.filter(ticket => ticket.status === status).length));
    $('ticket-stats').append(card);
  }
  const query = $('ticket-search').value.trim().toLowerCase();
  const filter = $('ticket-filter').value;
  const tickets = state.tickets.filter(ticket => (filter === 'all' || ticket.status === filter) && (`${ticket.id} ${ticket.title}`.toLowerCase().includes(query)));
  $('ticket-rows').replaceChildren(); $('tickets-empty').hidden = !!tickets.length;
  for (const ticket of tickets) {
    const row = element('tr'); const idCell = element('td');
    const link = element('button','ticket-link',`#${ticket.id}`); link.addEventListener('click',() => openTicket(ticket.id)); idCell.append(link);
    const titleCell = element('td');
    const open = element('button','ticket-link',`Open: ${ticket.title}`);
    open.addEventListener('click',() => openTicket(ticket.id)); titleCell.append(open);
    const status = element('td'); status.append(element('span',`status ${ticket.status}`,statusLabel(ticket.status)));
    row.append(idCell,titleCell,element('td','',ticket.employee_id),status,element('td','',dateLabel(ticket.created_at)));
    $('ticket-rows').append(row);
  }
}
async function openTicket(id) {
  try {
    const ticket = await api(`/tickets/${id}`); state.selectedTicket = ticket.id; state.selectedEmployee = ticket.employee_id;
    $("recovery-actions").hidden = state.employee.role !== "it_support";
    $("identity-verified").checked = false; $("reset-link-result").replaceChildren();
    $('ticket-detail-id').textContent = `TICKET #${ticket.id}`; $('ticket-detail-title').textContent = ticket.title;
    $('ticket-detail-meta').textContent = `${ticket.employee_id} · ${dateLabel(ticket.created_at)} · ${statusLabel(ticket.status)}`;
    $('ticket-detail-description').textContent = ticket.description;
    $('ticket-estimate').textContent = ticket.status === 'resolved' ? `Resolved · last updated ${ticket.updated_at || ticket.created_at}` : ticket.resolution_estimate ? `Initial complexity: ${ticket.complexity}. Provisional target: ${ticket.resolution_estimate}. Configured target; technician assessment required, not a guaranteed deadline.` : 'Resolution estimate: awaiting technician assessment.';
    $('support-actions').hidden = state.employee.role !== 'it_support'; $('ticket-status').value = ticket.status;
    $('ticket-detail-error').textContent = ''; $('ticket-dialog').showModal();
  } catch(error) { showError('tickets-error',error.message); }
}
$('login-form').addEventListener('submit',async event => {
  event.preventDefault(); $('login-button').disabled = true; $('login-error').textContent = '';
  try {
    const login = await api('/auth/login',{method:'POST',body:JSON.stringify({employee_id:$('employee-id').value.trim(),password:$('password').value})});
    state.token = login.access_token; $('password').value = '';
    sessionStorage.setItem("support-token", state.token);
    await enterWorkspace();
  } catch(error) { $('login-error').textContent = error.message; if(state.token) signOutLocal(); }
  finally { $('login-button').disabled = false; }
});
$('logout').addEventListener('click',async () => { try { await api('/auth/logout',{method:'POST'}); } finally { signOutLocal(); } });
$('new-chat').addEventListener('click',() => newConversation());
$('nav-chat').addEventListener('click',() => showView('chat'));
$('nav-tickets').addEventListener('click',async () => {showView('tickets'); try{await refreshTickets();}catch(error){showError('tickets-error',error.message);}});
$('refresh-tickets').addEventListener('click',async () => {try{await refreshTickets();showError('tickets-error','');}catch(error){showError('tickets-error',error.message);}});
$('ticket-search').addEventListener('input',renderTickets); $('ticket-filter').addEventListener('change',renderTickets);
$('close-ticket').addEventListener('click',() => $('ticket-dialog').close());
$('save-ticket').addEventListener('click',async () => {
  $('save-ticket').disabled = true;
  try {
    await api(`/tickets/${state.selectedTicket}`,{method:'PATCH',body:JSON.stringify({status:$('ticket-status').value})});
    $('ticket-dialog').close(); await refreshTickets(); notify('Ticket status updated.');
  } catch(error) { $('ticket-detail-error').textContent = error.message; }
  finally { $('save-ticket').disabled = false; }
});
$('messages').addEventListener('click',event => {
  const button = event.target.closest('[data-prompt]');
  if(button && !state.busy){$('message-input').value = button.dataset.prompt; $('message-input').focus();}
});
$('message-input').addEventListener('keydown',event => {
  if(event.key === 'Enter' && !event.shiftKey && !event.isComposing){event.preventDefault(); if(!state.busy) $('chat-form').requestSubmit();}
});
$('chat-form').addEventListener('submit',async event => {
  event.preventDefault(); const question = $('message-input').value.trim(); if(!question || state.busy) return;
  setBusy(true); showError('chat-error',''); addMessage('user',question); $('message-input').value = '';
  const pending = addMessage('pending','Starting. Local model requests can take a minute or two.');
  pending.querySelector('.message-label').prepend(element('span','spinner'));
  const startedAt = Date.now();
  const poll = setInterval(async () => {
    try {
      const progress = await api('/chat/progress');
      const seconds = Math.round((Date.now() - startedAt) / 1000);
      if (progress.steps.length) pending.querySelector('.message-body').textContent = progress.steps.map(step => '• ' + step.text).join('\n') + `\n\n${seconds}s elapsed`;
    } catch (_) { /* progress is a convenience; the chat request itself reports real errors */ }
  }, 1500);
  $('run-summary').textContent = 'Request in progress · results will appear when it completes';
  try {
    const result = await api('/chat',{method:'POST',body:JSON.stringify({message:question,session_id:state.session})});
    state.session = result.session_id; sessionStorage.setItem(`active-session:${state.employee.id}`, state.session); pending.remove();
    const engine = {pipeline: result.reply_source === 'model' ? 'agent pipeline + model reply' : 'agent pipeline', model:'AI agent (model loop)', playbook:'playbook engine', 'playbook-fallback':'playbook engine (model unreachable)', offline:'agent offline'}[result.routing] || result.routing;
    addMessage('assistant',result.answer);
    if (result.client_action === 'logout') setTimeout(() => $('logout').click(), 1500);
    renderTools(result.tool_results,result.sources); renderDrafts(result.pending_tickets);
    renderReceipts(result.created_tickets || []);
    await refreshTickets();
    $('run-summary').textContent = `${result.outcome.replaceAll('_',' ')} · ${engine}${result.model_calls ? ` · ${result.model_calls} model steps` : ''} · ${(result.duration_ms/1000).toFixed(1)}s · ${result.tool_results.length} tool calls · ${result.usage.input_tokens + result.usage.output_tokens} tokens${result.fallback_reason ? ' · ' + result.fallback_reason : ''}${result.reply_fallback ? ' · built-in reply used: ' + result.reply_fallback : ''}`;
    await refreshSessions();
  } catch(error) {
    pending.remove(); showError('chat-error',`${error.message} Check the saved conversation before retrying.`);
    $('message-input').value = question; $('run-summary').textContent = 'Request failed · no new evidence available';
  } finally {clearInterval(poll); setBusy(false); $('message-input').focus();}
});

$('issue-reset').addEventListener('click', async () => {
  if (!$('identity-verified').checked) { $('ticket-detail-error').textContent = 'Verify the employee’s identity first.'; return; }
  $('issue-reset').disabled = true;
  try {
    const data = await api('/auth/reset-link', {method:'POST', body:JSON.stringify({employee_id:state.selectedEmployee,identity_verified:true})});
    const link = element('a', '', window.location.origin + data.reset_path);
    link.href = data.reset_path; link.target = '_blank'; link.rel = 'noopener noreferrer';
    $('reset-link-result').replaceChildren(element('p','','One use · expires in 15 minutes. Copy and deliver privately to the verified employee.'),link);
    $('ticket-detail-error').textContent = '';
  } catch(error) { $('ticket-detail-error').textContent = error.message; }
  finally { $('issue-reset').disabled = false; }
});

(async function restoreLogin() {
  state.token = sessionStorage.getItem('support-token');
  if (!state.token) return;
  $('login-button').disabled = true;
  try { await enterWorkspace(); }
  catch(error) { signOutLocal(); $('login-error').textContent = error.message; }
  finally { $('login-button').disabled = false; }
})();

function renderReceipts(tickets) {
  for (const ticket of tickets) {
    const card = element('section','draft-card');
    card.append(element('span','pill',`TICKET #${ticket.id} · ${statusLabel(ticket.status)}`),element('h4','',ticket.title));
    const button = element('button','secondary','Open submitted ticket');
    button.addEventListener('click',() => openTicket(ticket.id)); card.append(button);
    $('draft-list').append(card);
  }
}
$('show-history').addEventListener('click',async () => {
  if(state.busy) return;
  try {
    const sessions = await api('/sessions');
    $('mobile-history').replaceChildren();
    for(const session of sessions) {
      const button=element('button','session-button',session.title);
      button.addEventListener('click',()=>loadSession(session.id));
      $('mobile-history').append(button);
    }
    if(!sessions.length) $('mobile-history').append(element('p','','No saved conversations.'));
    $('history-dialog').showModal();
  } catch(error) {showError('chat-error',error.message);}
});
$('close-history').addEventListener('click',() => $('history-dialog').close());

async function showDemoSymptoms() {
  // Demo testing aid: shows what this employee is experiencing, never the cause.
  if (!state.employee || state.employee.role !== 'employee') return;
  let data;
  try { data = await api('/demo/symptoms'); } catch { return; }
  const welcome = $('welcome'); if (!welcome) return;
  welcome.querySelector('.demo-symptoms')?.remove();
  const box = element('div', 'demo-symptoms');
  box.append(element('strong', '', 'Demo round: what you are experiencing'));
  if (!data.symptoms.length) box.append(element('p', '', 'Nothing is wrong for you this round. Try another employee, or report a problem anyway to see how the agent handles a healthy system.'));
  else { const list = element('ul'); data.symptoms.forEach(s => list.append(element('li', '', s))); box.append(list);
         box.append(element('p', '', 'Describe it in your own words. The cause is hidden.')); }
  welcome.querySelector('.suggestions')?.before(box);
}

/* Employees see technician status changes live: new "ticket update" messages are appended to the open chat. */
async function pollTicketUpdates() {
  if (!state.employee || state.employee.role !== 'employee' || state.busy || document.hidden) return;
  try {
    const before = Object.fromEntries(state.tickets.map(ticket => [ticket.id, ticket.status]));
    await refreshTickets();
    for (const ticket of state.tickets) {
      if (before[ticket.id] && before[ticket.id] !== ticket.status) notify(`Ticket #${ticket.id} is now ${statusLabel(ticket.status)}.`);
    }
    if (!state.session) return;
    const data = await api(`/sessions/${encodeURIComponent(state.session)}`);
    for (const message of data.messages) {
      if (message.kind === 'ticket_update' && !state.seenUpdates.has(message.uid)) {
        state.seenUpdates.add(message.uid); addMessage('update', message.content);
      }
    }
  } catch (_) { /* a missed poll is harmless; the next one retries */ }
}
setInterval(pollTicketUpdates, 5000);
