/**
 * Codebase Digital Twin - two-panel UI.
 *
 * Left panel lists the endpoints the parser found; clicking one asks the
 * backend for a narrative and a Mermaid sequence diagram and renders both.
 * Every node in the rendered diagram is clickable, and the selection is
 * resolved back to a trace node so the chat feature can pick it up.
 */

import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";

mermaid.initialize({
  startOnLoad: false,
  theme: "neutral",
  // The diagram is generated from our own manifest, not user input, and
  // "loose" keeps the rendered SVG inspectable for click binding.
  securityLevel: "loose",
  sequence: { useMaxWidth: false, actorMargin: 60 },
});

const elements = {
  repoName: document.getElementById("repo-name"),
  endpointList: document.getElementById("endpoint-list"),
  endpointCount: document.getElementById("endpoint-count"),
  rightPanel: document.getElementById("right-panel"),
  statusbar: document.getElementById("statusbar"),
  selection: document.getElementById("selection"),
  uploadButton: document.getElementById("upload-button"),
  uploadStatus: document.getElementById("upload-status"),
  zipInput: document.getElementById("zip-input"),
};

/** Id of the endpoint currently being shown, so stale responses can be dropped. */
let activeEntryPointId = null;

/** The most recent diagram selection; the chat feature will read this. */
let selectedNode = null;

// --------------------------------------------------------------------------
// Data
// --------------------------------------------------------------------------

/** Fetch JSON, turning a non-2xx response into an Error with the API's detail. */
async function fetchJson(url, options) {
  let response;
  try {
    response = await fetch(url, options);
  } catch {
    throw new Error("Could not reach the backend. Is the server still running?");
  }

  let payload = null;
  try {
    payload = await response.json();
  } catch {
    // Fall through to the status-based message below.
  }

  if (!response.ok) {
    throw new Error(payload?.detail ?? `Request failed with status ${response.status}.`);
  }
  return payload;
}

// --------------------------------------------------------------------------
// Upload
// --------------------------------------------------------------------------

/** Send a zip to the backend, then re-render the endpoint list from it. */
async function uploadArchive(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".zip")) {
    setUploadStatus("Please choose a .zip archive.", true);
    return;
  }

  elements.uploadButton.disabled = true;
  setUploadStatus(`Analyzing ${file.name}…`);
  elements.endpointList.replaceChildren(hint("Analyzing uploaded project…"));
  elements.rightPanel.replaceChildren(hint("Select an endpoint to trace what it runs."));
  setSelection(null);
  activeEntryPointId = null;

  const body = new FormData();
  body.append("file", file);

  try {
    const data = await fetchJson("/api/upload", { method: "POST", body });
    renderEndpoints(data);
    setUploadStatus(`${data.java_files} Java files analyzed`);
  } catch (error) {
    setUploadStatus(error.message, true);
    elements.endpointList.replaceChildren(hint("Upload failed."));
    showError("Could not analyze that archive", error.message);
  } finally {
    elements.uploadButton.disabled = false;
    elements.zipInput.value = "";
  }
}

function setUploadStatus(message, failed = false) {
  elements.uploadStatus.textContent = message;
  elements.uploadStatus.classList.toggle("failed", failed);
}

function wireUpload() {
  elements.uploadButton.addEventListener("click", () => elements.zipInput.click());
  elements.zipInput.addEventListener("change", (event) =>
    uploadArchive(event.target.files?.[0])
  );

  // Dropping a zip anywhere on the window is quicker than the file picker.
  let dragDepth = 0;
  window.addEventListener("dragenter", (event) => {
    event.preventDefault();
    dragDepth += 1;
    document.body.classList.add("dragging");
  });
  window.addEventListener("dragover", (event) => event.preventDefault());
  window.addEventListener("dragleave", () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) document.body.classList.remove("dragging");
  });
  window.addEventListener("drop", (event) => {
    event.preventDefault();
    dragDepth = 0;
    document.body.classList.remove("dragging");
    uploadArchive(event.dataTransfer?.files?.[0]);
  });
}

// --------------------------------------------------------------------------
// Left panel
// --------------------------------------------------------------------------

async function loadEndpoints() {
  let data;
  try {
    data = await fetchJson("/api/entry-points");
  } catch (error) {
    elements.repoName.textContent = "unavailable";
    elements.endpointList.replaceChildren(hint("Could not load endpoints."));
    showError("Could not load endpoints", error.message);
    return;
  }
  renderEndpoints(data);
}

/** Paint the endpoint list for a freshly loaded repository. */
function renderEndpoints(data) {
  elements.repoName.textContent = data.repo;
  elements.endpointCount.textContent = String(data.entry_points.length);

  if (data.entry_points.length === 0) {
    elements.endpointList.replaceChildren(
      hint("No endpoints found. The project may not expose Spring controllers.")
    );
    return;
  }

  elements.endpointList.replaceChildren(
    ...data.entry_points.map((entry) => renderEndpointItem(entry))
  );
}

