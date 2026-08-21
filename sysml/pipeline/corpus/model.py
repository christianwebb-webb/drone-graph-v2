"""The parse trees of one corpus, resolved into elements and typed relations.

`parse` reads a file into declarations with references written the way the author
wrote them. This turns that into a graph: every declaration gets an identity and
a qualified name, every reference is resolved to the declaration it names, and
every relationship the syntax states becomes a directed, typed edge with the file
and line it was read from.

Resolution is the part worth explaining, because it is where a graph like this is
usually wrong. A name in SysML is looked up the way a name in any scoped language
is: in the enclosing namespace first, then outward, and along the way through
whatever the namespace inherits and whatever it imports. Three of those matter
here and skipping any one of them loses real edges:

  inheritance   `launchVehicle : 'SA-506'` has no `stage1` of its own. `SA-506`
                is an individual of `SaturnV`, and the stages are declared on
                `SaturnV`. Stopping at the type finds nothing.
  imports       `import DroneBattery::**` is the only reason `DroneBatteryVariation`
                is visible from inside `package Drone`.
  feature chains `a.b.c` does not walk containment, it walks typing: `outbound` is
                typed `ExecuteOutboundJourney`, and the `prep` after it is a
                feature of that definition, declared nowhere near `outbound`.

Where a reference genuinely does not say which declaration it means, it is
dropped rather than guessed. An invented edge is worse than a missing one: a
missing edge makes an answer short, and an invented one makes it wrong while
looking exactly the same.

    from sysml.pipeline.corpus import model
    m = model.read(Path("models"))
    m.elements, m.relations
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from . import parse
from .parse import Element, Expr, Ref

# The relation vocabulary. Every one of these is stated by syntax -- there is no
# entry here that needs a language model to notice, and none of them is a guess.
#
# The pairs are (name, "what the edge means", from -> to). Direction is not a
# convention that can be picked either way: an inverted `specializes` says the
# general thing is the specific one, and a walk that crosses it arrives somewhere
# the model never puts it.
RELATIONS: dict[str, str] = {
    # structure
    "owns": "container -> the element declared inside it",
    "typedBy": "usage -> the definition after its `:`",
    "specializes": "definition -> the definition it is a kind of (`:>`)",
    "subsets": "feature -> the feature it is part of (`:>` on a usage)",
    "redefines": "feature -> the inherited feature it replaces (`:>>`)",
    "referencesFeature": "feature -> the feature it stands for (`::>`)",
    "conjugates": "port -> the port definition it is the reverse of (`~`)",
    "variantOf": "variant -> the variation it is an option of",
    "imports": "namespace -> the namespace it makes visible",
    "aliasOf": "alias -> what it renames",
    # values
    "bindsTo": "feature -> the feature or literal its `=` names",
    "assigns": "action -> the feature it writes",
    # requirements
    "satisfies": "design -> the requirement it meets",
    "verifies": "verification case -> the requirement it checks",
    "refines": "requirement -> the requirement it makes more precise",
    "derives": "requirement -> the requirement it comes from",
    "asserts": "element -> the constraint it declares true",
    "assumes": "element -> the constraint it takes as given",
    "requires": "element -> the constraint it demands",
    "dependsOn": "client -> supplier",
    "subject": "requirement or case -> what it is about",
    "actor": "case -> who takes part in it",
    "stakeholder": "requirement or concern -> whose interest it is",
    "objective": "case -> what it is trying to establish",
    "frames": "requirement -> the concern it addresses",
    "entryAction": "state -> what it runs on entry",
    "doAction": "state -> what it runs while in it",
    "exitAction": "state -> what it runs on the way out",
    # behaviour
    "performs": "part -> the action it carries out",
    "exhibits": "part -> the state machine it is in",
    "includes": "use case -> the use case it uses",
    "transitionsTo": "source -> target of a transition or succession",
    "triggeredBy": "transition -> the event that fires it",
    "guardedBy": "transition -> the constraint that has to hold",
    "effect": "transition -> the action it runs",
    "accepts": "element -> the payload it receives",
    "sends": "sender -> receiver",
    "flows": "source -> target of an item flow",
    "carries": "flow or message -> the item it moves",
    "via": "flow or transition -> the port it passes through",
    "startsAt": "body -> the first step in it",
    # structure between elements
    "connects": "connection -> each of its ends",
    "connectedTo": "one end of a connection -> the other",
    "allocates": "source -> what it is allocated to",
    # views
    "exposes": "view -> what it draws from",
    "renders": "view -> how it is drawn",
    # metadata
    "annotatedBy": "element -> the metadata written on it",
    # KerML's type operators. None of them is a specialisation, so none of them
    # belongs in a composition or inheritance walk.
    "disjointFrom": "type -> a type it shares no instances with",
    "unionOf": "type -> each type it is the union of",
    "intersectionOf": "type -> each type it is the intersection of",
    "differenceOf": "type -> the types it is the difference of, first minus rest",
    "chainOf": "feature -> each feature in the chain it stands for",
    "inverseOf": "feature -> the feature it is the reverse of",
}

# Kinds whose keyword *is* the relationship to their owner. `subject s : System;`
# says the requirement is about a System; recording only `owns` loses the keyword,
# which is the entire content of the statement.
ROLE_KINDS = {"subject", "actor", "stakeholder", "objective"}

# Kinds that a `then` can point at, so `previous` only moves for something a
# succession could actually name.
STEP_KINDS = {"action", "state", "snapshot", "timeslice", "occurrence", "event",
              "part", "item", "entry", "exit", "do", "decide", "merge", "fork",
              "join", "terminate", "accept", "send", "assign", "calculation",
              "feature", "return", "usecase", "analysis", "verification", "case"}

# Kinds that declare nothing worth a row of its own -- they exist to carry a
# relationship, and the relationship is what is kept.
STATEMENT_KINDS = {"import", "alias", "succession", "annotation", "filter"}


@dataclass
class Fact:
    """One resolved, directed relation with the line it was read from."""
    type: str
    source: str
    target: str
    file: str = ""
    line: int = 0
    detail: dict = field(default_factory=dict)

    def key(self) -> tuple:
        return (self.type, self.source, self.target)


@dataclass
class Model:
    elements: list[Element]
    relations: list[Fact]
    unresolved: list[dict]
    by_id: dict[str, Element] = field(default_factory=dict)

    def of_type(self, *types: str) -> list[Fact]:
        return [f for f in self.relations if f.type in types]


def model_of(relative_path: str) -> str:
    """Which model a file belongs to: the top directory, or the file itself.

    The boundary matters because it decides what may resolve against what. Two
    vehicles that both declare a `Battery` declare two different batteries, and a
    reference inside one of them means its own.
    """
    head = relative_path.split("/")[0]
    return head[:-len(".sysml")] if head.endswith(".sysml") else head


def read(root: Path) -> Model:
    """Every .sysml file under `root`, parsed, resolved and turned into facts."""
    trees: dict[str, list[Element]] = {}
    for path in sorted(root.rglob("*.sysml")):
        relative = path.relative_to(root).as_posix()
        trees[relative] = parse.parse_file(path, root)
    return build(trees)


def build(trees: dict[str, list[Element]]) -> Model:
    by_model: dict[str, list[Element]] = {}
    for relative, roots in trees.items():
        by_model.setdefault(model_of(relative), []).extend(roots)

    elements: list[Element] = []
    relations: list[Fact] = []
    unresolved: list[dict] = []
    by_id: dict[str, Element] = {}

    for name, roots in sorted(by_model.items()):
        scope = Scope(name, roots)
        elements.extend(scope.elements)
        by_id.update(scope.by_id)
        facts, missing = scope.facts()
        relations.extend(facts)
        unresolved.extend(missing)

    _name_uniquely(elements)
    seen: set[tuple] = set()
    deduped = []
    for fact in relations:
        if fact.key() in seen or fact.source == fact.target:
            continue
        seen.add(fact.key())
        deduped.append(fact)
    return Model(elements=elements, relations=deduped, unresolved=unresolved,
                 by_id=by_id)


class Scope:
    """One model's declarations, indexed the way SysML looks a name up."""

    def __init__(self, model: str, roots: list[Element]) -> None:
        self.model = model
        self.roots = roots
        self.elements: list[Element] = []
        self.by_id: dict[str, Element] = {}
        # Two tables per namespace, because SysML is case sensitive and these
        # models rely on it: `PerformLunarMission` is the action definition and
        # `performLunarMission` is the subject usage typed by it. Folding the two
        # together made every such usage resolve to itself, which silently cut
        # every feature chain that started at one -- 272 of the 273 `satisfy`
        # statements in this corpus begin exactly that way.
        #
        # The folded table is still kept, and consulted only when the exact name
        # misses and exactly one candidate answers to it. That is what lets
        # `satisfy 'flr-R001'` find `<'FLR-R001'>` without letting a definition
        # and its usage collide.
        self.members: dict[int, dict[str, Element]] = {}
        self.folded: dict[int, dict[str, list[Element]]] = {}
        self.by_qualified: dict[str, Element] = {}
        # Every element by its last name segment. A package in these models is
        # addressed by its own name from anywhere in the model -- `import
        # FunctionalRequirementsPackage::*` is written in eight files and the
        # package is declared inside none of them -- so a unique tail is a real
        # answer and the last one worth trying before giving up.
        self.by_tail: dict[str, list[Element]] = {}
        self.top: dict[str, Element] = {}
        self.top_folded: dict[str, list[Element]] = {}
        self._busy: set[tuple[int, str]] = set()
        self._label(roots, owner=None, path="")

    # naming

    def _label(self, members: list[Element], owner: Optional[Element], path: str) -> None:
        used: dict[str, int] = {}
        table: dict[str, Element] = {}
        folded: dict[str, list[Element]] = {}
        for element in members:
            element.owner = owner
            name = element.name or self._anonymous_name(element, used)
            element.qualified = f"{path}::{name}" if path else name
            element.model = self.model
            element.id = f"{self.model}::{element.qualified}"
            # Two declarations of the same qualified name are the same element
            # written twice, which SysML allows; let them share the row.
            if element.id in self.by_id:
                element.id = f"{element.id}#{element.line}"
            self.elements.append(element)
            self.by_id[element.id] = element
            self.by_qualified.setdefault(element.qualified.casefold(), element)
            for written in (element.name, element.short_name):
                if written:
                    table.setdefault(written, element)
                    self.by_tail.setdefault(written, []).append(element)
            # Only short names are matched with the case ignored. They are the one
            # thing this corpus is inconsistent about -- `requirement def
            # <'FLR-R001'>` is referred to as `satisfy 'flr-R001' by ...` -- and
            # ignoring case on ordinary names instead collapses `PerformLunarMission`
            # into the `performLunarMission` typed by it, which is a different
            # element and ends every feature chain that starts at one.
            if element.short_name:
                folded.setdefault(element.short_name.casefold(), []).append(element)
            self._label(element.children, element, element.qualified)
        self.members[id(owner) if owner else 0] = table
        self.folded[id(owner) if owner else 0] = folded
        if owner is None:
            self.top = table
            self.top_folded = folded

    def _named(self, owner: Optional[Element], name: str,
               exclude: Optional[Element] = None) -> Optional[Element]:
        """A member of a namespace by the name it was written under."""
        where = id(owner) if owner is not None else 0
        found = self.members.get(where, {}).get(name)
        if found is not None and found is not exclude:
            return found
        candidates = [c for c in self.folded.get(where, {}).get(name.casefold(), ())
                      if c is not exclude]
        return candidates[0] if len(candidates) == 1 else None

    def _unique_tail(self, name: str) -> Optional[Element]:
        candidates = self.by_tail.get(name, ())
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _anonymous_name(element: Element, used: dict[str, int]) -> str:
        """A name for a declaration that has none.

        `:>> weight = 275 [SI::g]` names no element and states a fact about one,
        so it is named after the feature it redefines -- which is both what it is
        called everywhere else and what a reader would look for. Where there is
        nothing to borrow, the kind and the line make an identity that is at least
        stable across runs.
        """
        for source in (element.redefines, element.references, element.supers):
            if source:
                base = source[0].segments[-1] if source[0].segments else ""
                if base:
                    count = used.get(base, 0)
                    used[base] = count + 1
                    return base if not count else f"{base}#{count}"
        base = f"{element.kind}@{element.line}"
        count = used.get(base, 0)
        used[base] = count + 1
        return base if not count else f"{base}#{count}"

    # resolution

    def resolve(self, reference: Ref, source: Element,
                exclude: Optional[Element] = None) -> Optional[Element]:
        """The declaration a written reference names, or None if it is ambiguous.

        `exclude` is for a reference that cannot mean the element that wrote it.
        `part :>> spacecraft { ... }` redefines an inherited `spacecraft`, and the
        redefinition is itself called `spacecraft` -- so an ordinary lookup from
        inside the snapshot finds the redefinition, points it at itself, and the
        edge disappears along with every chain that went through it.
        """
        if not reference.parts:
            return None
        segments = [(separator, name) for separator, name in reference.parts if name]
        if not segments or segments[0][1] in ("*", "**"):
            return None
        current = self._lookup(segments[0][1], source, exclude)
        for separator, name in segments[1:]:
            if current is None or name in ("*", "**"):
                break
            current = (self._member(current, name) if separator == "::"
                       else self._feature(current, name, exclude=exclude))
        return current

    def _lookup(self, name: str, source: Element,
                exclude: Optional[Element] = None) -> Optional[Element]:
        """The first segment: the enclosing scopes, outward, then the file roots."""
        element: Optional[Element] = source
        while element is not None:
            found = (self._feature(element, name, exclude=exclude)
                     or self._named(element, name, exclude))
            if found is not None and found is not source and found is not exclude:
                return found
            found = self._through_imports(element, name)
            if found is not None:
                return found
            element = element.owner
        return (self._named(None, name) or self.by_qualified.get(name.casefold())
                or self._unique_tail(name))

    def _member(self, namespace: Element, name: str) -> Optional[Element]:
        """`A::b` -- a member of a namespace, or of something it imports."""
        found = self._named(namespace, name)
        return found if found is not None else self._through_imports(namespace, name)

    def _feature(self, element: Element, name: str, depth: int = 6,
                 exclude: Optional[Element] = None) -> Optional[Element]:
        """`a.b` -- a feature of `a`, of what types it, or of what that specializes.

        Inheritance is not optional. Most references in an Apollo mission snapshot
        name a feature the snapshot never declares, because it is declared on the
        definition the snapshot is a slice of.
        """
        seen: set[int] = set()
        frontier = [element]
        while frontier and depth:
            following: list[Element] = []
            for current in frontier:
                if id(current) in seen:
                    continue
                seen.add(id(current))
                found = self._named(current, name, exclude)
                if found is not None:
                    return found
                for reference in (current.typed_by + current.supers
                                  + current.redefines + current.references):
                    target = self.resolve_shallow(reference, current)
                    if target is None:
                        # The cheap lookup only sees direct members, and a
                        # snapshot's `:> apollo11MissionSystem` names a feature of
                        # the definition the whole timeline is an individual of.
                        # Full resolution can re-enter here, so a reference being
                        # resolved is not resolved again.
                        marker = (id(current), reference.text)
                        if marker not in self._busy:
                            self._busy.add(marker)
                            try:
                                target = self.resolve(reference, current, exclude=current)
                            finally:
                                self._busy.discard(marker)
                    if target is not None and target is not current:
                        following.append(target)
            frontier, depth = following, depth - 1
        return None

    def resolve_shallow(self, reference: Ref, source: Element) -> Optional[Element]:
        """A reference resolved without following it back through inheritance.

        `_feature` calls this while it is walking the inheritance chain, and
        letting the two call each other freely is how a definition that
        specializes itself through a chain of four takes a stack with it.
        """
        segments = [(separator, name) for separator, name in reference.parts if name]
        if not segments:
            return None
        first = segments[0][1]
        current: Optional[Element] = None
        element: Optional[Element] = source
        while element is not None and current is None:
            if element is not source:
                current = self._named(element, first)
            current = current or self._through_imports(element, first)
            element = element.owner
        current = current or self._named(None, first) \
            or self.by_qualified.get(first.casefold())
        for _separator, name in segments[1:]:
            if current is None:
                return None
            current = self._named(current, name)
        return current

    def _through_imports(self, namespace: Optional[Element], name: str) -> Optional[Element]:
        """A name made visible by an `import` written in this namespace."""
        for child in (namespace.children if namespace is not None else self.roots):
            if child.kind != "import" or not child.references:
                continue
            target = child.references[0]
            path = [n for _, n in target.parts if n and n not in ("*", "**")]
            imported = self._by_path(path, namespace)
            if imported is None:
                continue
            found = self._named(imported, name)
            if found is not None:
                return found
            if target.wildcard == "**":
                found = self._deep(imported, name)
                if found is not None:
                    return found
        return None

    def _by_path(self, path: list[str], origin: Optional[Element] = None) -> Optional[Element]:
        """A namespace named by a path, seen from where the path was written.

        `import DroneBattery::**` sits inside `package Drone`, and `DroneBattery`
        is its sibling -- not a root package. Resolving the path from the root
        found nothing, so every name the import was supposed to bring into scope
        stayed unresolved. Imports are not followed while resolving an import,
        which is what keeps two packages importing each other from recursing.
        """
        if not path:
            return None
        found = self.by_qualified.get("::".join(path).casefold())
        if found is not None:
            return found
        current: Optional[Element] = None
        element = origin
        while element is not None and current is None:
            current = self._named(element, path[0])
            element = element.owner
        if current is None:
            current = self._named(None, path[0]) or self._unique_tail(path[0])
        for name in path[1:]:
            if current is None:
                return None
            current = self._named(current, name)
        return current

    def _deep(self, namespace: Element, name: str, depth: int = 4) -> Optional[Element]:
        """`import P::**` reaches inside P's own packages as well as P itself."""
        if depth <= 0:
            return None
        for child in namespace.children:
            if name in (child.name, child.short_name):
                return child
            if child.kind == "package":
                found = self._deep(child, name, depth - 1)
                if found is not None:
                    return found
        for child in namespace.children:
            if child.kind == "package":
                continue
            if name.casefold() in ((child.name or "").casefold(),
                                   (child.short_name or "").casefold()):
                return child
        return None

    # facts

    def miss(self, kind: str, source: Element, reference: str, line: int) -> dict:
        """One reference that resolved to nothing, and whether that is a failure.

        Most of them are not. `attribute massActual : ISQ::MassValue` names the
        SysML standard library, which is not in this repository and never will be:
        the reference is correct and its far end is simply outside the corpus.
        Counting those as errors hides the ones that are, so a miss whose first
        segment nothing in the corpus declares is marked `external` instead.
        """
        head = reference.replace(".", "::").split("::")[0]
        return {"type": kind, "from": source.id, "reference": reference,
                "file": source.file, "line": line,
                "external": head not in self.by_tail}

    def facts(self) -> tuple[list[Fact], list[dict]]:
        out: list[Fact] = []
        missing: list[dict] = []

        def point(kind: str, source: Element, reference: Ref, detail: dict | None = None,
                  origin: Element | None = None, exclude: Element | None = None) -> None:
            target = self.resolve(reference, origin or source, exclude)
            if target is None:
                missing.append(self.miss(kind, source, reference.text,
                                         reference.line or source.line))
                return
            out.append(Fact(type=kind, source=source.id, target=target.id,
                            file=source.file, line=reference.line or source.line,
                            detail=detail or {}))

        for element in self.elements:
            owner = element.owner
            if owner is not None and element.kind not in STATEMENT_KINDS:
                out.append(Fact("owns", owner.id, element.id, element.file, element.line))

            for reference in element.typed_by:
                point("conjugates" if element.conjugated else "typedBy", element, reference)
            for reference in element.supers:
                point("specializes" if element.is_definition else "subsets",
                      element, reference, exclude=element)
            for reference in element.redefines:
                point("redefines", element, reference, exclude=element)
            if element.kind not in ("import", "alias", "expose"):
                for reference in element.references:
                    point("referencesFeature", element, reference)

            if "variant" in element.modifiers and owner is not None:
                out.append(Fact("variantOf", element.id, owner.id, element.file, element.line))

            for label, references in element.type_ops.items():
                for reference in references:
                    point(label, element, reference, exclude=element)

            # `subject s : System;` inside a requirement or a case is the one thing
            # the requirement is about, and `actor`, `stakeholder` and `objective`
            # are the same shape. The keyword is the relationship: without an edge
            # for it the only thing recorded is that the requirement owns a feature,
            # which is true of all forty of its features and says nothing.
            if element.kind in ROLE_KINDS and owner is not None:
                out.append(Fact(element.kind, owner.id, element.id,
                                element.file, element.line))

            # A value that names something is an edge; a value that is a number
            # is an attribute, and `attributes` below is where it lands.
            if element.value and element.value["expr"].op == "ref":
                point("bindsTo", element, element.value["expr"].args[0])

            for annotation in element.annotations:
                metadata = self.resolve(annotation.name, element)
                if metadata is not None:
                    out.append(Fact("annotatedBy", element.id, metadata.id,
                                    element.file, annotation.line or element.line))

            self._relationship_facts(element, out, point, missing)

        out.extend(self._succession_facts(missing))
        return out, missing

    def _relationship_facts(self, element: Element, out: list[Fact], point,
                            missing: list[dict]) -> None:
        relationship = element.relationship
        if not relationship:
            return
        form = relationship.get("form")
        owner = element.owner
        file, line = element.file, element.line

        if form in ("satisfies", "verifies", "refines", "derives"):
            # `satisfy R by D` -- the requirement is what the statement names and
            # the design is what follows `by`, so the edge runs from the design.
            #
            # `D` is resolved from the body the statement is written in, not from
            # the statement itself. Resolving it from the statement lets the
            # requirement's own features into scope, and a requirement almost
            # always declares a `subject` named after the thing that satisfies it
            # -- so `satisfy R by drone` found the subject inside R rather than
            # the part beside it, and reported a requirement as satisfied by a
            # feature of itself.
            scope = element.owner or element
            target = self._own_target(element)
            for reference in relationship.get("by", []):
                source = self.resolve(reference, scope)
                if source is None or target is None:
                    missing.append(self.miss(
                        form, element,
                        reference.text if source is None
                        else (element.references[0].text if element.references
                              else element.name or ""),
                        reference.line or line))
                    continue
                out.append(Fact(form, source.id, target.id, file, reference.line or line))
            # `derive R from R2` and `refine R from R2` name both ends themselves,
            # and the statement's own subject is the source. Falling back to the
            # owner here would say the enclosing part was derived from something.
            for reference in relationship.get("source", []):
                origin = self.resolve(reference, scope)
                if origin is None or target is None:
                    missing.append(self.miss(form, element, reference.text,
                                             reference.line or line))
                    continue
                out.append(Fact(form, target.id, origin.id, file,
                                reference.line or line))
            if not relationship.get("by") and not relationship.get("source") \
                    and owner is not None and target is not None:
                out.append(Fact(form, owner.id, target.id, file, line))
            return

        if form in ("performs", "exhibits", "includes", "asserts", "assumes",
                    "requires", "frames", "renders", "doAction", "entryAction",
                    "exitAction"):
            source = owner or element
            target = self._own_target(element) or element
            if target is not element or element.kind not in STATEMENT_KINDS:
                out.append(Fact(form, source.id, target.id, file, line))
            return

        if form == "bindsTo":
            scope = element.owner or element
            ends = [self.resolve(reference, scope)
                    for reference in relationship.get("ends", [])]
            for first, second in zip(ends, ends[1:]):
                if first is not None and second is not None:
                    out.append(Fact("bindsTo", first.id, second.id, file, line))
            return

        if form == "connects":
            scope = element.owner or element
            ends = [self.resolve(reference, scope) for reference in relationship.get("ends", [])]
            named = [end for end in ends if end is not None]
            for end, reference in zip(ends, relationship.get("ends", [])):
                if end is None:
                    missing.append(self.miss("connects", element, reference.text,
                                             reference.line or line))
                    continue
                out.append(Fact("connects", element.id, end.id, file, reference.line or line))
            for first, second in zip(named, named[1:]):
                out.append(Fact("connectedTo", first.id, second.id, file, line,
                                {"through": element.id}))
            return

        if form in ("flows", "sends", "allocates", "dependsOn", "assigns"):
            scope = element.owner or element
            sources = [self.resolve(r, scope) for r in relationship.get("from", [])]
            targets = [self.resolve(r, scope) for r in relationship.get("to", [])]
            if not sources:
                sources = [element.owner] if form in ("sends", "assigns") else []
            if not sources:
                sources = [element]
            for source in sources:
                for target in targets:
                    if source is None or target is None:
                        continue
                    out.append(Fact(form, source.id, target.id, file, line))
                    if relationship.get("sequences"):
                        out.append(Fact("transitionsTo", source.id, target.id,
                                        file, line))
            payload = relationship.get("payload")
            if isinstance(payload, Ref):
                point("carries", element, payload)
            via = relationship.get("via")
            if isinstance(via, Ref):
                point("via", element, via)
            return

        if form == "imports" and element.owner is not None:
            for reference in relationship.get("to", []):
                path = [n for _, n in reference.parts if n and n not in ("*", "**")]
                target = self._by_path(path)
                if target is not None:
                    out.append(Fact("imports", element.owner.id, target.id, file, line,
                                    {"recursive": bool(relationship.get("recursive"))}))
            return

        if form == "aliasOf":
            for reference in relationship.get("to", []):
                point("aliasOf", element, reference)
            return

        if form == "exposes" and element.owner is not None:
            # `expose` is both a relationship keyword and a statement, and which
            # path read it decides whether the references landed in `to` or in
            # `references`. Either way they are the same references.
            for reference in relationship.get("to") or element.references:
                path = [n for _, n in reference.parts if n and n not in ("*", "**")]
                target = self._by_path(path) or self.resolve(reference, element)
                if target is not None:
                    out.append(Fact("exposes", element.owner.id, target.id, file, line))
            return

        if form in ("transitionsTo", "accepts"):
            # A branch names its own arms, and both are reachable, so both get an
            # edge. `branch` says which is which, because "what happens if the
            # guard fails" is a different question from "what happens next".
            if element.kind == "if":
                for slot, branch in (("to", "then"), ("otherwise", "else")):
                    for reference in relationship.get(slot, []):
                        point("transitionsTo", element, reference, {"branch": branch})
            trigger = relationship.get("trigger")
            if isinstance(trigger, Ref):
                point("accepts" if element.kind == "accept" else "triggeredBy",
                      element, trigger)
            effect = relationship.get("effect")
            if isinstance(effect, Ref):
                point("effect", element, effect)
            guard = relationship.get("guard")
            if isinstance(guard, Expr):
                for reference in guard.refs():
                    point("guardedBy", element, reference)
            via = relationship.get("via")
            if isinstance(via, Ref):
                point("via", element, via)
            # The two ends are joined in `_succession_facts`, which is the only
            # place that can see the order a body declares its members in.
            return

    def _own_target(self, element: Element) -> Optional[Element]:
        """What a relationship-prefixed declaration is about.

        `satisfy Pkg::longDistance by drone` names the requirement as a reference;
        `perform action checkStatus { ... }` declares the action itself; `exhibit
        state phases : StateAction` declares a usage of a definition. Each is the
        thing the edge should point at, in that order of preference.
        """
        for reference in element.references:
            found = self.resolve(reference, element)
            if found is not None:
                return found
        if element.name:
            found = self.resolve(Ref(element.name, [("", element.name)], element.line),
                                 element.owner or element)
            if found is not None and found is not element:
                return found
        return element

    def _succession_facts(self, missing: list[dict]) -> list[Fact]:
        """`first a; then b; then c;` and every state transition, in order.

        A `then` states its target and leaves its source to the body it is written
        in: it starts wherever the previous member left off. Nothing in a single
        statement can see that, so the whole body is walked here, in declaration
        order, carrying the step the last statement arrived at.
        """
        out: list[Fact] = []

        def walk(members: list[Element]) -> None:
            previous: Optional[Element] = None
            for element in members:
                relationship = element.relationship or {}
                form = relationship.get("form")
                if form == "startsAt":
                    for reference in relationship.get("to", []):
                        target = self.resolve(reference, element)
                        if target is not None:
                            owner = element.owner
                            if owner is not None:
                                out.append(Fact("startsAt", owner.id, target.id,
                                                element.file, element.line))
                            previous = target
                elif form in ("transitionsTo", "accepts"):
                    sources = [self.resolve(r, element)
                               for r in relationship.get("from", [])] or [previous]
                    targets = [self.resolve(r, element) for r in relationship.get("to", [])]
                    detail = {}
                    trigger = relationship.get("trigger")
                    if isinstance(trigger, Ref):
                        detail["trigger"] = trigger.text
                    guard = relationship.get("guard")
                    if isinstance(guard, Expr):
                        detail["guard"] = guard.text
                    for source in sources:
                        for target in targets:
                            if source is None or target is None:
                                missing.append(self.miss(
                                    "transitionsTo", element,
                                    relationship.get("to", [Ref("")])[0].text
                                    if targets and target is None else "<source>",
                                    element.line))
                                continue
                            out.append(Fact("transitionsTo", source.id, target.id,
                                            element.file, element.line, detail))
                    if targets and targets[-1] is not None and form == "transitionsTo":
                        previous = targets[-1]
                elif element.kind in STEP_KINDS:
                    previous = element
                walk(element.children)

        walk(self.roots)
        return out


