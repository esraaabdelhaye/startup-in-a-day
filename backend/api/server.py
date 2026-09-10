"""HTTP layer exposing the analysis pipeline to the frontend.

Three endpoints are all the UI needs:

``GET /api/entry-points``
    The endpoints found in the loaded repository, for the left-hand list.

``GET /api/trace/{entry_point_id}``
    Compression plus synthesis for one endpoint, returning the narrative, the
    Mermaid source, and the trace nodes behind them.

``POST /api/upload``
    Replace the loaded repository with the contents of an uploaded ``.zip``.

A repository is analyzed once when it is loaded -- parsing is the slow part
and the source does not change underneath us -- and synthesized traces are
memoized per entry point, so clicking back to an endpoint you already opened
is instant and free rather than a second billed Claude call.

Usage::

    python -m api.server                 # serves the bundled demo-shop fixture
    python -m api.server <path_to_repo>
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Optional, Sequence

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent.code_chat import CodeChatError, answer_question
from agent.trace_synthesizer import TraceSynthesisError, synthesize_trace
from parser.compress import UnknownEntryPointError, compress_for_agent
from parser.spring_analyze import analyze_repository

__all__ = ["create_app", "main"]

#: Repository root, i.e. the parent of ``backend/``.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Static assets served at ``/``.
FRONTEND_DIR = PROJECT_ROOT / "frontend"

#: Repository analyzed when none is given on the command line.
DEFAULT_REPO = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "demo-shop"

#: Entry point fields the list endpoint exposes.  File paths and line numbers
#: stay server-side until the chat feature needs them.
ENTRY_POINT_FIELDS = ("id", "method", "path", "function", "class")

#: Largest upload accepted, before decompression.
MAX_UPLOAD_BYTES = 64 * 1024 * 1024

#: Largest total size of extracted Java source.  Guards against an archive
#: that is small on disk but expands to something enormous.
MAX_EXTRACTED_BYTES = 128 * 1024 * 1024

#: Most ``.java`` files extracted from one archive.
MAX_JAVA_FILES = 20_000

#: Chunk size for streaming archive members to disk.
COPY_CHUNK = 64 * 1024


@dataclass
class AnalysisState:
    """The loaded repository and everything derived from it."""

    repo: Path
    manifest: "dict[str, Any]"
    #: Human-readable name of the source, e.g. a directory or archive name.
    label: str
    #: entry point id -> synthesized trace, memoized across requests.
    traces: "dict[str, dict[str, Any]]" = field(default_factory=dict)
    #: Temporary directory to remove when this state is replaced, if any.
    temp_dir: "Optional[Path]" = None

    def discard(self) -> None:
        """Delete any temporary extraction directory backing this state."""
        if self.temp_dir is not None:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.temp_dir = None


def load_env_file(path: Path) -> "list[str]":
    """Populate missing environment variables from a ``.env`` file.

    Real environment variables always win, and values are never logged.
    Returns the names of the variables that were set, so the caller can report
    what happened without disclosing anything.
    """
    if not path.is_file():
        return []
    loaded: "list[str]" = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        name = name.strip()
        if name and not os.environ.get(name):
            os.environ[name] = value.strip().strip("\"'")
            loaded.append(name)
    return loaded


#: Lines of context shown either side of a sink, which records a single call
#: line rather than a span.
SINK_CONTEXT_LINES = 3


class ArchiveError(ValueError):
    """Raised when an uploaded archive cannot be safely turned into sources."""


class AskRequest(BaseModel):
    """A question about one node of the current trace."""

    function_id: str
    question: str
    #: Prior turns of this node's conversation, for a follow-up question.
    history: "list[dict[str, str]]" = Field(default_factory=list)


@dataclass(frozen=True)
class SourceLocation:
    """Where a manifest node's source lives, and what to call it."""

    label: str
    file: str
    line_start: int
    line_end: int


def locate_node(manifest: "dict[str, Any]", node_id: str) -> "Optional[SourceLocation]":
    """Find a node's source location in the full manifest.

    Entry points and functions record a span directly.  Sinks record only the
    line of the call, so a few lines either side are included -- a lone
    ``repository.save(order);`` is not enough for the model to say anything
    useful about it.
    """
    for entry in manifest.get("entry_points") or []:
        if entry["id"] == node_id:
            return SourceLocation(
                label=f"{entry.get('class')}.{entry.get('function')}",
                file=entry["file"],
                line_start=entry["line_start"],
                line_end=entry["line_end"],
            )

    function = (manifest.get("functions") or {}).get(node_id)
    if function is not None:
        return SourceLocation(
            label=f"{function.get('class')}.{function.get('name')}",
            file=function["file"],
            line_start=function["line_start"],
            line_end=function["line_end"],
        )

    for sink in manifest.get("sinks") or []:
        if sink["id"] == node_id:
            line = sink.get("line") or 1
            return SourceLocation(
                label=sink.get("call") or sink.get("description") or node_id,
                file=sink["file"],
                line_start=max(1, line - SINK_CONTEXT_LINES),
                line_end=line + SINK_CONTEXT_LINES,
            )

    return None