/** One row in the endpoint list. */
function renderEndpointItem(entry) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "endpoint";
  button.dataset.id = entry.id;

  const route = document.createElement("span");
  route.className = "endpoint-route";

  const method = document.createElement("span");
  method.className = "method";
  method.textContent = entry.method;

  const path = document.createElement("span");
  path.className = "endpoint-path";
  path.textContent = entry.path;

  route.append(method, path);

  const fn = document.createElement("span");
  fn.className = "endpoint-fn";
  fn.textContent = `${entry.class}.${entry.function}`;

  button.append(route, fn);
  button.addEventListener("click", () => selectEndpoint(entry, button));

  const item = document.createElement("li");
  item.append(button);
  return item;
}

function markSelected(button) {
  for (const other of elements.endpointList.querySelectorAll(".endpoint.selected")) {
    other.classList.remove("selected");
  }
  button.classList.add("selected");
}

// --------------------------------------------------------------------------
// Right panel
// --------------------------------------------------------------------------

async function selectEndpoint(entry, button) {
  markSelected(button);
  activeEntryPointId = entry.id;
  setSelection(null);
  showLoading(`${entry.method} ${entry.path}`);

  let trace;
  try {
    trace = await fetchJson(`/api/trace/${encodeURIComponent(entry.id)}`);
  } catch (error) {
    if (activeEntryPointId === entry.id) {
      showError("Could not generate this trace", error.message);
    }
    return;
  }

  // A slower earlier request must not overwrite a newer selection.
  if (activeEntryPointId !== entry.id) return;
  await showTrace(trace);
}

function showLoading(label) {
  const wrap = document.createElement("div");

  const title = document.createElement("h2");
  title.className = "trace-title";
  title.textContent = label;

  const loading = document.createElement("p");
  loading.className = "loading";
  loading.append(spinner(), document.createTextNode("Tracing execution with Claude…"));

  wrap.append(title, loading);
  elements.rightPanel.replaceChildren(wrap);
}

function showError(heading, message) {
  const box = document.createElement("div");
  box.className = "error";

  const title = document.createElement("h3");
  title.textContent = heading;

  const detail = document.createElement("p");
  detail.textContent = message;

  box.append(title, detail);
  elements.rightPanel.replaceChildren(box);
}

async function showTrace(trace) {
  const wrap = document.createElement("div");
  // Anchor the chat panel appends to when a diagram node is clicked.
  wrap.id = "trace-body";

  const title = document.createElement("h2");
  title.className = "trace-title";
  title.textContent = trace.entry_point;

  const narrative = document.createElement("p");
  narrative.className = "narrative";
  narrative.textContent = trace.narrative;

  const diagram = document.createElement("div");
  diagram.className = "diagram";

  wrap.append(title, narrative, diagram);
  elements.rightPanel.replaceChildren(wrap);

  try {
    const { svg, bindFunctions } = await mermaid.render(
      `diagram-${Date.now()}`,
      trace.mermaid
    );
    diagram.innerHTML = svg;
    bindFunctions?.(diagram);
    bindDiagramClicks(diagram, trace.trace ?? []);
  } catch (error) {
    // Valid JSON from the model does not guarantee renderable Mermaid, so a
    // render failure is reported next to the narrative rather than swallowed.
    diagram.replaceChildren();
    diagram.append(hint(`Diagram could not be rendered: ${error.message}`));
  }
}

// --------------------------------------------------------------------------
// Diagram click capture
// --------------------------------------------------------------------------

/**
 * Make every participant and message in the rendered diagram clickable.
 *
 * Mermaid puts a `name` attribute on actor shapes, but the participant's
 * label text sits on top of that shape and swallows the click. Rather than
 * binding element by element, one delegated listener resolves whatever was
 * clicked by walking up from the event target.
 */
function bindDiagramClicks(container, traceNodes) {
  const participants = new Set(
    [...container.querySelectorAll("[name]")].map((element) => element.getAttribute("name"))
  );

  container.addEventListener("click", (event) => {
    const hit = resolveClick(event.target, participants);
    if (!hit) return;
    setSelection({ ...hit, node: matchTraceNode(traceNodes, hit) });
  });
}

/** Work out what the user clicked: a participant, a message, or nothing. */
function resolveClick(target, participants) {
  const named = target.closest?.("[name]");
  if (named) {
    return { kind: "participant", label: named.getAttribute("name") };
  }

  const text = target.closest?.("text");
  const label = text?.textContent.trim();
  if (!label) return null;

  return { kind: participants.has(label) ? "participant" : "message", label };
}

