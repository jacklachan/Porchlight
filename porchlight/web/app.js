// Porchlight family dashboard. No build step: plain modules, one status fetch, re-render.

const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const DAY_START_HOUR = 6;
const DAY_END_HOUR = 22;

let state = null;
let live = { pc: null, sessionUrl: null, watchTimer: null };
const WATCH_EVERY_MS = 20000;

// ---- talking to the server ------------------------------------------------

function authHeaders() {
  const token = localStorage.getItem("pl_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function api(path, options = {}) {
  const headers = { ...authHeaders(), "X-Porchlight-User": me(), ...(options.headers || {}) };
  let body = options.body;
  if (body && !(body instanceof Blob) && typeof body !== "string") {
    body = JSON.stringify(body);
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, { ...options, headers, body });
  if (res.status === 401 && /dashboard token/.test(await res.clone().text())) {
    const token = prompt("This Porchlight is protected. Enter the dashboard token:");
    if (token) {
      localStorage.setItem("pl_token", token.trim());
      return api(path, options);
    }
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || `Request failed (${res.status})`);
  return data;
}

function me() {
  return $("you").value.trim() || "family";
}

// ---- time -------------------------------------------------------------------

function clock(ms) {
  return new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", timeZone: state.timezone }).format(ms);
}
function minuteOfDay(ms) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: state.timezone,
  }).formatToParts(ms);
  const get = (t) => Number(parts.find((p) => p.type === t).value);
  return get("hour") * 60 + get("minute");
}
function duration(ms) {
  const m = Math.max(0, Math.round(ms / 60000));
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60), r = m % 60;
  return r >= 5 ? `${h} h ${r} min` : `${h} h`;
}

// ---- words for states -------------------------------------------------------

function stateTag(exp) {
  const delivery = exp.kind === "delivery";
  switch (exp.state) {
    case "scheduled": return ["Expected", ""];
    case "arrived": return ["On the porch", "wait"];
    case "overdue_pickup": return ["Still outside", "warn"];
    case "completed": return [delivery ? "Brought in" : "Came by", "good"];
    case "missed": return ["Not seen", "warn"];
    default: return [exp.state, ""];
  }
}
function stateLine(exp) {
  const window = `${clock(exp.window_start)} to ${clock(exp.window_end)}`;
  if (exp.state === "completed" && exp.kind === "delivery" && exp.arrived_at) {
    return `Arrived ${clock(exp.arrived_at)}, brought in ${clock(exp.completed_at)}`;
  }
  if (exp.state === "completed") return `Seen at ${clock(exp.completed_at)}`;
  if (exp.arrived_at) return `Arrived ${clock(exp.arrived_at)}, outside for ${duration(state.now - exp.arrived_at)}`;
  return exp.unplanned ? "Not on the plan" : `Expected ${window}`;
}

// ---- rendering --------------------------------------------------------------

function render() {
  const s = state;
  $("sky").dataset.tone = s.tone;
  $("headline").textContent = s.headline;
  $("person").textContent = `${s.person_name}'s front door`;
  $("asof").textContent = `as of ${clock(s.now)}`;
  document.title = s.alerts.length ? `(${s.alerts.length}) Porchlight` : "Porchlight";

  renderRing();
  renderDay();
  renderNeeds();
  renderCheckins();
  renderPlans();
  renderDoor();
  renderFunnel();
  renderPrivacy();
  renderStandIn();

  const v = s.vision;
  $("vision-note").textContent =
    v.provider === "none"
      ? "No vision model is connected, so every frame is held for you to confirm."
      : `Frames are read by ${v.provider}${v.model ? ` (${v.model})` : ""}. Readings under ${Math.round(v.threshold * 100)}% confidence are held for you.`;
  $("mcp-url").textContent = s.mcp_url;
}

