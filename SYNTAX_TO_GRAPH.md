# SysML syntax -> what it becomes in the graph

`sysml/pipeline/language/parse.py` reads a `.sysml` file into declarations, `model.py`
beside it resolves the references between them, and `sysml/pipeline/corpus/write.py`
writes both into Layer 2. Nothing in that path infers anything. This file is the
mapping, written from those three modules.

The other direction -- how to query what comes out -- is
`sysml/pipeline/examples/aql_examples.md`.

## Three destinations

- **an element**, a row in `{project}_elements` with a `kind`
- **a field** on an element: a value in `attributes`, a `multiplicity`, a modifier
  flag, a `doc`, a `rationale`
- **a relation**, a row in `{project}_declarations` with a `label` and a direction

Every one of them carries the file, the line and the column it was read from.

## Declarations

The keyword decides the kind, and `def` decides whether it is a definition or a
usage. `part def Pump` and `part coolantPump : Pump` are both `kind: "part"`; the
first has `is_definition: true`. They are two rows, joined by `typedBy`.

Every SysML v2 declaration keyword is recognised, in both its one-word and its
phrase form where it has one (`analysis` and `analysis case`, `verification` and
`verification case`, `use case`, `event occurrence`). A declaration with no keyword
at all -- `end capa ::> goal`, `text = "..."`, `:>> weight = 275 [SI::g]` -- is a
`feature`, which is what SysML calls it, and it is kept because that is where a
great many values are written.

## Modifiers are kept, not stripped

Everything in front of the keyword lands in `modifiers`, and the six that get asked
about are hoisted to their own boolean: `abstract`, `variation`, `variant`,
`individual`, `reference` (from `ref`), `end`. `in`/`out`/`inout` become
`direction`, `public`/`private`/`protected` become `visibility`, and `~` on a port
becomes `conjugated`.

None of this is decoration. `abstract` is the difference between a part that can
exist and one that only classifies. `individual` marks the one that actually
existed. `variation` and `variant` are a choice point and its options, and the
`variantOf` edge between them is the only record of which options there were.

## Values

`attribute dryMass = 137000 [kg]` writes, on the element that declares it:

```json
"attributes": { "dryMass": { "value": 137000, "unit": "kg", "text": "137000[kg]" } }
```

and the `dryMass` declaration is also a row of its own, with the same value in its
`value` field. The two are not duplicates: the map is there so a rollup is a field
read, the row is there so the declaration has a line. **An element's own value is
not repeated into its own map** -- putting it in both is how a sum over a subtree
counts one number twice.

Four value shapes, and only the first two are numbers:

| written | stored |
| --- | --- |
| `= 137000 [kg]`, `= 4.5 [SI::m/s]`, `= 3 ['s']` | `{value, unit}`, unit reduced to the bare symbol |
| `= "text"`, `= 5`, `= true` | `{value}` |
| `= mass + sum(parts.mass)` | `{expression, operator}` -- never evaluated |
| `= Kind::option` | `{reference}` -- and a `bindsTo` edge |

A multiplicity is `{text, lower, upper}`. `[*]` is `lower: 0, upper: null`, and
`[numberOfEnginesVariation]` -- a multiplicity that names a feature -- keeps its
text with both bounds null, because that is what the model says.

## Documentation

`doc /* ... */` becomes `doc`, with the `*` margin removed. `@Rationale { text =
"..." }` becomes `rationale`, and every `#Name` and `@Name {...}` is kept in
`annotations` with its bindings. An `@Name { ... }` written as a member annotates
the element whose body it is written in -- not whatever declaration follows it.

`<'DE-REQ-1'>` is a short name, not an element: it lands in `short_name` on the
element it was declared on. One element, one row, whichever of its two names is
used.

## Relations

Every relationship SysML can state becomes an edge with a direction. The full list
with directions is in `sysml/pipeline/examples/aql_examples.md`; the summary is:

| written | `label` |
| --- | --- |
| declared inside another element | `owns` |
| `part p : Pump` | `typedBy` |
| `part def Boost :> Pump` | `specializes` (on a definition) / `subsets` (on a usage) |
| `:>> feature`, `redefines feature` | `redefines` |
| `::> feature`, `references feature` | `referencesFeature` |
| `port p : ~PortDef` | `conjugates` |
| `variant part x` inside a `variation` | `variantOf` |
| `import A::B::*` | `imports` |
| `alias N for X` | `aliasOf` |
| `= someFeature` | `bindsTo` |
| `satisfy R by D` | `satisfies`, D -> R |
| `verify`, `refine`, `derive` | `verifies`, `refines`, `derives` |
| `#refinement dependency A to B` | `dependsOn`, A -> B |
| `assert` / `assume` / `require` constraint | `asserts` / `assumes` / `requires` |
| `subject`, `actor`, `stakeholder`, `objective` | the keyword itself |
| `frame concern c` | `frames` |
| `perform action a` | `performs` |
| `exhibit state s` | `exhibits` |
| `include use case u` | `includes` |
| `entry` / `do` / `exit` action | `entryAction` / `doAction` / `exitAction` |
| `transition first A accept E if G do X then B` | `transitionsTo` A -> B, with `trigger` and `guard` on the edge, plus `triggeredBy`, `guardedBy`, `effect` |
| `first a; then b; then c;` | `startsAt` and a chain of `transitionsTo` |
| `accept E then S` | `transitionsTo` with `trigger` |
| `connect a to b`, `connect (a, b, c)` | `connects` from the connection to each end, and `connectedTo` between the ends |
| `flow of I from A to B` | `flows` A -> B, plus `carries` to I |
| `message m of I from A to B` | `sends` A -> B, plus `carries` |
| `allocate A to B` | `allocates` |
| `send X via P to Y` | `sends`, plus `via` |
| `assign x := e` | `assigns` |
| `expose A::*` | `exposes` |
| `#Name`, `@Name` | `annotatedBy` |

Two more join the layers: `DECLARED_IN` from an element to the source file it was
written in, and `READ_AS` from an element to the Layer 3 entity that is a reading
of it.

## Resolution, and what is dropped

A reference is resolved the way SysML scopes a name: the enclosing namespace first,
then outward, through whatever each namespace inherits (`typedBy`, `:>`, `:>>`) and
whatever it imports. `A::b` looks inside the namespace `A`; `a.b` follows a feature
of whatever types `a`, which is a different walk and reaches a different element.

Names are compared **case sensitively**, because SysML is: `PerformLunarMission` is
a definition and `performLunarMission` is a usage typed by it. Only a short name is
matched with case ignored, because that is the one thing source files are
inconsistent about.

A reference that resolves to nothing produces no edge. Most of those are correct
references to the SysML standard library -- `ISQ::MassValue`, `ScalarValues::Real`
-- which is not part of any corpus here; those are recorded as `external` in
`out/corpus.json` and are not failures. A reference whose first segment the corpus
does declare, and which still does not resolve, is recorded as one.

The text of every reference is kept on the element as written (`typed_by`,
`specializes`, `redefines`, `references`) whether or not it resolved, so a
library-typed attribute still says what it is typed by.

## What produces nothing

- an anonymous declaration with no name, no redefinition and no reference to
  borrow a name from still becomes a row, named `kind@line`
- a `/* */` comment that follows no `doc`, `comment` or `rep` keyword
- a reference whose target is genuinely ambiguous after scoping -- guessing would
  invent a relation the file never states