def read_snippet(repo: Path, location: SourceLocation) -> str:
    """Read exactly the located lines from disk.

    The path comes from our own manifest, but it is still resolved and checked
    for containment: the manifest may describe an uploaded archive, and a
    path escaping the analyzed tree should never turn into a file read.
    """
    root = repo.resolve()
    target = (root / location.file).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise FileNotFoundError(location.file)

    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, location.line_start)
    end = min(len(lines), location.line_end)
    return "\n".join(lines[start - 1 : end])


def safe_member_target(root: Path, name: str) -> "Optional[Path]":
    """Resolve an archive member to a path inside ``root``, or ``None``.

    Archive member names are attacker-controlled: they can contain ``..``
    segments, absolute paths, or Windows drive letters, any of which would let
    an extraction write outside the destination directory ("zip slip").  Every
    member is resolved and checked for containment before it is written.
    """
    normalized = name.replace("\\", "/").strip()
    if not normalized or normalized.endswith("/"):
        return None
    # Drop leading slashes and any drive prefix so the name cannot be absolute.
    normalized = normalized.lstrip("/")
    if ":" in normalized.split("/")[0]:
        return None

    candidate = (root / normalized).resolve()
    if not candidate.is_relative_to(root.resolve()):
        return None
    return candidate


def _copy_limited(source: BinaryIO, target: Path, remaining: int) -> int:
    """Stream one member to disk, refusing to exceed ``remaining`` bytes.

    The declared size in the archive index is not trusted -- a crafted archive
    can understate it -- so the budget is enforced against bytes actually read.
    """
    written = 0
    with open(target, "wb") as handle:
        while True:
            chunk = source.read(COPY_CHUNK)
            if not chunk:
                return written
            written += len(chunk)
            if written > remaining:
                raise ArchiveError(
                    f"archive expands beyond the {MAX_EXTRACTED_BYTES // (1024 * 1024)} MB limit"
                )
            handle.write(chunk)


def extract_java_sources(archive: zipfile.ZipFile, destination: Path) -> int:
    """Extract only the ``.java`` files from ``archive`` into ``destination``.

    Restricting extraction to Java source is both a speed and a safety
    measure: the analyzer reads nothing else, and everything an archive might
    smuggle in -- executables, symlinks, huge binaries -- is simply never
    written to disk.

    Returns the number of files extracted.
    """
    extracted = 0
    budget = MAX_EXTRACTED_BYTES

    for info in archive.infolist():
        if info.is_dir() or not info.filename.lower().endswith(".java"):
            continue

        target = safe_member_target(destination, info.filename)
        if target is None:
            continue  # Refuses to escape the destination; skip it silently.

        if extracted >= MAX_JAVA_FILES:
            raise ArchiveError(f"archive contains more than {MAX_JAVA_FILES:,} Java files")

        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as member:
            budget -= _copy_limited(member, target, budget)
        extracted += 1

    return extracted


def load_repository(source: Path, label: str, temp_dir: "Optional[Path]" = None) -> AnalysisState:
    """Analyze ``source`` and wrap the result in a fresh state object."""
    return AnalysisState(
        repo=Path(source).resolve(),
        manifest=analyze_repository(source),
        label=label,
        temp_dir=temp_dir,
    )


