# Querying a graph built from SysML v2 models

This file is passed verbatim as the `aql_examples` argument of
`ReadOnlyArangoGraphQAChain.from_llm`. Its only reader is the model that turns an
engineer's English question into AQL and runs it. That reader already sees the
collection schema; what it cannot see, and what this file is the only source of, is
what the fields mean, which layer is entitled to answer which question, and the
shapes a query has to take to come back with the right rows instead of no rows.

Nothing here describes any particular model. Every statement is a property of the
pipeline that built the graph, so it holds for any SysML v2 corpus put through it.
A second file, written from a survey of the graph actually loaded, adds what is
specific to it.

## The one rule that decides everything else

The graph has two halves and they are not interchangeable.

**Layer 2** — `sysml_elements` and `sysml_declarations` — is a parse of the source
text. Every row is a declaration a `.sysml` file makes, with the file and line it
was written on. Every edge is a relationship the syntax states. Nothing in it was
inferred, summarised or guessed; it is exactly what the language says, and it is
the same on every run.

**Layer 3** — `sysml_Entities`, `sysml_Relations`, `sysml_Chunks`,
`sysml_Communities` — is a language model's reading of the same text. It holds
prose descriptions, embeddings, community summaries, and relations that were read
out of sentences rather than out of syntax.

So:

* **Any question with a definite answer goes to Layer 2.** Values, units,
  containment, typing, specialisation, requirement satisfaction, connections,
  state transitions, counts, "how many", "which ones", "what is inside", "what is
  it made of", "sum", "largest", "does anything satisfy".
* **Only questions about meaning go to Layer 3.** "What is this for", "summarise",
  "what is this model about", "find anything to do with X".

`sysml_Entities` carries **no attribute values and no structural relations at
all**. A numeric or structural question sent there returns nothing, or worse,
returns a sentence from a `description` that happens to contain a number. If a
question can be answered from `sysml_elements`, answer it from `sysml_elements`.

---

## `sysml_elements` — one row per declaration

One row for every declaration in every source file, including nested ones and ones
with no name of their own.

| field | what it is |
| --- | --- |
| `name` | the name **exactly as written**, case preserved. May be `null`. |
| `display` | an UPPER CASE label, unique within the module. Use this to show a result. |
| `qualified` | the `::` path from the file's root namespace, e.g. `Pkg::Def::feature`. |
| `identity` | `module::qualified` — unique across the whole graph. |
| `kind` | lower case, from the closed list below. |
| `is_definition` | `true` for `part def X`, `false` for `part x : X`. |
| `module` | which model the file belongs to. |
| `source_file`, `source_line`, `source_column` | where the declaration is. Always present. |
| `owner` | the `identity` of the enclosing declaration, or `null` at the top. |
| `depth` | how deeply nested, `0` at the top of a file. |
| `children` | how many declarations are directly inside it. |
| `anonymous` | `true` when the source gave it no name. |
| `attributes` | a MAP of feature name → value. See below. |
| `value`, `value_operator` | the element's own value, when it has one. `=` is a binding, `:=` a default. |
| `multiplicity` | `{text, lower, upper}`. `upper` is `null` for `*`. |
| `modifiers` | a LIST of the words in front of the keyword. |
| `abstract`, `variation`, `variant`, `individual`, `reference`, `end` | booleans hoisted out of `modifiers`. |
| `direction` | `in`, `out`, `inout`, or `null`. |
| `visibility` | `public`, `private`, `protected`, or `null`. |
| `conjugated` | `true` for a port typed `~SomePort`. |
| `short_name` | the identifier in `<'...'>`. Often an external tracking id. |
| `doc` | the text of the `doc /* ... */` comment, or `null`. |
| `rationale` | the text of an `@Rationale { text = "..." }` annotation, or `null`. |
| `annotations` | a LIST of `{name, values}` for every `#Name` and `@Name {...}`. |
| `comments` | a LIST of other comment text. |
| `typed_by`, `specializes`, `redefines`, `references` | LISTS of reference text **as written**, resolved or not. |
| `description` | a generated sentence for lexical search. Not a fact. |

### The schema you were given is a sample, so it is incomplete

The schema handed to you alongside this document is built by reading one document
per collection. Every field that only some rows carry is therefore missing from it,
and **a field's absence from the schema is not evidence that the field does not
exist.** These are written by the same code that writes the rows, so they are always
these, whatever corpus is loaded:

On `sysml_elements`, present only when the source says so:

| field | when | shape |
| --- | --- | --- |
| `multiplicity` | a `[...]` was written | `{text, lower, upper}` |
| `value`, `value_operator` | the declaration has an `=` or `:=` | see the four value shapes above |
| `expression` | a constraint or calculation body | `{text, operator}` |
| `annotations` | an `@Meta` or `#meta` was written on it | `[{name, values}]` |
| `rationale` | an annotation carried a `text =` | a string |
| `states` | the declaration is a relationship statement | the edge label it states |

On `sysml_declarations`, present only on some labels: `trigger` and `guard` on a
transition, `through` on a `connectedTo`, `recursive` on an `imports`, `branch` on a
branch. `source_file` and `source_line` are on every relationship the syntax states
and absent on `READ_AS`, which is the join to Layer 3 rather than a statement in any
file.

### The shape of a row, which the field list does not convey

