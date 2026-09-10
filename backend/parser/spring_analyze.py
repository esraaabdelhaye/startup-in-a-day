"""Static analysis of a Spring Boot repository into an execution manifest.

The manifest produced here is the single source of truth for every downstream
feature of the Codebase Digital Twin (diagram rendering, "ask about this
function" chat, the UI).  Those consumers never re-parse Java: they read this
JSON and, when they need to show code, seek to ``file`` + ``line_start`` ..
``line_end``.  Line accuracy is therefore the primary correctness property of
this module -- see :class:`TokenIndex` for how exact spans are recovered.

Analysis runs in two passes:

Pass 1 (:meth:`SpringAnalyzer.scan_components`)
    Index every type in the repository: its Spring stereotype annotations, the
    interfaces it implements, and its injected dependencies (field name ->
    declared type).  Produces the interface -> implementation map that makes
    call resolution possible.

Pass 2 (:meth:`SpringAnalyzer.trace_entry_points`)
    Walk out from each ``@RestController`` handler, resolving calls made on
    injected fields through the pass 1 registry, and recording terminal
    database / outbound-HTTP operations as sinks.

Usage::

    python -m parser.spring_analyze <path_to_repo>
"""

from __future__ import annotations

import argparse
import json
import sys
from bisect import bisect_left
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

import javalang
from javalang import tree as jtree
from javalang.parser import JavaSyntaxError
from javalang.tokenizer import LexerError

__all__ = ["analyze_repository", "SpringAnalyzer", "main"]


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

#: How many hops out from an entry point we follow before giving up.  Depth 1 is
#: the controller's direct collaborator.  Guards against deep or cyclic graphs.
DEFAULT_MAX_DEPTH = 3

#: Spring stereotypes that make a class a resolvable component.
COMPONENT_ANNOTATIONS = frozenset({"Service", "Component", "Repository", "RestController", "Controller"})

#: Stereotypes whose methods can be HTTP entry points.  ``@Controller`` is
#: included alongside ``@RestController`` because server-rendered MVC apps route
#: through it and would otherwise yield no entry points at all.
CONTROLLER_ANNOTATIONS = frozenset({"RestController", "Controller"})

#: Mapping annotation -> implied HTTP verb.  ``RequestMapping`` carries its verb
#: in a ``method =`` attribute instead, hence ``None``.
MAPPING_ANNOTATIONS: "dict[str, Optional[str]]" = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
    "RequestMapping": None,
}

#: HTTP verb reported for a ``@RequestMapping`` that does not pin one down.
ANY_HTTP_METHOD = "ANY"

#: Directories that never contain first-party source.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".gradle",
        ".idea",
        ".mvn",
        "target",
        "build",
        "out",
        "bin",
        "node_modules",
        "__pycache__",
    }
)

#: Suffixes that mark a type as a data repository even without ``@Repository``.
REPOSITORY_TYPE_SUFFIXES = ("Repository", "Repo", "Dao", "DAO")

#: Base interfaces that make a type a Spring Data repository.
REPOSITORY_BASE_TYPES = frozenset(
    {
        "Repository",
        "CrudRepository",
        "JpaRepository",
        "PagingAndSortingRepository",
        "MongoRepository",
        "ReactiveCrudRepository",
        "JpaSpecificationExecutor",
    }
)

#: Types that reach a database without going through Spring Data.
RAW_DB_TYPES = frozenset(
    {"EntityManager", "JdbcTemplate", "NamedParameterJdbcTemplate", "SessionFactory", "DataSource"}
)

#: Types that perform outbound HTTP.
HTTP_CLIENT_TYPES = frozenset({"RestTemplate", "WebClient"})

#: Method-name prefixes that indicate a mutating persistence call.
DB_WRITE_PREFIXES = (
    "save",
    "delete",
    "remove",
    "persist",
    "merge",
    "insert",
    "update",
    "flush",
    "store",
)

#: Method-name prefixes that indicate a reading persistence call.
DB_READ_PREFIXES = (
    "find",
    "get",
    "read",
    "query",
    "select",
    "exists",
    "count",
    "search",
    "stream",
    "list",
)

#: Interfaces that would only add noise to an interface -> implementation map.
IGNORED_INTERFACES = frozenset(
    {
        "Serializable",
        "Cloneable",
        "Comparable",
        "Runnable",
        "Callable",
        "Iterable",
        "AutoCloseable",
        "Closeable",
    }
)

