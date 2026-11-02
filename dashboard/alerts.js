/* NETRAX popups (guide's request, 2 Nov), shared by the control room, entrance and admin pages.
   Alert popup: every NEW PPE / restricted-zone / fire / smoke incident pops up; several at once are kept separately
   ("1 of 3", brief section 2). Closing with "Later" only hides the popup - the incident stays active on the dashboard.
   It pops again when the incident gets worse (more PPE missing, higher fire level).
   Entrance popup: green = verified / may enter, red = do not enter, amber = manual check, with the person's name. */
window.NetraxAlerts = (function () {
  const ITEM = {helmet:"HELMET", vest:"SAFETY VEST", gloves:"GLOVES", boots:"SAFETY SHOES", goggles:"GOGGLES", mask:"MASK"};
  const iname = i => ITEM[i] || String(i).toUpperCase();
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const fmt = t => t ? new Date(t * 1000).toLocaleTimeString("en-IN", {timeZone:"Asia/Kolkata", hour12:false}) + " IST" : "–";
  const POP = new Set(["ppe_missing", "unauthorized_entry", "fire", "smoke"]);
  let opt = {canAct: () => false, onEvent: () => {}, view: null, tone: () => {}, skipL3: false, role: () => "viewer"};
  const queue = [], sigs = new Map(), fresh = new Map();
  let idx = 0, el = null, gateEl = null, miniEl = null, gateT = null, miniT = null, liveCam = null, liveUrl = null, liveOn = false;

  function mount() {
    if (el) return;
    el = document.createElement("div"); el.className = "nxp"; el.setAttribute("role", "alertdialog"); el.setAttribute("aria-modal", "true");
    gateEl = document.createElement("div"); gateEl.className = "nxg"; gateEl.setAttribute("role", "alertdialog");
    miniEl = document.createElement("div"); miniEl.className = "nxmini"; miniEl.setAttribute("role", "status");
    document.body.append(el, gateEl, miniEl);
    el.addEventListener("click", onClick); gateEl.addEventListener("click", onGateClick);
    miniEl.addEventListener("click", () => miniEl.classList.remove("show"));
    document.addEventListener("keydown", ev => { if (ev.key === "Escape") { closeGate(); if (el.classList.contains("show")) later(); } });
  }
  const sig = e => [(e.items_missing || []).slice().sort().join(","), e.fire_level || 0, e.type].join("|");
  const wanted = e => POP.has(e.type) && (e.status === "open" || e.status === "escalated") && !(opt.skipL3 && (e.fire_level || 0) >= 3);

  function seed(events) { for (const e of events) if (POP.has(e.type)) sigs.set(e.event_id, sig(e)); }
  function offer(e) {
    if (!e || !POP.has(e.type)) return;
    mount();
    const at = queue.findIndex(x => x.event_id === e.event_id);
    if (!wanted(e)) {                          // acknowledged, resolved, false alarm, or Level 3 (has its own screen)
      if (at >= 0) { queue.splice(at, 1); if (idx >= queue.length) idx = Math.max(0, queue.length - 1); render(); }
      if (e.status !== "verifying") sigs.set(e.event_id, sig(e));
      return;
    }
    const old = sigs.get(e.event_id), now = sig(e);
    if (at >= 0) {
      queue[at] = e;
      if (old !== now) { markFresh(e, old); sigs.set(e.event_id, now); idx = at; opt.tone(); }
      render();
    } else if (old !== now) {
      markFresh(e, old); sigs.set(e.event_id, now);
      queue.push(e);
      const rank = x => (x.type === "fire" ? 0 : x.type === "smoke" ? 1 : x.severity === "Critical" ? 2 : 3);
      if (!el.classList.contains("show") || rank(e) < rank(queue[idx] || e)) idx = queue.length - 1;   // fire takes over the view
      opt.tone(); render();
    }
  }
  function markFresh(e, old) {
    const was = (old || "").split("|")[0].split(",");
    fresh.set(e.event_id, old ? (e.items_missing || []).filter(i => !was.includes(i)) : []);
  }
  function later() { const e = queue[idx]; if (!e) return; queue.splice(idx, 1); if (idx >= queue.length) idx = Math.max(0, queue.length - 1); render(); }
  async function onClick(ev) {
    const b = ev.target.closest("[data-nx]"); if (!b) return;
    const a = b.dataset.nx, e = queue[idx]; if (!e) return;
    if (a === "prev") { idx = (idx - 1 + queue.length) % queue.length; return render(); }
    if (a === "next") { idx = (idx + 1) % queue.length; return render(); }
    if (a === "later") return later();
    if (a === "view") { later(); if (opt.view) opt.view(e.event_id); else location.href = "/#event=" + encodeURIComponent(e.event_id); return; }
    b.disabled = true;
    try {
      const r = await fetch(`/api/events/${e.event_id}/${a}`, {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
      if (!r.ok) throw new Error(r.status);
      const u = await r.json(); offer(u); opt.onEvent(u);
    } catch (x) { b.disabled = false; b.textContent = "Failed - try again"; }
  }
  function card(e) {
    const fire = e.type === "fire" || e.type === "smoke", lvl = e.fire_level || 0, w = e.type === "smoke" ? "SMOKE" : "FIRE";
    const warn = fire ? lvl === 1 : e.severity !== "Critical" && e.severity !== "High";
    const tone = fire ? (lvl === 1 ? "warn" : "fire") : warn ? "warn" : "";
    const icon = fire ? "🔥" : e.type === "unauthorized_entry" ? "⛔" : "▲";
    const small = fire ? `${w} · LEVEL ${lvl || "?"} OF 3` : e.type === "unauthorized_entry" ? "RESTRICTED ZONE" : `SAFETY VIOLATION · ${String(e.severity || "").toUpperCase()}`;
    const head = fire ? `${w} DETECTED` : e.type === "unauthorized_entry" ? "Not authorised in this zone" : "Required PPE missing";
    const msg = fire ? {1:"Small " + w.toLowerCase() + " detected. Immediate inspection required.", 2:"Immediate emergency response required.", 3:"EVACUATE THE AFFECTED AREA IMMEDIATELY."}[lvl || 2] : "";
    const nw = fresh.get(e.event_id) || [];
    const items = e.type === "ppe_missing" ? (e.items_missing || []).map(i => `<span class="item${nw.includes(i) ? " fresh" : ""}">✖ NO ${iname(i)}</span>`).join("") : "";
    const who = e.worker_id === "W-***" ? "Registered worker (name hidden)" : e.worker_id ? (String(e.worker_id).startsWith("W-") ? `${e.worker_id} ${e.worker_name || ""}` : "Unregistered person") : (fire ? "" : "Not identified");
    const useSnap = !fire && e.snapshot;
    liveCam = useSnap ? null : (e.camera_id || null);
    const can = opt.canAct();
    const acts = can ? `<button class="btn red" data-nx="ack">Acknowledge</button><button class="btn" data-nx="resolve">Resolve</button>` : `<small>View only - a supervisor must act</small>`;
    return `<div class="nxp-card ${tone}">
      <div class="nxp-band"><span class="nxp-ring" aria-hidden="true">${icon}</span><div><small>${esc(small)}</small><h2 id="nxpT">${esc(head)}</h2></div><span class="sp"></span>
        ${queue.length > 1 ? `<span class="nxp-nav"><button data-nx="prev" aria-label="Previous incident">‹</button>${idx + 1} of ${queue.length}<button data-nx="next" aria-label="Next incident">›</button></span>` : ""}
        <button class="nxp-x" data-nx="later" aria-label="Hide this popup">✕</button></div>
      <div class="nxp-body"><figure class="nxp-shot">${useSnap ? `<img src="${esc(e.snapshot)}?v=${e.updated_at || ""}" alt="Snapshot of the incident"><figcaption>📸 AT THE ALERT</figcaption>` : `<img id="nxLive" alt="Live view of the affected camera" hidden><div class="nolive" id="nxNoLive">${e.camera_id ? "Connecting to the camera…" : "Reported by the safety station"}</div><figcaption id="nxLiveTag" hidden>● LIVE</figcaption>`}</figure>
        <div>${items ? `<div class="nxp-items">${items}</div>` : ""}${msg ? `<p class="nxp-msg">${esc(msg)}</p>` : ""}
          <dl class="nxp-kv">${who ? `<dt>Worker</dt><dd>${esc(who)}</dd>` : ""}<dt>Zone</dt><dd>${esc(e.zone_name || e.zone_id || "—")}</dd>
            <dt>Camera</dt><dd>${esc(e.camera_name || "")} ${esc(e.camera_id || "safety station")}</dd><dt>Time</dt><dd>${fmt(e.started_at)}</dd>
            ${e.status === "escalated" ? `<dt>Status</dt><dd>ESCALATED</dd>` : ""}</dl></div></div>
      <div class="nxp-acts">${acts}<button class="btn" data-nx="view">Details</button><span class="sp"></span><button class="btn" data-nx="later">Later</button></div></div>`;
  }
  function render() {
    if (!el) return;
    const e = queue[idx];
    el.classList.toggle("show", !!e);
    if (!e) { el.innerHTML = ""; liveCam = null; return; }
    el.innerHTML = card(e);
    el.setAttribute("aria-labelledby", "nxpT");
    startLive();
  }
  async function startLive() {
    if (liveOn) return; liveOn = true;
    while (el.classList.contains("show") && liveCam) {
      try {
        const r = await fetch(`/api/frame/${liveCam}?t=${Date.now()}`, {cache:"no-store"});
        const img = document.getElementById("nxLive");
        if (r.status === 200 && img) {
          const u = URL.createObjectURL(await r.blob()); img.src = u; img.hidden = false;
          const n = document.getElementById("nxNoLive"); if (n) n.hidden = true;
          const t = document.getElementById("nxLiveTag"); if (t) t.hidden = false;
          if (liveUrl) URL.revokeObjectURL(liveUrl); liveUrl = u;
        } else if (r.status !== 200) {
          const n = document.getElementById("nxNoLive"); if (n) n.textContent = "No picture from this camera right now";
        }
      } catch {}
      await new Promise(r => setTimeout(r, 200));
    }
    liveOn = false;
  }

  /* ---------- entrance verdict ---------- */
  const MARK = {approved:'<path d="M28 52 l15 15 l30 -32"/>', denied:'<path d="M33 33 l34 34 M67 33 l-34 34"/>', manual:'<path d="M50 26 v30 M50 72 v1"/>'};
  function who(g) {
    const i = g.identity || {};
    if (i.kind === "registered" && i.worker_id) return {name:`${i.worker_id} ${i.name || ""}`.trim(), how:{face:"IDENTIFIED BY FACE", rfid:"IDENTIFIED BY RFID CARD", "face+rfid":"IDENTIFIED BY FACE + RFID CARD"}[i.by] || "REGISTERED WORKER"};
    if (i.kind === "unregistered" || !g.identity && !g.worker_id) return {name:"UNREGISTERED WORKER", how:"NOT IN THE WORKER LIST · LOGGED AS UNREGISTERED"};
    return {name:"REGISTERED WORKER", how:"NAME HIDDEN FOR YOUR ROLE"};
  }
  function chips(g) {
    return (g.required || []).map(i => (g.missing || []).includes(i) ? `<span class="item">✖ ${iname(i)}</span>` : (g.ok || []).includes(i) ? `<span class="item ok">✓ ${iname(i)}</span>` : `<span class="item unk">? ${iname(i)}</span>`).join("");
  }
  function closeGate() { if (gateEl) gateEl.classList.remove("show"); clearTimeout(gateT); }
  function gate(g, o) {
    mount(); o = o || {};
    if (g.override) { closeGate(); return; }
    const w = who(g);
    if (o.compact && g.result === "approved") {
      miniEl.innerHTML = `<span class="ic" aria-hidden="true">✓</span><div><b>${esc(w.name)}</b><span>Entrance: ${esc((g.verdict || "").replace(/^.*? - |^.*? verified - /, "")) || "verified"}</span></div>`;
      miniEl.classList.add("show"); clearTimeout(miniT); miniT = setTimeout(() => miniEl.classList.remove("show"), 6000);
      return;
    }
    const head = {approved:"VERIFIED — YOU MAY ENTER", denied:"DO NOT ENTER", manual:"MANUAL CHECK REQUIRED"}[g.result] || g.result;
    const ms = {approved:6000, denied:12000}[g.result];
    const can = opt.canAct() && g.result === "manual";
    gateEl.className = "nxg show " + g.result;
    gateEl.innerHTML = `<div class="nxg-card"><svg class="nxg-mark" viewBox="0 0 100 100" aria-hidden="true"><circle cx="50" cy="50" r="45" transform="rotate(-90 50 50)"/>${MARK[g.result] || ""}</svg>
      <h1 class="nxg-head">${esc(head)}</h1>
      <div class="nxg-who">${esc(w.name)}<small>${esc(w.how)}</small></div>
      <p class="nxg-verdict">${esc(g.verdict || g.message || "")}</p>
      <div class="nxg-items">${chips(g)}</div>
      ${g.reason && g.result !== "approved" ? `<p class="nxg-meta">${esc(g.reason)}</p>` : ""}
      ${g.snapshot ? `<div class="nxg-row"><img src="${esc(g.snapshot)}" alt="Snapshot at the check"></div>` : ""}
      <div class="nxg-meta">${esc(g.zone_name || g.zone_id || "")} · ${esc(g.camera_id || "")} · ${esc(g.time || "")} · decided in ${g.decision_ms ?? "–"} ms${g.controller ? ` · gate: ${esc(g.gate)} (${esc(g.controller)})` : ""}</div>
      <div class="nxg-acts">${can ? `<button class="btn pri" data-gate="approved" data-id="${esc(g.event_id)}">Checked — allow entry</button><button class="btn red" data-gate="denied" data-id="${esc(g.event_id)}">Checked — refuse</button>` : ""}<button class="btn" data-gate="close">Close</button></div>
      ${ms ? `<div class="nxg-bar" style="--t:${ms}ms"></div>` : ""}</div>`;
    clearTimeout(gateT); if (ms) gateT = setTimeout(closeGate, ms);
  }
  async function onGateClick(ev) {
    const b = ev.target.closest("[data-gate]"); if (!b) return;
    if (b.dataset.gate === "close") return closeGate();
    b.disabled = true;
    try {
      const r = await fetch(`/api/entrance/${b.dataset.id}/decide`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({decision:b.dataset.gate, note:"manual check at the entrance"})});
      if (!r.ok) throw new Error(r.status);
      closeGate();
    } catch { b.disabled = false; b.textContent = "Not saved - try again"; }
  }
  return {init: o => { Object.assign(opt, o); mount(); }, seed, offer, gate, closeGate, chips, who, iname};
})();