One flat document per declaration. There is exactly one nested map of values on it,
`attributes`, and nothing else lives inside that map. In particular `annotations`,
`multiplicity`, `value` and `expression` are **siblings of** `attributes`, not
members of it: `e.attributes.annotations` is always null and a query built on it
returns nothing.

The modifiers are top-level booleans -- `abstract`, `variation`, `variant`,
`individual`, `reference`, `end`, `conjugated` -- with the words also in the
`modifiers` list. **A modifier is never a `kind`.** `kind == "variation"` and
`kind == "abstract"` match nothing at all; the questions those look like are
`e.variation == true` and `e.abstract == true`. Likewise "part definition" is
`kind == "part" AND is_definition == true`, because definition-versus-usage is a flag
and not a kind of its own.

`depth` and `children` are already computed on the row: `depth` is how many `::`
segments the qualified name has, `children` is the number of declarations written
directly inside this one. A question about direct members needs neither a traversal
nor a subquery.

An element with no name in the source has `anonymous: true` and a `display` of
`KIND@line`. There are usually many of them -- connections, successions and
transitions are frequently unnamed -- so a query that only looks at named elements
silently drops a whole class of declaration.

### `typed_by`, `specializes`, `redefines`, `references`: a field and a label

Each of those four names is **both** a field on the element and an edge label, and
they mean different things. The field is the reference *as written in the source*,
kept whether or not it resolved. The edge exists only where the far end is a
declaration inside this corpus. A reference to a standard-library type resolves to
nothing, so it appears in the field and produces no edge.

So: use the field to answer what the source says, use the edge to traverse. Counting
the field counts references that point outside the corpus; counting the edge does
not. Neither is wrong, and reporting one as the other is.

This is also the only way to tell two absences apart. "Nothing satisfies this
requirement" and "what satisfies it is outside the corpus" both show up as a missing
edge; the written text is what distinguishes them.

### The same number is on two rows, so do not add it twice

A declaration that assigns a value carries it in its own `value`, and its **owner**
carries the same number in `attributes` under that declaration's name. One value,
two places, by design: the map is what makes a rollup one hop instead of two.

A sum that collects both `e.attributes[...]` and the `value` of the elements
underneath `e` therefore counts every figure twice, and the total looks plausible.
Pick one. `attributes` on the owner is almost always the one you want.

### `owns` does not reach everything

A declaration that exists only to state a relationship -- an `import`, an `alias`, a
`succession`, an annotation, a package `filter` -- gets no `owns` edge, because it is
not a member of anything in the sense a composition walk means. Those rows are real
and queryable; they are simply not reachable by walking `owns` from their file's
package. Find them by `kind` and `module`, never by traversal.

### `kind` is a closed list

`accept`, `action`, `actor`, `alias`, `allocation`, `analysis`, `assign`,
`attribute`, `binding`, `calculation`, `case`, `comment`, `concern`, `connection`,
`constraint`, `decide`, `dependency`, `do`, `doc`, `entry`, `enumeration`, `event`,
`exit`, `expose`, `filter`, `flow`, `fork`, `import`, `interface`, `item`, `join`,
`merge`, `message`, `metadata`, `objective`, `occurrence`, `package`, `part`,
`port`, `rendering`, `rep`, `requirement`, `return`, `send`, `snapshot`,
`stakeholder`, `state`, `subject`, `succession`, `terminate`, `timeslice`,
`transition`, `usecase`, `verification`, `view`, `viewpoint`.

There is no `component`, `system`, `function`, `module`, `element`,
`partDefinition` or `requirementUsage` kind. A noun in a question is not
automatically a kind. If the word is not on that list, do not filter on `kind` at
all — filter on what is actually being asked about.

`kind` does not distinguish a definition from a usage: both are `part`, and
`is_definition` separates them. A question about "the parts of X" almost always
means usages inside X, not every `part def` in the corpus.

### `attributes` is a map, and its values have four shapes

`attributes` is a MAP from feature name to a small object. It is present and empty
on most rows, so test `LENGTH(ATTRIBUTES(e.attributes)) > 0`, never
`e.attributes != null`. `e.attributes[*].value` does **not** iterate a map.

| written in the source | stored |
| --- | --- |
| `attribute dryMass = 137000 [kg]` | `{value: 137000, unit: "kg", text: "137000[kg]"}` |
| `attribute count = 5` | `{value: 5, unit: null, text: "5"}` |
| `attribute label = "text"` | `{value: "text", text: "\"text\""}` |
| `attribute total = a + b` | `{expression: "a + b", operator: "+"}` |
| `attribute sel = Kind::option` | `{reference: "Kind::option", text: "Kind::option"}` |

Only the first two are numbers. An entry with `expression` is a formula that was
never evaluated — including it in a `SUM` is wrong, and `SUM` will silently skip it
because it is not a number. Guard on `IS_NUMBER(a.value)` when you mean arithmetic.

Units are reduced to the bare symbol, so `[kg]`, `[SI::kg]` and `['kg']` all read
`kg`. Two values with different units must not be added. **Always group a sum by
unit** rather than assuming one.

An element's own value is in `value`; the values of the features declared directly
inside it are copied into `attributes` so a lookup is one read. Those features are
still rows of their own, with their own lines, so both routes work and neither is
a duplicate of the other in a count — count rows, not map entries, when asked "how
many attributes".

