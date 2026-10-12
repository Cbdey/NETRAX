/* NETRAX look & feel: day / night switch + motion. Reads the page, never changes its text or data.
   - theme: saved in this browser (localStorage), first visit follows the computer's light / dark setting
   - boxes pop in on first appearance; re-rendered boxes (every second) do not replay the animation
   - a NEW alert pops + shakes; an active Critical / High / Medium alert gets a red reason line and a breathing glow
   - numbers that change (KPIs, people inside, worker count) do a small bump */
(function () {
  const KEY = "netrax-theme", root = document.documentElement;
  const forced = new URLSearchParams(location.search).get("theme");       // ?theme=dark / ?theme=light
  const saved = forced || (() => { try { return localStorage.getItem(KEY); } catch { return null; } })();
  const dark0 = saved ? saved === "dark" : !!(window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches);
  root.dataset.theme = dark0 ? "dark" : "light";

  function setTheme(t) {
    root.classList.add("nx-switching");
    root.dataset.theme = t;
    try { localStorage.setItem(KEY, t); } catch {}
    const b = document.querySelector(".nx-theme");
    if (b) { b.querySelector(".knob").textContent = t === "dark" ? "☾" : "☀";
             b.querySelector(".lbl").textContent = t === "dark" ? "Night" : "Day";
             b.setAttribute("aria-label", `Switch to ${t === "dark" ? "day" : "night"} mode`); }
    setTimeout(() => root.classList.remove("nx-switching"), 400);
  }

  function addSwitch() {
    const head = document.querySelector("header"); if (!head || head.querySelector(".nx-theme")) return;
    const b = document.createElement("button");
    b.type = "button"; b.className = "nx-theme";
    b.innerHTML = `<span class="knob" aria-hidden="true">${"☀"}</span><span class="lbl"></span>`;
    b.onclick = () => setTheme(root.dataset.theme === "dark" ? "light" : "dark");
    const anchor = head.querySelector(".pill") || head.querySelector("nav");
    anchor ? anchor.parentNode.insertBefore(b, anchor) : head.appendChild(b);
    if (!head.querySelector("nav") && head.querySelector(".hwlink")) b.style.marginLeft = "0";
    setTheme(root.dataset.theme);
  }

  // ---------- pop-in without replaying on every re-render ----------
  const POP = ".kpi,.panel,.station,.banner,.tile,.card,.sc,.person,.toast";
  const seen = new Set(), t0 = performance.now();
  const keyOf = el => {
    const id = el.querySelector && el.querySelector("[data-id]");
    if (el.classList.contains("card") && id) return "card:" + id.dataset.id;
    if (el.classList.contains("toast")) return null;                        // always new
    const p = el.parentElement, i = p ? [...p.children].indexOf(el) : 0;
    return (el.className.split(" ")[0]) + ":" + (p && (p.id || p.className)) + ":" + i;
  };
  function mark(el) {
    if (!el.matches || !el.matches(POP)) return;
    const k = keyOf(el);
    if (k && seen.has(k)) return;
    if (k) seen.add(k);
    const early = performance.now() - t0 < 2500;
    if (el.classList.contains("card") && !early) el.classList.add("nx-new");    // a new alert arriving live
    else if (early || !k) {
      const sib = el.parentElement ? [...el.parentElement.children].indexOf(el) : 0;
      el.style.setProperty("--nx-d", (early ? Math.min(sib, 8) * 0.05 : 0) + "s");
      el.classList.add("nx-pop");
    }
  }

  // ---------- live warnings in the alert feed ----------
  function grade(card) {
    const ttl = (card.querySelector(".ttl") || {}).textContent || "";
    const live = !!card.querySelector('[data-a="ack"]');                  // open / escalated, not yet acknowledged
    card.classList.toggle("nx-live", live);
    card.classList.toggle("nx-crit", live && /^CRITICAL/.test(ttl));
    card.classList.toggle("nx-high", live && /^HIGH/.test(ttl));
    card.classList.toggle("nx-med", live && /^(MEDIUM|LOW)/.test(ttl));
  }

  // ---------- value bump ----------
  const BUMP = ["kCompliance", "kActive", "kCams", "kLatency", "nIn", "wCount"];
  const last = {};
  function bumpCheck() {
    BUMP.forEach(id => {
      const el = document.getElementById(id); if (!el) return;
      const v = el.textContent;
      if (last[id] !== undefined && last[id] !== v) {
        el.classList.remove("nx-bump"); void el.offsetWidth; el.classList.add("nx-bump");
      }
      last[id] = v;
    });
    const act = document.getElementById("kActive");                       // red KPI while alerts are open
    if (act) act.closest(".kpi").classList.toggle("nx-hot", parseInt(act.textContent, 10) > 0);
  }

  function scan(node) {
    if (node.nodeType !== 1) return;
    mark(node);
    node.querySelectorAll && node.querySelectorAll(POP).forEach(mark);
    if (node.classList.contains("card")) grade(node);
    node.querySelectorAll && node.querySelectorAll(".card").forEach(grade);
  }

  function start() {
    addSwitch();
    scan(document.body);
    bumpCheck();
    new MutationObserver(muts => {
      for (const m of muts) m.addedNodes.forEach(scan);
      bumpCheck();
    }).observe(document.body, {childList: true, subtree: true, characterData: true});
  }
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", start) : start();
})();
