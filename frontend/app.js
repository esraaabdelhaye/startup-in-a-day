/**
 * Codebase Digital Twin - three-panel UI.
 *
 * Left panel lists the endpoints the parser found and collapses to a rail.
 * The centre shows the narrative and a Mermaid sequence diagram. Clicking a
 * node in that diagram opens the right-hand panel, where the user can ask
 * questions about that method's actual source.
 */

import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
import markdownit from "https://cdn.jsdelivr.net/npm/markdown-it@14/+esm";
import hljs from "https://cdn.jsdelivr.net/npm/highlight.js@11/lib/core/+esm";
import javaGrammar from "https://cdn.jsdelivr.net/npm/highlight.js@11/lib/languages/java/+esm";

hljs.registerLanguage("java", javaGrammar);

/**
 * Renderer for chat answers.
 *
 * markdown-it rather than marked because `html: false` is its default: an
 * answer is derived from source in an uploaded archive, so a Java comment
 * containing markup is untrusted input. Raw HTML is escaped, which removes
 * the need for a separate sanitiser.
 */
const markdown = markdownit({
  html: false,
  linkify: false,
  breaks: false,
  highlight(code, language) {
    if (language && hljs.getLanguage(language)) {
      try {
        return hljs.highlight(code, { language, ignoreIllegals: true }).value;
      } catch {
        // Fall through to markdown-it's own escaping.
      }
    }
    return "";
  },
});

/**
 * Emit fenced blocks as `<pre data-lang="java"><code class="hljs">`.
 *
 * The language lands on the `pre` so CSS can show it as a corner tag; the
 * default renderer only puts it on the `code` element, where `attr()` cannot
 * reach it from the block's own pseudo-element.
 */
markdown.renderer.rules.fence = (tokens, index, options) => {
  const token = tokens[index];
  const language = (token.info || "").trim().split(/\s+/)[0].toLowerCase();
  const body =
    options.highlight?.(token.content, language) || markdown.utils.escapeHtml(token.content);
  const tag = language ? ` data-lang="${markdown.utils.escapeHtml(language)}"` : "";
  return `<pre${tag}><code class="hljs">${body}</code></pre>\n`;
};

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
  tracePanel: document.getElementById("trace-panel"),
  uploadButton: document.getElementById("upload-button"),
  uploadStatus: document.getElementById("upload-status"),
  zipInput: document.getElementById("zip-input"),
  leftToggle: document.getElementById("left-toggle"),
  leftToggleArrow: document.getElementById("left-toggle-arrow"),
  chatPanel: document.getElementById("chat-panel"),
  chatTitle: document.getElementById("chat-title"),
  chatSource: document.getElementById("chat-source"),
  chatTurns: document.getElementById("chat-turns"),
  chatForm: document.getElementById("chat-form"),
  chatInput: document.getElementById("chat-input"),
  chatSubmit: document.getElementById("chat-submit"),
  chatClose: document.getElementById("chat-close"),
};

/** Id of the endpoint being shown, so stale responses can be dropped. */
let activeEntryPointId = null;

/** The most recent diagram selection. */
let selectedNode = null;

/** Manifest node the question panel is currently pointed at. */
let chatNodeId = null;

/** Conversation for that node, reset whenever the selection changes. */
let chatHistory = [];

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

/** Collapse the endpoint list to a rail, or restore it. */
function setLeftCollapsed(collapsed) {
  document.body.classList.toggle("left-collapsed", collapsed);
  elements.leftToggle.setAttribute("aria-expanded", String(!collapsed));
  // The arrow points the way the panel will move.
  elements.leftToggleArrow.textContent = collapsed ? "›" : "‹";
  const label = collapsed ? "Show the endpoint list" : "Hide the endpoint list";
  elements.leftToggle.title = label;
  elements.leftToggle.querySelector(".visually-hidden").textContent = label;
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
  elements.tracePanel.replaceChildren(hint("Select an endpoint to trace what it runs."));
  setSelection(null);
  activeEntryPointId = null;
  setLeftCollapsed(false);

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
// Centre panel
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
  elements.tracePanel.replaceChildren(wrap);
}

function showError(heading, message) {
  const box = document.createElement("div");
  box.className = "error";

  const title = document.createElement("h3");
  title.textContent = heading;

  const detail = document.createElement("p");
  detail.textContent = message;

  box.append(title, detail);
  elements.tracePanel.replaceChildren(box);
}