def attributes_of(element: Element) -> dict[str, Any]:
    """The values declared *inside* an element, as a map keyed by feature name.

    A value is a declaration of its own -- it has a name and a line, and it is a
    row like any other -- and it is repeated here so that reading `dryMass` off
    the part that declares it is a field lookup rather than a traversal.

    The element's own value is deliberately NOT in its own map; it is in `value`.
    Putting it in both is how a rollup over a subtree counts one number twice:
    the walk reaches the part, reads `dryMass` off its map, then reaches the
    `dryMass` declaration underneath it and reads the same number again. Which
    happens only for the attributes a file writes on their own line, so the total
    is wrong by a different amount depending on how the source was formatted --
    the worst kind of wrong, because it looks plausible.
    """
    values: dict[str, Any] = {}
    for child in element.children:
        if not child.value:
            continue
        name = child.name or (child.redefines[0].segments[-1] if child.redefines else None)
        if name:
            values[name] = value_of(child.value["expr"])
    return values


def value_of(expr: Expr) -> dict[str, Any]:
    """A parsed value, in the shape a query can use.

    A number with a unit is a number with a unit; a name is a reference; anything
    else is kept as the expression it is, and never quietly turned into a number.
    """
    if expr.op == "quantity":
        return {"value": expr.args[0], "unit": expr.args[1], "text": expr.text}
    if expr.op == "number":
        return {"value": expr.args[0], "unit": None, "text": expr.text}
    if expr.op == "string":
        return {"value": expr.args[0], "text": expr.text}
    if expr.op == "boolean":
        return {"value": expr.args[0], "text": expr.text}
    if expr.op == "ref":
        return {"reference": expr.args[0].text, "text": expr.text}
    return {"expression": expr.text, "operator": expr.op}