#: Types not worth treating as an injected dependency when inferring injection
#: from ``private final`` fields (constructor / Lombok style).
NON_INJECTABLE_TYPES = frozenset(
    {
        "String",
        "Integer",
        "Long",
        "Double",
        "Float",
        "Boolean",
        "Short",
        "Byte",
        "Character",
        "BigDecimal",
        "BigInteger",
        "Object",
        "List",
        "Set",
        "Map",
        "Collection",
        "Optional",
        "int",
        "long",
        "double",
        "float",
        "boolean",
        "short",
        "byte",
        "char",
    }
)


# --------------------------------------------------------------------------- #
# Line spans
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Span:
    """An inclusive, 1-based line range within a single file."""

    line_start: int
    line_end: int


class TokenIndex:
    """Maps ``javalang`` AST nodes to exact inclusive line ranges.

    ``javalang`` records a start position on declaration nodes but never records
    where a node *ends*, so spans have to be recovered from the token stream.
    For a given declaration this class walks forward to the body's opening brace
    and on to its matching close, and walks backward to the start of the
    declaration so annotations and modifiers are part of the span -- the
    ``@PostMapping`` line is something a reader of the snippet needs to see.

    Working on tokens rather than raw text means braces inside comments and
    string literals cannot throw the brace matching off: the lexer has already
    discarded comments and collapsed each string literal into a single token.
    """

    #: Tokens that terminate the preceding class-body member when scanning back.
    _MEMBER_BOUNDARIES = frozenset({";", "{", "}"})

    def __init__(self, tokens: "Sequence[Any]") -> None:
        self._tokens = list(tokens)
        # Parallel (line, column) list so a node position can be binary-searched.
        self._positions: "list[tuple[int, int]]" = [
            (token.position[0], token.position[1]) for token in self._tokens
        ]

    @classmethod
    def from_source(cls, source: str) -> "TokenIndex":
        """Tokenize ``source`` and build an index over the resulting tokens."""
        return cls(list(javalang.tokenizer.tokenize(source)))

    def declaration_span(self, node: Any) -> "Optional[Span]":
        """Return the full span of a method or constructor declaration.

        The span starts at the first annotation or modifier of the declaration
        and ends on the line holding the closing brace of its body.  Returns
        ``None`` when the span cannot be established, so callers can record a
        warning instead of emitting a line number that is merely plausible.
        """
        position = getattr(node, "position", None)
        if position is None:
            return None
        anchor = self._index_at(position[0], position[1])
        if anchor is None:
            return None

        start_line = self._tokens[self._declaration_start(anchor)].position[0]
        end_line = self._body_end_line(anchor)
        if end_line is None:
            return None
        # A declaration cannot end before it starts; if brace matching went
        # sideways, degrade to a single-line span rather than an inverted one.
        return Span(start_line, max(start_line, end_line))

    def _index_at(self, line: int, column: int) -> "Optional[int]":
        """Index of the first token at or after ``(line, column)``."""
        index = bisect_left(self._positions, (line, column))
        return index if index < len(self._tokens) else None

    def _declaration_start(self, anchor: int) -> int:
        """Walk back from ``anchor`` to the first token of the declaration.

        Scanning stops at the token that closed the previous member (``;``,
        ``{`` or ``}``) at parenthesis depth zero.  The depth check keeps
        annotation arguments such as ``@RequestMapping({"/a"})`` from being
        mistaken for a member boundary.
        """
        depth = 0
        index = anchor
        while index > 0:
            value = self._tokens[index - 1].value
            if value == ")":
                depth += 1
            elif value == "(":
                depth -= 1
            elif depth <= 0 and value in self._MEMBER_BOUNDARIES:
                break
            index -= 1
        return index

    def _body_end_line(self, anchor: int) -> "Optional[int]":
        """Line of the closing brace of the body that follows ``anchor``.

        Abstract and interface methods have no body; for those the declaration's
        terminating semicolon marks the end of the span.
        """
        depth = 0
        opening: "Optional[int]" = None
        for index in range(anchor, len(self._tokens)):
            value = self._tokens[index].value
            if value == "(":
                depth += 1
            elif value == ")":
                depth -= 1
            elif depth <= 0:
                if value == ";":
                    return self._tokens[index].position[0]
                if value == "{":
                    opening = index
                    break
        if opening is None:
            return None

        depth = 0
        for index in range(opening, len(self._tokens)):
            value = self._tokens[index].value
            if value == "{":
                depth += 1
            elif value == "}":
                depth -= 1
                if depth == 0:
                    return self._tokens[index].position[0]
        return None


# --------------------------------------------------------------------------- #
# AST helpers
# --------------------------------------------------------------------------- #