function renderRing() {
  const c = state.connection;
  const dot = $("ring-dot");
  dot.className = "dot";
  let label;
  if (c.simulated) { dot.classList.add("sim"); label = "Local stand-in, not Ring"; }
  else if (c.last_error) { dot.classList.add("bad"); label = "Ring needs a new token"; }
  else if (c.device_name && c.device_online === false) { label = `Ring: ${c.device_name} (offline)`; }
  else if (c.device_name) { dot.classList.add("ok"); label = `Ring: ${c.device_name}`; }
  else if (!c.auth_mode) { dot.classList.add("bad"); label = "Connect Ring"; }
  else label = "Ring: connecting";
  $("ring-label").textContent = label;
  $("ring-facts").innerHTML = [
    ["Talking to", c.simulated ? "the bundled local stand-in (not Ring)" : c.api_base],
    ["Device", c.device_name ? `${c.device_name}${c.device_online === false ? " (offline)" : c.device_online ? " (online)" : ""}` : "none found yet"],
    ["Sign-in", c.auth_mode === "refresh_token" ? "OAuth refresh token" : c.auth_mode === "access_token" ? "Playground access token" : "no token"],
    ["Last checked", c.last_poll ? clock(c.last_poll) : "not yet"],
    ["Problem", c.last_error || "none"],
  ].map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
}

function renderDay() {
  // 6am to 10pm unless something today falls outside that; then the strip widens to fit.
  let startHour = DAY_START_HOUR, endHour = DAY_END_HOUR;
  const planned = state.expectations.filter((e) => !e.unplanned);
  for (const e of planned) {
    startHour = Math.min(startHour, Math.floor(minuteOfDay(e.window_start) / 60));
    endHour = Math.max(endHour, Math.ceil(minuteOfDay(e.window_end) / 60));
  }
  if ((endHour - startHour) % 2) endHour = Math.min(24, endHour + 1);
  const span = (endHour - startHour) * 60;
  const pos = (ms) => Math.min(100, Math.max(0, ((minuteOfDay(ms) - startHour * 60) / span) * 100));
  // Overlapping windows share the strip: each takes the first lane that is free.
  const laneEnds = [];
  const placed = planned.map((e) => {
      const left = pos(e.window_start);
      const right = Math.max(pos(e.window_end), left + 1.2);
      let lane = laneEnds.findIndex((end) => end <= left);
      if (lane === -1) lane = laneEnds.length;
      laneEnds[lane] = right;
      return { e, left, width: right - left, lane };
    });
  const lanes = Math.max(1, laneEnds.length);
  const bars = placed.map(({ e, left, width, lane }) => {
    const height = (36 - (lanes - 1) * 3) / lanes;
    return `<button class="bar" type="button" data-evidence="${esc(e.id)}" data-state="${esc(e.state)}"
      style="left:${left}%;width:${width}%;top:${5 + lane * (height + 3)}px;height:${height}px"
      title="${esc(e.title)}: ${esc(stateTag(e)[0])}">${esc(e.title)}</button>`;
  });
  const nowMinute = minuteOfDay(state.now);
  const nowMark =
    nowMinute >= startHour * 60 && nowMinute <= endHour * 60 ? `<div class="now" style="left:${pos(state.now)}%"></div>` : "";
  $("day-track").innerHTML = bars.join("") + nowMark;
  $("day-track").style.backgroundSize = `${100 / (endHour - startHour)}% 100%, auto`;
  const labels = [];
  for (let h = startHour; h <= endHour; h += 2) {
    labels.push(`<span>${h % 12 || 12}${h % 24 < 12 ? "am" : "pm"}</span>`);
  }
  $("day-hours").innerHTML = labels.join("");
}