### `multiplicity` says how many, and null does not mean unbounded

`{text, lower, upper}`, where `text` is what was written. `upper` is null for `*`
and for `1 .. *`, and also null when the bound is a symbolic name rather than a
number -- so `upper == null` is not a test for "unbounded", it is a test for "no
numeric upper bound", which is a different set. Test `text` for `*` when the question
is about being unbounded.

Most declarations have no `multiplicity` at all, and a missing field reads as null
too, so a filter on `upper == null` alone matches every row that never mentioned
multiplicity.

### Write literals, never bind parameters

The query you write is executed exactly as written, with no bind values supplied.
`@name` is not a placeholder that gets filled in later — it is a query that fails
with "no value specified for declared bind parameter". Take the value out of the
question and put it in the query as a string literal.

Put it in one place at the top, so the rest of the query reads as a shape:

```aql
LET wanted = "<NAME>"
FOR e IN sysml_elements
  FILTER e.name == wanted OR e.display == UPPER(wanted)
  LIMIT 5
  RETURN {name: e.display, kind: e.kind, at: CONCAT(e.source_file, ":", e.source_line)}
```

**`"<NAME>"`, `"<ELEMENT>"`, `"<STATE>"`, `"<PART>"`, `"<ATTRIBUTE>"` and
`"<WORD>"` in the examples below mark where a value out of the question goes.**
Replace the whole token, quotes kept, with the real string. Never leave one in a
query you run.

### Names are case-sensitive, and that is load-bearing

SysML is case-sensitive and these models rely on it: `Pump` is a definition and
`pump` is a usage typed by it. They are two different rows. Never compare with
`LOWER()` on both sides unless you mean to merge them.

`name`, `qualified`, `identity` and `short_name` keep the source's own case.
`display` is upper case. So a name lower-cased before comparison matches nothing
at all -- `e.identity == "somepackage::something"` is always false, because
`identity` is `SomePackage::Something`. When in doubt compare against `display`
with `UPPER()` applied to the search term only, never to both sides.

A question, though, supplies a name in whatever case the person typed, and an
unquoted SysML identifier cannot contain a space — so a two-word thing is declared
as one word. A lookup therefore has to try several forms. Use this shape:

```aql
LET wanted = "<NAME>"
LET squashed = UPPER(SUBSTITUTE(wanted, " ", ""))
LET plain = UPPER(wanted)
LET hit = FIRST(
  FOR e IN sysml_elements
    FILTER e.name == wanted OR e.display == plain OR e.display == squashed
       OR e.short_name == wanted OR UPPER(e.short_name) == plain
    SORT e.depth
    RETURN e)
LET fallback = hit != null ? null : FIRST(
  FOR e IN sysml_elements
    FILTER CONTAINS(UPPER(e.display), squashed) OR CONTAINS(UPPER(e.qualified), plain)
    SORT LENGTH(e.display), e.depth
    RETURN e)
RETURN hit != null ? hit : fallback
```

Stopping at the first rung is the most expensive mistake possible here, because
the query still runs, still returns cleanly, and returns nothing — which reads as
"the model does not contain that" rather than "the name was never found". Always
resolve a supplied name all the way down, in one query.

**Never invent an owner prefix.** `display` is the bare name wherever only one
declaration claims it, and takes as many owner segments as it needs, joined with
`_`, only where several do. Which of the two a given element got depends on what
else is in the corpus, so it cannot be worked out from the question. A question that
mentions two things ("the battery's STANDBY state", "the engine's thrust") is naming
an owner and a member, not a compound name: look the member up on its own, and use
the owner as a filter or as the starting vertex if you need it. Writing
`display == "BATTERY_STANDBY"` because the question said "battery" and "standby"
guesses a name that probably does not exist, and the query comes back empty and
confident.

Where a name in an example below is written as a single `==`, that is shorthand,
and it is only safe because those names come from this document. **A name that came
from the question always takes the full lookup above.** The space-squashed rung in
particular is not optional: an unquoted SysML identifier cannot contain a space, so a
question about "Saturn V" can only ever match a stored `SATURNV`, and a query that
tries `name == "Saturn V"` and `display == "SATURN V"` and stops has tested the two
forms that cannot exist. Two rungs is not the shape; four is.

**This ladder is for element names only.** An *attribute* name is not a name from
the question -- the second half of this document lists the attribute names that
occur, spelled exactly as the source spells them, so an attribute is indexed
directly: `e.attributes.dryMass`, or `e.attributes["dryMass"]`. Reaching for
`CONTAINS` there is how `CONTAINS(LOWER(field), "dryMass")` gets written, which
folds one side of the comparison and matches nothing. Search across attribute names
only when looking for one this document does not name, and then fold both sides.

"Which elements mention a dry mass", "which have a failure rate", "what does the
model say about capacity" are all **presence tests on the attribute**:
`FILTER e.attributes.dryMass != null`. They are not text searches. In particular
`a.text` is the source spelling of the *value* -- `"137000[kg]"` -- so looking for the
words of the question inside it matches nothing, every time. What an element "says
about" an attribute is that attribute's `value` and `unit`.

---

## `sysml_declarations` — one row per relationship the syntax states

