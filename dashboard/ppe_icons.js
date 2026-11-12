/* NETRAX PPE icons (12 Nov polish). Simple flat original shapes, inline SVG, work offline.
   ppeIcon(item, state, size)  item: helmet vest gloves goggles mask boots
                               state: "ok" (green item) | "miss" (dark item in a red prohibition sign) | "unk" (grey item + "?")
   ppeRow(required, ok, missing, uncertain, size)  icons in a fixed order with a small label + tooltip ("Helmet: missing")
   ppeRowFor(event, size)      the same from an alert / entrance record (old records without items_ok etc. still work) */
(function () {
  const ORDER = ["helmet", "vest", "gloves", "goggles", "mask", "boots"];
  const LABEL = {helmet: "Helmet", vest: "Vest", gloves: "Gloves", goggles: "Goggles", mask: "Mask", boots: "Shoes"};
  const LONG = {helmet: "Helmet", vest: "Safety vest", gloves: "Gloves", goggles: "Safety goggles", mask: "Mask", boots: "Safety shoes"};
  const STATE = {ok: "worn", miss: "missing", unk: "cannot see"};
  // each shape: main parts use fill="currentColor"; details use the "hi" (lighter) colour
  const SHAPE = {
    helmet: `<path d="M9 31a15 15 0 0 1 30 0z" fill="currentColor"/><rect x="5" y="30" width="38" height="5" rx="2.5" fill="currentColor"/><rect x="22" y="15" width="4" height="15" rx="2" fill="var(--hi)"/>`,
    vest: `<path d="M13 8h7l4 9 4-9h7l5 8v28H30V31h-12v13H8V16z" fill="currentColor"/><rect x="8" y="27" width="10" height="3" fill="var(--hi)"/><rect x="30" y="27" width="10" height="3" fill="var(--hi)"/><rect x="23" y="17" width="2" height="14" fill="var(--hi)"/>`,
    gloves: `<rect x="15" y="19" width="19" height="17" rx="4" fill="currentColor"/><rect x="15.5" y="8" width="4" height="15" rx="2" fill="currentColor"/><rect x="20.5" y="6" width="4" height="16" rx="2" fill="currentColor"/><rect x="25.5" y="7" width="4" height="15" rx="2" fill="currentColor"/><rect x="30.3" y="10" width="3.7" height="13" rx="1.8" fill="currentColor"/><rect x="7" y="19" width="4.5" height="12" rx="2.2" transform="rotate(-35 9 25)" fill="currentColor"/><rect x="16" y="37" width="17" height="6" rx="1.5" fill="currentColor"/><rect x="16" y="37" width="17" height="2" fill="var(--hi)"/>`,
    goggles: `<rect x="3" y="22" width="6" height="3" rx="1.5" fill="currentColor"/><rect x="39" y="22" width="6" height="3" rx="1.5" fill="currentColor"/><rect x="8" y="16" width="15" height="14" rx="6" fill="currentColor"/><rect x="25" y="16" width="15" height="14" rx="6" fill="currentColor"/><rect x="11" y="19" width="9" height="8" rx="4" fill="var(--hi)"/><rect x="28" y="19" width="9" height="8" rx="4" fill="var(--hi)"/><rect x="21" y="21" width="6" height="3" fill="currentColor"/>`,
    mask: `<path d="M11 21 3 15M11 31l-8 5M37 21l8-6M37 31l8 5" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/><path d="M10 19q14-6 28 0v10q-14 10-28 0z" fill="currentColor"/><path d="M14 23h20M14 27h20" stroke="var(--hi)" stroke-width="1.6"/>`,
    boots: `<path d="M13 6h13v22l11 5q4 2 4 6v3H13z" fill="currentColor"/><rect x="11" y="41" width="31" height="4" rx="1.5" fill="currentColor"/><rect x="13" y="12" width="13" height="2.5" fill="var(--hi)"/>`
  };
  function ppeIcon(item, state, size) {
    const s = size || 30, body = SHAPE[item] || SHAPE.helmet;
    const tone = state === "ok" ? "var(--ok)" : state === "miss" ? "var(--miss-ink,#3f4654)" : "var(--unk)";
    let over = "";
    if (state === "miss") over = `<circle cx="24" cy="24" r="20.5" fill="none" stroke="var(--crit)" stroke-width="4"/><path d="M10 10 38 38" stroke="var(--crit)" stroke-width="4" stroke-linecap="round"/>`;
    if (state === "unk") over = `<circle cx="39.5" cy="8.5" r="7" fill="var(--panel,#fff)" stroke="var(--unk)" stroke-width="2"/><text x="39.5" y="12.4" text-anchor="middle" font-size="11" font-weight="700" fill="var(--unk)" font-family="Segoe UI,Arial,sans-serif">?</text>`;
    const g = state === "miss" ? `<g transform="translate(24 24) scale(.66) translate(-24 -24)">${body}</g>` : body;
    return `<svg class="ppe-svg ppe-${state}" width="${s}" height="${s}" viewBox="0 0 48 48" aria-hidden="true" style="color:${tone};--hi:${state === "ok" ? "rgba(255,255,255,.55)" : "rgba(255,255,255,.35)"}">${g}${over}</svg>`;
  }
  const arr = x => Array.isArray(x) ? x : typeof x === "string" && x ? (x.startsWith("[") ? JSON.parse(x) : x.split(",")) : [];
  // fresh: items just ADDED to an open alert ("also missing") get an outline
  function ppeRow(required, ok, missing, uncertain, size, fresh) {
    required = arr(required); ok = arr(ok); missing = arr(missing); uncertain = arr(uncertain); fresh = arr(fresh);
    const show = ORDER.filter(i => required.includes(i) || ok.includes(i) || missing.includes(i) || uncertain.includes(i));
    return `<div class="ppe-row" role="list">${show.map(i => {
      const st = missing.includes(i) ? "miss" : ok.includes(i) ? "ok" : "unk";
      return `<span class="ppe-i s-${st}${fresh.includes(i) ? " fresh" : ""}" role="listitem" title="${LONG[i]}: ${STATE[st]}">${ppeIcon(i, st, size)}<small>${LABEL[i]}</small></span>`;
    }).join("")}</div>`;
  }
  // alert / entrance record -> icons. New records carry items_required / items_ok / items_uncertain; old ones only the missing list.
  function ppeRowFor(e, size, fresh) {
    e = e || {};
    const missing = arr(e.items_missing || e.missing), ok = arr(e.items_ok || e.ok), unc = arr(e.items_uncertain || e.uncertain);
    const req = arr(e.items_required || e.required);
    return ppeRow(req.length ? req : missing.concat(ok, unc), ok, missing, unc, size, fresh);
  }
  window.ppeIcon = ppeIcon; window.ppeRow = ppeRow; window.ppeRowFor = ppeRowFor;
  window.PPE_ORDER = ORDER; window.PPE_LABEL = LONG;
})();