function renderNeeds() {
  const cards = [];
  for (const a of state.alerts) {
    cards.push(`<article class="need ${a.level === "urgent" ? "urgent" : ""}">
      <span class="kind">${a.level === "urgent" ? "Urgent" : "Worth a look"} · ${esc(clock(a.ts))}</span>
      <h3>${esc(a.title)}</h3>
      <p>${esc(a.body)}</p>
      <div class="row-actions">
        <button class="btn ghost" type="button" data-evidence="${esc(a.id)}">See the evidence</button>
        <button class="btn ghost" type="button" data-ack="${esc(a.id)}">I've seen this</button>
      </div>
    </article>`);
  }
  for (const p of state.proposed_actions) {
    cards.push(`<article class="need ask">
      <span class="kind">Waiting for your say-so · suggested by ${esc(p.proposed_by === "policy" ? "Porchlight" : "the assistant")}</span>
      <h3>${esc(p.title)}</h3>
      <p>${esc(p.detail || p.reason)}</p>
      <div class="row-actions">
        <button class="btn go" type="button" data-decide="${esc(p.id)}" data-approve="1">Yes, go ahead</button>
        <button class="btn ghost" type="button" data-decide="${esc(p.id)}" data-approve="0">Not now</button>
      </div>
    </article>`);
  }
  for (const o of state.review_queue.slice(0, 3)) {
    const guess =
      o.package_present === null
        ? "Porchlight could not read this frame."
        : `Porchlight thinks ${o.package_present ? "there is a package" : "the porch is clear"}, but is only ${Math.round(o.confidence * 100)}% sure.`;
    cards.push(`<article class="need ask">
      <span class="kind">Your eyes needed · ${esc(clock(o.ts))}</span>
      ${o.frame_url ? `<img src="${esc(o.frame_url)}" alt="Frame from the doorbell at ${esc(clock(o.ts))}">` : ""}
      <h3>Is there a package at the door?</h3>
      <p>${esc(guess)} Nothing changes until you answer.</p>
      <div class="row-actions">
        <button class="btn" type="button" data-review="${esc(o.id)}" data-package="1">Yes, a package</button>
        <button class="btn" type="button" data-review="${esc(o.id)}" data-package="0">No, it is clear</button>
        <button class="btn ghost" type="button" data-dismiss="${esc(o.id)}">Can't tell</button>
      </div>
    </article>`);
  }
  $("needs").hidden = cards.length === 0;
  $("needs-list").innerHTML = cards.join("");
}

function checkinRow(e, compact = false) {
  const [label, tone] = stateTag(e);
  const frame = state.frames[e.id];
  const thumb = compact
    ? ""
    : frame
      ? `<img class="thumb" src="${esc(frame)}" alt="">`
      : `<span class="thumb">${e.kind === "delivery" ? "delivery" : "visit"}</span>`;
  const canCancel = e.state === "scheduled";
  const hasStory = e.state !== "scheduled";
  return `<li class="checkin">
    ${thumb}
    <div><div class="title">${esc(e.title)}</div><div class="when">${esc(stateLine(e))}</div></div>
    <div class="side">
      <span class="tag ${tone}">${esc(label)}</span>
      ${hasStory ? `<button class="link" type="button" data-evidence="${esc(e.id)}">Evidence</button>` : ""}
      ${canCancel ? `<button class="link" type="button" data-cancel="${esc(e.id)}">Stop expecting</button>` : ""}
    </div>
  </li>`;
}

function renderCheckins() {
  $("checkins").innerHTML = state.expectations.map((e) => checkinRow(e)).join("");
  $("checkins-empty").hidden = state.expectations.length > 0;
  $("tomorrow-wrap").hidden = state.tomorrow.length === 0;
  $("tomorrow").innerHTML = state.tomorrow.map((e) => checkinRow(e, true)).join("");
}