An edge collection. `label` is the relationship, written in **camelCase, not
lowercased**: `typedBy`, `dependsOn`, `transitionsTo`, `variantOf`,
`referencesFeature`. Comparing against a lowercase literal matches nothing.

Every edge carries `label`, `module`, `source_file`, `source_line` and a readable
`description`. Some carry extra fields: `trigger` and `guard` on a transition,
`through` on a `connectedTo`, `recursive` on an `imports`.

### The labels, and which way each points

The words in the `from -> to` column below are **roles, not values**. "namespace",
"design", "client", "supplier", "sender", "container" describe what an endpoint is
doing in that relationship; none of them is a `kind`, and filtering `kind ==
"namespace"` or `kind == "client"` matches nothing. The kinds are the closed list
above, and the element that imports is a `package`.

| `label` | from → to |
| --- | --- |
| `owns` | container → the element declared inside it |
| `typedBy` | usage → the definition after its `:` |
| `specializes` | definition → the definition it is a kind of (`:>`) |
| `subsets` | feature → the feature it is part of (`:>` on a usage) |
| `redefines` | feature → the inherited feature it replaces (`:>>`) |
| `referencesFeature` | feature → the feature it stands for (`::>`) |
| `conjugates` | port → the port definition it is the reverse of (`~`) |
| `variantOf` | variant → the variation it is an option of |
| `imports` | namespace → the namespace it makes visible |
| `aliasOf` | alias → what it renames |
| `bindsTo` | feature → the feature its `=` names |
| `assigns` | action → the feature it writes |
| `satisfies` | design → the requirement it meets |
| `verifies` | verification case → the requirement it checks |
| `refines` | requirement → the requirement it makes more precise |
| `derives` | requirement → the requirement it comes from |
| `asserts` / `assumes` / `requires` | element → the constraint |
| `dependsOn` | client → supplier |
| `subject` | requirement or case → what it is about |
| `actor` | case → who takes part in it |
| `stakeholder` | requirement or concern → whose interest it is |
| `objective` | case → what it is trying to establish |
| `frames` | requirement → the concern it addresses |
| `performs` | part → the action it carries out |
| `exhibits` | part → the state machine it is in |
| `includes` | use case → the use case it uses |
| `entryAction` / `doAction` / `exitAction` | state → the action it runs |
| `transitionsTo` | source state or step → target |
| `triggeredBy` | transition → the event that fires it |
| `guardedBy` | transition → the constraint that has to hold |
| `effect` | transition → the action it runs |
| `accepts` | element → the payload it receives |
| `sends` | sender → receiver |
| `flows` | source → target of an item flow |
| `carries` | flow or message → the item it moves |
| `via` | flow or transition → the port it passes through |
| `startsAt` | body → the first step in it |
| `connects` | the connection element → each of its ends |
| `connectedTo` | one end → the other, with `through` naming the connection |
| `allocates` | source → what it is allocated to |
| `exposes` | view → what it draws from |
| `renders` | view → how it is drawn |
| `annotatedBy` | element → the metadata written on it |
| `disjointFrom` | type → a type it shares no instances with |
| `unionOf` / `intersectionOf` / `differenceOf` | type → each type it is built from |
| `chainOf` | feature → each feature in the chain it stands for |
| `inverseOf` | feature → the feature it is the reverse of |
| `DECLARED_IN` | element → the `sysml_sources` row of its file |
| `READ_AS` | element → the `sysml_Entities` row that is a reading of it |

Direction is the single most common cause of an empty result. "What owns X",
"what satisfies X", "what depends on X" are **INBOUND** from X. "What does X
contain", "what does X specialize", "what does X satisfy" are **OUTBOUND**. Read
the table rather than guessing from the English, because the English often runs the
other way: a requirement is *satisfied by* a design, but the edge points design →
requirement.

Note `DECLARED_IN` and `READ_AS` live in the same edge collection and cross into
other collections. Any traversal that does not name its labels will wander into
`sysml_sources` and `sysml_Entities`. **Always constrain the labels.**

---

## Traversals

### A label ending in `Of` runs from the dependent element outward

`variantOf`, `aliasOf`, `chainOf`, `inverseOf`, `unionOf`, `intersectionOf`,
`differenceOf`: in each, the edge starts at the element that *is* the variant, the
alias, the chain, the union, and points at what it is a variant or alias or union of.

So the collection is always the inbound side. "What are the options for this variation
point" is `INBOUND` from the variation: the variants point at it, not the other way
round. Walking `OUTBOUND` from the thing being asked about returns nothing and reads
as "it has no options", which is the failure this rule exists to prevent.

### `COUNT(expr)` inside `AGGREGATE` does not count what you think

This is AQL semantics rather than anything about this graph, and it silently produces
a wrong number rather than an error. In `COLLECT k = e.kind AGGREGATE n = COUNT(cond ?
1 : null)`, `COUNT` counts every row in the group; the expression does not filter it.
A group of 167 usages and no definitions reports 167 for both.

Count a condition with `SUM`, which does look at the value:

```aql
FOR e IN sysml_elements
  COLLECT kind = e.kind
  AGGREGATE definitions = SUM(e.is_definition ? 1 : 0),
            usages = SUM(e.is_definition ? 0 : 1)
  FILTER definitions > 0 AND usages > 0
  RETURN {kind, definitions, usages}
```

### Constrain every edge on the path, not just the last one

