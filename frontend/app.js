const form = document.getElementById('chat-form');
const promptInput = document.getElementById('prompt');
const threadEl = document.getElementById('thread');
const examplesEl = document.getElementById('examples');
const statusEl = document.getElementById('status');
const sendBtn = document.getElementById('send-btn');
const newChatBtn = document.getElementById('new-chat-btn');
const signOutBtn = document.getElementById('sign-out-btn');
const userNameEl = document.getElementById('user-name');
const signInForm = document.getElementById('sign-in-form');
const accessKeyInput = document.getElementById('access-key');
const signInError = document.getElementById('sign-in-error');

const KEY_STORAGE = 'shopping-assistant-access-key';

// Follow-up messages carry the signed session token from the first reply, so the agent
// remembers what it already found.
let sessionToken = null;
let accessKey = null;
let busy = false;

// The key is remembered in this browser only as a convenience; it still works if storage is blocked.
function loadKey() {
  try {
    return localStorage.getItem(KEY_STORAGE);
  } catch {
    return null;
  }
}

function saveKey(key) {
  try {
    if (key) localStorage.setItem(KEY_STORAGE, key);
    else localStorage.removeItem(KEY_STORAGE);
  } catch {
    // Storage unavailable: the key lasts until the page is closed.
  }
}

function authHeaders() {
  return { Authorization: `Bearer ${accessKey}` };
}

function showApp(user) {
  document.body.classList.remove('signed-out');
  userNameEl.textContent = user;
  promptInput.focus();
}

function showSignIn(message = '') {
  accessKey = null;
  saveKey(null);
  resetChat();
  document.body.classList.add('signed-out');
  userNameEl.textContent = '';
  signInError.textContent = message;
  signInError.hidden = !message;
  accessKeyInput.value = '';
  accessKeyInput.focus();
}

// Returns the user's name if the key is valid, or null if it was rejected.
async function checkKey(key) {
  const response = await fetch('/api/me', { headers: { Authorization: `Bearer ${key}` } });
  if (response.status === 401) return null;
  if (!response.ok) throw await errorFromResponse(response);
  return (await response.json()).user;
}

function siteName(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return url;
  }
}

// Every link the agent gives opens in a new tab and passes no referrer.
// A bare URL as link text is shortened to its site name, so long store URLs don't flood the reply.
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') {
    node.setAttribute('target', '_blank');
    node.setAttribute('rel', 'noopener noreferrer nofollow');
    const label = node.textContent.trim();
    if (/^https?:\/\//i.test(label)) {
      node.textContent = siteName(label);
      node.title = label;
    }
  }
});

function renderMarkdown(text) {
  return DOMPurify.sanitize(marked.parse(text, { gfm: true, breaks: true }));
}

function setStatus(mode, text) {
  statusEl.className = `status ${mode}`;
  statusEl.textContent = text;
}

function setBusy(isBusy) {
  busy = isBusy;
  sendBtn.disabled = isBusy;
  newChatBtn.disabled = isBusy;
  document.querySelectorAll('.chip').forEach((chip) => (chip.disabled = isBusy));
}

function scrollToBottom() {
  threadEl.scrollTop = threadEl.scrollHeight;
}

function addMessage(role) {
  const el = document.createElement('div');
  el.className = `message ${role}`;
  threadEl.append(el);
  scrollToBottom();
  return el;
}

// Each step is described as present-tense (shown while it runs) and past-tense (kept in the list).
function describeTool(payload) {
  if (payload.name === 'web_search') {
    const query = payload.detail ? `“${payload.detail}”` : 'the web';
    return { now: `Searching ${query}`, done: `Searched ${query}` };
  }
  if (payload.name === 'web_fetch') {
    const site = payload.detail ? payload.detail.replace(/^www\./, '') : 'a page';
    return { now: `Reading ${site}`, done: `Read ${site}` };
  }
  return { now: `Using ${payload.name}`, done: `Used ${payload.name}` };
}

// Turns low-level failures into something a shopper can act on.
function friendlyError(message) {
  if (/failed to fetch|networkerror|load failed/i.test(message)) {
    return "Couldn't reach the assistant. Check your connection and try again.";
  }
  return message;
}

