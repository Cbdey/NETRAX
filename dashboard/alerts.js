/* NETRAX alerts on every page (5 Nov: "all alerts in each panel should come in the side part like in the control room;
   when an alert comes it should slightly pop up with a border colour, not cover the page").
   - Side rail (admin, entrance, hardware pages): a docked panel on the right. Every new PPE / restricted-zone / fire alert
     slides into it with a coloured, glowing border; an alert that gets worse ("also missing: gloves") glows again.
     Entrance decisions appear there as a container: GREEN = verified, RED = do not enter, AMBER = manual check.
     The control room has the same cards in its own "Live alerts" and "Entrance" panels (index.html uses card() / keyed()).
   - FIRE / SMOKE: red frame around every page + a slim red banner with the fire level until the fire is resolved.
   - The full green / red / amber entrance screen is shown only on the entrance display (/entrance). */
window.NetraxAlerts = (function () {
  const ITEM = {helmet:"HELMET", vest:"SAFETY VEST", gloves:"GLOVES", boots:"SAFETY SHOES", goggles:"GOGGLES", mask:"MASK"};
  const iname = i => ITEM[i] || String(i).toUpperCase();
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const fmt = t => t ? new Date(t * 1000).toLocaleTimeString("en-IN", {timeZone:"Asia/Kolkata", hour12:false}) : "–";
  const ago = t => { const s = Math.max(0, Date.now() / 1000 - t); return s < 60 ? `${Math.floor(s)} s ago` : s < 3600 ? `${Math.floor(s / 60)} min ago` : `${Math.floor(s / 3600)} h ago`; };
  const POP = new Set(["ppe_missing", "unauthorized_entry", "fire", "smoke"]);
  let opt = {canAct: () => false, onEvent: () => {}, view: null, tone: () => {}, rail: "dock", fireBanner: true, railGate: true};
  const sigs = new Map(), fires = new Map(), active = new Map(), gates = [], fresh = new Map();
  let fireEl = null, gateEl = null, rail = null, gateT = null;

  /* ---------- shared card html + keyed list (no re-render flicker, animation only for new / worse) ---------- */
  const whoOf = e => e.worker_id === "W-***" ? "Registered worker (name hidden)" : e.worker_id && String(e.worker_id).startsWith("W-")
    ? `${e.worker_id} · ${e.worker_name || ""}` : "Unregistered person";
  /* 12 Nov polish: ONE icon card everywhere - person · camera · zone · time · PPE icons · Acknowledge / Resolve.
     No sentences; everything else (snapshot, confidence, messages, escalate, false alarm) is in the details drawer. */
  const person = e => window.personLabel ? personLabel(e) : esc(whoOf(e));
  const ACKED = e => e.status === "acknowledged";
  function alertCard(e) {
    const fire = e.type === "fire" || e.type === "smoke", f = fresh.get(e.event_id);
    const isNew = f && Date.now() - f.at < 12000;
    const pill = isNew ? `<span class="newpill">${(f.added || []).length ? "UPDATED" : "NEW"}</span>` : "";
    const head = fire ? `<span class="fireh">🔥 ${e.type === "smoke" ? "Smoke" : "Fire"} · Level ${e.fire_level || "?"}</span>` : person(e);
    const body = fire ? "" : e.type === "unauthorized_entry" ? `<div class="zonebar">⛔ Not allowed in this zone</div>`
      : window.ppeRowFor ? ppeRowFor(e, 28, f && f.added) : esc((e.items_missing || []).map(iname).join(", "));
    const acts = opt.canAct() ? `<div class="acts">${ACKED(e) ? "" : `<button class="btn btn-primary" data-nxact="ack" data-id="${esc(e.event_id)}">Acknowledge</button>`}<button class="btn btn-ok" data-nxact="resolve" data-id="${esc(e.event_id)}">Resolve</button></div>` : "";
    const ack = ACKED(e) && e.acked_by ? `<div class="ackby">Ack by ${esc(e.acked_by)} ${fmt(e.acked_at)}</div>` : "";
    return {cls: "nx-card" + (fire ? " fire" : e.type === "unauthorized_entry" ? " warn" : "") + (ACKED(e) ? " acked" : " unacked"),
      html: `<div class="top">${head}${pill}<span class="t" data-t="${e.started_at}">${ago(e.started_at)}</span></div>
      <div class="where">${esc(e.camera_name || e.camera_id || "Safety station")} · ${esc(e.zone_name || e.zone_id || "")} · ${fmt(e.started_at)}</div>${body}${ack}${acts}`};
  }
  function gateCard(g) {
    const r = g.override ? g.override.decision : g.result, w = who(g), f = fresh.get(g.event_id);
    // big PERMITTED (green) / NOT PERMITTED (red) / MANUAL CHECK (amber) + person + icons - a small card, never a full screen
    const head = {approved: "PERMITTED", denied: "NOT PERMITTED", manual: "MANUAL CHECK"}[r] || r;
    const can = opt.canAct() && g.result === "manual" && !g.override;
    const tone = {approved: "ok", denied: "crit", manual: "warn"}[r] || "warn";
    return {cls: "nx-card gate " + tone, html: `<div class="top"><i class="lamp" aria-hidden="true"></i><b class="verdict ${tone}">${head}</b>${f && Date.now() - f.at < 12000 ? `<span class="newpill">NEW</span>` : ""}<span class="sp"></span><span class="t" data-t="${g.at}">${ago(g.at)}</span></div>
      <div class="top"><span class="nx-person">${esc(w.name)}</span></div>${window.ppeRowFor ? ppeRowFor(g, 26) : `<div class="items">${chips(g)}</div>`}
      ${g.override ? `<div class="ackby">Decided by ${esc(g.override.by)}</div>` : ""}
      ${can ? `<div class="acts"><button class="btn btn-ok" data-gate="approved" data-id="${esc(g.event_id)}">Allow</button><button class="btn btn-danger" data-gate="denied" data-id="${esc(g.event_id)}">Refuse</button></div>` : ""}`};
  }
  function keyed(box, items, idOf, cardOf, emptyHtml) {
    const nodes = box.__nodes || (box.__nodes = new Map()), keep = new Set();
    let prev = null;
    const ph = box.querySelector(":scope > .nxempty"); if (ph) ph.remove();
    for (const it of items) {
      const id = idOf(it); keep.add(id);
      const c = cardOf(it), sig = c.cls + c.html.replace(/<span class="t"[^<]*<\/span>/, "");
      let n = nodes.get(id);
      if (!n) { n = document.createElement("div"); n.dataset.nxview = id; nodes.set(id, n); }
      if (n.__sig !== sig) {
        n.className = c.cls; n.innerHTML = c.html; n.__sig = sig;
        const f = fresh.get(id);
        if (f && !f.shown) { f.shown = true; n.classList.add((f.added || []).length ? "nx-upd" : "nx-new"); }
      }
      const want = prev ? prev.nextSibling : box.firstChild;
      if (want !== n) box.insertBefore(n, want);
      prev = n;
    }
    for (const [id, n] of nodes) if (!keep.has(id)) { n.remove(); nodes.delete(id); }
    box.querySelectorAll(".t[data-t]").forEach(x => { x.textContent = ago(+x.dataset.t); });
    if (!items.length && emptyHtml) box.insertAdjacentHTML("beforeend", `<div class="nxempty">${emptyHtml}</div>`);
  }

  /* ---------- mount ---------- */
  function mount() {
    if (fireEl) return;
    fireEl = document.createElement("div"); fireEl.className = "nxfire"; fireEl.setAttribute("role", "alert");
    gateEl = document.createElement("div"); gateEl.className = "nxg"; gateEl.setAttribute("role", "alertdialog");
    document.body.append(fireEl, gateEl);
    if (opt.rail === "dock") {
      rail = document.createElement("aside"); rail.className = "nxrail"; rail.setAttribute("aria-label", "Live alerts");
      rail.innerHTML = `<div class="rh"><b>Live alerts</b><span class="pill p-crit" id="nxrN">0</span><span class="sp"></span><button class="btn sm" id="nxrHide" aria-expanded="true" title="Hide / show">⟩</button></div>
        <div class="rbody">
          <div class="rgrp g-fire" id="nxrGF"><div class="rsec">🔥 Fire &amp; smoke<span class="rn" id="nxrNF"></span></div><div class="rlist" id="nxrF"></div></div>
          <div class="rgrp g-ppe" id="nxrGA"><div class="rsec">🦺 PPE &amp; access<span class="rn" id="nxrNA"></span></div><div class="rlist" id="nxrA"></div></div>
          ${opt.railGate ? `<div class="rgrp g-gate" id="nxrGG"><div class="rsec">🚪 Entrance</div><div class="rlist" id="nxrG"></div></div>` : ""}</div>`;
      document.body.append(rail); document.body.classList.add("nx-rail-on");
      rail.querySelector("#nxrHide").onclick = () => { const c = document.body.classList.toggle("nx-rail-min"); rail.querySelector("#nxrHide").textContent = c ? "⟨" : "⟩"; };
    }
    document.body.addEventListener("click", onClick);
    document.addEventListener("keydown", ev => { if (ev.key === "Escape") closeGate(); });
    setInterval(renderRail, 15000);
  }
  const sig = e => [(e.items_missing || []).slice().sort().join(","), e.fire_level || 0].join("|");
  const isActive = e => e.status === "open" || e.status === "escalated";

  function seed(events) {
    for (const e of events) {
      if (POP.has(e.type)) { sigs.set(e.event_id, sig(e)); fireTrack(e); if (isActive(e)) active.set(e.event_id, e); }
    }
    renderFire(); renderRail();
  }
  function seedGates(list) { gates.splice(0, gates.length, ...list.slice(0, 6)); renderRail(); }

  /* ---------- incidents ---------- */
  function offer(e) {
    if (!e || !POP.has(e.type)) return;
    mount();
    fireTrack(e); renderFire();
    const old = sigs.get(e.event_id), now = sig(e);
    sigs.set(e.event_id, now);
    if (isActive(e)) active.set(e.event_id, e); else active.delete(e.event_id);
    if (isActive(e) && old !== now) {
      const was = old ? old.split("|")[0].split(",") : null;
      const added = was ? (e.items_missing || []).filter(i => !was.includes(i)) : [];
      if (!old || added.length || (e.fire_level || 0) > +(old.split("|")[1] || 0)) {
        fresh.set(e.event_id, {at: Date.now(), added, shown: false});
        opt.tone(e);
      }
    }
    renderRail();
  }
  /* 11 Nov: the side panel has the SAME sections as the control room (fire & smoke / PPE & access / entrance); a new alert
     pops inside its own section and that section's border glows once */
  function bumpGrp(id) { const b = rail.querySelector("#" + id); if (!b) return; b.classList.remove("bump"); void b.offsetWidth; b.classList.add("bump");
    clearTimeout(b._bt); b._bt = setTimeout(() => b.classList.remove("bump"), 2800); }
  const isFire = e => e.type === "fire" || e.type === "smoke";
  function renderRail() {
    if (!rail) return;
    const list = [...active.values()].sort((a, b) => (b.fire_level || 0) - (a.fire_level || 0) || b.started_at - a.started_at);
    const fl = list.filter(isFire), pl = list.filter(e => !isFire(e));
    rail.querySelector("#nxrN").textContent = list.length;
    rail.classList.toggle("hot", list.length > 0);
    rail.querySelector("#nxrNF").textContent = fl.length || ""; rail.querySelector("#nxrNA").textContent = pl.length || "";
    rail.querySelector("#nxrGF").classList.toggle("hot", fl.length > 0); rail.querySelector("#nxrGA").classList.toggle("hot", pl.length > 0);
    const popF = fl.some(e => fresh.has(e.event_id) && !fresh.get(e.event_id).shown), popA = pl.some(e => fresh.has(e.event_id) && !fresh.get(e.event_id).shown);
    keyed(rail.querySelector("#nxrF"), fl.slice(0, 10), e => e.event_id, alertCard, "✓ No fire or smoke.");
    keyed(rail.querySelector("#nxrA"), pl.slice(0, 30), e => e.event_id, alertCard, "✓ No PPE or access alert.");
    if (popF) bumpGrp("nxrGF"); if (popA) bumpGrp("nxrGA");
    const g = rail.querySelector("#nxrG");
    if (g) keyed(g, gates.filter(x => Date.now() / 1000 - x.at < 3600).slice(0, 6), x => x.event_id, gateCard, "No entrance checks in the last hour.");
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
    if (i.kind === "registered" && i.worker_id) return {name: i.name ? `${i.name} · ${i.worker_id}` : i.worker_id, how:{face:"IDENTIFIED BY FACE", rfid:"IDENTIFIED BY RFID CARD", "face+rfid":"IDENTIFIED BY FACE + RFID CARD"}[i.by] || "REGISTERED WORKER"};
    if (i.kind === "unregistered" || !g.identity && !g.worker_id) return {name:"Unregistered person", how:"NOT IN THE WORKER LIST · LOGGED AS UNREGISTERED"};
    return {name:"Registered worker", how:"NAME HIDDEN FOR YOUR ROLE"};
  }
  function chips(g) {
    return (g.required || []).map(i => (g.missing || []).includes(i) ? `<span class="item">✖ ${iname(i)}</span>` : (g.ok || []).includes(i) ? `<span class="item ok">✓ ${iname(i)}</span>` : `<span class="item unk">? ${iname(i)}</span>`).join("");
  }
  function closeGate() { if (gateEl) gateEl.classList.remove("show"); clearTimeout(gateT); }
  function gate(g, o) {
    mount(); o = o || {};
    const at = gates.findIndex(x => x.event_id === g.event_id);
    if (at >= 0) gates.splice(at, 1); else fresh.set(g.event_id, {at: Date.now(), shown: false});
    gates.unshift(g); gates.splice(8);
    renderRail();
    if (g.override) { closeGate(); return; }
    if (!o.screen || !opt.gateScreen) return; // 5 Nov: no full-screen entrance overlay any more (small cards only)
    const w = who(g), can = opt.canAct() && g.result === "manual";
    const head = {approved:"VERIFIED — YOU MAY ENTER", denied:"DO NOT ENTER", manual:"MANUAL CHECK REQUIRED"}[g.result] || g.result;
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
    const a = ev.target.closest("[data-nxact]");
    if (a) {
      a.disabled = true;
      try {
        const r = await fetch(`/api/events/${a.dataset.id}/${a.dataset.nxact}`, {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
        if (!r.ok) throw new Error(r.status);
        const u = await r.json(); offer(u); opt.onEvent(u);
      } catch { a.disabled = false; a.textContent = "Failed - retry"; }
      return;
    }
    const b = ev.target.closest("[data-gate]");
    if (b) {
      if (b.dataset.gate === "close") return closeGate();
      b.disabled = true;
      try {
        const r = await fetch(`/api/entrance/${b.dataset.id}/decide`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({decision:b.dataset.gate, note:"manual check at the entrance"})});
        if (!r.ok) throw new Error(r.status);
        closeGate();
      } catch { b.disabled = false; b.textContent = "Not saved - try again"; }
      return;
    }
    const v = ev.target.closest("[data-nxview]");
    if (v && v.closest(".nxrail, .nxside") && !ev.target.closest("button:not([data-nxview])")) {
      if (opt.view) opt.view(v.dataset.nxview); else location.href = "/#event=" + encodeURIComponent(v.dataset.nxview);
    }
  }
  return {init: o => { Object.assign(opt, o); mount(); }, seed, seedGates, offer, gate, closeGate, chips, who, iname, whoOf,
          alertCard, gateCard, keyed, fresh};
})();
