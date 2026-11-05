/* NETRAX live video (4 Nov): one WebSocket brings every camera's newest picture the moment it is ready
   (backend /ws/video). Any <img data-live="cam_01"> on the page shows that camera; nothing else to wire.
   Falls back to polling /api/frame if the socket is down, so a picture never stops.
   NetraxVideo.age[cam] = picture age in ms (capture -> on this screen), NetraxVideo.fps[cam] = pictures per second. */
window.NetraxVideo = (function () {
  const age = {}, fps = {}, count = {}, urls = {}, seen = new Set();
  let ws = null, live = false, lastWanted = "", onAge = null;
  const dec = new TextDecoder();

  // 5 Nov: decode OFF-screen first, swap only a fully decoded picture (no half-drawn / blank frames, no main-thread
  // decode stall); while one is decoding only the NEWEST waiting picture is kept, so the video never builds up lag.
  const busy = {}, waiting = {};
  function show(cam, blob, cap, srv, sent) {
    if (busy[cam]) { waiting[cam] = [blob, cap, srv, sent]; return; }
    busy[cam] = true;
    const u = URL.createObjectURL(blob), pre = new Image();
    pre.src = u;
    const done = () => {
      busy[cam] = false;
      const w = waiting[cam]; waiting[cam] = null;
      if (w) show(cam, ...w);
    };
    (pre.decode ? pre.decode() : Promise.resolve()).then(() => {
      requestAnimationFrame(() => { paint(cam, u, cap, srv, sent); done(); });
    }, () => { URL.revokeObjectURL(u); done(); });
  }
  function paint(cam, u, cap, srv, sent) {
    const imgs = document.querySelectorAll(`img[data-live="${cam}"]`);
    if (!imgs.length) { URL.revokeObjectURL(u); return; }
    imgs.forEach(i => { i.src = u; });
    const old = urls[cam]; urls[cam] = u;
    if (old) setTimeout(() => URL.revokeObjectURL(old), 1000);
    count[cam] = (count[cam] || 0) + 1;
    if (cap && srv) {
      const a = Math.max(0, Math.round((srv - cap) * 1000 + (performance.now() - sent)));
      age[cam] = a;
      if (onAge) onAge(cam, a);
    }
  }
  function wanted() {
    const s = new Set();
    document.querySelectorAll("img[data-live]").forEach(i => { if (i.dataset.live) s.add(i.dataset.live); });
    return [...s].sort();
  }
  function subscribe() {
    const w = wanted(), key = w.join(",");
    if (ws && ws.readyState === 1 && key !== lastWanted) { ws.send(JSON.stringify({cams: w})); lastWanted = key; }
  }
  function connect() {
    ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/video`);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => { live = true; lastWanted = ""; subscribe(); };
    ws.onmessage = m => {
      const t = performance.now();
      const u8 = new Uint8Array(m.data), nl = u8.indexOf(10);
      if (nl < 0) return;
      const [cam, cap, srv] = dec.decode(u8.subarray(0, nl)).split("|");
      show(cam, new Blob([u8.subarray(nl + 1)], {type: "image/jpeg"}), parseFloat(cap), parseFloat(srv), t);
    };
    ws.onclose = ev => { live = false; if (ev.code === 4401) return; setTimeout(connect, 1500); };
    ws.onerror = () => { try { ws.close(); } catch {} };
  }
  async function poll(cam) {                         // fallback while the socket is down
    if (seen.has(cam)) return; seen.add(cam);
    while (true) {
      if (live || document.hidden || !document.querySelector(`img[data-live="${cam}"]`)) { await new Promise(r => setTimeout(r, 700)); continue; }
      const t0 = performance.now();
      try {
        const r = await fetch(`/api/frame/${cam}?t=${Date.now()}`, {cache: "no-store"});
        if (r.status === 200) show(cam, await r.blob(), parseFloat(r.headers.get("X-Captured-At")), parseFloat(r.headers.get("X-Server-Time")), t0);
      } catch {}
      await new Promise(r => setTimeout(r, Math.max(20, 90 - (performance.now() - t0))));
    }
  }
  setInterval(() => {                                  // pictures per second, per camera, over the last 2 s
    for (const c of Object.keys(count)) { fps[c] = +(count[c] / 2).toFixed(1); count[c] = 0; }
    subscribe();
    wanted().forEach(poll);
  }, 2000);
  connect();
  return {age, fps, onAge: f => { onAge = f; }, isLive: () => live};
})();
