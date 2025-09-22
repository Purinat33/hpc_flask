// static/js/copilot-widget.js
(function () {
  function boot() {
    const css = `
    .copilot-btn{position:fixed;right:18px;bottom:18px;border-radius:999px;padding:12px 14px;font-weight:600;border:none;box-shadow:0 8px 24px rgba(0,0,0,.18);cursor:pointer;z-index:2147483647}
    .copilot-panel{position:fixed;right:18px;bottom:70px;width:360px;max-height:60vh;background:#121212;color:#eee;border:1px solid #333;border-radius:12px;box-shadow:0 24px 48px rgba(0,0,0,.35);display:none;flex-direction:column;overflow:hidden;z-index:2147483647}
    .copilot-header{padding:10px 12px;border-bottom:1px solid #2b2b2b;font-weight:700}
    .copilot-body{padding:10px;overflow:auto;gap:10px;display:flex;flex-direction:column}
    .copilot-msg{padding:8px 10px;border-radius:8px;line-height:1.35}
    .copilot-user{background:#1e293b} .copilot-bot{background:#0f172a}
    .copilot-form{display:flex;gap:6px;border-top:1px solid #2b2b2b;padding:8px}
    .copilot-input{flex:1;padding:8px;border-radius:8px;border:1px solid #333;background:#0b0b0b;color:#eee}
    .copilot-send{padding:8px 12px;border-radius:8px;border:1px solid #333;background:#1f2937;color:#eee;cursor:pointer}
    .copilot-src{font-size:12px;opacity:.8;margin-top:6px}
    @media print { .copilot-btn,.copilot-panel{ display:none !important } }
    .copilot-body pre { padding:8px; overflow:auto; background:#0b0b0b; border:1px solid #333; border-radius:8px; }
    .copilot-body code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .copilot-body a { color:#93c5fd; text-decoration: underline; }
    `;
    const style = document.createElement('style'); style.textContent = css; document.head.appendChild(style);

    const btn = document.createElement('button');
    btn.className = 'copilot-btn';
    btn.textContent = '❓ Help';
    document.body.appendChild(btn);

    const panel = document.createElement('div');
    panel.className = 'copilot-panel';
    panel.innerHTML = `
      <div class="copilot-header">Docs Copilot</div>
      <div class="copilot-body" id="copilot-body"></div>
      <form class="copilot-form" id="copilot-form">
        <input class="copilot-input" id="copilot-input" placeholder="Ask about this app..." />
        <button class="copilot-send" type="submit">Send</button>
      </form>`;
    document.body.appendChild(panel);

    const body = panel.querySelector('#copilot-body');
    const form = panel.querySelector('#copilot-form');
    const input = panel.querySelector('#copilot-input');

    function addMsg(txt, who='bot') {
      const div = document.createElement('div');
      div.className = 'copilot-msg copilot-' + (who === 'user' ? 'user' : 'bot');
      div.innerHTML = txt;
      body.appendChild(div);
      body.scrollTop = body.scrollHeight;
    }

async function ask(q) {
  addMsg(q, 'user');
  input.value = '';
  addMsg('Thinking…');

  try {
    const res = await fetch('/copilot/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ q })
    });

    // Try JSON first, fall back to text
    let data = null;
    const ct = res.headers.get('content-type') || '';
    if (ct.includes('application/json')) {
      try { data = await res.json(); } catch (_) {}
    }
    if (!data) {
      const txt = await res.text();
      data = { answer_html: txt || 'Server error. Check logs.', sources: [] };
    }

    // remove "Thinking…"
    body.lastChild?.remove();

    // now build the HTML safely
    let html;
    if (data.answer_is_html) {
    html = data.answer_html || '';
    } else {
    // legacy: plain text from server
    html = (data.answer_html || '').replace(/\n/g, '<br/>');
    }
    if (Array.isArray(data.sources) && data.sources.length) {
    const srcs = data.sources
        .map(s => `• ${s.file}${s.anchor ? '#' + s.anchor : ''} (${s.score})`)
        .join('<br/>');
    html += `<div class="copilot-src"><b>Sources</b><br/>${srcs}</div>`;
    }
    addMsg(html || 'No answer.', 'bot');
  } catch (err) {
    // remove "Thinking…" and show the error
    body.lastChild?.remove();
    addMsg('Network error: ' + (err && err.message ? err.message : err), 'bot');
  }
}


    btn.addEventListener('click', () => {
      panel.style.display = (panel.style.display === 'flex') ? 'none' : 'flex';
      panel.style.flexDirection = 'column';
      input.focus();
    });

    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const q = input.value.trim();
      if (q) ask(q);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