/** Best-effort mapping from a clicked label back to a node in the trace. */
function matchTraceNode(traceNodes, hit) {
  if (hit.kind === "participant") {
    return traceNodes.find((node) => node.class === hit.label) ?? null;
  }
  return (
    traceNodes.find((node) => node.signature === hit.label) ??
    traceNodes.find((node) => node.signature.includes(hit.label)) ??
    traceNodes.find((node) => hit.label.includes(node.signature)) ??
    null
  );
}

/**
 * Record the current diagram selection and open the chat panel for it.
 *
 * The value is also kept on `window.digitalTwin` and announced as a DOM event,
 * so other features can observe selections without reaching into this module.
 */
function setSelection(hit) {
  selectedNode = hit;
  window.digitalTwin.selection = hit;

  if (!hit) {
    elements.statusbar.hidden = true;
    elements.selection.textContent = "";
    closeChat();
    return;
  }

  elements.statusbar.hidden = false;
  elements.selection.textContent = hit.node
    ? `${hit.node.id} · ${hit.node.class ?? "—"} · ${hit.node.signature}`
    : `${hit.label} (${hit.kind})`;

  openChat(hit);
  window.dispatchEvent(new CustomEvent("digitaltwin:select", { detail: hit }));
}

// --------------------------------------------------------------------------
// Chat about a node
// --------------------------------------------------------------------------

/** Conversation for the node currently open, reset whenever it changes. */
let chatHistory = [];

function closeChat() {
  chatHistory = [];
  document.getElementById("chat")?.remove();
}

/**
 * Show the question box for a clicked node.
 *
 * A participant on its own is a class, not a method, so there is no snippet
 * to ask about; the panel says so rather than offering an input that would
 * always fail.
 */
function openChat(hit) {
  closeChat();
  const container = document.getElementById("trace-body");
  if (!container) return;

  const panel = document.createElement("section");
  panel.className = "chat";
  panel.id = "chat";

  const heading = document.createElement("p");
  heading.className = "chat-head";
  heading.textContent = `Ask about ${hit.node?.signature ?? hit.label}`;
  panel.append(heading);

  const source = document.createElement("p");
  source.className = "chat-source";
  panel.append(source);

  if (!hit.node) {
    source.textContent = `No source is recorded for "${hit.label}" — click a call arrow or a box that maps to a method.`;
    container.append(panel);
    return;
  }

  source.textContent = `${hit.node.id} · ${hit.node.class ?? "—"}`;

  const turns = document.createElement("div");
  turns.className = "chat-turns";

  const form = document.createElement("form");
  form.className = "chat-form";

  const input = document.createElement("input");
  input.className = "chat-input";
  input.type = "text";
  input.placeholder = "What does this do? Why is it written this way?";
  input.setAttribute("aria-label", "Question about this method");

  const submit = document.createElement("button");
  submit.type = "submit";
  submit.className = "button";
  submit.textContent = "Ask";

  form.append(input, submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    askQuestion(hit.node.id, input, submit, turns);
  });

  panel.append(turns, form);
  container.append(panel);
  input.focus();
}

/** Send one question and append the answer, keeping history for follow-ups. */
async function askQuestion(functionId, input, submit, turns) {
  const question = input.value.trim();
  if (!question) return;

  input.value = "";
  input.disabled = true;
  submit.disabled = true;

  const turn = document.createElement("div");
  const asked = document.createElement("p");
  asked.className = "chat-turn-question";
  asked.textContent = question;

  const pending = document.createElement("p");
  pending.className = "loading";
  pending.append(spinner(), document.createTextNode("Reading the source…"));

  turn.append(asked, pending);
  turns.append(turn);

  try {
    const data = await fetchJson("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        function_id: functionId,
        question,
        history: chatHistory,
      }),
    });
    pending.replaceWith(answerParagraph(data.answer));
    chatHistory.push(
      { role: "user", content: question },
      { role: "assistant", content: data.answer }
    );
  } catch (error) {
    pending.replaceWith(answerParagraph(error.message, true));
  } finally {
    input.disabled = false;
    submit.disabled = false;
    input.focus();
  }
}

function answerParagraph(text, failed = false) {
  const paragraph = document.createElement("p");
  paragraph.className = failed ? "chat-turn-answer failed" : "chat-turn-answer";
  paragraph.textContent = text;
  return paragraph;
}

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

function hint(text) {
  const element = document.createElement("p");
  element.className = "hint";
  element.textContent = text;
  return element;
}

function spinner() {
  const element = document.createElement("span");
  element.className = "spinner";
  return element;
}

window.digitalTwin = {
  selection: null,
  get selected() {
    return selectedNode;
  },
};

wireUpload();
loadEndpoints();
