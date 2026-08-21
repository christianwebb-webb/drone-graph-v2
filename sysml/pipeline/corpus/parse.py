"""SysML v2 textual notation -> a tree of declared elements.

This is a parser for the language, not a reader for these three models. It
recognises the whole declaration grammar -- every definition and usage keyword,
every specialisation operator, every relationship statement, multiplicities,
metadata annotations, `doc` comments and expressions -- so a .sysml file this
project has never seen parses on the same rules as the ones it ships with.

Why that is worth the code: everything a SysML file states, it states exactly.
`connect battery to powerManagementModule` is not a hint that those two are
related, it is the model saying they are connected, in that direction, at that
line. The previous version of this project read five of those forms with regexes
and left the rest to a language model, which then reported roughly what the file
already said. What the syntax states belongs in the graph as fact; only what the
syntax does not state -- what an element is for, what a paragraph of prose means
-- is worth asking a model about.

The output is a tree. `Element` is one declaration with everything written on it:
kind, name, short name, modifiers, multiplicity, every specialisation, its value,
its documentation, its metadata annotations, the relationship its keyword states,
and the elements declared inside it. References are kept as written, as `Ref`,
and resolved later -- `model` needs the whole corpus before it can say what
`Drone_SharedAssetsSuperset::Drone` names.

    from sysml.pipeline.corpus import parse
    tree = parse.parse_file(path)

Reading order, for anyone extending it: the tables below say what the keywords
are, `Parser.member` is the top of the grammar, `Parser.declaration` is the shape
every declaration shares, and the handlers after it are the statements whose
shape is their own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .lex import COMMENT, END, NAME, NUMBER, OP, STRING, Token, lex


class ParseError(Exception):
    def __init__(self, message: str, token: Token, path: str = "") -> None:
        where = (f"{path}:{token.line}:{token.column}" if path
                 else f"line {token.line}, column {token.column}")
        super().__init__(f"{message} at {where} (near {token.text!r})")
        self.token = token
        self.path = path


# The keyword that introduces a definition, and the kind it makes. A definition
# and a usage written with the same keyword are the same kind -- `part def Pump`
# and `part coolantPump : Pump` are both parts -- and `is_definition` tells them
# apart. That is the language's own distinction (PartDefinition against
# PartUsage), and keeping it as a flag rather than as two kinds means a question
# about parts finds both without knowing the pair of names.
DEFINITION_KINDS = {
    "attribute": "attribute", "enum": "enumeration", "occurrence": "occurrence",
    "item": "item", "part": "part", "port": "port", "connection": "connection",
    "interface": "interface", "flow": "flow", "allocation": "allocation",
    "action": "action", "state": "state", "constraint": "constraint",
    "requirement": "requirement", "concern": "concern", "calc": "calculation",
    "case": "case", "analysis": "analysis", "verification": "verification",
    "view": "view", "viewpoint": "viewpoint", "rendering": "rendering",
    "metadata": "metadata",
}

# Usage-only keywords and the phrases of more than one word. `analysis case` and
# `analysis` are both written and mean the same definition, as are `verification
# case` and `verification`; `use case` has no one-word form.
USAGE_KINDS = {
    ("use", "case"): "usecase",
    ("analysis", "case"): "analysis",
    ("verification", "case"): "verification",
    ("event", "occurrence"): "event",
    ("package",): "package",
    ("snapshot",): "snapshot",
    ("timeslice",): "timeslice",
    ("event",): "event",
    ("message",): "message",
    ("transition",): "transition",
    # A succession flow both sequences and transfers, so it stays a succession and
    # `keyword_tail` gives it a `flows` edge as well. Read as one word it becomes a
    # succession named "flow" with its two ends thrown away.
    ("succession", "flow"): "succession",
    ("succession",): "succession",
    ("binding",): "binding",
    # The control nodes of an action body. Without these, `if` is read as the name
    # of a member and `else` as another one, so a branch becomes two elements
    # called "if" and "else" and the two arms are lost.
    ("if",): "if",
    ("while",): "loop",
    ("loop",): "loop",
    ("for",): "loop",
    ("subject",): "subject",
    ("actor",): "actor",
    ("stakeholder",): "stakeholder",
    ("objective",): "objective",
    ("return",): "return",
    ("entry",): "entry",
    ("exit",): "exit",
    ("do",): "do",
    ("accept",): "accept",
    ("send",): "send",
    ("assign",): "assign",
    ("connect",): "connection",
    ("bind",): "binding",
    ("allocate",): "allocation",
    ("dependency",): "dependency",
    ("import",): "import",
    ("alias",): "alias",
    ("filter",): "filter",
    ("expose",): "expose",
    ("first",): "succession",
    ("then",): "succession",
    ("decide",): "decide",
    ("merge",): "merge",
    ("fork",): "fork",
    ("join",): "join",
    ("terminate",): "terminate",
    ("doc",): "doc",
    ("comment",): "comment",
    ("rep",): "rep",
    ("use",): "usecase",
}

# Longest phrase first, so `use case def` is tried before `use` -- otherwise the
# name of every use case becomes "case".
KEYWORD_PHRASES: tuple[tuple[tuple[str, ...], str], ...] = tuple(sorted(
    list(USAGE_KINDS.items()) + [((word,), kind) for word, kind in DEFINITION_KINDS.items()],
    key=lambda pair: -len(pair[0]),
))

# Everything that may stand in front of the keyword. Missing one is not a missing
# adjective: `standard library package ScalarValues` stops being recognised as a
# package at all, and everything inside it loses its owner.
#
# These are kept rather than dropped. `abstract` is the difference between a part
# that can exist and one that only classifies; `individual` marks the one S-IC
# that actually flew; `variation` and `variant` are a choice point and its
# options; `in`/`out` is the direction of a port's payload. The previous version
# stripped all of them, so none of it reached the graph.
MODIFIERS = {
    "abstract", "variation", "variant", "individual", "readonly", "derived",
    "end", "ref", "ordered", "nonunique", "constant", "portion", "standard",
    "library", "crossing", "default", "bare",
}
DIRECTIONS = {"in", "out", "inout"}
VISIBILITY = {"public", "private", "protected"}

# Keywords that introduce a declaration by naming the relationship it takes part
# in. `perform action checkStatus` declares an action and says the enclosing part
# performs it; `assert constraint { x < 5 }` declares a constraint and says it is
# asserted. Where no keyword follows, the kind is implied -- `perform load` is
# still an action, and the name is a reference to one declared elsewhere.
RELATIONSHIP_PREFIXES = {
    "perform": ("performs", "action"),
    "exhibit": ("exhibits", "state"),
    "include": ("includes", "usecase"),
    "assert": ("asserts", "constraint"),
    "assume": ("assumes", "constraint"),
    "require": ("requires", "constraint"),
    "satisfy": ("satisfies", "requirement"),
    "verify": ("verifies", "requirement"),
    "refine": ("refines", "requirement"),
    "derive": ("derives", "requirement"),
    "frame": ("frames", "concern"),
    "render": ("renders", "rendering"),
    "expose": ("exposes", "expose"),
    # A state's three action slots. `do action recoveryQuarantineOperations { ... }`
    # declares the action and says when it runs; reading `do` as the declaration
    # keyword instead makes an element called "action" and throws the real name
    # away, which takes every feature chain through that action with it.
    "do": ("doAction", "action"),
    "entry": ("entryAction", "action"),
    "exit": ("exitAction", "action"),
}

# Long-form spellings of the specialisation operators. SysML accepts both, and
# `enum redColor redefines red` means exactly what `:>>` means.
SPECIALIZATION_WORDS = {
    "specializes": ":>", "subsets": ":>", "redefines": ":>>",
    "references": "::>", "conjugates": "~", "conjugate": "~",
}

# KerML's type operators, which a .sysml file may use and which are not
# specialisations. Left unhandled, the keyword is read as the name of a member and
# `part x : A unions b, c` declares three elements called `unions`, `b` and `c`
# owned by whatever encloses it -- rows that are not merely missing but false.
TYPE_OPERATORS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("disjoint", "from"), "disjointFrom"),
    (("inverse", "of"), "inverseOf"),
    (("unions",), "unionOf"),
    (("intersects",), "intersectionOf"),
    (("differences",), "differenceOf"),
    (("chains",), "chainOf"),
)

# Words that follow a declaration's name and are not a name themselves.
# `parallel` is a state's concurrency, `variant`-style modifiers may trail the
# name, and the type operators above all begin with one of these.
TRAILING_MODIFIERS = {"parallel"}

# Words that close a name slot rather than filling it: `satisfy X by Y` has to
# stop before `by`. They are perfectly good element names too, so a word here is
# only a tail word when what follows it is not a declaration's punctuation.
TAIL_WORDS = {
    "by", "to", "from", "of", "via", "then", "first", "accept", "if", "do",
    "else", "for", "about", "defined", "typed", "connect", "language",
} | set(SPECIALIZATION_WORDS) | DIRECTIONS | TRAILING_MODIFIERS | {
    word for phrase, _ in TYPE_OPERATORS for word in phrase[:1]}

# Statements whose shape is not a declaration's. Everything else goes through
# `declaration`, which is most of the language.
STATEMENT_HANDLERS = {
    "doc": "comment_statement", "comment": "comment_statement",
    "rep": "comment_statement",
    "import": "import_statement", "alias": "alias_statement",
    "filter": "filter_statement", "expose": "expose_statement",
    "dependency": "dependency_statement",
    "connect": "connect_statement", "bind": "bind_statement",
    "allocate": "allocate_statement",
    "accept": "accept_statement", "send": "send_statement",
    "assign": "assign_statement",
    "first": "first_statement", "then": "then_statement",
}


@dataclass
class Ref:
    """A reference exactly as written.

    `parts` keeps the separators, because they do not mean the same thing: `A::b`
    looks `b` up inside the namespace `A`, and `a.b` follows a feature of whatever
    types `a`. Flattening them into one path is how a resolver ends up walking
    containment where the file walks typing.
    """
    text: str
    parts: list[tuple[str, str]] = field(default_factory=list)
    line: int = 0
    column: int = 0

    @property
    def segments(self) -> list[str]:
        return [name for _, name in self.parts if name]

    @property
    def is_chain(self) -> bool:
        return any(separator == "." for separator, _ in self.parts)

    @property
    def wildcard(self) -> str:
        last = self.parts[-1][1] if self.parts else ""
        return last if last in ("*", "**") else ""

    def __str__(self) -> str:
        return self.text


@dataclass
class Expr:
    """A parsed expression, kept both as a tree and as the text it came from.

    The tree is what makes `drone.totalMass <= 750` usable: a bound on a named
    feature is a fact worth storing next to the feature it bounds. The text is
    what keeps it honest -- anything the tree flattens can still be read.
    """
    op: str
    args: list[Any] = field(default_factory=list)
    text: str = ""

    def refs(self) -> list[Ref]:
        found: list[Ref] = []
        queue: list[Any] = [self]
        while queue:
            node = queue.pop(0)
            if isinstance(node, Ref):
                found.append(node)
            elif isinstance(node, Expr):
                queue = list(node.args) + queue
        return found


@dataclass
class Annotation:
    """A `#Name` prefix or an `@Name { ... }` annotation.

    `@Rationale { text = "..." }` is the only prose in these requirement files
    that is not a `doc` comment, and there are 294 of them. It is a declaration
    with a body, so it parses like one and its bindings land in `values`.
    """
    name: Ref
    prefix: bool
    values: dict[str, Any] = field(default_factory=dict)
    closed: bool = False
    line: int = 0


@dataclass
class Element:
    """One declaration and everything written on it."""
    kind: str
    is_definition: bool = False
    name: Optional[str] = None
    short_name: Optional[str] = None

    modifiers: list[str] = field(default_factory=list)
    visibility: Optional[str] = None
    direction: Optional[str] = None

    multiplicity: Optional[dict] = None
    typed_by: list[Ref] = field(default_factory=list)
    conjugated: bool = False
    supers: list[Ref] = field(default_factory=list)       # `:>`, specializes, subsets
    redefines: list[Ref] = field(default_factory=list)    # `:>>`
    references: list[Ref] = field(default_factory=list)   # `::>` and reference slots

    value: Optional[dict] = None          # {"op": "=" | ":=", "expr": Expr}
    doc: Optional[str] = None
    comments: list[str] = field(default_factory=list)
    annotations: list[Annotation] = field(default_factory=list)

    # What the keyword says about the relationship this declaration takes part
    # in -- {"form": "satisfies", "by": [Ref]} and the like. `form` is the name
    # `model` writes the edge under.
    relationship: Optional[dict] = None

    # KerML's type operators: {"disjointFrom": [Ref], "unionOf": [Ref, Ref]}.
    # Separate from `supers` because none of them is a specialisation -- a walk
    # that treats `differences` as `:>` inverts the meaning of the type.
    type_ops: dict[str, list[Ref]] = field(default_factory=dict)

    result: Optional[Expr] = None         # a constraint's or calculation's body
    children: list["Element"] = field(default_factory=list)
    # Elements declared inside a succession -- `then action load : Load;` declares
    # the action and the edge to it in one statement. The action belongs to the
    # body the succession is written in, not to the succession, so it is kept
    # aside here and spliced back in as a sibling.
    inline: list["Element"] = field(default_factory=list)

    file: str = ""
    line: int = 0
    column: int = 0

    # Filled in by `model`, not here: the element's place in the corpus once the
    # whole corpus is known.
    qualified: str = ""
    owner: Optional["Element"] = None
    model: str = ""
    id: str = ""
    display: str = ""

    @property
    def anonymous(self) -> bool:
        return not self.name

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


class Parser:
    def __init__(self, tokens: list[Token], path: str = "") -> None:
        self.tokens = tokens
        self.path = path
        self.pos = 0

    # cursor

    def peek(self, ahead: int = 0) -> Token:
        return self.tokens[min(self.pos + ahead, len(self.tokens) - 1)]

    def take(self) -> Token:
        token = self.peek()
        if token.kind != END:
            self.pos += 1
        return token

    def at_op(self, *symbols: str) -> bool:
        return self.peek().is_op(*symbols)

    def at_name(self, *words: str) -> bool:
        return self.peek().is_name(*words)

    def at_end(self) -> bool:
        return self.peek().kind == END

    def accept_op(self, *symbols: str) -> Optional[Token]:
        return self.take() if self.at_op(*symbols) else None

    def accept_name(self, *words: str) -> Optional[Token]:
        return self.take() if self.at_name(*words) else None

    def expect_op(self, symbol: str) -> Token:
        if not self.at_op(symbol):
            raise ParseError(f"expected {symbol!r}", self.peek(), self.path)
        return self.take()

    # the top of the grammar

    def parse(self) -> list[Element]:
        members: list[Element] = []
        while not self.at_end():
            if self.accept_op(";"):
                continue
            before = self.pos
            member = self.member()
            if member is not None:
                members.append(member)
                members.extend(member.inline)
                member.inline = []
            if self.pos == before:
                self.take()                       # never spin on an unreadable token
        return members

    def member(self) -> Optional[Element]:
        """One declaration, with everything that may stand in front of it.

        A stray block comment is a member of the namespace in SysML and carries no
        name; it is read and dropped here rather than in the lexer, because the
        lexer cannot tell it from the body of a `doc`.
        """
        start = self.peek()
        if start.kind == COMMENT:
            self.take()
            return None

        annotations: list[Annotation] = []
        modifiers: list[str] = []
        visibility: Optional[str] = None
        direction: Optional[str] = None
        prefix: Optional[str] = None

        while True:
            if self.at_op("#"):
                annotations.append(self.prefix_annotation())
                continue
            if self.at_op("@"):
                annotation = self.annotation()
                if annotation.values or annotation.closed:
                    # `@Rationale { text = "..." }` is a member of the body it is
                    # written in, and it annotates that body's element -- not
                    # whatever declaration happens to follow it. Treating it as a
                    # prefix put all 294 rationales on the `#refinement
                    # dependency` written underneath them, and lost the 22 that
                    # had nothing underneath at all.
                    return Element(kind="annotation", name=str(annotation.name),
                                   annotations=[annotation], file=self.path,
                                   line=annotation.line, column=start.column)
                annotations.append(annotation)
                continue
            token = self.peek()
            if token.kind != NAME:
                break
            word, following = token.text, self.peek(1)
            declares_something = (following.kind == NAME
                                  or following.is_op("#", "@", "<", ":", ":>", ":>>",
                                                     "::>", "~", "["))
            if word in VISIBILITY and declares_something:
                visibility, _ = word, self.take()
                continue
            if word in DIRECTIONS and declares_something:
                direction, _ = word, self.take()
                continue
            if word in MODIFIERS and declares_something:
                modifiers.append(word)
                self.take()
                continue
            if word in RELATIONSHIP_PREFIXES and prefix is None \
                    and not following.is_op(";", "{", "=", "}"):
                prefix = word
                self.take()
                continue
            break

        kind, is_definition, phrase = self.keyword_phrase()
        implied = False
        if kind is None:
            if prefix:
                # `perform Crew::connectSpacecraft :>> connectSpacecraft;` -- the
                # keyword is implied by the prefix, and what follows is a
                # reference to the element being performed rather than a new name.
                kind, implied = RELATIONSHIP_PREFIXES[prefix][1], True
            elif self.at_op("}") or self.at_end():
                return None
            else:
                # A feature declared with no keyword: `end capa ::> goal`,
                # `text = "..."`, `:>> weight = 275 [SI::g]`. These carry values,
                # and dropping them is how a battery's mass never reached a graph.
                kind = "feature"

        element = Element(
            kind=kind, is_definition=is_definition, modifiers=modifiers,
            visibility=visibility, direction=direction, annotations=annotations,
            file=self.path, line=start.line, column=start.column,
        )
        if prefix:
            form, _ = RELATIONSHIP_PREFIXES[prefix]
            element.relationship = {"form": form, "keyword": prefix}

        handler = STATEMENT_HANDLERS.get(" ".join(phrase)) if phrase else None
        if handler:
            return getattr(self, handler)(element)
        return self.declaration(element, reference_slot=implied)

    # prefixes

    def prefix_annotation(self) -> Annotation:
        token = self.expect_op("#")
        return Annotation(name=self.reference(), prefix=True, line=token.line)

    def annotation(self) -> Annotation:
        """`@Name`, and `@Name { feature = value; ... }`.

        The body is what separates the two ways an annotation is used. A bare
        `@Name` (or `#Name`) prefixes the declaration after it; one with a body is
        a metadata usage in its own right and belongs to whatever declares it.
        """
        token = self.expect_op("@")
        name = self.reference()
        values: dict[str, Any] = {}
        closed = False
        if self.at_op("{"):
            for child in self.body():
                if isinstance(child, Element) and child.name and child.value:
                    values[child.name] = child.value["expr"]
            closed = True
        if self.accept_op(";"):
            closed = True
        return Annotation(name=name, prefix=False, values=values,
                          closed=closed, line=token.line)

    def keyword_phrase(self) -> tuple[Optional[str], bool, tuple[str, ...]]:
        """The declaration keyword, whether `def` follows it, and the words used."""
        if self.peek().kind != NAME:
            return None, False, ()
        for words, kind in KEYWORD_PHRASES:
            if all(self.peek(i).is_name(word) for i, word in enumerate(words)):
                for _ in words:
                    self.take()
                return kind, self.accept_name("def") is not None, words
        return None, False, ()

    # declarations

    def declaration(self, element: Element, reference_slot: bool = False) -> Element:
        """`<short> name [mult] : T :> S :>> R ::> P = v ( { ... } | ; )`

        The parts after the name come in any order and any number, and they may
        also come *after* the body: `connection : C { end capa ::> x; } :>
        capabilityToGoals;` is how every capability-to-goal edge in the Apollo
        model is written. So the tail is read in a loop, and the loop is entered
        again once the body closes.
        """
        if self.at_op("<"):
            element.short_name = self.short_name()

        if self.peek().kind == NAME and not self._name_is_tail():
            if reference_slot or self.peek(1).is_op("::", "."):
                # A qualified name can never be a declaration's own name, so it is
                # a reference to something declared elsewhere.
                element.references.append(self.reference())
            else:
                element.name = self.take().text

        self.declaration_tail(element)

        if self.at_op("{"):
            for child in self.body():
                self._absorb(element, child)
            if self._tail_after_body():
                self.declaration_tail(element)
        self.accept_op(";")
        return element

    def _tail_after_body(self) -> bool:
        """Whether what follows a closed body still belongs to that declaration.

        Almost nothing does. `connection : C { end capa ::> x; } :> capabilityToGoals;`
        is the one shape in this corpus that carries a specialisation past its own
        brace, and reading the tail unconditionally is worse than not reading it
        at all: `:>> body = X { ... }` followed by `:>> flightControl = Y;` becomes
        one redefinition of two features whose value is the second one's, and
        `part engine4 { ... }` followed by `connect a to b;` swallows the
        connection and reports it at the part's line.

        So the tail continues only for a specialisation operator, and only when
        nothing between here and the end of the statement can start a declaration
        of its own.
        """
        if not self.at_op(":", ":>", ":>>", "::>"):
            return False
        for ahead in range(len(self.tokens) - self.pos):
            token = self.peek(ahead)
            if token.kind == END or token.is_op(";", "}"):
                return True
            if token.is_op("=", ":=", "{", "["):
                return False
        return True

    def _absorb(self, element: Element, child: Any) -> None:
        """Put one body member where it belongs on its owner."""
        if isinstance(child, Expr):
            element.result = child
        elif child.kind == "annotation":
            element.annotations.extend(child.annotations)
        elif child.kind == "doc":
            element.doc = "\n\n".join(x for x in (element.doc, child.doc) if x)
        elif child.kind in ("comment", "rep"):
            if child.doc:
                element.comments.append(child.doc)
        else:
            element.children.append(child)

    def declaration_tail(self, element: Element) -> None:
        """Everything a declaration can carry, read until none of it comes next."""
        while True:
            if self.at_op("["):
                element.multiplicity = self.multiplicity()
                continue
            if self.accept_op(":"):
                self.type_list(element)
                continue
            if self.accept_op(":>>"):
                element.redefines.extend(self.reference_list())
                continue
            if self.accept_op(":>"):
                element.supers.extend(self.reference_list())
                continue
            if self.accept_op("::>"):
                element.references.extend(self.reference_list())
                continue
            if self.at_name("defined", "typed") and self.peek(1).is_name("by"):
                self.take()
                self.take()
                self.type_list(element)
                continue
            if self.type_operator(element):
                continue
            if self.peek().kind == NAME and self.peek().text in TRAILING_MODIFIERS:
                element.modifiers.append(self.take().text)
                continue
            word = self.peek().text if self.peek().kind == NAME else ""
            if word in SPECIALIZATION_WORDS:
                symbol = SPECIALIZATION_WORDS[self.take().text]
                if symbol == "~":
                    element.conjugated = True
                    self.type_list(element)
                elif symbol == ":>>":
                    element.redefines.extend(self.reference_list())
                elif symbol == "::>":
                    element.references.extend(self.reference_list())
                else:
                    element.supers.extend(self.reference_list())
                continue
            if self.at_op("=", ":="):
                symbol = self.take().text
                element.value = {"op": symbol, "expr": self.expression()}
                continue
            if self.keyword_tail(element):
                continue
            break

    def type_operator(self, element: Element) -> bool:
        """`disjoint from X`, `unions A, B`, `chains a.b` and the rest of KerML's.

        They read like a specialisation and are not one: a union is a type built
        from two others, and following it as `:>` walks into a type the element is
        not. So each gets its own label rather than joining `supers`.
        """
        for phrase, label in TYPE_OPERATORS:
            if all(self.peek(i).is_name(word) for i, word in enumerate(phrase)):
                for _ in phrase:
                    self.take()
                element.type_ops.setdefault(label, []).extend(self.reference_list())
                return True
        return False

    def type_list(self, element: Element) -> None:
        """The types after a `:`, allowing `~` for a conjugated port."""
        while True:
            if self.accept_op("~"):
                element.conjugated = True
            element.typed_by.append(self.reference())
            if not self.accept_op(","):
                break

    def keyword_tail(self, element: Element) -> bool:
        """The words a relationship keyword adds after the name.

        Each states two ends and a direction, and each was invisible before: 76
        `connect`s, 19 `transition`s, 369 `#refinement dependency A to B`. They
        are read into one shape -- a form and the references it joins -- so the
        model has one thing to walk rather than a case per keyword.
        """
        relationship = element.relationship or {}
        kind = element.kind
        form = relationship.get("form")

        # `if cond then a else b` and `while cond { ... }`. The condition is an
        # expression, and each arm is a target, so a branch reads the same way a
        # transition does and answers "what can happen next".
        if kind in ("if", "loop"):
            if self.at_op("(") or not self.at_name("then", "else", "in"):
                if "guard" not in relationship and not self.at_op("{", ";"):
                    relationship["guard"] = self.expression()
                    element.relationship = relationship
                    return True
            if self.accept_name("in"):                 # `for i in collection`
                relationship["over"] = self.expression()
                element.relationship = relationship
                return True
            for word, slot in (("then", "to"), ("else", "otherwise")):
                if self.at_name(word):
                    self.take()
                    relationship["form"] = "transitionsTo"
                    relationship[slot] = [self.succession_target(element)]
                    element.relationship = relationship
                    return True
            return False

        if self.at_name("by"):
            self.take()
            relationship.setdefault("form", "verifies" if kind == "verification"
                                    else "satisfies")
            relationship["by"] = self.reference_list()
            element.relationship = relationship
            return True

        if self.at_name("connect"):
            self.take()
            relationship["form"] = "connects"
            relationship["ends"] = self.connect_ends()
            element.relationship = relationship
            return True

        if kind in ("flow", "message") and self.at_name("of"):
            self.take()
            relationship["payload"] = self.reference()
            element.relationship = relationship
            return True

        # `derive R from R2` states which requirement R was derived from. Read as a
        # plain tail word, `from` closes the name slot and R2 becomes a member of
        # the enclosing body -- so the statement declared the wrong thing and lost
        # the only reference that carried its meaning.
        if self.at_name("from") and form in ("derives", "refines"):
            self.take()
            relationship["source"] = self.reference_list()
            element.relationship = relationship
            return True

        if self.at_name("from") and kind in ("flow", "message", "allocation",
                                             "dependency", "connection", "binding",
                                             "succession"):
            self.take()
            relationship.setdefault("form", _END_FORMS.get(kind, "connects"))
            relationship["sequences"] = kind == "succession"
            relationship["from"] = self.reference_list()
            element.relationship = relationship
            return True

        if self.at_name("to") and kind in ("flow", "message", "allocation",
                                           "dependency", "connection", "binding"):
            self.take()
            relationship.setdefault("form", _END_FORMS.get(kind, "connects"))
            relationship["sequences"] = relationship.get("sequences") or kind == "succession"
            relationship["to"] = self.reference_list()
            element.relationship = relationship
            return True

        if kind in ("transition", "succession") and self.at_name("first"):
            self.take()
            relationship["form"] = "transitionsTo"
            relationship["from"] = [self.reference()]
            element.relationship = relationship
            return True

        if kind in ("transition", "succession", "accept") and self.at_name("accept"):
            self.take()
            relationship.setdefault("form", "transitionsTo")
            relationship["trigger"] = self.trigger()
            element.relationship = relationship
            return True

        if kind in ("transition", "succession") and self.at_name("if"):
            self.take()
            relationship["guard"] = self.expression()
            element.relationship = relationship
            return True

        if kind in ("transition", "succession") and self.at_name("do"):
            self.take()
            relationship["effect"] = self.reference()
            element.relationship = relationship
            return True

        # One `then` per statement. A body that reads `first start; then a; then b;`
        # is three successions in a row, and letting one declaration swallow the
        # next `then` collapses the whole chain into a single edge to its last
        # member -- which is what happened to every mission phase in the Apollo
        # timeline the first time this was written.
        if self.at_name("then") and "to" not in relationship \
                and kind in ("transition", "succession", "accept", "send",
                             "do", "entry", "exit"):
            self.take()
            relationship.setdefault("form", "transitionsTo")
            relationship["to"] = [self.succession_target(element)]
            element.relationship = relationship
            return True

        if self.at_name("via") and kind in ("accept", "send", "message"):
            self.take()
            relationship["via"] = self.reference()
            element.relationship = relationship
            return True

        return False

    def connect_ends(self) -> list[Ref]:
        """`connect a to b`, `connect (a, b, c)` and `connect from a to b`."""
        if self.at_op("("):
            self.take()
            ends = []
            while not self.at_op(")") and not self.at_end():
                ends.append(self.reference())
                if not self.accept_op(","):
                    break
            self.expect_op(")")
            return ends
        self.accept_name("from")
        ends = self.reference_list()
        while self.accept_name("to"):
            ends.extend(self.reference_list())
        return ends

    def succession_target(self, owner: Optional[Element] = None) -> Ref:
        """What follows `then`.

        Usually a name, but `then action loadConsumables : Load;` and
        `then timeslice liftoff { ... }` declare the target inline -- which is how
        every mission phase in the Apollo timeline is written. The declaration is
        read in full and kept as a child, and the edge points at its name.
        """
        mark = self.pos
        kind, _is_definition, phrase = self.keyword_phrase()
        self.pos = mark
        if kind is None:
            return self.reference()
        declared = self.member()
        if declared is None:
            return Ref(text="")
        if owner is not None:
            owner.inline.append(declared)
        name = declared.name or ""
        return Ref(text=name, parts=[("", name)] if name else [],
                   line=declared.line, column=declared.column)

    def trigger(self) -> Ref:
        """The payload named after `accept`.

        `accept SigSwitchOn then standBy` names a signal; `accept sig : Signal via
        port` declares a parameter typed by one. Both come back as the type,
        because the type is what the transition actually triggers on.
        """
        if self.peek().kind == NAME and self.peek(1).is_op(":"):
            self.take()
            self.take()
        return self.reference()

    # statements whose shape is their own

    def comment_statement(self, element: Element) -> Element:
        """`doc /* */`, `comment [name] [about A, B] /* */`, `rep [name] language "x" /* */`."""
        if self.at_op("<"):
            element.short_name = self.short_name()
        if self.peek().kind == NAME and not self.peek().is_name("about", "language"):
            element.name = self.take().text
        if self.accept_name("about"):
            element.references.extend(self.reference_list())
        self.accept_name("language")
        if self.peek().kind == STRING:
            element.value = {"op": "=", "expr": Expr("string", [self.take().text])}
        if self.peek().kind == COMMENT:
            element.doc = self.take().text
        self.accept_op(";")
        return element

    def import_statement(self, element: Element) -> Element:
        """`import A::B::*;`, `import A::**;`, `import A::B;`"""
        target = self.reference(wildcards=True)
        element.references.append(target)
        element.name = target.text
        element.relationship = {"form": "imports", "to": [target],
                                "recursive": target.wildcard == "**",
                                "wildcard": bool(target.wildcard)}
        self.declaration_tail(element)
        self.accept_op(";")
        return element

    def alias_statement(self, element: Element) -> Element:
        """`alias N for A::B;`"""
        if self.at_op("<"):
            element.short_name = self.short_name()
        if self.peek().kind == NAME and not self.peek().is_name("for"):
            element.name = self.take().text
        if self.accept_name("for"):
            target = self.reference()
            element.references.append(target)
            element.relationship = {"form": "aliasOf", "to": [target]}
            element.name = element.name or (target.segments[-1] if target.segments else None)
        self.accept_op(";")
        return element

    def filter_statement(self, element: Element) -> Element:
        element.result = self.expression()
        self.accept_op(";")
        return element

    def expose_statement(self, element: Element) -> Element:
        """`expose A::B::*;` -- what a view draws from."""
        element.references.extend(self.reference_list(wildcards=True))
        element.relationship = {"form": "exposes", "to": list(element.references)}
        self.declaration_tail(element)
        self.accept_op(";")
        return element

    def dependency_statement(self, element: Element) -> Element:
        """`dependency [<sn>] [name] [from] A[, A] to B[, B];`

        The name is optional and so is `from`, which is why the clients cannot be
        read out of the name slot: `#refinement dependency SICEngineConfiguration
        to FLR-R008` has no name at all, and taking the client for one is how 369
        refinement dependencies became 369 elements called after their source.
        """
        if self.at_op("<"):
            element.short_name = self.short_name()
        if self.peek().kind == NAME and self.peek(1).is_name("from"):
            element.name = self.take().text
        self.accept_name("from")
        clients = self.reference_list()
        suppliers: list[Ref] = []
        if self.accept_name("to"):
            suppliers = self.reference_list()
        element.relationship = {"form": "dependsOn", "from": clients, "to": suppliers}
        self.accept_op(";")
        return element

    def connect_statement(self, element: Element) -> Element:
        """`connect a to b;` and `connect (a, b, c);`"""
        element.relationship = {"form": "connects", "ends": self.connect_ends()}
        self.declaration_tail(element)
        self.accept_op(";")
        return element

    def bind_statement(self, element: Element) -> Element:
        """`bind a = b;`"""
        ends = [self.reference()]
        if self.accept_op("=") or self.accept_op(":="):
            ends.append(self.reference())
        element.relationship = {"form": "bindsTo", "ends": ends}
        self.accept_op(";")
        return element

    def allocate_statement(self, element: Element) -> Element:
        """`allocate A to B;`"""
        clients = self.reference_list()
        targets = self.reference_list() if self.accept_name("to") else []
        element.relationship = {"form": "allocates", "from": clients, "to": targets}
        self.declaration_tail(element)
        self.accept_op(";")
        return element

    def accept_statement(self, element: Element) -> Element:
        """`accept SigSwitchOn then standBy;` and `accept x : T via port;`"""
        relationship = element.relationship or {}
        relationship["form"] = "accepts"
        relationship["trigger"] = self.trigger()
        element.relationship = relationship
        self.declaration_tail(element)
        if self.at_op("{"):
            for child in self.body():
                self._absorb(element, child)
        self.accept_op(";")
        return element

    def send_statement(self, element: Element) -> Element:
        """`send payload via port to target;`"""
        relationship = element.relationship or {}
        relationship["form"] = "sends"
        relationship["payload"] = self.expression()
        if self.accept_name("via"):
            relationship["via"] = self.reference()
        if self.accept_name("to"):
            relationship["to"] = self.reference_list()
        element.relationship = relationship
        self.accept_op(";")
        return element

    def assign_statement(self, element: Element) -> Element:
        """`assign x := expression;`"""
        target = self.reference()
        self.accept_op(":=") or self.accept_op("=")
        element.references.append(target)
        element.value = {"op": ":=", "expr": self.expression()}
        element.relationship = {"form": "assigns", "to": [target]}
        self.accept_op(";")
        return element

    def first_statement(self, element: Element) -> Element:
        """`first start;` -- where a sequence of successions begins."""
        element.relationship = {"form": "startsAt", "to": [self.reference()]}
        self.accept_op(";")
        return element

    def then_statement(self, element: Element) -> Element:
        """`then done;`, `then action load : Load;`, `then timeslice liftoff { ... }`

        The source is whatever came before it in the same body, which the parser
        cannot see -- `model` joins them, walking each body in declaration order.
        """
        target = self.succession_target(element)
        element.relationship = {"form": "transitionsTo", "to": [target],
                                "from_previous": True}
        self.declaration_tail(element)
        self.accept_op(";")
        return element

    # pieces

    def short_name(self) -> str:
        self.expect_op("<")
        parts: list[str] = []
        while not self.at_op(">") and not self.at_end():
            parts.append(self.take().text)
        self.expect_op(">")
        return "".join(parts)

    def _name_is_tail(self) -> bool:
        """Whether the next name closes the name slot instead of filling it.

        `satisfy longDistance by drone` has no name of its own, and reading
        `longDistance` as one leaves `by drone` dangling. Tail words are also
        perfectly good element names, so a word followed by a declaration's own
        punctuation is a name regardless of the list.
        """
        token = self.peek()
        if token.text not in TAIL_WORDS:
            return False
        return not self.peek(1).is_op(":", ":>", ":>>", "::>", "=", ":=",
                                      "{", ";", "[", ",", "}")

    def multiplicity(self) -> dict:
        """`[1]`, `[1..*]`, `[*]`, `[0..*]`, and `[numberOfEnginesVariation]`.

        The last is why the bounds are optional. A multiplicity in SysML is an
        expression, and this model uses a variation attribute as one: the number
        of engines is a choice made elsewhere, and the bracket names the choice.
        """
        open_token = self.expect_op("[")
        depth, parts = 1, []
        while not self.at_end():
            if self.at_op("["):
                depth += 1
            elif self.at_op("]"):
                depth -= 1
                if depth == 0:
                    break
            parts.append(self.take())
        self.expect_op("]")
        text = _join(parts)
        lower = upper = None
        if ".." in text:
            left, _, right = text.partition("..")
            lower, upper = _bound(left), _bound(right)
        elif text.strip() == "*":
            lower, upper = 0, None
        elif text.strip().isdigit():
            lower = upper = int(text.strip())
        return {"text": text, "lower": lower, "upper": upper, "line": open_token.line}

    def reference(self, wildcards: bool = False) -> Ref:
        """One qualified name or feature chain: `A::B::c`, `a.b.c`, `A::**`."""
        token = self.peek()
        if token.kind != NAME:
            raise ParseError("expected a name", token, self.path)
        parts: list[tuple[str, str]] = [("", self.take().text)]
        while self.at_op("::", "."):
            separator = self.take().text
            if wildcards and self.at_op("**"):
                self.take()
                parts.append((separator, "**"))
                continue
            if wildcards and self.at_op("*"):
                self.take()
                parts.append((separator, "*"))
                continue
            if self.peek().kind != NAME:
                parts.append((separator, ""))
                break
            parts.append((separator, self.take().text))
        text = "".join(separator + name for separator, name in parts)
        return Ref(text=text, parts=parts, line=token.line, column=token.column)

    def reference_list(self, wildcards: bool = False) -> list[Ref]:
        refs = [self.reference(wildcards)]
        while self.accept_op(","):
            refs.append(self.reference(wildcards))
        return refs

    def body(self) -> list[Any]:
        """The members between `{` and `}`, or the expression that is the body.

        A constraint's body is an expression (`{ drone.totalMass <= 750 }`) and a
        part's body is a list of declarations, and the braces look the same. The
        terminator separates them: a declaration ends at a `;`, an expression runs
        to the closing brace. Where that reading is wrong the member parse fails
        and the expression is tried instead, so neither shape depends on guessing
        right the first time.
        """
        self.expect_op("{")
        out: list[Any] = []
        while not self.at_op("}") and not self.at_end():
            if self.accept_op(";"):
                continue
            mark = self.pos
            if self._segment_is_expression():
                out.append(self.expression())
                self.accept_op(";")
                continue
            try:
                member = self.member()
            except ParseError:
                self.pos = mark
                out.append(self.expression())
                self.accept_op(";")
                continue
            if member is not None:
                out.append(member)
                out.extend(member.inline)
                member.inline = []
            if self.pos == mark:
                self.take()
        self.expect_op("}")
        return out

    def _segment_is_expression(self) -> bool:
        """Whether what follows runs to the closing brace rather than to a `;`."""
        token = self.peek()
        if token.kind in (NUMBER, STRING) or token.is_op("(", "-", "+", "!"):
            return True
        if token.kind != NAME:
            return False
        if token.text in MODIFIERS or token.text in VISIBILITY \
                or token.text in DIRECTIONS or token.text in RELATIONSHIP_PREFIXES:
            return False
        for words, _kind in KEYWORD_PHRASES:
            if all(self.peek(i).is_name(word) for i, word in enumerate(words)):
                return False
        depth = 0
        for ahead in range(len(self.tokens) - self.pos):
            token = self.peek(ahead)
            if token.kind == END:
                return True
            if token.is_op("{", "(", "["):
                depth += 1
            elif token.is_op(")", "]"):
                depth -= 1
            elif token.is_op("}"):
                if depth == 0:
                    return True
                depth -= 1
            elif token.is_op(";") and depth == 0:
                return False
        return True

    # expressions

    def expression(self) -> Expr:
        start = self.pos
        node = self._binary(0)
        node.text = _join(self.tokens[start:self.pos])
        return node

    # Lowest precedence first. `..` binds tighter than the comparisons so a range
    # inside a bound reads as one value.
    LEVELS: tuple[tuple[str, ...], ...] = (
        ("??",), ("implies",), ("or", "|"), ("xor",), ("and", "&"),
        ("==", "!=", "===", "!=="), ("<", ">", "<=", ">="), ("..",),
        ("+", "-"), ("*", "/", "%"), ("^",),
    )

    def _binary(self, level: int) -> Expr:
        if level >= len(self.LEVELS):
            return self._unary()
        operators = self.LEVELS[level]
        left = self._binary(level + 1)
        while True:
            token = self.peek()
            symbol = token.text if token.kind in (OP, NAME) else None
            if symbol not in operators:
                break
            self.take()
            left = Expr(symbol, [left, self._binary(level + 1)])
        return left

    def _unary(self) -> Expr:
        token = self.peek()
        if token.is_op("-", "+", "~", "!") or token.is_name("not", "all"):
            self.take()
            return Expr(f"unary {token.text}", [self._unary()])
        return self._postfix(self._primary())

    def _postfix(self, node: Expr) -> Expr:
        while True:
            if self.at_op("("):
                self.take()
                args: list[Any] = []
                while not self.at_op(")") and not self.at_end():
                    args.append(self.expression())
                    if not self.accept_op(","):
                        break
                self.expect_op(")")
                node = Expr("call", [node] + args)
                continue
            if self.at_op("["):
                self.take()
                index = Expr("empty") if self.at_op("]") else self.expression()
                self.expect_op("]")
                # `21 ['%']` is a quantity, not an index: a literal with a unit.
                node = (Expr("quantity", [node.args[0], _unit(index)])
                        if node.op == "number" else Expr("index", [node, index]))
                continue
            if self.at_op("->", "?."):
                node = Expr(self.take().text, [node, self._primary()])
                continue
            if self.at_name("as", "istype", "hastype", "meta"):
                node = Expr(self.take().text, [node, self.reference()])
                continue
            if self.at_op("?"):
                self.take()
                then = self.expression()
                self.accept_name("else")
                node = Expr("if", [node, then, self.expression()])
                continue
            break
        return node

    def _primary(self) -> Expr:
        token = self.peek()
        if token.kind == NUMBER:
            self.take()
            text = token.text
            return Expr("number", [float(text) if ("." in text or "e" in text.lower())
                                   else int(text)])
        if token.kind == STRING:
            self.take()
            return Expr("string", [token.text])
        if token.kind == COMMENT:
            self.take()
            return Expr("comment", [token.text])
        if token.is_op("("):
            self.take()
            items = []
            while not self.at_op(")") and not self.at_end():
                items.append(self.expression())
                if not self.accept_op(","):
                    break
            self.expect_op(")")
            return items[0] if len(items) == 1 else Expr("tuple", items)
        if token.is_op("{"):
            depth, parts = 0, []
            while not self.at_end():
                current = self.peek()
                if current.is_op("{"):
                    depth += 1
                elif current.is_op("}"):
                    depth -= 1
                    if depth == 0:
                        self.take()
                        break
                parts.append(self.take())
            return Expr("body", [_join(parts[1:])])
        if token.is_name("if"):
            self.take()
            condition = self.expression()
            self.accept_op("?")
            then = self.expression()
            self.accept_name("else")
            return Expr("if", [condition, then, self.expression()])
        if token.is_name("true", "false"):
            self.take()
            return Expr("boolean", [token.text == "true"])
        if token.is_name("null"):
            self.take()
            return Expr("null", [])
        if token.kind == NAME:
            return Expr("ref", [self.reference()])
        raise ParseError("expected an expression", token, self.path)


_END_FORMS = {"flow": "flows", "message": "sends", "allocation": "allocates",
              "dependency": "dependsOn", "connection": "connects", "binding": "bindsTo",
              # `succession flow a to b` transfers and sequences, so its ends
              # carry both edges. Only a succession *flow* takes from/to; a
              # plain one is written with `first`/`then`.
              "succession": "flows"}


def _join(tokens: list[Token]) -> str:
    """Token text back to something readable, with spaces only where needed."""
    out: list[str] = []
    tight_left = {")", "]", ",", ";", "::", ".", "[", "("}
    tight_right = {"(", "[", "::", ".", "~", "#", "@"}
    for token in tokens:
        text = (f"'{token.text}'" if token.kind == NAME and not _plain(token.text)
                else f'"{token.text}"' if token.kind == STRING else token.text)
        if out and text not in tight_left and out[-1] not in tight_right:
            out.append(" ")
        out.append(text)
    return "".join(out).strip()


def _plain(text: str) -> bool:
    return bool(text) and (text[0].isalpha() or text[0] == "_") \
        and all(c.isalnum() or c == "_" for c in text)


def _bound(text: str) -> Optional[int]:
    text = text.strip()
    return int(text) if text.isdigit() else None


def _unit(index: Expr) -> Optional[str]:
    """The bare symbol out of `[SI::cm]`, `['s']` or `[kg]`.

    The same unit is written all three ways in one corpus. Reducing it to the
    symbol is what lets a rollup add two masses instead of treating them as two
    different units and either refusing or adding things it should not.
    """
    refs = index.refs()
    if refs:
        return refs[-1].segments[-1]
    if index.op == "string":
        return index.args[0]
    return index.text or None


def parse_text(text: str, path: str = "") -> list[Element]:
    return Parser(lex(text), path).parse()


def parse_file(path: Path, relative_to: Optional[Path] = None) -> list[Element]:
    name = path.relative_to(relative_to).as_posix() if relative_to else path.name
    return parse_text(path.read_text(encoding="utf-8"), name)