`FOR v, e IN 1..6 OUTBOUND x sysml_declarations FILTER e.label == 'owns'` filters
only the final edge of each path. Over six hops that reaches most of the graph.
Filter the path:

```aql
LET start = FIRST(FOR e IN sysml_elements
                    FILTER e.display == UPPER(wanted) OR e.name == wanted
                       OR e.display == UPPER(SUBSTITUTE(wanted, " ", ""))
                       OR CONTAINS(e.display, UPPER(SUBSTITUTE(wanted, " ", "")))
                    SORT e.depth, LENGTH(e.display) RETURN e)
FOR v, e, p IN 1..6 OUTBOUND start sysml_declarations
  OPTIONS {bfs: true, uniqueVertices: "global"}
  FILTER p.edges[*].label ALL IN ["owns"]
  RETURN DISTINCT {name: v.display, kind: v.kind,
                   at: CONCAT(v.source_file, ":", v.source_line)}
```

### A containment walk has to cross typing, not just ownership

This is the single most important thing to know about a SysML graph. A *usage*
carries almost nothing: `part stage1 : Booster` declares that `stage1` exists and
is a `Booster`, and every value, port and sub-part is declared on `Booster`. A walk
that follows only `owns` from a usage stops immediately and reports that the thing
is empty.

The composition walk is `owns` **plus** `typedBy` **plus** `subsets` and
`redefines`, and nothing else:

```aql
LET start = FIRST(FOR e IN sysml_elements
                    FILTER e.display == UPPER(wanted) OR e.name == wanted
                       OR e.display == UPPER(SUBSTITUTE(wanted, " ", ""))
                       OR CONTAINS(e.display, UPPER(SUBSTITUTE(wanted, " ", "")))
                    SORT e.depth, LENGTH(e.display) RETURN e)
FOR v, e, p IN 0..8 OUTBOUND start sysml_declarations
  OPTIONS {bfs: true, uniqueVertices: "global"}
  FILTER p.edges[*].label ALL IN ["owns", "typedBy", "subsets", "redefines"]
  RETURN DISTINCT v
```

Do **not** let `specializes` into a composition walk. It climbs to an abstract
supertype and then descends into everything else that specializes it, which is a
different subtree entirely and inflates every total.

`redefines` is in the list because a redefinition is where an inherited feature is
given its actual value; leaving it out reads the general value instead of the
specific one.

### Depth, and stopping

Eight hops is usually enough and is cheap with `bfs` and `uniqueVertices:
"global"`. Without `uniqueVertices` a diamond in the typing graph is walked once
per path and every aggregate is multiplied.

---

## Numbers

### Summing an attribute over a subtree

The question "what is the total mass of X" is: resolve X, take its composition
subtree, find every numeric attribute whose name matches, and group by unit.

```aql
LET wanted = "<NAME>"
LET root = FIRST(
  FOR e IN sysml_elements
    FILTER e.name == wanted OR e.display == UPPER(wanted)
       OR e.display == UPPER(SUBSTITUTE(wanted, " ", ""))
       OR CONTAINS(e.display, UPPER(SUBSTITUTE(wanted, " ", "")))
    SORT e.depth, LENGTH(e.display) RETURN e)
LET subtree = (
  FOR v, e, p IN 0..8 OUTBOUND root sysml_declarations
    OPTIONS {bfs: true, uniqueVertices: "global"}
    FILTER p.edges[*].label ALL IN ["owns", "typedBy", "subsets", "redefines"]
    RETURN DISTINCT v)
FOR v IN subtree
  FOR field IN ATTRIBUTES(v.attributes)
    LET a = v.attributes[field]
    FILTER CONTAINS(LOWER(field), LOWER("<ATTRIBUTE>")) AND IS_NUMBER(a.value)
    COLLECT unit = a.unit AGGREGATE total = SUM(a.value), parts = LENGTH(1)
    RETURN {unit, total, contributing_elements: parts}
```

Three things that query gets right and a naive one does not. It groups by `unit`,
so two incompatible units are reported as two totals rather than added. It tests
`IS_NUMBER`, so a formula or a string is skipped rather than silently coerced. And
it walks from the root with `uniqueVertices: "global"`, so a definition reached by
two different usages is counted once.

That last one is a trade-off worth stating: if the same definition is used twice
and both instances physically exist, counting it once **undercounts**. When the
question is about physical totals and the model uses repeated usages, walk without
`uniqueVertices` and multiply by `multiplicity.lower` instead. Say which reading
was used when the answer could be either.

### Ranking, extremes, counting

```aql
FOR e IN sysml_elements
  FOR field IN ATTRIBUTES(e.attributes)
    LET a = e.attributes[field]
    FILTER CONTAINS(LOWER(field), LOWER("<ATTRIBUTE>")) AND IS_NUMBER(a.value)
    SORT a.value DESC
    LIMIT 10
    RETURN {element: e.display, value: a.value, unit: a.unit,
            at: CONCAT(e.source_file, ":", e.source_line)}
```

### "For each part of X" is a walk from X, not a name match

A question that scopes something to a subject -- "for each stage of the Saturn V",
"the parts of the mission", "every requirement under this package" -- is asking for
the composition subtree of that subject. Resolve the subject, then walk.