// Builds an assistant reply: a collapsible list of research steps, then the answer.
function createReply() {
  const el = addMessage('assistant');

  const progress = document.createElement('details');
  progress.className = 'progress';
  const summary = document.createElement('summary');
  const spinner = document.createElement('span');
  spinner.className = 'spinner';
  const summaryText = document.createElement('span');
  summaryText.textContent = 'Starting research…';
  summary.append(spinner, summaryText);
  const steps = document.createElement('ol');
  steps.className = 'steps';
  progress.append(summary, steps);

  const answer = document.createElement('div');
  answer.className = 'markdown';
  el.append(progress, answer);

  let searches = 0;
  let pages = 0;

  return {
    el,
    answer,
    addStep(text, kind) {
      const li = document.createElement('li');
      li.className = kind;
      li.textContent = text;
      steps.append(li);
    },
    addTool(payload) {
      const step = describeTool(payload);
      this.addStep(step.done, 'tool');
      summaryText.textContent = `${step.now}…`;
      if (payload.name === 'web_search') searches += 1;
      else if (payload.name === 'web_fetch') pages += 1;
    },
    setCurrent(text) {
      summaryText.textContent = text;
    },
    finish() {
      spinner.remove();
      if (!steps.children.length) {
        progress.remove();
        return;
      }
      const parts = [];
      if (searches) parts.push(`${searches} search${searches === 1 ? '' : 'es'}`);
      if (pages) parts.push(`${pages} page${pages === 1 ? '' : 's'} read`);
      summaryText.textContent = parts.length ? `Researched: ${parts.join(', ')}` : 'Research steps';
    },
  };
}

async function errorFromResponse(response) {
  let detail = '';
  try {
    const body = await response.json();
    detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? body);
  } catch {
    // Not JSON; fall back to the status code.
  }
  const error = new Error(detail || `Request failed (${response.status})`);
  error.status = response.status;
  return error;
}

async function send(prompt) {
  if (busy || !prompt) return;
  examplesEl.hidden = true;

  addMessage('user').textContent = prompt;
  const reply = createReply();

  // Text the agent writes before a tool call is narration ("Let me check Flipkart"), so it moves
  // into the research steps. The text after the last tool call is the answer.
  let text = '';
  let failed = false;
  let finished = false;
  setStatus('running', 'Researching…');
  setBusy(true);

  const showError = (message) => {
    failed = true;
    const err = document.createElement('p');
    err.className = 'error';
    err.textContent = friendlyError(message);
    reply.el.append(err);
    setStatus('error', 'Something went wrong');
  };

  try {
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(sessionToken ? { prompt, session: sessionToken } : { prompt }),
    });
    if (response.status === 401) {
      showSignIn('Your access key is no longer valid. Sign in again.');
      return;
    }
    if (!response.ok) throw await errorFromResponse(response);
    if (!response.body) throw new Error('Response has no body to stream');

    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split('\n\n');
      buffer = chunks.pop() || '';

      for (const chunk of chunks) {
        const line = chunk.split('\n').find((l) => l.startsWith('data: '));
        if (!line) continue;
        const payload = JSON.parse(line.slice(6));

        if (payload.type === 'session') {
          sessionToken = payload.session;
        } else if (payload.type === 'text') {
          text += (text ? '\n\n' : '') + payload.text;
          reply.answer.innerHTML = renderMarkdown(text);
          reply.setCurrent('Writing the answer…');
        } else if (payload.type === 'tool') {
          if (text) {
            reply.addStep(text.trim(), 'note');
            text = '';
            reply.answer.innerHTML = '';
          }
          reply.addTool(payload);
        } else if (payload.type === 'error') {
          showError(payload.error);
        } else if (payload.type === 'done') {
          finished = true;
        }
        scrollToBottom();
      }
    }

    if (!finished && !failed) showError('The connection dropped before the answer was finished. Please try again.');
  } catch (error) {
    showError(error.message);
  } finally {
    reply.finish();
    if (!failed) setStatus('idle', 'Ready');
    setBusy(false);
    scrollToBottom();
    promptInput.focus();
  }
}

form.addEventListener('submit', (event) => {
  event.preventDefault();
  const prompt = promptInput.value.trim();
  if (!prompt || busy) return;
  promptInput.value = '';
  send(prompt);
});

// Enter sends; Shift+Enter adds a new line.
promptInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

examplesEl.addEventListener('click', (event) => {
  const chip = event.target.closest('.chip');
  if (chip) send(chip.textContent.trim());
});

function resetChat() {
  sessionToken = null;
  threadEl.querySelectorAll('.message').forEach((el) => el.remove());
  examplesEl.hidden = false;
  setStatus('idle', 'Ready');
}

newChatBtn.addEventListener('click', () => {
  resetChat();
  promptInput.focus();
});

signOutBtn.addEventListener('click', () => showSignIn());

signInForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const key = accessKeyInput.value.trim();
  if (!key) return;
  const button = signInForm.querySelector('button');
  button.disabled = true;
  try {
    const user = await checkKey(key);
    if (!user) {
      signInError.textContent = "That access key wasn't recognised. Check it and try again.";
      signInError.hidden = false;
      return;
    }
    accessKey = key;
    saveKey(key);
    showApp(user);
  } catch (error) {
    signInError.textContent = friendlyError(error.message);
    signInError.hidden = false;
  } finally {
    button.disabled = false;
  }
});

// On load, reuse a remembered key if the server still accepts it.
(async () => {
  const saved = loadKey();
  if (!saved) return showSignIn();
  try {
    const user = await checkKey(saved);
    if (!user) return showSignIn('Your access key is no longer valid. Sign in again.');
    accessKey = saved;
    showApp(user);
  } catch (error) {
    showSignIn(friendlyError(error.message));
  }
})();