def create_app(repo: Path) -> FastAPI:
    """Build the application for one repository.

    Analysis runs here rather than in a startup hook so that a repository that
    cannot be read fails immediately and visibly, not on the first request.
    """
    app = FastAPI(title="Codebase Digital Twin", docs_url="/api/docs", redoc_url=None)
    app.state.analysis = load_repository(repo, label=Path(repo).resolve().name)

    def current() -> AnalysisState:
        """The repository currently loaded, which an upload can replace."""
        return app.state.analysis

    def entry_points_payload(state: AnalysisState) -> "dict[str, Any]":
        """The left panel's view of a loaded repository."""
        return {
            "repo": state.label,
            "entry_points": [
                {key: entry.get(key) for key in ENTRY_POINT_FIELDS}
                for entry in state.manifest["entry_points"]
            ],
            "warnings": state.manifest["warnings"],
        }

    @app.get("/api/entry-points")
    def list_entry_points() -> "dict[str, Any]":
        """Endpoints discovered in the loaded repository."""
        return entry_points_payload(current())

    @app.post("/api/upload")
    async def upload_repository(file: UploadFile) -> "dict[str, Any]":
        """Replace the loaded repository with an uploaded ``.zip`` archive."""
        filename = file.filename or "upload.zip"
        if not filename.lower().endswith(".zip"):
            raise HTTPException(status_code=400, detail="Please upload a .zip archive.")

        payload = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(payload) > MAX_UPLOAD_BYTES:
            limit = MAX_UPLOAD_BYTES // (1024 * 1024)
            raise HTTPException(status_code=413, detail=f"Archive is larger than {limit} MB.")

        destination = Path(tempfile.mkdtemp(prefix="digital-twin-"))
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                extracted = extract_java_sources(archive, destination)
        except zipfile.BadZipFile:
            shutil.rmtree(destination, ignore_errors=True)
            raise HTTPException(status_code=400, detail="That file is not a valid zip archive.")
        except ArchiveError as error:
            shutil.rmtree(destination, ignore_errors=True)
            raise HTTPException(status_code=413, detail=str(error)) from None

        if extracted == 0:
            shutil.rmtree(destination, ignore_errors=True)
            raise HTTPException(
                status_code=400, detail="No .java files were found in that archive."
            )

        state = load_repository(destination, label=filename, temp_dir=destination)
        previous = app.state.analysis
        app.state.analysis = state
        previous.discard()  # Only after the replacement succeeded.

        return {**entry_points_payload(state), "java_files": extracted}

    @app.get("/api/trace/{entry_point_id}")
    def get_trace(entry_point_id: str) -> "dict[str, Any]":
        """Narrative, Mermaid diagram and trace nodes for one endpoint."""
        state = current()
        cached = state.traces.get(entry_point_id)
        if cached is not None:
            return {**cached, "cached": True}

        try:
            skeleton = compress_for_agent(state.manifest, entry_point_id)
        except UnknownEntryPointError as error:
            raise HTTPException(status_code=404, detail=error.args[0]) from None

        try:
            result = synthesize_trace(skeleton)
        except TraceSynthesisError as error:
            # 502: the failure is upstream of us, and the message is already
            # written for a human and free of any credential material.
            raise HTTPException(status_code=502, detail=str(error)) from None

        payload = {
            "entry_point": skeleton["entry_point"],
            "narrative": result["narrative"],
            "mermaid": result["mermaid"],
            # Returned so the UI can map a clicked diagram node back to a node
            # id, which is what the chat feature will look up.
            "trace": skeleton["trace"],
        }
        state.traces[entry_point_id] = payload
        return {**payload, "cached": False}

    @app.post("/api/ask")
    def ask_about_node(request: AskRequest) -> "dict[str, Any]":
        """Answer a question about the source behind one trace node."""
        state = current()

        location = locate_node(state.manifest, request.function_id)
        if location is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"{request.function_id!r} is not in the loaded manifest. "
                    "It may belong to a project that has since been replaced."
                ),
            )

        try:
            snippet = read_snippet(state.repo, location)
        except (OSError, FileNotFoundError):
            raise HTTPException(
                status_code=404,
                detail=f"Source for {location.label} is no longer readable at {location.file}.",
            ) from None

        try:
            answer = answer_question(
                snippet=snippet,
                question=request.question,
                label=location.label,
                history=request.history,
            )
        except CodeChatError as error:
            raise HTTPException(status_code=502, detail=str(error)) from None

        return {
            "answer": answer,
            "label": location.label,
            "file": location.file,
            "line_start": location.line_start,
            "line_end": location.line_end,
        }

    if FRONTEND_DIR.is_dir():
        # Mounted last: a bare "/" mount would otherwise shadow the API routes.
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

    return app


def main(argv: "Optional[Sequence[str]]" = None) -> int:
    """CLI entry point: analyze a repository and serve the UI."""
    import uvicorn

    arguments = _parse_args(argv)
    repo = arguments.repo.resolve()
    if not repo.is_dir():
        print(f"error: {repo} is not a directory", file=sys.stderr)
        return 2

    loaded = load_env_file(PROJECT_ROOT / ".env")
    if "ANTHROPIC_API_KEY" in loaded:
        print("loaded ANTHROPIC_API_KEY from .env", file=sys.stderr)
    elif not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "warning: ANTHROPIC_API_KEY is not set; endpoints will list but traces will fail",
            file=sys.stderr,
        )

    print(f"analyzing {repo} ...", file=sys.stderr)
    app = create_app(repo)
    manifest = app.state.analysis.manifest
    print(
        f"ready: {len(manifest['entry_points'])} entry points, "
        f"{len(manifest['warnings'])} warnings",
        file=sys.stderr,
    )
    print(f"open http://{arguments.host}:{arguments.port}", file=sys.stderr)

    uvicorn.run(app, host=arguments.host, port=arguments.port, log_level="warning")
    return 0


def _parse_args(argv: "Optional[Sequence[str]]") -> argparse.Namespace:
    """Build and run the argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m api.server",
        description="Serve the Codebase Digital Twin UI for one repository.",
    )
    parser.add_argument(
        "repo",
        nargs="?",
        type=Path,
        default=DEFAULT_REPO,
        help="repository to analyze (default: the bundled demo-shop fixture)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