function renderPlans() {
  const repeat = { daily: "Every day", weekdays: "Weekdays", weekly: "Weekly" };
  $("plans").innerHTML = state.plans
    .map((p) => {
      const h = Math.floor(p.start_minute / 60), m = String(p.start_minute % 60).padStart(2, "0");
      const start = `${h % 12 || 12}:${m} ${h < 12 ? "AM" : "PM"}`;
      return `<li><span>${esc(p.title)}<small>${esc(repeat[p.repeat] || p.repeat)} from ${start}, ${p.kind}</small></span>
        <button class="link" type="button" data-remove-plan="${esc(p.id)}">Remove</button></li>`;
    })
    .join("");
  $("plans-empty").hidden = state.plans.length > 0;
  $("contacts").innerHTML = state.contacts
    .map((c) => `<li><span>${esc(c.name)}<small>${esc([c.role, c.channel].filter(Boolean).join(" · "))}</small></span>
      <button class="link" type="button" data-remove-contact="${esc(c.id)}">Remove</button></li>`)
    .join("");
  $("contacts-empty").hidden = state.contacts.length > 0;
}

const SOURCES = {
  ring_snapshot: "Ring snapshot",
  ring_live_view: "Frame from Ring live view",
  stand_in_snapshot: "Stand-in frame, not Ring",
};

// Who or what stands behind a reading, in plain words.
function readBy(o) {
  if (o.reviewed_by) return `Confirmed by ${o.reviewed_by}`;
  if (o.status === "unusable") return "Set aside without asking a model";
  if (o.provider === "unchanged") return "Porch unchanged, so the earlier reading stands (no model call)";
  if (o.provider === "none") return "Not read by a model";
  return `Read by ${o.provider}${o.model ? ` (${o.model})` : ""}, ${Math.round(o.confidence * 100)}% sure`;
}

function renderDoor() {
  const o = state.last_observation;
  if (live.pc) return; // live video is showing; leave the frame alone
  const img = $("frame-img");
  if (o && o.frame_url) {
    if (img.getAttribute("src") !== o.frame_url) img.src = o.frame_url;
    img.alt = `Doorbell frame at ${clock(o.ts)}`;
    img.hidden = false;
    $("frame-empty").hidden = true;
    $("frame-cap").textContent = `${SOURCES[o.frame_source] || o.frame_source} · ${clock(o.ts)}`;
    const who = readBy(o);
    const held = o.status === "needs_review" ? " Held for you to confirm." : "";
    $("reading").innerHTML = `${esc(o.summary)}${esc(held)}<span class="meta">${esc(who)} · frame <span class="mono">${esc((o.snapshot_sha256 || "").slice(0, 12))}</span></span>`;
  } else {
    img.hidden = true;
    $("frame-empty").hidden = false;
    $("frame-cap").textContent = "";
    $("reading").textContent = "";
  }
}

function renderFunnel() {
  const f = state.frame_stats;
  if (!f || !f.frames) return ($("funnel").textContent = "");
  const parts = [`${f.frames} frame${f.frames === 1 ? "" : "s"} today`];
  if (f.read_by_model) parts.push(`${f.read_by_model} read by the model`);
  if (f.unchanged) parts.push(`${f.unchanged} unchanged, so not sent`);
  if (f.unusable) parts.push(`${f.unusable} unusable`);
  if (f.confirmed_by_person) parts.push(`${f.confirmed_by_person} confirmed by a person`);
  if (f.events_skipped) parts.push(`${f.events_skipped} event${f.events_skipped === 1 ? "" : "s"} not worth a look`);
  $("funnel").textContent = parts.join(" · ");
}

function renderPrivacy() {
  const paused = state.paused_until;
  $("pause-tag").hidden = !paused;
  $("resume-btn").hidden = !paused;
  document.querySelectorAll("[data-pause]").forEach((b) => (b.hidden = !!paused));
  const days = state.retention_days;
  const keep = `Frames are deleted after ${days} day${days === 1 ? "" : "s"}; what was seen stays in the record. Nobody is identified, and frames never leave this server except to the model that reads them.`;
  $("privacy-note").textContent = paused
    ? `Paused${state.paused_forever ? "" : ` until ${clock(paused)}`}. Nothing at the door is being watched or recorded, and anything due in this time will be marked "not watched", not "missed".`
    : `${state.person_name} or anyone in the family can pause Porchlight at any time. ${keep}`;
  const offline = state.connection.device_online === false && !state.connection.simulated;
  if (offline && !$("door-msg").textContent) {
    doorMessage("This device is currently offline. Live video and motion events are unavailable until it reconnects.");
  }
}