The member word matters here. **"stage", "part", "component", "subsystem", "module",
"step", "element" and the like are positions in the subtree, not `kind` values and
not names.** There is no `kind` called "stage"; a Saturn V stage is whatever the walk
from `SATURNV` reaches. Looking for the word itself -- as a kind, or inside a name --
is the mistake, and it is the reason to walk rather than filter. Only the *subject*
of the question ("the Saturn V") is a name to resolve. What it is
never asking for is elements whose *name* resembles the subject's:
`CONTAINS(e.display, "SATURNV")` finds the subject and anything that happens to be
named after it, and misses every member with a name of its own -- which is most of
them, because a well-named part is not named after its parent. The query runs and
returns a short, confident, wrong list.

```aql
LET wanted = "<ELEMENT>"
LET root = FIRST(FOR e IN sysml_elements
                    FILTER e.display == UPPER(wanted) OR e.name == wanted
                       OR e.display == UPPER(SUBSTITUTE(wanted, " ", ""))
                       OR CONTAINS(e.display, UPPER(SUBSTITUTE(wanted, " ", "")))
                    SORT e.depth, LENGTH(e.display) RETURN e)
FOR v, edge, path IN 1..8 OUTBOUND root sysml_declarations
  OPTIONS {bfs: true, uniqueVertices: "global"}
  FILTER path.edges[*].label ALL IN ["owns", "typedBy", "subsets", "redefines"]
  FILTER IS_NUMBER(v.attributes.<ATTRIBUTE>.value)
  RETURN {member: v.display, value: v.attributes.<ATTRIBUTE>.value,
          at: CONCAT(v.source_file, ":", v.source_line)}
```

### A superlative ranges over every element, not the top-level ones

"Which element has the most X" is a sort over the whole collection. Filtering to
`e.owner == null` first looks like it is finding the top-level thing to ask about,
and it is not: it throws away everything declared inside a package, which is where
almost every real subject lives. The largest subtree in a corpus is routinely a
requirement or a part *inside* the package that appears to contain it, so ranking
roots reports the container and misses the answer by one level.

```aql
FOR e IN sysml_elements
  FILTER e.children > 0
  LET below = LENGTH(
    FOR v, edge, path IN 1..8 OUTBOUND e sysml_declarations
      OPTIONS {bfs: true, uniqueVertices: "global"}
      FILTER path.edges[*].label ALL IN ["owns", "typedBy", "subsets", "redefines"]
      RETURN DISTINCT v._id)
  SORT below DESC
  LIMIT 5
  RETURN {element: e.display, kind: e.kind, below,
          at: CONCAT(e.source_file, ":", e.source_line)}
```

Return the top few rather than one. A superlative over a corpus is usually a near
tie, and the runners-up are what let the reader see whether the winner is meaningful
or an artefact of where a package boundary happens to fall. Start the walk at `1..`,
not `0..`, or the count includes the element itself and every figure is one too
large.

### A formula is not a dead end -- it says where the numbers are

An attribute whose entry has `expression` instead of `value` was never evaluated, so
it cannot go into a `SUM`. That is not the end of the question. A declared total is
written as a formula precisely because the figures live on the attributes the formula
names, which are almost always siblings on the same element or members of its
subtree. So when the attribute a question asks for turns out to be a formula, read
the names out of `expression` and sum *those*, and say in the answer that the model
declares the total as a formula over them rather than as a figure.

```aql
LET root = FIRST(FOR e IN sysml_elements FILTER e.display == "<ELEMENT>" RETURN e)
LET declared = root.attributes.<ATTRIBUTE>
LET parts = (FOR field IN ATTRIBUTES(root.attributes)
  LET a = root.attributes[field]
  FILTER IS_NUMBER(a.value) AND CONTAINS(declared.expression, field)
  RETURN {field, value: a.value, unit: a.unit})
RETURN {declared_as: declared.expression, total: SUM(parts[*].value), parts}
```

Returning no rows because the only attribute that matched was a formula reports "the
model does not say" about something the model says completely. If nothing in
`expression` matches a sibling attribute, fall back to every numeric attribute on the
element whose name suggests the same quantity, and say which ones you added.

### A total and its parts are one query, returning both

"What is the total, and what makes it up" asks for two things, and an aggregate on
its own answers half of it. Keep the contributing rows with `INTO` and return them
beside the total, so the reader can name the parts and their values rather than only
the names of the fields:

```aql
LET wanted = "<ELEMENT>"
LET root = FIRST(FOR e IN sysml_elements
                    FILTER e.display == UPPER(wanted) OR e.name == wanted
                       OR e.display == UPPER(SUBSTITUTE(wanted, " ", ""))
                       OR CONTAINS(e.display, UPPER(SUBSTITUTE(wanted, " ", "")))
                    SORT e.depth, LENGTH(e.display) RETURN e)
FOR field IN ATTRIBUTES(root.attributes)
  LET a = root.attributes[field]
  FILTER IS_NUMBER(a.value)
  COLLECT unit = a.unit INTO rows = {field, value: a.value}
  RETURN {unit, total: SUM(rows[*].value), parts: rows}
```

An answer that says "composed of four contributing parts" and names the fields
without their values has computed the total and thrown away what was asked for
alongside it.

"How many" always means a count computed in AQL. The result set handed back to the
reader is capped, so a query that returns one row per match reports the cap as the
answer:

