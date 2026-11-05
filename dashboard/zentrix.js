/* NETRAX Zentrix (5 Nov): the AI command assistant on every page - typed or spoken commands of the supervisor / admin.
   "Hey Zentrix, analyse the recent alerts and acknowledge them" · "open the admin panel" · "enlarge the welding bay camera"
   · "resolve the last alert" · "allow the person at the entrance" · "generate today's report as PDF" · questions about the past.
   Acts ONLY on command (backend/agent.py + POST /api/zentrix). Risky bulk changes are confirmed first.
   Pages register what they can do: NetraxZentrix.init({handlers: {focus, unfocus, view, fullscreen, event, report, mute, theme}, mount}).
   A command for another page opens that page and finishes there (queued in sessionStorage). */
window.NetraxZentrix = (function () {
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const store = {
    get(k, d, s = sessionStorage) { try { const v = s.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v, s = sessionStorage) { try { s.setItem(k, JSON.stringify(v)); } catch {} }
  };
  const HOME = {focus: "/", unfocus: "/", view: "/", fullscreen: "/", report: "/admin#reports"};
  const WAKE = /^\s*(?:(?:hey|hi|hello|ok|okay)\s+)?(?:zentrix|zen\s*trix|zentrex|zentricks|zen\s*tricks|sentrix|centrix|sentry\s*x|zendrix|xentrix|zentrics|sentrics|sun\s*tricks)\b[\s,.:!-]*/i;
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  let voiceWhy = "";
  let H = {}, me = {role: "viewer", user: ""}, root = null, log, input, micBtn, wakeBtn, spkBtn, stateEl;
  let open = store.get("zx-open", false), busy = false, rec = null, recMode = null, wakeOn = store.get("zx-wake", false, localStorage),
      speakOn = store.get("zx-speak", true, localStorage), armedUntil = 0, inline = false;
  let history = store.get("zx-log", []);

  const css = `
.zx-launch{position:fixed;left:16px;bottom:16px;z-index:96;display:flex;align-items:center;gap:10px;border:2px solid var(--teal,#14b8a6);
  background:var(--panel,#11222b);color:var(--ink,#eaf3f4);border-radius:999px;padding:10px 18px 10px 10px;font:800 16px/1.1 inherit;cursor:pointer;
  box-shadow:0 8px 28px rgba(0,0,0,.45),0 0 18px -4px var(--teal,#14b8a6)}
.zx-launch small{display:block;font-weight:600;font-size:11.5px;color:var(--muted,#93aab1)}
.zx-navbtn{border:1px solid var(--teal,#14b8a6);background:transparent;color:var(--teal,#14b8a6);border-radius:8px;padding:5px 10px;font-weight:800;cursor:pointer;font-size:13px;margin-left:4px}
.zx-navbtn:hover{background:var(--teal,#14b8a6);color:#05201c}
.zx-launch .dot{width:34px;height:34px;font-size:17px;border-radius:50%;background:var(--teal,#14b8a6);color:#05201c;display:grid;place-items:center;font-size:14px}
.zx-launch.listen .dot{animation:zxp 1.2s infinite}
@keyframes zxp{0%{box-shadow:0 0 0 0 rgba(20,184,166,.6)}100%{box-shadow:0 0 0 12px rgba(20,184,166,0)}}
.zx{display:flex;flex-direction:column;background:var(--panel,#11222b);color:var(--ink,#eaf3f4);border:1px solid var(--line,#24414b);border-radius:14px;overflow:hidden}
.zx.float{position:fixed;left:16px;bottom:16px;z-index:97;width:min(420px,calc(100vw - 32px));height:min(620px,calc(100vh - 90px));box-shadow:0 16px 48px rgba(0,0,0,.45)}
.zx.inline{height:520px}
.zx-head{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--line,#24414b);background:var(--panel2,#162c37)}
.zx-head b{font-size:16px;letter-spacing:.03em}.zx-head .sp{flex:1}
.zx-head small{color:var(--muted,#93aab1);font-size:12px}
.zx-ib{border:1px solid var(--line,#24414b);background:transparent;color:var(--ink,#eaf3f4);border-radius:8px;padding:4px 8px;cursor:pointer;font-size:13px}
.zx-ib[aria-pressed="true"]{background:var(--teal,#14b8a6);color:#05201c;border-color:var(--teal,#14b8a6)}
.zx-ib:disabled{opacity:.45;cursor:not-allowed}
.zx-log{flex:1;overflow:auto;padding:10px 12px;display:flex;flex-direction:column;gap:8px}
.zx-m{max-width:92%;padding:8px 11px;border-radius:12px;font-size:14px;line-height:1.4;white-space:pre-wrap;word-wrap:break-word}
.zx-m.u{align-self:flex-end;background:var(--teal,#14b8a6);color:#05201c;border-bottom-right-radius:4px}
.zx-m.a{align-self:flex-start;background:var(--panel2,#162c37);border:1px solid var(--line,#24414b);border-bottom-left-radius:4px}
.zx-m.a.err{border-color:var(--crit,#ef4444)}
.zx-m .mic{font-size:11px;opacity:.7;display:block;margin-bottom:2px}
.zx-an{display:flex;flex-direction:column;gap:6px;margin-top:6px;white-space:normal}
.zx-ac{border:1px solid var(--line,#24414b);border-left:5px solid var(--grey,#64748b);border-radius:8px;padding:6px 8px;background:var(--panel,#11222b);cursor:pointer}
.zx-ac.hot{border-left-color:var(--crit,#ef4444)}.zx-ac.ok{border-left-color:var(--ok,#22c55e)}.zx-ac.unk{border-left-color:var(--warn,#f5b301)}
.zx-ac b{display:block;font-size:13px}.zx-ac small{color:var(--muted,#93aab1);font-size:12px;display:block}
.zx-ac .rec{display:inline-block;margin-top:3px;font-size:11px;font-weight:800;padding:1px 7px;border-radius:999px;background:var(--panel2,#162c37);border:1px solid var(--line,#24414b)}
.zx-btns{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
.zx-btns button{border:1px solid var(--line,#24414b);background:var(--panel,#11222b);color:var(--ink,#eaf3f4);border-radius:8px;padding:5px 11px;cursor:pointer;font-weight:700;font-size:13px}
.zx-btns button.pri{background:var(--teal,#14b8a6);color:#05201c;border-color:var(--teal,#14b8a6)}
.zx-btns button.red{background:var(--crit,#ef4444);color:#fff;border-color:var(--crit,#ef4444)}
.zx-chips{display:flex;gap:6px;flex-wrap:nowrap;overflow-x:auto;padding:0 12px 8px;scrollbar-width:thin}
.zx-chips button{flex:none;white-space:nowrap;border:1px dashed var(--line,#24414b);background:transparent;color:var(--muted,#93aab1);border-radius:999px;padding:3px 9px;cursor:pointer;font-size:12px}
.zx-foot{display:flex;gap:6px;padding:10px 12px;border-top:1px solid var(--line,#24414b)}
.zx-foot input{flex:1;min-width:0;padding:9px 11px;border-radius:9px;border:1px solid var(--line,#24414b);background:var(--panel2,#162c37);color:var(--ink,#eaf3f4);font-size:14px}
.zx-foot button{border-radius:9px;border:1px solid var(--line,#24414b);background:var(--panel2,#162c37);color:var(--ink,#eaf3f4);padding:0 12px;cursor:pointer;font-size:16px}
.zx-foot button.send{background:var(--teal,#14b8a6);color:#05201c;border-color:var(--teal,#14b8a6);font-weight:800;font-size:14px}
.zx-foot button.rec{background:var(--crit,#ef4444);color:#fff;border-color:var(--crit,#ef4444);animation:zxp 1.2s infinite}
.zx-state{font-size:12px;color:var(--muted,#93aab1);padding:0 12px 6px;min-height:16px}
.zx-recs{display:flex;flex-direction:column;gap:4px;margin-top:6px}
.zx-recs a{color:inherit;text-decoration:none;font-size:12px;border:1px solid var(--line,#24414b);border-radius:7px;padding:4px 7px;display:block}
@media (prefers-reduced-motion:reduce){.zx-launch.listen .dot,.zx-foot button.rec{animation:none}}`;

  /* ---------- UI ---------- */
  function build(mount) {
    const st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);
    inline = !!mount;
    root = document.createElement("section");
    root.className = "zx " + (inline ? "inline" : "float");
    root.setAttribute("aria-label", "Zentrix AI command assistant");
    root.innerHTML = `<div class="zx-head"><span aria-hidden="true">🤖</span><div><b>Zentrix</b><br><small id="zxRole">AI command assistant</small></div><span class="sp"></span>
        <button class="zx-ib" id="zxWake" aria-pressed="false" title="Always listen for 'Hey Zentrix'">👂 Hey Zentrix</button>
        <button class="zx-ib" id="zxSpk" aria-pressed="true" title="Speak the answers">🔊</button>
        ${inline ? "" : `<button class="zx-ib" id="zxClose" title="Close (Alt+Z)">✕</button>`}</div>
      <div class="zx-log" id="zxLog" aria-live="polite"></div>
      <div class="zx-state" id="zxState"></div>
      <div class="zx-chips" id="zxChips"></div>
      <form class="zx-foot" id="zxForm"><button type="button" id="zxMic" title="Click, then speak your command (Alt+M)">🎙 Speak</button>
        <label class="sr" for="zxIn" style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)">Command</label>
        <input id="zxIn" placeholder="Type or say a command…" autocomplete="off" maxlength="400"><button class="send" type="submit">Send</button></form>`;
    (mount || document.body).appendChild(root);
    log = root.querySelector("#zxLog"); input = root.querySelector("#zxIn"); micBtn = root.querySelector("#zxMic");
    wakeBtn = root.querySelector("#zxWake"); spkBtn = root.querySelector("#zxSpk"); stateEl = root.querySelector("#zxState");
    root.querySelector("#zxForm").onsubmit = ev => { ev.preventDefault(); const t = input.value.trim(); if (t) { input.value = ""; send(t); } };
    const ex = ["Analyse the recent alerts", "Acknowledge the latest alert", "Enlarge the welding bay camera", "Show all cameras",
      "Open the admin panel", "Generate today's report", "What happened in the last hour?", "Help"];
    root.querySelector("#zxChips").innerHTML = ex.map(q => `<button type="button" data-zxq="${esc(q)}">${esc(q)}</button>`).join("");
    root.addEventListener("click", ev => {
      const q = ev.target.closest("[data-zxq]"); if (q) return send(q.dataset.zxq);
      const s = ev.target.closest("[data-zxsend]"); if (s) { s.closest(".zx-btns").querySelectorAll("button").forEach(b => b.disabled = true); return send(s.dataset.zxsend); }
      const e = ev.target.closest("[data-zxev]"); if (e) { ev.preventDefault(); return runOps([{op: "event", id: e.dataset.zxev}]); }
    });
    spkBtn.onclick = () => { speakOn = !speakOn; store.set("zx-speak", speakOn, localStorage); if (!speakOn) try { speechSynthesis.cancel(); } catch {} paint(); };
    wakeBtn.onclick = () => { if (voiceWhy) return push({who: "a", text: voiceWhy, err: true}); wakeOn = !wakeOn; store.set("zx-wake", wakeOn, localStorage); wakeOn ? startWake() : stopRec(); paint(); };
    micBtn.onclick = () => (voiceWhy ? push({who: "a", text: voiceWhy, err: true}) : recMode === "ptt" ? stopRec() : startPTT());
    if (!inline) {
      root.querySelector("#zxClose").onclick = () => setOpen(false);
      const l = document.createElement("button"); l.className = "zx-launch"; l.id = "zxLaunch"; l.type = "button";
      l.innerHTML = `<span class="dot" aria-hidden="true">🎙</span><span>Zentrix AI<small>type or speak a command · Alt+Z</small></span>`; l.title = "Zentrix AI assistant (Alt+Z)";
      l.onclick = () => setOpen(true); document.body.appendChild(l);
    }
    document.addEventListener("keydown", ev => {
      if (ev.altKey && (ev.key === "z" || ev.key === "Z")) { ev.preventDefault(); setOpen(inline ? true : !open); if (open || inline) input.focus(); }
      if (ev.altKey && (ev.key === "m" || ev.key === "M")) { ev.preventDefault(); setOpen(true); micBtn.click(); }
    });
    const nav = document.querySelector(".nxnav");          // a Zentrix button in every page's top menu too
    if (nav && !document.getElementById("zxNavBtn")) {
      const b = document.createElement("button"); b.type = "button"; b.id = "zxNavBtn"; b.className = "zx-navbtn"; b.textContent = "🤖 Zentrix";
      b.title = "Open the Zentrix AI assistant (Alt+Z)"; b.onclick = () => { setOpen(true); input.focus(); }; nav.appendChild(b);
    }
    if (!SR || !window.isSecureContext) {                  // keep the buttons visible; explain on click
      voiceWhy = !SR ? "Voice needs Microsoft Edge or Google Chrome (this browser has no speech recognition). You can type the command." :
        `The browser allows the microphone only on a secure page. On the laptop open http://127.0.0.1:8000${location.pathname} - voice works there. From another device: type the command.`;
      micBtn.title = wakeBtn.title = voiceWhy; wakeOn = false; micBtn.style.opacity = wakeBtn.style.opacity = ".6";
    }
    history.forEach(m => render(m, false));
    if (!history.length) render({who: "a", text: "Hello, I am Zentrix. Tell me what to do - type or press 🎙 and speak, or switch on 👂 and say \"Hey Zentrix …\". I act only on your command."}, false);
    setOpen(inline || open);
    paint();
  }
  function setOpen(v) {
    open = v; store.set("zx-open", v);
    if (inline) return;
    root.style.display = v ? "" : "none";
    const l = document.getElementById("zxLaunch"); if (l) l.style.display = v ? "none" : "";
    if (v) log.scrollTop = log.scrollHeight;
  }
  function paint() {
    if (!root) return;
    wakeBtn.setAttribute("aria-pressed", wakeOn); spkBtn.setAttribute("aria-pressed", speakOn); spkBtn.textContent = speakOn ? "🔊" : "🔇";
    micBtn.className = recMode === "ptt" ? "rec" : "";
    const l = document.getElementById("zxLaunch"); if (l) l.classList.toggle("listen", recMode === "wake");
    stateEl.textContent = busy ? "Working…" : recMode === "ptt" ? "Listening… speak your command" :
      recMode === "wake" ? (Date.now() < armedUntil ? "Yes? Say the command…" : "Listening for “Hey Zentrix”") : "";
  }

  /* ---------- messages ---------- */
  function push(m) { history.push(m); history = history.slice(-40); store.set("zx-log", history); render(m, true); }
  function render(m, scroll) {
    const d = document.createElement("div");
    d.className = "zx-m " + (m.who === "u" ? "u" : "a") + (m.err ? " err" : "");
    let h = (m.voice ? `<span class="mic">🎙 heard</span>` : "") + esc(m.text);
    if (m.analysis && m.analysis.length) {
      h += `<div class="zx-an">` + m.analysis.map(a => {
        const cls = /^(STILL|REAL|New)/.test(a.verdict) ? "hot" : /^No longer|no longer/.test(a.verdict) ? "ok" : /^Cannot|Waiting/.test(a.verdict) ? "unk" : "";
        const rec = {ack: "→ acknowledge", resolve: "→ resolve", escalate: "→ escalate", dismiss: "→ false alarm"}[a.recommend] || "→ your decision";
        return `<div class="zx-ac ${cls}" data-zxev="${esc(a.event_id)}" role="button" tabindex="0" title="Open details"><b>${esc(a.title)}</b>
          <small>${esc(a.time)} · ${esc(a.zone || "")}${a.camera ? " · " + esc(a.camera) : ""} · ${esc(a.status)}</small>
          <small><b style="display:inline">${esc(a.verdict)}</b>${a.why && a.why.length ? " - " + esc(a.why.join("; ")) : ""}</small><span class="rec">${rec}</span></div>`;
      }).join("") + `</div>`;
    }
    if (m.records && m.records.length) {
      h += `<div class="zx-recs">` + m.records.slice(-8).reverse().map(c => `<a href="/#event=${encodeURIComponent(c.event_id)}" data-zxev="${esc(c.event_id)}">${esc(c.time)} · ${esc(c.what)} · ${esc(c.zone || "")} · ${esc(c.status)}</a>`).join("") + `</div>`;
    }
    if (m.confirm && !m.used) h += `<div class="zx-btns"><button class="red" data-zxsend="confirm">✓ Confirm</button><button data-zxsend="cancel">Cancel</button></div>`;
    else if (m.suggest && !m.used) h += `<div class="zx-btns"><button class="pri" data-zxsend="apply">Apply recommendations</button></div>`;
    d.innerHTML = h;
    log.appendChild(d);
    if (scroll) log.scrollTop = log.scrollHeight;
  }

  /* ---------- talk to the backend ---------- */
  async function send(text, voice) {
    if (busy) return;
    setOpen(true);
    history.forEach(m => { if (m.confirm || m.suggest) m.used = true; });
    log.querySelectorAll(".zx-btns button").forEach(b => b.disabled = true);
    push({who: "u", text, voice: !!voice});
    busy = true; paint();
    try {
      const r = await fetch("/api/zentrix", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({text, page: location.pathname + location.hash})});
      if (r.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname); return; }
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.status);
      const msg = {who: "a", text: d.reply || "Done.", analysis: d.analysis, confirm: d.confirm, suggest: d.suggest,
        records: d.history && d.history.records};
      push(msg);
      say(d.speak || d.reply);
      if (d.ui && d.ui.length) setTimeout(() => runOps(d.ui), 350);
    } catch (x) { push({who: "a", text: "Sorry, that did not work: " + x.message, err: true}); }
    finally { busy = false; paint(); }
  }
  function say(t) {
    if (!speakOn || !t || !window.speechSynthesis) return;
    try {
      speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(String(t).replace(/\bevt_\w+/g, "").slice(0, 300));
      const v = speechSynthesis.getVoices().find(v => /en-IN/i.test(v.lang)) || speechSynthesis.getVoices().find(v => /^en/i.test(v.lang));
      if (v) u.voice = v; u.rate = 1.05; speechSynthesis.speak(u);
    } catch {}
  }

  /* ---------- run UI operations (here, or on the page that can do them) ---------- */
  function runOps(ops) {
    for (let i = 0; i < ops.length; i++) {
      const op = ops[i];
      if (op.op === "navigate") return go(op.url, ops.slice(i + 1));
      if (op.op === "reload") { location.reload(); return; }
      if (H[op.op]) { try { H[op.op](op); } catch (x) { console.warn("zentrix op", op, x); } continue; }
      if (op.op === "theme") { const r = document.documentElement; r.dataset.theme = op.mode;
        ["netrax-cr-theme", "netrax-theme"].forEach(k => { try { localStorage.setItem(k, op.mode); } catch {} }); continue; }
      if (op.op === "mute") { try { localStorage.setItem("netrax-mute", op.on ? "1" : "0"); } catch {} continue; }
      if (op.op === "event") return go("/#event=" + encodeURIComponent(op.id), ops.slice(i + 1));
      if (HOME[op.op]) return go(HOME[op.op], ops.slice(i));
    }
  }
  function go(url, rest) {
    const u = new URL(url, location.href);
    if (u.pathname === location.pathname) {
      if (u.hash && u.hash !== location.hash) location.hash = u.hash;
      if (rest && rest.length) setTimeout(() => runOps(rest), 250);
      return;
    }
    store.set("zx-queue", rest || []);
    location.href = u.pathname + u.search + u.hash;
  }

  /* ---------- voice ---------- */
  function newRec(cont) {
    const r = new SR(); r.lang = "en-IN"; r.continuous = cont; r.interimResults = true; r.maxAlternatives = 1; return r;
  }
  function stopRec() { const r = rec; rec = null; recMode = null; if (r) try { r.onend = null; r.abort(); } catch {} paint(); }
  function startPTT() {
    if (!SR) return;
    stopRec();
    rec = newRec(false); recMode = "ptt"; paint();
    let finalText = "";
    rec.onresult = ev => {
      let interim = "";
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        if (ev.results[i].isFinal) finalText += ev.results[i][0].transcript; else interim += ev.results[i][0].transcript;
      }
      input.value = (finalText + interim).trim();
    };
    rec.onerror = ev => { if (ev.error === "not-allowed") push({who: "a", text: "The microphone is blocked. Allow it in the browser's address bar.", err: true});
      else if (ev.error === "network") push({who: "a", text: "Speech recognition needs internet (the browser's speech service). Type the command instead.", err: true}); };
    rec.onend = () => {
      recMode = null; rec = null; paint();
      const t = (finalText || input.value).trim().replace(WAKE, "").trim();
      input.value = "";
      if (t) send(t, true);
      if (wakeOn) setTimeout(startWake, 400);
    };
    try { rec.start(); } catch { stopRec(); }
  }
  function startWake() {
    if (!SR || !wakeOn || recMode === "ptt") return;
    stopRec();
    rec = newRec(true); recMode = "wake"; paint();
    rec.onresult = ev => {
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        if (!ev.results[i].isFinal) continue;
        if (window.speechSynthesis && speechSynthesis.speaking) continue;     // do not hear our own voice
        const heard = ev.results[i][0].transcript.trim();
        const m = heard.match(WAKE);
        if (m) {
          const cmd = heard.slice(m[0].length).trim();
          if (cmd) send(cmd, true);
          else { armedUntil = Date.now() + 7000; say("Yes?"); setOpen(true); paint(); setTimeout(paint, 7100); }
        } else if (Date.now() < armedUntil && heard) { armedUntil = 0; send(heard, true); }
      }
    };
    rec.onerror = ev => { if (ev.error === "not-allowed" || ev.error === "service-not-allowed") { wakeOn = false; store.set("zx-wake", false, localStorage);
      push({who: "a", text: "The microphone is blocked, so 'Hey Zentrix' is off. Allow the microphone and switch 👂 on again.", err: true}); } };
    rec.onend = () => { if (recMode === "wake" && wakeOn) setTimeout(() => { if (recMode === "wake" || !rec) startWake(); }, 300); };
    try { rec.start(); } catch { setTimeout(startWake, 1500); }
  }

  /* ---------- start ---------- */
  async function init(opt) {
    opt = opt || {};
    H = Object.assign({}, opt.handlers || {});
    build(opt.mount || null);
    try { const r = await fetch("/api/me"); if (r.ok) me = await r.json(); } catch {}
    const roleEl = root.querySelector("#zxRole");
    roleEl.textContent = me.role === "viewer" ? "Viewer: questions + navigation only" : `AI command assistant · ${me.user || "local"} (${me.role})`;
    const q = store.get("zx-queue", []);
    if (q.length) { store.set("zx-queue", []); setTimeout(() => runOps(q), 600); }
    if (wakeOn) setTimeout(startWake, 800);
  }
  return {init, send, register: (h) => Object.assign(H, h)};
})();