function renderStandIn() {
  const show = state.demo.enabled && state.connection.simulated;
  $("stand-in").hidden = !show;
  const offset = state.demo.clock_offset_minutes;
  $("clock-note").textContent = offset ? `The stand-in clock is ${duration(offset * 60000)} ahead of real time.` : "";
}

async function renderLedger() {
  const { entries } = await api("/api/ledger?limit=40");
  $("ledger").innerHTML = entries
    .map((e) => `<li><time>${esc(clock(e.ts))}</time><div>${esc(e.what)} <span class="who">· ${esc(e.actor)}</span>
      ${e.rule_id ? `<span class="rule"><code>${esc(e.rule_id)}</code> ${esc(e.rule || "")}</span>` : ""}</div></li>`)
    .join("") || `<li><time></time><div class="hint">Nothing has happened yet.</div></li>`;
}

// ---- evidence ---------------------------------------------------------------

async function showEvidence(id) {
  const chain = await api(`/api/evidence/${encodeURIComponent(id)}`);
  $("ev-title").textContent = `Evidence: ${chain.subject.title}`;
  $("ev-steps").innerHTML = chain.ledger
    .map((e) => `<li><div class="what">${esc(clock(e.ts))} · ${esc(e.what)}</div>
      ${e.rule_id ? `<div class="rule"><code>${esc(e.rule_id)}</code> ${esc(e.rule || "")}</div>` : `<div class="rule">by ${esc(e.actor)}</div>`}</li>`)
    .join("");
  $("ev-frames").innerHTML = chain.observations
    .map((o) => {
      const who = readBy(o);
      return `<figure class="ev-frame" style="margin:0">
        ${o.frame_url ? `<img src="${esc(o.frame_url)}" alt="Frame at ${esc(clock(o.ts))}">` : ""}
        <div><b>${esc(clock(o.ts))}</b> · ${esc(o.summary)}<br>${esc(who)} · ${esc(SOURCES[o.frame_source] || o.frame_source)}
        <span class="hash mono">${esc(o.id)} · sha256 ${esc(o.snapshot_sha256 || "")}</span></div>
      </figure>`;
    })
    .join("");
  $("ev-events").innerHTML = chain.events.length
    ? "Ring events: " + chain.events.map((e) => `${esc(e.type)}${e.sub_type ? ` (${esc(e.sub_type)})` : ""} at ${esc(clock(e.ts))} via ${esc(e.source)} <span class="mono">${esc(e.id)}</span>`).join("; ")
    : "";
  $("evidence-dialog").showModal();
}

// ---- live view (WHEP) ---------------------------------------------------------

function doorMessage(text, bad = false) {
  $("door-msg").textContent = text;
  $("door-msg").classList.toggle("bad", bad);
}

async function startLive() {
  doorMessage("Starting live view…");
  try {
    const pc = new RTCPeerConnection({ iceServers: [{ urls: "stun:stun.l.google.com:19302" }] });
    live.pc = pc;
    pc.addTransceiver("audio", { direction: "sendrecv" });
    pc.addTransceiver("video", { direction: "recvonly" });
    pc.ontrack = (e) => {
      if (e.streams[0]) {
        $("live").srcObject = e.streams[0];
        $("live").hidden = false;
        $("frame-img").hidden = true;
        $("frame-empty").hidden = true;
        $("frame-cap").textContent = "Ring live view";
        $("live-actions").hidden = false;
        doorMessage("");
      }
    };
    await pc.setLocalDescription(await pc.createOffer());
    await new Promise((resolve) => {
      if (pc.iceGatheringState === "complete") return resolve();
      const timer = setTimeout(resolve, 3000);
      pc.onicegatheringstatechange = () => {
        if (pc.iceGatheringState === "complete") { clearTimeout(timer); resolve(); }
      };
    });
    const answer = await api("/api/ring/whep", { method: "POST", body: { sdp_offer: pc.localDescription.sdp } });
    live.sessionUrl = answer.session_url;
    await pc.setRemoteDescription({ type: "answer", sdp: answer.sdp_answer });
  } catch (err) {
    await stopLive();
    doorMessage(`Live view did not start: ${err.message}`, true);
  }
}