def _name_uniquely(elements: list[Element]) -> None:
    """Give every element a `display` that is unique within its model.

    Identity is the qualified name; `display` is what a reader and a search index
    see. A bare name only one declaration claims is left alone, because that is
    the name that reads well and that a question uses. A contested one takes as
    many owner segments as it needs to separate it -- `spacecraft` is declared 21
    times in the Apollo model, once per mission snapshot, and one row for all of
    them merges 21 subtrees into a rollup that answers about the wrong moment.
    """
    grouped: dict[tuple[str, str], list[Element]] = {}
    for element in elements:
        key = (element.model, (element.name or element.qualified.split("::")[-1]).upper())
        grouped.setdefault(key, []).append(element)

    for (_model, bare), group in grouped.items():
        if len(group) == 1:
            group[0].display = bare
            continue
        remaining, depth = list(group), 1
        while remaining and depth < 8:
            by_suffix: dict[str, list[Element]] = {}
            for element in remaining:
                segments = element.qualified.upper().split("::")
                suffix = "_".join(segments[max(0, len(segments) - depth - 1):])
                by_suffix.setdefault(suffix, []).append(element)
            still: list[Element] = []
            for suffix, members in by_suffix.items():
                if len(members) == 1:
                    members[0].display = suffix
                else:
                    still.extend(members)
            remaining, depth = still, depth + 1
        for element in remaining:
            element.display = element.qualified.upper().replace("::", "_")

    # Whatever still collides differs only in case or not at all, and both happen:
    # `Vehicle` the definition beside `vehicle` the usage typed by it is the
    # commonest pair in SysML there is, and a file may declare the same qualified
    # name twice. Sharing one `display` is not cosmetic -- a lookup by name returns
    # whichever row comes first, so a rollup that lands on the definition reports
    # the four attributes it declares instead of the several hundred values under
    # the usage, and reports them without any sign that it picked.
    settled: dict[tuple[str, str], list[Element]] = {}
    for element in elements:
        settled.setdefault((element.model, element.display), []).append(element)
    for (_model, display), group in settled.items():
        if len(group) == 1:
            continue
        definitions = [e for e in group if e.is_definition]
        if len(definitions) == 1 and len(group) == 2:
            definitions[0].display = f"{display}_DEF"
            continue
        for element in group:
            element.display = f"{display}_L{element.line}"
    # A line number separates almost everything; an index settles the rest.
    final: dict[tuple[str, str], list[Element]] = {}
    for element in elements:
        final.setdefault((element.model, element.display), []).append(element)
    for (_model, display), group in final.items():
        for index, element in enumerate(group[1:], start=2):
            element.display = f"{display}_{index}"


def key_of(text: str) -> str:
    """A stable ArangoDB `_key` for an identity that may hold anything."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", text)[:200].strip("_")
    return f"{slug}_{hashlib.sha1(text.encode('utf-8')).hexdigest()[:10]}"
