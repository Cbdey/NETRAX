/* NETRAX PPE icons (12 Nov polish; 13 Nov: colour PNG pictures, offline, same as the camera overlay).
   ppeIcon(item, state, size)  item: helmet vest gloves goggles mask boots
                               state: "ok" (green tick) | "miss" (red prohibition sign) | "unk" (faded grey + "?")
   ppeRow(required, ok, missing, uncertain, size)  icons in a fixed order with a small label + tooltip ("Helmet: missing")
   ppeRowFor(event, size)      the same from an alert / entrance record (old records without items_ok etc. still work) */
(function () {
  const ORDER = ["helmet", "vest", "gloves", "goggles", "mask", "boots"];
  const LABEL = {helmet: "Helmet", vest: "Vest", gloves: "Gloves", goggles: "Goggles", mask: "Mask", boots: "Shoes"};
  const LONG = {helmet: "Helmet", vest: "Safety vest", gloves: "Gloves", goggles: "Safety goggles", mask: "Mask", boots: "Safety shoes"};
  const STATE = {ok: "worn", miss: "missing", unk: "cannot see"};
  // 13 Nov (jury: "colourful real icons"): the same colour pictures as inside the camera picture (edge/annotate.py),
  // drawn by tools/make_ppe_icons.py -> dashboard/icons/ppe/<item>_<ok|miss|unk>.png. ok = green tick, miss = red
  // prohibition sign, unk = faded grey + "?".
  const BASE = "/static/icons/ppe/";
  function ppeIcon(item, state, size) {
    const s = size || 30, it = ORDER.includes(item) ? item : "helmet", st = ["ok", "miss", "unk"].includes(state) ? state : "unk";
    return `<img class="ppe-svg ppe-${st}" src="${BASE}${it}_${st}.png?v=13nov2" width="${s}" height="${s}" alt="" draggable="false" loading="lazy">`;
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