async function stopLive() {
  clearInterval(live.watchTimer);
  live.watchTimer = null;
  $("watch").checked = false;
  live.pc?.close();
  live.pc = null;
  $("live").srcObject = null;
  $("live").hidden = true;
  $("live-actions").hidden = true;
  if (live.sessionUrl) {
    api("/api/ring/whep/stop", { method: "POST", body: { session_url: live.sessionUrl } }).catch(() => {});
    live.sessionUrl = null;
  }
  if (state) renderDoor();
}

async function captureLive(quiet = false) {
  const video = $("live");
  if (!video.videoWidth) return quiet ? null : doorMessage("The live view has no picture yet.", true);
  const canvas = document.createElement("canvas");
  const scale = Math.min(1, 1280 / video.videoWidth);
  canvas.width = Math.round(video.videoWidth * scale);
  canvas.height = Math.round(video.videoHeight * scale);
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  const blob = await new Promise((r) => canvas.toBlob(r, "image/jpeg", 0.85));
  if (!quiet) doorMessage("Reading the frame…");
  try {
    const { observation } = await api("/api/observations/frame", { method: "POST", body: blob, headers: { "Content-Type": "image/jpeg" } });
    const said =
      observation.status === "needs_review" ? "Frame saved. It needs your confirmation above."
      : observation.status === "unusable" ? observation.summary
      : `${clock(observation.ts)}: ${observation.summary}`;
    doorMessage(quiet ? `Watching. ${said}` : said);
    await refresh();
  } catch (err) {
    doorMessage(err.message, true);
  }
}

// ---- actions ------------------------------------------------------------------