```aql
FOR e IN sysml_elements
  COLLECT kind = e.kind, definition = e.is_definition WITH COUNT INTO n
  SORT n DESC
  RETURN {kind, definition, n}
```

### Which attribute names exist

A question names an attribute in English and the source names it in camelCase.
Before filtering on an exact name, find the real one:

```aql
FOR e IN sysml_elements
  FOR field IN ATTRIBUTES(e.attributes)
    FILTER CONTAINS(LOWER(field), LOWER("<ATTRIBUTE>"))
    COLLECT name = field, unit = e.attributes[field].unit WITH COUNT INTO n
    SORT n DESC
    RETURN {attribute: name, unit, occurrences: n}
```

---

## Requirements

A requirement is satisfied by a design, and the edge runs design → requirement. So
"what satisfies R" is INBOUND from R, and the anti-join for coverage is an absent
inbound edge:

```aql
FOR r IN sysml_elements
  FILTER r.kind == "requirement"
  LET met = LENGTH(FOR v, e IN 1..1 INBOUND r sysml_declarations
                     FILTER e.label IN ["satisfies", "verifies"] RETURN 1)
  FILTER met == 0
  RETURN {requirement: r.display, short_name: r.short_name,
          text: r.doc, at: CONCAT(r.source_file, ":", r.source_line)}
```

A requirement written `requirement def <'ID-1'> Name` is **one row**: `Name` in
`name`, `ID-1` in `short_name`. Never expect `name == 'ID-1'`. A model may also
declare a *usage* whose name is another element's short name; those are two
distinct rows and a question about the identifier should return both.

`doc` is the requirement's own text and `rationale` is why it exists. Both are read
straight from the source, so quoting them is quoting the model.

A requirement's `subject` says what it constrains; its `asserts` / `requires` edges
lead to the constraint elements, whose `expression.text` is the actual bound the
model states.

---

## Behaviour and structure

A state machine is `exhibits` from the part to the state, `owns` down to the
states, and `transitionsTo` between them. The trigger is on the edge:

```aql
LET wanted = "<STATE>"
LET state = FIRST(FOR e IN sysml_elements
                    FILTER e.kind == "state"
                    FILTER e.display == wanted OR e.name == wanted
                       OR CONTAINS(e.display, wanted)
                    SORT LENGTH(e.display) RETURN e)
FOR v, e IN 1..1 OUTBOUND state sysml_declarations
  FILTER e.label == "transitionsTo"
  RETURN {to: v.display, trigger: e.trigger, guard: e.guard,
          at: CONCAT(e.source_file, ":", e.source_line)}
```

`transitionsTo` also carries plain successions in an action body, so the same query
answers "what happens after this step".

A connection is an element with `connects` edges to each of its ends, plus a
`connectedTo` edge directly between the ends carrying `through`. Query the ends
directly:

```aql
LET part = FIRST(FOR e IN sysml_elements
                   FILTER e.display == UPPER(wanted) OR e.name == wanted
                      OR e.display == UPPER(SUBSTITUTE(wanted, " ", ""))
                      OR CONTAINS(e.display, UPPER(SUBSTITUTE(wanted, " ", "")))
                   SORT e.depth, LENGTH(e.display) RETURN e)
FOR v, e IN 1..1 ANY part sysml_declarations
  FILTER e.label == "connectedTo"
  RETURN {other: v.display, through: e.through,
          at: CONCAT(e.source_file, ":", e.source_line)}
```

Ports are found by `kind == "port"`; a conjugated one has `conjugated == true`,
meaning its payload flows the other way.

---

## Layers 1 and 2 around the elements

`sysml_modules` is one row per model, with `name` and `files`. It is the isolation
boundary: names resolve only within a module, and nothing structural crosses one.
Every element and every declaration carries `module`, so filtering there is cheaper
than a join.

`sysml_sources` is one row per source file: `filename`, `module`, `lines`,
`characters`, `citable_url`, `file_id`. Reach it from an element with the
`DECLARED_IN` edge, or just read `source_file`.

`sysml_domains` is the Leiden clustering **of whole files**, joined by
`IN_DOMAIN` edges in `sysml_corpus_relations`. `sysml_similarities` holds
`SIMILAR_TO` between two files with `similarity_score`, `rank` and `rrf_score`.
Both are about documents, never about elements — a question about which *elements*
resemble each other is not answerable from them.

---

## Layer 3, and what it is still good for

`sysml_Entities` has `entity_name` (UPPER CASE), `entity_type` (lower case),
`description`, `files`, `models` and an embedding. It has no values and no
structure. `sysml_Relations` holds `PART_OF` (chunk → document), `MENTIONED_IN`
(entity → chunk), `IN_COMMUNITY` and `SUB_COMMUNITY_OF`, plus any `RELATED_TO`
edges that survived — those are relations read out of prose that the syntax does
not state, and they carry `relationship_type` in lower case.

`files` and `models` are LISTS on Layer 3 **vertices**. Use `IN`, never `==`.

**Layer 3 edges carry no provenance at all** -- no `module`, no `models`, no
`files`. Grouping `sysml_Relations` by model directly returns nulls or nothing;
reach the model through an endpoint instead, with `DOCUMENT(r._from).models`.