async function showTrace(trace) {
  const wrap = document.createElement("div");

  const title = document.createElement("h2");
  title.className = "trace-title";
  title.textContent = trace.entry_point;

  const narrative = document.createElement("p");
  narrative.className = "narrative";
  narrative.textContent = trace.narrative;

  const diagram = document.createElement("div");
  diagram.className = "diagram";

  wrap.append(title, narrative, diagram);
  elements.tracePanel.replaceChildren(wrap);

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
 * Record the current diagram selection and point the question panel at it.
 *
 * The value is also kept on `window.digitalTwin` and announced as a DOM event,
 * so other features can observe selections without reaching into this module.
 */
function setSelection(hit) {
  selectedNode = hit;
  window.digitalTwin.selection = hit;

  if (!hit) {
    closeChat();
    return;
  }

  openChat(hit);
  window.dispatchEvent(new CustomEvent("digitaltwin:select", { detail: hit }));
}

// --------------------------------------------------------------------------
// Question panel
// --------------------------------------------------------------------------

function closeChat() {
  chatNodeId = null;
  chatHistory = [];
  document.body.classList.remove("chat-open");
  elements.chatPanel.hidden = true;
  elements.chatTurns.replaceChildren();
}

/**
 * Point the right-hand panel at a clicked node.
 *
 * A participant on its own is a class, not a method, so there is no snippet
 * to ask about; the panel says so instead of offering an input that could
 * only fail.
 */
function openChat(hit) {
  chatNodeId = hit.node?.id ?? null;
  chatHistory = [];

  elements.chatTurns.replaceChildren();
  elements.chatPanel.hidden = false;
  document.body.classList.add("chat-open");

  elements.chatTitle.textContent = hit.node?.signature ?? hit.label;

  const askable = Boolean(chatNodeId);
  elements.chatSource.textContent = askable
    ? `${hit.node.id} · ${hit.node.class ?? "—"}`
    : `No source recorded for "${hit.label}" — click a call arrow instead.`;

  elements.chatForm.hidden = !askable;
  if (askable) elements.chatInput.focus();
}

/** Send one question and append the answer, keeping history for follow-ups. */
async function askQuestion(question) {
  const turn = document.createElement("div");

  const asked = document.createElement("p");
  asked.className = "chat-turn-question";
  asked.textContent = question;

  const pending = document.createElement("p");
  pending.className = "loading";
  pending.append(spinner(), document.createTextNode("Reading the source…"));

  turn.append(asked, pending);
  elements.chatTurns.append(turn);
  elements.chatTurns.scrollTop = elements.chatTurns.scrollHeight;

  const askedAbout = chatNodeId;

  try {
    const data = await fetchJson("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        function_id: askedAbout,
        question,
        history: chatHistory,
      }),
    });
    pending.replaceWith(answerParagraph(data.answer));
    // Only keep history if the panel is still pointed at the same node.
    if (chatNodeId === askedAbout) {
      chatHistory.push(
        { role: "user", content: question },
        { role: "assistant", content: data.answer }
      );
    }
  } catch (error) {
    pending.replaceWith(answerParagraph(error.message, true));
  } finally {
    elements.chatTurns.scrollTop = elements.chatTurns.scrollHeight;
  }
}

/**
 * Render one answer.
 *
 * Errors stay as plain text - they are our own messages, not model output.
 * Answers are markdown, so they need a block container: `<p>` cannot legally
 * contain the `<pre>` and `<ul>` elements the renderer produces, and browsers
 * would silently restructure the DOM around them.
 */
function answerParagraph(text, failed = false) {
  if (failed) {
    const paragraph = document.createElement("p");
    paragraph.className = "chat-turn-answer failed";
    paragraph.textContent = text;
    return paragraph;
  }

  const block = document.createElement("div");
  block.className = "chat-turn-answer";
  block.innerHTML = markdown.render(text);
  return block;
}

function wireChat() {
  elements.chatClose.addEventListener("click", closeChat);

  elements.chatForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const question = elements.chatInput.value.trim();
    if (!question || !chatNodeId) return;

    elements.chatInput.value = "";
    elements.chatInput.disabled = true;
    elements.chatSubmit.disabled = true;
    try {
      await askQuestion(question);
    } finally {
      elements.chatInput.disabled = false;
      elements.chatSubmit.disabled = false;
      elements.chatInput.focus();
    }
  });
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

elements.leftToggle.addEventListener("click", () =>
  setLeftCollapsed(!document.body.classList.contains("left-collapsed"))
);
wireUpload();
wireChat();
loadEndpoints();