def annotation_name(annotation: Any) -> str:
    """Simple name of an annotation (``org.x.Service`` -> ``Service``)."""
    return str(annotation.name).split(".")[-1]


def type_name(node: Any) -> "Optional[str]":
    """Simple name of a type reference, ignoring generics and qualifiers.

    ``com.demo.PaymentService`` and ``List<Order>`` collapse to
    ``PaymentService`` and ``List`` respectively -- the manifest resolves
    components by simple name, matching how Spring wires them by type.
    """
    if node is None:
        return None
    if isinstance(node, jtree.ReferenceType):
        # javalang nests a dotted name as a chain of sub_types; the innermost
        # link holds the actual class name.
        current = node
        while getattr(current, "sub_type", None) is not None:
            current = current.sub_type
        return current.name
    return getattr(node, "name", None)


def annotation_strings(annotation: Any, keys: "Sequence[str]" = ("value", "path")) -> "list[str]":
    """String literals assigned to ``keys`` (or to the single unnamed value).

    Handles the four shapes Spring mappings appear in: ``@GetMapping``,
    ``@GetMapping("/a")``, ``@GetMapping(path = "/a")`` and
    ``@GetMapping({"/a", "/b"})``.
    """
    element = getattr(annotation, "element", None)
    if element is None:
        return []
    if isinstance(element, list):
        values: "list[str]" = []
        for pair in element:
            if getattr(pair, "name", None) in keys:
                values.extend(_literal_strings(pair.value))
        return values
    return _literal_strings(element)


def _literal_strings(node: Any) -> "list[str]":
    """Flatten an annotation element into the string literals it contains."""
    if isinstance(node, jtree.Literal):
        value = str(node.value)
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            return [value[1:-1]]
        return []
    if isinstance(node, jtree.ElementArrayValue):
        values: "list[str]" = []
        for item in node.values or []:
            values.extend(_literal_strings(item))
        return values
    return []


def request_mapping_verbs(annotation: Any) -> "list[str]":
    """HTTP verbs named by a ``@RequestMapping(method = ...)`` attribute."""
    element = getattr(annotation, "element", None)
    if not isinstance(element, list):
        return []
    for pair in element:
        if getattr(pair, "name", None) == "method":
            return _member_names(pair.value)
    return []


def _member_names(node: Any) -> "list[str]":
    """Flatten ``RequestMethod.POST`` / ``{RequestMethod.POST, ...}`` to names."""
    if isinstance(node, jtree.MemberReference):
        return [node.member]
    if isinstance(node, jtree.ElementArrayValue):
        names: "list[str]" = []
        for item in node.values or []:
            names.extend(_member_names(item))
        return names
    return []