**Most of `sysml_Relations` is not a relation anyone asked about.** The collection
holds the graph's own plumbing alongside the inferred relations, so "how many
relations did the model infer" is a count of `type == "RELATED_TO"` and nothing
else. Counting the whole collection overstates it by an order of magnitude, and the
figure looks plausible either way:

```aql
FOR r IN sysml_Relations
  COLLECT type = r.type WITH COUNT INTO n
  SORT n DESC
  RETURN {type, n}
```

Only `RELATED_TO` carries `relationship_type`, `description` and an embedding. The
other types have `_from`, `_to` and `type`, so a query that filters on any other
field silently matches none of them.

Use Layer 3 for a vague or plural question, where the answer is prose:

```aql
FOR e IN sysml_Entities
  FILTER CONTAINS(UPPER(e.entity_name), UPPER("<WORD>"))
     OR CONTAINS(LOWER(e.description), LOWER("<WORD>"))
  LIMIT 15
  RETURN {name: e.entity_name, type: e.entity_type, description: e.description,
          files: e.files}
```

A question about both layers at once -- "how much of this was read from the syntax
and how much was inferred" -- has to name both collections. There is no single
collection holding all the relations, and the layer is not a field on a row:

```aql
LET stated = (FOR d IN sysml_declarations
  FILTER d.label NOT IN ["DECLARED_IN", "READ_AS"]
  COLLECT model = d.module WITH COUNT INTO n
  RETURN {model, n})
LET inferred = (FOR r IN sysml_Relations
  FILTER r.type == "RELATED_TO"
  COLLECT model = FIRST(DOCUMENT(r._from).models) WITH COUNT INTO n
  RETURN {model, n})
RETURN {stated, inferred}
```

Do not label the non-`RELATED_TO` rows "read from the syntax". They are the graph's
own plumbing -- entity-to-chunk, chunk-to-document, community membership -- and
counting them as relations of the corpus inflates the inferred side into the
thousands.

To go from a description back to the fact, follow `READ_AS` backwards into Layer 2:

```aql
FOR entity IN sysml_Entities
  FILTER CONTAINS(UPPER(entity.entity_name), UPPER("<WORD>"))
  FOR element, edge IN 1..1 INBOUND entity sysml_declarations
    FILTER edge.label == "READ_AS"
    LIMIT 15
    RETURN {read_as: entity.description, declared: element.display,
            kind: element.kind, attributes: element.attributes,
            at: CONCAT(element.source_file, ":", element.source_line)}
```

Not every entity has one: the match is written only where exactly one declaration
in the module answers to the name. An entity without a `READ_AS` edge is a name the
extraction read in prose that no declaration backs, and it should not be counted in
an answer about what the model contains.

---

## Mistakes to avoid

* Filtering `sysml_declarations` on a lowercase label. They are camelCase.
* Answering a numeric question from `sysml_Entities`. There are no numbers there.
* Walking `sysml_declarations` without naming the labels. `DECLARED_IN` and
  `READ_AS` lead out of the element graph.
* Following only `owns` from a usage. Cross `typedBy` or the subtree is empty.
* Letting `specializes` into a composition rollup.
* Walking an `...Of` label outbound from the thing being asked about. The variants,
  aliases and constituents are on the inbound side.
* Counting a condition with `COUNT(cond ? 1 : null)` in an `AGGREGATE`. Use `SUM`.
* Treating a modifier as a kind: `kind == "variation"`, `kind == "abstract"`.
  They are booleans on the row.
* Looking for `annotations`, `multiplicity` or `value` inside `attributes`. They are
  siblings of it.
* Concluding a field does not exist because the sampled schema omits it.
* Adding an owner's `attributes` value and the child's own `value` in one sum.
* Walking `owns` to find imports, aliases or successions. They have no `owns` edge.
* Ranking only `e.owner == null` for a superlative. The deepest subtree is usually
  inside a package, not the package.
* Scoping "the parts of X" with `CONTAINS(display, "X")` instead of walking from X.
* Counting all of `sysml_Relations` as inferred relations, or grouping it by
  `module`, which no edge in it has.
* Treating a type operator as a specialisation. `unionOf`, `intersectionOf`,
  `differenceOf` and `disjointFrom` say how a type was built, not what it is a kind
  of, so a rollup or an inheritance walk must not cross them.
* Summing across units, or summing a formula.
* Using `e.attributes != null` as a test for "has attributes"; the map is usually
  present and empty.
* Resolving a supplied name in fewer than all four rungs: `name`, `display`,
  the space-squashed upper-case form, `short_name`, then `CONTAINS` as a fallback.
  A two-word question needs the squashed rung to match anything at all.
* Building a compound `display` out of two words in the question. Owner prefixes
  are only present where a bare name was contested; look the member up alone.
* Writing `@anything`. Bind parameters are never filled; inline the literal.
* Lower-casing a stored name before comparing it. Only `display` is upper case;
  everything else keeps the source's own capitalisation.
* Case-folding one side of a comparison and not the other. `CONTAINS(LOWER(field),
  "dryMass")` never matches, because the left side has already been lowered. Wrap
  both sides or neither.
* Returning one row per match for a "how many" question.
* Reporting an empty result as "the model does not contain that". An empty result
  means the query found nothing; say so, and say what was searched.

Every query must be read-only. Never emit `INSERT`, `UPDATE`, `REPLACE`, `REMOVE`,
`UPSERT` or `TRUNCATE`.
