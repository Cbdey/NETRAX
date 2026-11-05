/* NETRAX alerts on every page (4 Nov rework, user: "big popups cover 40 % of the screen and get irritating").
   - PPE / restricted-zone alerts are NOT popped up any more. On the control room they appear in the alert panel
     (index.html handles that: the new card slides in with a glowing border). Other pages (admin, entrance) show a
     small corner card with a coloured border that hides itself after a few seconds (mode "toast").
   - FIRE / SMOKE: the whole page gets a red frame and a slim red banner with the fire level on EVERY page, until the
     fire is acknowledged or resolved (Level 3 keeps the full-screen evacuation screen on the control room).
   - Entrance decision: the full green / red / amber screen only on the entrance display (/entrance); everywhere else a
     compact corner card (green VERIFIED, red DO NOT ENTER, amber MANUAL CHECK with Allow / Refuse). */
window.NetraxAlerts = (function () {
  const ITEM = {helmet:"HELMET", vest:"SAFETY VEST", gloves:"GLOVES", boots:"SAFETY SHOES", goggles:"GOGGLES", mask:"MASK"};
  const iname = i => ITEM[i] || String(i).toUpperCase();
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const fmt = t => t ? new Date(t * 1000).toLocaleTimeString("en-IN", {timeZone:"Asia/Kolkata", hour12:false}) : "–";
  const POP = new Set(["ppe_missing", "unauthorized_entry", "fire", "smoke"]);
  let opt = {canAct: () => false, onEvent: () => {}, view: null, tone: () => {}, mode: "toast", fireBanner: true};
  const sigs = new Map(), fires = new Map();
  let stack = null, fireEl = null, gateEl = null, gateT = null;

  function mount() {
    if (stack) return;
    stack = document.createElement("div"); stack.className = "nxt-stack"; stack.setAttribute("aria-live", "assertive");
    fireEl = document.createElement("div"); fireEl.className = "nxfire"; fireEl.setAttribute("role", "alert");
    gateEl = document.createElement("div"); gateEl.className = "nxg"; gateEl.setAttribute("role", "alertdialog");
    document.body.append(stack, fireEl, gateEl);
    document.body.addEventListener("click", onClick);
    document.addEventListener("keydown", ev => { if (ev.key === "Escape") closeGate(); });
  }
  const sig = e => [(e.items_missing || []).slice().sort().join(","), e.fire_level || 0, e.status].join("|");
  const active = e => e.status === "open" || e.status === "escalated";
  const whoOf = e => e.worker_id === "W-***" ? "Registered worker" : e.worker_id && String(e.worker_id).startsWith("W-")
    ? `${e.worker_id} ${e.worker_name || ""}`.trim() : "Unregistered person";

  function seed(events) { for (const e of events) if (POP.has(e.type)) { sigs.set(e.event_id, sig(e)); fireTrack(e); } renderFire(); }

  /* ---------- incidents ---------- */
  function offer(e) {
    if (!e || !POP.has(e.type)) return;
    mount();
    fireTrack(e); renderFire();
    const old = sigs.get(e.event_id), now = sig(e);
    sigs.set(e.event_id, now);
    if (!active(e) || old === now) return;
    const was = old ? old.split("|")[0].split(",") : null;
    const added = was ? (e.items_missing || []).filter(i => !was.includes(i)) : [];
    if (old && !added.length && (e.fire_level || 0) <= +(old.split("|")[1] || 0)) return;   // not worse: no new signal
    opt.tone(e);
    if (opt.mode === "toast" && e.type !== "fire" && e.type !== "smoke") toast(e, added);
  }
  function toast(e, added) {
    const serious = e.severity === "Critical" || e.severity === "High";
    const t = document.createElement("div");
    t.className = "nxt " + (serious ? "crit" : "warn");
    const what = e.type === "unauthorized_entry" ? "Not authorised in this zone"
      : (added.length ? "Also missing: " : "Missing: ") + (added.length ? added : e.items_missing || []).map(iname).join(", ");
    t.innerHTML = `<b>${serious ? "▲" : "⚠"} ${esc(what)}</b><span>${esc(whoOf(e))} · ${esc(e.zone_name || e.zone_id || "")} · ${fmt(e.started_at)}</span>`;
    t.dataset.nxview = e.event_id; t.setAttribute("role", "button"); t.tabIndex = 0;
    stack.prepend(t);
    while (stack.children.length > 3) stack.lastElementChild.remove();
    setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 400); }, 7000);
  }

  /* ---------- fire: red frame + banner on every page ---------- */
  function fireTrack(e) {
    if (e.type !== "fire" && e.type !== "smoke") return;
    if (["open", "escalated", "acknowledged"].includes(e.status)) fires.set(e.event_id, e);   // red until the fire is resolved
    else fires.delete(e.event_id);
  }
  function renderFire() {
    if (!fireEl) return;
    const list = [...fires.values()].sort((a, b) => (b.fire_level || 0) - (a.fire_level || 0));
    const on = list.length > 0;
    document.body.classList.toggle("nx-fire", on);
    fireEl.classList.toggle("show", on && opt.fireBanner);
    document.body.classList.toggle("nx-firebar", on && opt.fireBanner);
    if (!on || !opt.fireBanner) return;
    const e = list[0], l = e.fire_level || 2, w = e.type === "smoke" ? "SMOKE" : "FIRE";
    const msg = {1: "Small " + w.toLowerCase() + " - immediate inspection required", 2: "Immediate emergency response required",
                 3: "EVACUATE THE AFFECTED AREA IMMEDIATELY"}[l] || "";
    fireEl.innerHTML = `<span class="lv">LEVEL ${l}</span><b>🔥 ${w} DETECTED</b><span>${esc(msg)}</span>
      <span class="sp"></span><span>${esc(e.zone_name || e.zone_id || "")} · ${esc(e.camera_id || "safety station")} · ${fmt(e.started_at)}</span>
      ${list.length > 1 ? `<span>+${list.length - 1} more</span>` : ""}<a class="btn sm" href="/#event=${encodeURIComponent(e.event_id)}">Open</a>`;
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
    const w = who(g), can = opt.canAct() && g.result === "manual";
    const head = {approved:"VERIFIED — YOU MAY ENTER", denied:"DO NOT ENTER", manual:"MANUAL CHECK REQUIRED"}[g.result] || g.result;
    if (o.compact) {
      const t = document.createElement("div");
      t.className = "nxt " + ({approved: "ok", denied: "crit", manual: "warn"}[g.result] || "warn");
      t.innerHTML = `<b>🚪 ${esc(head)}</b><span>${esc(w.name)} · ${esc(g.verdict || "")}</span>
        ${can ? `<span class="acts"><button class="btn sm pri" data-gate="approved" data-id="${esc(g.event_id)}">Allow</button><button class="btn sm red" data-gate="denied" data-id="${esc(g.event_id)}">Refuse</button></span>` : ""}`;
      stack.prepend(t);
      while (stack.children.length > 3) stack.lastElementChild.remove();
      if (g.result !== "manual") setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 400); }, 6000);
      return;
    }
    const ms = {approved:6000, denied:12000}[g.result];
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
  async function onClick(ev) {
    const v = ev.target.closest("[data-nxview]");
    if (v && !ev.target.closest("button")) {
      v.remove();
      if (opt.view) opt.view(v.dataset.nxview); else location.href = "/#event=" + encodeURIComponent(v.dataset.nxview);
      return;
    }
    const b = ev.target.closest("[data-gate]"); if (!b) return;
    if (b.dataset.gate === "close") return closeGate();
    b.disabled = true;
    try {
      const r = await fetch(`/api/entrance/${b.dataset.id}/decide`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({decision:b.dataset.gate, note:"manual check at the entrance"})});
      if (!r.ok) throw new Error(r.status);
      closeGate();
      const t = b.closest(".nxt"); if (t) t.remove();
    } catch { b.disabled = false; b.textContent = "Not saved - try again"; }
  }
  return {init: o => { Object.assign(opt, o); mount(); }, seed, offer, gate, closeGate, chips, who, iname, whoOf};
})();
