# startup-in-a-day

Codebase Digital Twin & Execution Tracer - upload a Spring Boot repo, pick a
user-facing action, and see exactly what code runs, in what order, down to the
database call.

## Demo video

link: https://drive.google.com/file/d/16B46W0-rCJXkkBAkT248YAN8CQJck6ve/view


https://github.com/user-attachments/assets/b8e26fda-5280-4fe3-a9a6-15b22ea4fb47
## Layout

```
backend/    Python analyzer, agent, and API server (parser/, agent/, api/, tests/, .venv/)
frontend/   UI (static, served by the backend - no separate build/run step)
```

## Quickstart (run the whole app)

Requires Python 3.11+ and an [Anthropic API key](https://console.anthropic.com/settings/keys).

```bash
git clone <this-repo-url>
cd startup-in-a-day

# 1. API key - copy the template and fill in your key
cp .env.example .env
#   edit .env and set ANTHROPIC_API_KEY=sk-ant-...

# 2. Backend setup
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt -r requirements-dev.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt    # macOS/Linux

# 3. Run the server (serves both the API and the frontend)
.venv/Scripts/python -m api.server        # Windows
# .venv/bin/python -m api.server          # macOS/Linux
```

Then open **http://127.0.0.1:8000**. It starts pre-loaded with a bundled demo
Spring Boot project (`backend/tests/fixtures/demo-shop`) so there's something to
click immediately - upload your own `.zip` of a Spring Boot project from the UI
to analyze it instead.

To point the server at a specific project on startup instead of the bundled demo:

```bash
.venv/Scripts/python -m api.server /path/to/your/spring-project
```

The frontend loads Mermaid, IBM Plex fonts, and a couple of small JS libraries
from a CDN at runtime, so an internet connection is required even though there's
no separate frontend build step.

No `.env` and nothing loaded? The server logs `warning: ANTHROPIC_API_KEY is not
set` on startup and the endpoint list will still load, but generating a trace or
asking a question will fail until the key is set.

## `backend/parser/spring_analyze.py`

Static parser that turns raw Java source into the JSON **manifest** every other
feature reads. Nothing downstream re-parses Java: the diagram, the
click-to-ask-about-this-function chat, and the UI all consume this file, and
fetch source snippets by `file` + `line_start`..`line_end`.

### Usage

All commands run from `backend/`.

```bash
cd backend
python -m parser.spring_analyze <path_to_repo>            # pretty JSON to stdout
python -m parser.spring_analyze <path_to_repo> -o out.json
python -m parser.spring_analyze <path_to_repo> --max-depth 5
```

```python
from parser.spring_analyze import analyze_repository

manifest = analyze_repository("path/to/repo", max_depth=3)
```

### Setup

```bash
cd backend
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.venv/Scripts/python.exe -m pytest tests/ -q
```

## `backend/parser/compress.py`

Reduces a manifest to a compact **skeleton** for one selected entry point,
which is what actually gets sent to the Claude API for trace/diagram
generation. Walks the call graph the parser already built and keeps only the
nodes reachable from that entry point, each as a single line.

```bash
cd backend
python -m parser.compress <manifest.json> <entry_point_id>   # skeleton to stdout
```

```python
from parser.compress import compress_for_agent

skeleton = compress_for_agent(manifest, "ep_1")
```

The CLI reports the size comparison on stderr, e.g.
`manifest: 14,899 chars | skeleton: 894 chars | reduction: 94.0% | nodes: 4`.
On Spring PetClinic, skeletons run 94-98% smaller than the full manifest.

File paths, line numbers, warnings, and unreachable nodes are deliberately
excluded. The click-to-chat feature looks locations up in the full manifest by
node id, so this skeleton stays purely about flow.

## `backend/agent/trace_synthesizer.py`

Sends a skeleton to the Claude API and gets back the two things the frontend
renders: a short plain-English narrative, and the same chain as Mermaid.js
`sequenceDiagram` source.

```bash
cd backend
export ANTHROPIC_API_KEY=...        # never hardcoded, never logged
python -m agent.trace_synthesizer <manifest.json> <entry_point_id>
```

```python
from agent.trace_synthesizer import synthesize_trace

result = synthesize_trace(skeleton)   # {"narrative": ..., "mermaid": ...}
```

One API call per click (model `claude-sonnet-4-6`, ~450 input / ~300 output
tokens for a typical trace). The response is parsed defensively: markdown
fences are stripped before `json.loads`, and a parse failure earns exactly one
repair attempt before erroring. Mermaid source is validated to be non-empty and
to start with `sequenceDiagram`, so broken syntax fails here rather than in the
browser. `synthesize()` returns the same result plus token counts for budget
tracking; the CLI prints them to stderr.

### How it works

**Pass 1 - component registry.** Indexes every type: its Spring stereotypes,
implemented interfaces, and injected dependencies (field name -> declared
type). Produces the interface -> implementation map. Injection is detected from
`@Autowired` fields, `private final` fields (constructor / Lombok style), and
constructor parameters.

**Pass 2 - call tracing.** Walks out from each controller handler, resolving
calls made on injected fields through the registry, breadth-first to
`--max-depth` hops. Terminal operations become sinks: `db_read`, `db_write`, or
`http_call`.

### Line accuracy

`javalang` records where a node *starts* but never where it *ends*, so method
spans are recovered by locating the body's opening brace in the token stream
and walking to its matching close. Working on tokens rather than text means
braces inside comments and string literals cannot shift a span. Spans start at
the first annotation, so `@PostMapping(...)` is part of the snippet.

### Known limitations

- **`javalang` implements the Java 8 grammar.** Java 16+ syntax
  (pattern-matching `instanceof`, records, text blocks, sealed types) fails to
  parse. Affected files are skipped and reported in `warnings`; the rest of the
  run is unaffected. Spring PetClinic loses 1 of 40 files this way.
- Receivers more complex than a field are not resolved (`a.b.c()`), by design -
  guessing there produces confidently wrong edges.
- One implementation per interface; extras are reported in `warnings`.
- Overloaded methods trace the first declaration, and are reported.
