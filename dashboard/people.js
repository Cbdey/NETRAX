/* NETRAX person label (12 Nov polish) - the SAME identity rule on every page:
     registered worker   -> "Name · W-004"
     unknown person      -> "Unregistered person" (+ small track number)
     unauthorised entry  -> "Name · W-002" + amber UNAUTHORISED badge
   Names hidden for the viewer role (worker_id "W-***") stay hidden: "Registered worker".
   personLabel(e) -> HTML;  personText(e) -> plain text (titles, aria, voice previews). */
(function () {
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  function parts(e) {
    e = e || {};
    const id = e.worker_id || (e.identity && e.identity.worker_id) || "";
    const name = e.worker_name || (e.identity && e.identity.name) || "";
    if (id === "W-***") return {main: "Registered worker", sub: "", reg: true};
    if (String(id).startsWith("W-")) return {main: name ? `${name} · ${id}` : id, sub: "", reg: true};
    const tr = e.track_id != null && e.track_id !== "" ? `#${e.track_id}` : "";
    return {main: "Unregistered person", sub: tr, reg: false};
  }
  function personText(e) {
    const p = parts(e);
    return p.main + (e && e.type === "unauthorized_entry" ? " · UNAUTHORISED" : "");
  }
  function personLabel(e) {
    const p = parts(e);
    const badge = e && e.type === "unauthorized_entry" ? ` <span class="nx-badge warn">UNAUTHORISED</span>` : "";
    return `<span class="nx-person${p.reg ? "" : " unreg"}">${esc(p.main)}${p.sub ? ` <small>${esc(p.sub)}</small>` : ""}</span>${badge}`;
  }
  window.personLabel = personLabel; window.personText = personText;
})();