async function act(button, fn) {
  button.disabled = true;
  try {
    await fn();
    await refresh();
  } catch (err) {
    doorMessage(err.message, true);
    alert(err.message);
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("click", (event) => {
  const b = event.target.closest("button");
  if (!b) return;
  const d = b.dataset;
  if (d.open) return $(d.open).showModal();
  if ("close" in d) return b.closest("dialog").close();
  if (d.evidence) return showEvidence(d.evidence).catch((e) => alert(e.message));
  if (d.ack) return act(b, () => api(`/api/alerts/${d.ack}/ack`, { method: "POST", body: { by: me() } }));
  if (d.decide) return act(b, () => api(`/api/actions/${d.decide}/decide`, { method: "POST", body: { approve: d.approve === "1", by: me() } }));
  if (d.review) return act(b, () => api(`/api/observations/${d.review}/review`, { method: "POST", body: { package_present: d.package === "1", reviewer: me() } }));
  if (d.dismiss) return act(b, () => api(`/api/observations/${d.dismiss}/dismiss`, { method: "POST", body: { by: me() } }));
  if (d.cancel) return act(b, () => api(`/api/expectations/${d.cancel}`, { method: "DELETE" }));
  if (d.removePlan) return act(b, () => api(`/api/plans/${d.removePlan}`, { method: "DELETE" }));
  if (d.removeContact) return act(b, () => api(`/api/contacts/${d.removeContact}`, { method: "DELETE" }));
  if (d.pause !== undefined) return act(b, () => api("/api/privacy/pause", { method: "POST", body: { minutes: Number(d.pause) || null, by: me() } }));
  if (d.scene) return act(b, () => api("/api/demo/scene", { method: "POST", body: { scene: d.scene, event: d.event || "motion" } }));
  if (d.advance) return act(b, () => api("/api/demo/advance", { method: "POST", body: { minutes: Number(d.advance) } }));
});

$("look-btn").addEventListener("click", (e) =>
  act(e.currentTarget, async () => {
    doorMessage("Asking the doorbell for a frame…");
    const { observation } = await api("/api/porch/check", { method: "POST" });
    doorMessage(observation.status === "needs_review" ? "Got a frame. It needs your confirmation above." : "");
  }),
);
$("live-btn").addEventListener("click", () => (live.pc ? stopLive() : startLive()));
$("live-stop").addEventListener("click", stopLive);
$("capture-btn").addEventListener("click", () => captureLive());
// Keep watching: look at the live view every 20 seconds. Unchanged frames cost nothing; only a change is read.
$("watch").addEventListener("change", (e) => {
  clearInterval(live.watchTimer);
  live.watchTimer = e.target.checked ? setInterval(() => captureLive(true), WATCH_EVERY_MS) : null;
  if (e.target.checked) captureLive(true);
});
$("reset-btn").addEventListener("click", (e) => {
  if (confirm("Clear everything in this local stand-in session?")) act(e.currentTarget, () => api("/api/demo/reset", { method: "POST" }));
});
$("resume-btn").addEventListener("click", (e) => act(e.currentTarget, () => api("/api/privacy/resume", { method: "POST", body: { by: me() } })));
$("delete-frames-btn").addEventListener("click", (e) => {
  if (confirm("Delete every stored frame? The record of what was seen stays, but the pictures cannot be brought back.")) {
    act(e.currentTarget, () => api("/api/privacy/delete-frames", { method: "POST", body: { by: me() } }));
  }
});
$("ring-pill").addEventListener("click", () => $("ring-dialog").showModal());
$("you").value = localStorage.getItem("pl_name") || "";
$("you").addEventListener("change", () => localStorage.setItem("pl_name", $("you").value.trim()));

function wireForm(formId, submit) {
  const form = $(formId);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const error = form.querySelector(".form-error");
    error.textContent = "";
    try {
      await submit(new FormData(form));
      form.reset();
      form.closest("dialog").close();
      await refresh();
    } catch (err) {
      error.textContent = err.message;
    }
  });
}

wireForm("add-form", (f) =>
  api("/api/expectations", {
    method: "POST",
    body: {
      title: f.get("title"),
      kind: f.get("kind"),
      start_time: f.get("day") + f.get("start"),
      end_time: f.get("day") + f.get("end"),
      collect_within_minutes: f.get("kind") === "delivery" ? Number(f.get("collect")) : null,
    },
  }),
);
$("add-form").addEventListener("change", () => {
  $("collect-field").hidden = new FormData($("add-form")).get("kind") !== "delivery";
});
wireForm("plan-form", (f) =>
  api("/api/plans", {
    method: "POST",
    body: { title: f.get("title"), kind: f.get("kind"), start_time: f.get("start"), duration_minutes: Number(f.get("duration")), repeat: f.get("repeat") },
  }),
);
wireForm("contact-form", (f) =>
  api("/api/contacts", { method: "POST", body: { name: f.get("name"), role: f.get("role") || "Neighbour", channel: f.get("channel") || "" } }),
);
wireForm("ring-form", async (f) => {
  const token = String(f.get("token") || "").trim();
  if (!token) throw new Error("Paste a token first.");
  await api("/api/ring/token", { method: "POST", body: { token } });
});

// ---- refresh loop ---------------------------------------------------------------

let refreshing = null;
async function refresh() {
  if (refreshing) return refreshing;
  refreshing = (async () => {
    try {
      state = await api("/api/status");
      render();
      await renderLedger();
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

refresh().catch((err) => { $("headline").textContent = `Could not load: ${err.message}`; });
setInterval(() => refresh().catch(() => {}), 15000);
if (!localStorage.getItem("pl_token")) {
  const events = new EventSource("/api/stream");
  events.onmessage = () => refresh().catch(() => {});
}