def join_route(*segments: "Optional[str]") -> str:
    """Join a class-level prefix and a method-level path into one route."""
    parts = [segment.strip("/") for segment in segments if segment and segment.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def render_argument(node: Any) -> str:
    """Best-effort single-token rendering of a call argument for descriptions."""
    if isinstance(node, jtree.Literal):
        return str(node.value)
    if isinstance(node, jtree.MemberReference):
        return f"{node.qualifier}.{node.member}" if node.qualifier else node.member
    if isinstance(node, jtree.MethodInvocation):
        qualifier = f"{node.qualifier}." if node.qualifier else ""
        return f"{qualifier}{node.member}(...)"
    if isinstance(node, jtree.ClassCreator):
        return f"new {type_name(node.type)}(...)"
    if isinstance(node, jtree.This):
        return "this"
    return "..."


def render_call(invocation: Any, receiver: "Optional[str]" = None) -> str:
    """Render ``orderRepository.save(order)`` from a MethodInvocation node.

    ``receiver`` overrides the node's own qualifier, which is how a
    ``this.``-prefixed call keeps its field name in the description (see
    :meth:`SpringAnalyzer._iter_invocations`).
    """
    arguments = ", ".join(render_argument(argument) for argument in invocation.arguments or [])
    qualifier = receiver or invocation.qualifier
    prefix = f"{qualifier}." if qualifier else ""
    return f"{prefix}{invocation.member}({arguments})"


def parameter_records(declaration: Any) -> "list[dict[str, str]]":
    """Declared parameters of a method, as ``{"name", "type"}`` records.

    Consumers that render a one-line signature need the declared types, so
    these are recorded for every traced method rather than only for handlers.
    """
    return [
        {"name": parameter.name, "type": type_name(parameter.type) or "?"}
        for parameter in declaration.parameters or []
    ]


def qualifier_receiver(invocation: Any) -> "Optional[str]":
    """Field name a call is qualified with, or ``None`` if it is unqualified.

    Deeply qualified receivers (``a.b.c()``) are deliberately left unresolved:
    the declared type of ``a`` says nothing about the type of ``a.b``, and
    guessing would produce confidently wrong edges in the graph.
    """
    qualifier = invocation.qualifier
    if not qualifier:
        return None
    return qualifier if "." not in qualifier else None


def iter_type_declarations(node: Any) -> "Iterator[Any]":
    """Yield a type declaration and every type nested inside it."""
    yield node
    for member in getattr(node, "body", None) or []:
        if isinstance(member, (jtree.ClassDeclaration, jtree.InterfaceDeclaration)):
            yield from iter_type_declarations(member)


# --------------------------------------------------------------------------- #
# Pass 1 model
# --------------------------------------------------------------------------- #


@dataclass
class JavaType:
    """A single class or interface, indexed during pass 1."""

    name: str
    file: str
    is_interface: bool
    annotations: "dict[str, Any]" = field(default_factory=dict)
    implements: "tuple[str, ...]" = ()
    extends: "tuple[str, ...]" = ()
    #: Injected dependency name -> declared type simple name.
    injected_fields: "dict[str, str]" = field(default_factory=dict)
    #: Method name -> declarations (a name can have several overloads).
    methods: "dict[str, list[Any]]" = field(default_factory=dict)
    #: Route prefix from a class-level ``@RequestMapping``.
    route_prefix: str = ""

    @property
    def is_component(self) -> bool:
        """Whether the type carries a Spring stereotype annotation."""
        return bool(COMPONENT_ANNOTATIONS & set(self.annotations))

    @property
    def is_controller(self) -> bool:
        """Whether the type can host HTTP entry points."""
        return bool(CONTROLLER_ANNOTATIONS & set(self.annotations))

    @property
    def is_repository(self) -> bool:
        """Whether calls on this type should be treated as database access."""
        return (
            "Repository" in self.annotations
            or bool(REPOSITORY_BASE_TYPES & set(self.extends))
            or self.name.endswith(REPOSITORY_TYPE_SUFFIXES)
        )


@dataclass
class ResolvedTarget:
    """The outcome of resolving a call receiver against the registry."""

    #: Declared type of the receiver, as written in the source.
    declared_type: str
    #: Concrete implementing type, when one could be found.
    implementation: "Optional[JavaType]" = None
    #: Interface the implementation was reached through, when applicable.
    via_interface: "Optional[str]" = None


# --------------------------------------------------------------------------- #
# Analyzer
# --------------------------------------------------------------------------- #


class SpringAnalyzer:
    """Two-pass static analyzer over a directory of Java source.

    Construct with the repository root, call :meth:`analyze`, and serialize the
    returned manifest.  The instance holds all intermediate state (registry,
    token indexes, warnings) and is single-use.
    """

    def __init__(self, root: Path, max_depth: int = DEFAULT_MAX_DEPTH) -> None:
        self.root = Path(root).resolve()
        self.max_depth = max_depth
        self.warnings: "list[str]" = []

        self._types: "dict[str, JavaType]" = {}
        self._interface_impls: "dict[str, str]" = {}
        self._token_indexes: "dict[str, TokenIndex]" = {}

        self._entry_points: "list[dict[str, Any]]" = []
        self._functions: "dict[str, dict[str, Any]]" = {}
        self._sinks: "list[dict[str, Any]]" = []
        #: (class name, method name) -> function id, so a shared helper is one node.
        self._function_ids: "dict[tuple[str, str], str]" = {}
        #: (file, line, rendered call) -> sink id, so a call site is one node.
        self._sink_ids: "dict[tuple[str, int, str], str]" = {}

    # -- public API -------------------------------------------------------- #

    def analyze(self) -> "dict[str, Any]":
        """Run both passes and return the manifest as a JSON-ready dict."""
        self.scan_components()
        self.trace_entry_points()
        return {
            "entry_points": self._entry_points,
            "functions": self._functions,
            "sinks": self._sinks,
            "warnings": self.warnings,
        }

    # -- pass 1 ------------------------------------------------------------ #

    def scan_components(self) -> None:
        """Pass 1: index every type and build the interface -> impl map."""
        for path in self._iter_java_files():
            relative = path.relative_to(self.root).as_posix()
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
                tree = javalang.parse.parse(source)
                self._token_indexes[relative] = TokenIndex.from_source(source)
            except (JavaSyntaxError, LexerError) as error:
                self.warnings.append(f"{relative}: could not parse Java source ({_describe(error)}); file skipped")
                continue
            except OSError as error:
                self.warnings.append(f"{relative}: could not read file ({_describe(error)}); file skipped")
                continue
            except Exception as error:  # pragma: no cover - javalang edge cases
                self.warnings.append(f"{relative}: unexpected parser failure ({_describe(error)}); file skipped")
                continue

            for declaration in tree.types or []:
                for type_declaration in iter_type_declarations(declaration):
                    self._index_type(type_declaration, relative)

        self._build_interface_map()

    def _iter_java_files(self) -> "Iterator[Path]":
        """Yield every ``.java`` file under the root, in a stable order."""
        for path in sorted(self.root.rglob("*.java")):
            if any(part in SKIP_DIRS for part in path.relative_to(self.root).parts):
                continue
            yield path

    def _index_type(self, declaration: Any, relative_path: str) -> None:
        """Record one class or interface in the registry."""
        annotations = {annotation_name(a): a for a in declaration.annotations or []}
        is_interface = isinstance(declaration, jtree.InterfaceDeclaration)

        java_type = JavaType(
            name=declaration.name,
            file=relative_path,
            is_interface=is_interface,
            annotations=annotations,
            implements=self._supertype_names(getattr(declaration, "implements", None)),
            extends=self._supertype_names(getattr(declaration, "extends", None)),
            methods=self._collect_methods(declaration),
            route_prefix=self._route_prefix(annotations),
        )
        java_type.injected_fields = self._collect_injected_fields(declaration)

        existing = self._types.get(java_type.name)
        if existing is not None:
            self.warnings.append(
                f"Duplicate class name {java_type.name!r} in {existing.file} and {relative_path}; "
                f"keeping {existing.file}"
            )
            return
        self._types[java_type.name] = java_type

    @staticmethod
    def _supertype_names(node: Any) -> "tuple[str, ...]":
        """Normalize an ``extends``/``implements`` clause to simple names.

        ``extends`` is a single node on classes but a list on interfaces, so
        both shapes are accepted.
        """
        if node is None:
            return ()
        nodes = node if isinstance(node, list) else [node]
        names = [type_name(item) for item in nodes]
        return tuple(name for name in names if name)

    @staticmethod
    def _collect_methods(declaration: Any) -> "dict[str, list[Any]]":
        """Group the type's own method declarations by name."""
        methods: "dict[str, list[Any]]" = {}
        for member in declaration.body or []:
            if isinstance(member, jtree.MethodDeclaration):
                methods.setdefault(member.name, []).append(member)
        return methods

    @staticmethod
    def _route_prefix(annotations: "dict[str, Any]") -> str:
        """Route prefix declared by a class-level ``@RequestMapping``."""
        mapping = annotations.get("RequestMapping")
        if mapping is None:
            return ""
        paths = annotation_strings(mapping)
        return paths[0] if paths else ""

    def _collect_injected_fields(self, declaration: Any) -> "dict[str, str]":
        """Map dependency name -> declared type for one type declaration.

        Three injection styles are recognised, in decreasing order of
        confidence: an explicit ``@Autowired`` field; a ``private final`` field
        of a non-trivial type (constructor or Lombok injection); and a
        constructor parameter, which covers the case where the backing field is
        declared with a different name than the parameter.
        """
        injected: "dict[str, str]" = {}

        for member in declaration.body or []:
            if not isinstance(member, jtree.FieldDeclaration):
                continue
            declared = type_name(member.type)
            if not declared:
                continue
            annotations = {annotation_name(a) for a in member.annotations or []}
            modifiers = set(member.modifiers or ())
            autowired = "Autowired" in annotations or "Inject" in annotations
            constructor_injected = "final" in modifiers and declared not in NON_INJECTABLE_TYPES
            if not (autowired or constructor_injected):
                continue
            for declarator in member.declarators or []:
                injected[declarator.name] = declared

        for member in declaration.body or []:
            if not isinstance(member, jtree.ConstructorDeclaration):
                continue
            for parameter in member.parameters or []:
                declared = type_name(parameter.type)
                if declared and declared not in NON_INJECTABLE_TYPES:
                    injected.setdefault(parameter.name, declared)

        return injected

    def _build_interface_map(self) -> None:
        """Map each implemented interface to a single implementing class.

        Types are visited in sorted-path order, so "first implementation wins"
        is deterministic across runs.
        """
        for java_type in self._types.values():
            if java_type.is_interface or not java_type.is_component:
                continue
            for interface in java_type.implements:
                if interface in IGNORED_INTERFACES:
                    continue
                incumbent = self._interface_impls.get(interface)
                if incumbent is None:
                    self._interface_impls[interface] = java_type.name
                elif incumbent != java_type.name:
                    self.warnings.append(
                        f"Interface {interface!r} has multiple implementations; "
                        f"using {incumbent!r} and ignoring {java_type.name!r}"
                    )

    # -- pass 2 ------------------------------------------------------------ #

    def trace_entry_points(self) -> None:
        """Pass 2: collect controller entry points and trace their call graphs.

        Functions are expanded breadth-first so that a helper reachable at more
        than one depth is expanded at its shallowest one; otherwise a function
        first reached at the depth limit would be permanently recorded with an
        empty ``calls`` list.
        """
        queue: "deque[tuple[JavaType, Any, str, int]]" = deque()

        for java_type in sorted(self._types.values(), key=lambda t: (t.file, t.name)):
            if not java_type.is_controller:
                continue
            for method in self._iter_mapped_methods(java_type):
                entry = self._build_entry_point(java_type, method)
                if entry is not None:
                    self._entry_points.append(entry)
                    queue.append((java_type, method, entry["id"], 0))

        while queue:
            owner, method, node_id, depth = queue.popleft()
            calls = self._resolve_calls(owner, method, depth, queue)
            if node_id.startswith("ep_"):
                self._entry_point_by_id(node_id)["calls"] = calls
            else:
                self._functions[node_id]["calls"] = calls

    def _iter_mapped_methods(self, controller: "JavaType") -> "Iterator[Any]":
        """Yield the controller's methods that carry a request mapping."""
        for methods in controller.methods.values():
            for method in methods:
                if any(annotation_name(a) in MAPPING_ANNOTATIONS for a in method.annotations or []):
                    yield method

    def _build_entry_point(self, controller: "JavaType", method: Any) -> "Optional[dict[str, Any]]":
        """Build one entry point record, or ``None`` if its span is unknown."""
        span = self._span(controller.file, method)
        if span is None:
            self.warnings.append(
                f"{controller.file}: no line information for {controller.name}.{method.name}; entry point skipped"
            )
            return None

        mapping = next(
            (a for a in method.annotations or [] if annotation_name(a) in MAPPING_ANNOTATIONS),
            None,
        )
        http_method = self._http_method(mapping, controller, method)
        paths = annotation_strings(mapping)
        if len(paths) > 1:
            self.warnings.append(
                f"{controller.file}: {controller.name}.{method.name} maps several paths {paths}; "
                f"using {paths[0]!r}"
            )

        return {
            "id": f"ep_{len(self._entry_points) + 1}",
            "method": http_method,
            "path": join_route(controller.route_prefix, paths[0] if paths else ""),
            "function": method.name,
            "class": controller.name,
            "file": controller.file,
            "line_start": span.line_start,
            "line_end": span.line_end,
            "parameters": parameter_records(method),
            "calls": [],
        }

    def _http_method(self, mapping: Any, controller: "JavaType", method: Any) -> str:
        """HTTP verb for a mapped method, from the annotation or its attributes."""
        name = annotation_name(mapping)
        verb = MAPPING_ANNOTATIONS.get(name)
        if verb is not None:
            return verb
        verbs = request_mapping_verbs(mapping)
        if not verbs:
            return ANY_HTTP_METHOD
        if len(verbs) > 1:
            self.warnings.append(
                f"{controller.file}: {controller.name}.{method.name} maps several HTTP methods {verbs}; "
                f"using {verbs[0]!r}"
            )
        return verbs[0]

    def _resolve_calls(
        self,
        owner: "JavaType",
        method: Any,
        depth: int,
        queue: "deque[tuple[JavaType, Any, str, int]]",
    ) -> "list[str]":
        """Resolve every outgoing call in ``method`` to a function or sink id.

        Returns the ids in source order, enqueueing newly discovered functions
        for expansion at ``depth + 1``.
        """
        call_ids: "list[str]" = []
        for invocation, receiver in self._iter_invocations(method):
            call_id = self._resolve_invocation(owner, invocation, receiver, depth, queue)
            if call_id is not None and call_id not in call_ids:
                call_ids.append(call_id)
        return call_ids

    @staticmethod
    def _iter_invocations(method: Any) -> "Iterator[tuple[Any, Optional[str]]]":
        """Yield ``(invocation, receiver)`` pairs, ordered by source position.

        ``javalang`` does not model ``this.field.call()`` as a qualified
        invocation: it parses to a ``This`` node whose selectors are the field
        reference followed by the call, leaving the invocation's own qualifier
        empty.  Those pairs are recovered here so an explicit ``this.``
        receiver resolves exactly like a bare one -- without this, every
        ``this.``-prefixed dependency call would silently vanish from the graph.
        """
        this_receivers: "dict[int, str]" = {}
        for _, node in method.filter(jtree.This):
            selectors = node.selectors or []
            if (
                len(selectors) >= 2
                and isinstance(selectors[0], jtree.MemberReference)
                and isinstance(selectors[1], jtree.MethodInvocation)
            ):
                # Only the first pair: later selectors chain off the call's
                # return value, not off the field.
                this_receivers[id(selectors[1])] = selectors[0].member

        invocations = [node for _, node in method.filter(jtree.MethodInvocation)]
        invocations.sort(key=lambda node: node.position or (0, 0))
        for invocation in invocations:
            yield invocation, this_receivers.get(id(invocation)) or qualifier_receiver(invocation)

    def _resolve_invocation(
        self,
        owner: "JavaType",
        invocation: Any,
        receiver: "Optional[str]",
        depth: int,
        queue: "deque[tuple[JavaType, Any, str, int]]",
    ) -> "Optional[str]":
        """Classify one call as a sink, an intermediary, or noise to ignore."""
        if receiver is None:
            # An unqualified call: only interesting when it targets a helper
            # declared on the same class.
            if invocation.member in owner.methods:
                return self._record_function(owner, invocation.member, None, depth, queue)
            return None

        declared_type = owner.injected_fields.get(receiver)
        if declared_type is None:
            return None

        target = self._resolve_type(declared_type)
        kind = self._sink_kind(target, invocation.member)
        if kind is not None:
            return self._record_sink(owner, invocation, receiver, kind)

        if target.implementation is None:
            self.warnings.append(
                f"{owner.file}: no implementation found for {declared_type!r}; "
                f"call {render_call(invocation, receiver)} in {owner.name} left untraced"
            )
            return None
        if invocation.member not in target.implementation.methods:
            return None
        return self._record_function(target.implementation, invocation.member, target.via_interface, depth, queue)

    def _resolve_type(self, declared_type: str) -> "ResolvedTarget":
        """Resolve a declared type to its concrete implementation, if known."""
        known = self._types.get(declared_type)
        if known is not None and not known.is_interface:
            return ResolvedTarget(declared_type=declared_type, implementation=known)

        implementation_name = self._interface_impls.get(declared_type)
        if implementation_name is not None:
            return ResolvedTarget(
                declared_type=declared_type,
                implementation=self._types.get(implementation_name),
                via_interface=declared_type,
            )
        return ResolvedTarget(declared_type=declared_type, implementation=known)

    def _sink_kind(self, target: "ResolvedTarget", method_name: str) -> "Optional[str]":
        """Sink classification for a call, or ``None`` if it is not a sink."""
        declared = target.declared_type
        if declared in HTTP_CLIENT_TYPES:
            return "http_call"
        if declared in RAW_DB_TYPES:
            return self._db_kind(method_name)

        implementation = target.implementation
        is_repository = declared.endswith(REPOSITORY_TYPE_SUFFIXES)
        if implementation is not None and implementation.is_repository:
            is_repository = True
        known = self._types.get(declared)
        if known is not None and known.is_repository:
            is_repository = True
        return self._db_kind(method_name) if is_repository else None

    @staticmethod
    def _db_kind(method_name: str) -> str:
        """Whether a persistence call reads or writes.

        Anything on a repository reaches the database, so an unrecognised name
        is reported as a read rather than dropped.
        """
        lowered = method_name.lower()
        if lowered.startswith(DB_WRITE_PREFIXES):
            return "db_write"
        if lowered.startswith(DB_READ_PREFIXES):
            return "db_read"
        return "db_read"

    def _record_sink(
        self, owner: "JavaType", invocation: Any, receiver: "Optional[str]", kind: str
    ) -> str:
        """Record a terminal operation, reusing the id for a repeated call site."""
        line = invocation.position[0] if invocation.position else 0
        rendered = render_call(invocation, receiver)
        key = (owner.file, line, rendered)
        if key in self._sink_ids:
            return self._sink_ids[key]

        sink_id = f"sink_{len(self._sinks) + 1}"
        self._sink_ids[key] = sink_id
        self._sinks.append(
            {
                "id": sink_id,
                "kind": kind,
                # ``call`` is the machine-readable form; ``description`` is the
                # same call rendered for humans with its location appended.
                "call": rendered,
                "description": f"{rendered} in {Path(owner.file).name} line {line}",
                "class": owner.name,
                "file": owner.file,
                "line": line,
            }
        )
        return sink_id

    def _record_function(
        self,
        owner: "JavaType",
        method_name: str,
        via_interface: "Optional[str]",
        depth: int,
        queue: "deque[tuple[JavaType, Any, str, int]]",
    ) -> "Optional[str]":
        """Record an intermediary function, enqueueing it for expansion.

        A function already recorded is returned as-is, which both dedupes shared
        helpers into one graph node and terminates cycles.
        """
        key = (owner.name, method_name)
        if key in self._function_ids:
            return self._function_ids[key]

        declarations = owner.methods.get(method_name) or []
        if not declarations:
            return None
        if len(declarations) > 1:
            self.warnings.append(
                f"{owner.file}: {owner.name}.{method_name} is overloaded "
                f"({len(declarations)} declarations); tracing the first"
            )
        declaration = declarations[0]

        span = self._span(owner.file, declaration)
        if span is None:
            self.warnings.append(
                f"{owner.file}: no line information for {owner.name}.{method_name}; function skipped"
            )
            return None

        function_id = f"fn_{len(self._functions) + 1}"
        self._function_ids[key] = function_id
        self._functions[function_id] = {
            "name": method_name,
            "class": owner.name,
            "resolved_from_interface": via_interface,
            "parameters": parameter_records(declaration),
            "file": owner.file,
            "line_start": span.line_start,
            "line_end": span.line_end,
            "calls": [],
            "type": "intermediary",
        }

        if depth + 1 < self.max_depth:
            queue.append((owner, declaration, function_id, depth + 1))
        else:
            self.warnings.append(
                f"Depth limit {self.max_depth} reached at {owner.name}.{method_name}; "
                f"its outgoing calls were not traced"
            )
        return function_id

    # -- shared helpers ----------------------------------------------------- #

    def _span(self, relative_path: str, node: Any) -> "Optional[Span]":
        """Line span of a declaration, using the file's cached token index."""
        index = self._token_indexes.get(relative_path)
        return index.declaration_span(node) if index is not None else None

    def _entry_point_by_id(self, entry_id: str) -> "dict[str, Any]":
        """Look up an already-emitted entry point by its id."""
        for entry in self._entry_points:
            if entry["id"] == entry_id:
                return entry
        raise KeyError(entry_id)


def _describe(error: BaseException) -> str:
    """Short, single-line description of an exception for a warning message."""
    text = str(error).strip().splitlines()
    return f"{type(error).__name__}: {text[0]}" if text else type(error).__name__


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #


def analyze_repository(root: "Path | str", max_depth: int = DEFAULT_MAX_DEPTH) -> "dict[str, Any]":
    """Analyze the Java sources under ``root`` and return the manifest dict.

    Args:
        root: Repository root to scan recursively for ``.java`` files.
        max_depth: How many call hops to follow out from each entry point.

    Returns:
        A manifest with ``entry_points``, ``functions``, ``sinks`` and
        ``warnings`` keys, ready to be serialized as JSON.
    """
    return SpringAnalyzer(Path(root), max_depth=max_depth).analyze()


def main(argv: "Optional[Sequence[str]]" = None) -> int:
    """CLI entry point: print the manifest for a repository as pretty JSON."""
    arguments = _parse_args(argv)
    if not arguments.repo.is_dir():
        print(f"error: {arguments.repo} is not a directory", file=sys.stderr)
        return 2

    manifest = analyze_repository(arguments.repo, max_depth=arguments.max_depth)
    payload = json.dumps(manifest, indent=arguments.indent, ensure_ascii=False)

    if arguments.output is not None:
        arguments.output.write_text(payload + "\n", encoding="utf-8")
        print(
            f"wrote {arguments.output} "
            f"({len(manifest['entry_points'])} entry points, "
            f"{len(manifest['functions'])} functions, "
            f"{len(manifest['sinks'])} sinks, "
            f"{len(manifest['warnings'])} warnings)",
            file=sys.stderr,
        )
    else:
        print(payload)
    return 0


def _parse_args(argv: "Optional[Sequence[str]]") -> argparse.Namespace:
    """Build and run the argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m parser.spring_analyze",
        description="Parse a Spring Boot repository into an execution manifest (JSON).",
    )
    parser.add_argument("repo", type=Path, help="path to the repository root to analyze")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="write the manifest to this file instead of stdout",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=DEFAULT_MAX_DEPTH,
        help=f"call-graph depth to trace from each entry point (default: {DEFAULT_MAX_DEPTH})",
    )
    parser.add_argument("--indent", type=int, default=2, help="JSON indentation (default: 2)")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
