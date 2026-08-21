"""The AQLizer's worked examples: one hand-written half, one from the graph.

`aql_examples.md` beside this file is hand-written and says nothing about any particular
corpus. Everything in it is a property of this pipeline -- how `parse` reads SysML,
what `model` resolves, which fields `corpus` writes and which way each edge points
-- so it is as true of a corpus loaded tomorrow as of one loaded today. That is the
whole reason it can be hand-written: none of it can go stale when the input changes.

What it cannot contain is anything about the corpus actually loaded: which kinds
occur, what the attributes are called, what units they are in, how names were
disambiguated, which relations are populated and which are empty. Those are exactly
the facts that decide whether a generated query returns rows, and they change with
every import. So this step reads them off the live graph and asks a strong model to
write a second half, which is appended to the first.

The result, `out/aql_examples.md`, is the only file the read side uses. There is no
mode where the hand-written half is passed on its own: half a file produces
queries that are structurally right and filter on attribute names that do not
exist. `nl` falls back to the hand-written file only when this step has never run.

Every ```aql block in both halves is parsed, run against the graph and counted.
Three outcomes are told apart: a block that does not parse is broken; one that runs
and returns nothing has a wrong assumption in it, which is exactly the mistake the
examples exist to prevent; one that returns rows is what it claims to be. Failures go
back to the model with the error attached, for as many rounds as asked for.

    python -m sysml.pipeline.examples.generate
    python -m sysml.pipeline.examples.generate --rounds 2
    python -m sysml.pipeline.examples.generate --check-only
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from ... import config


def SUBSTRING(text: str, start: int, length: int) -> str:
    """AQL's SUBSTRING, for evaluating a survey expression in Python."""
    return (text or "")[start:start + length]


# Not a bare `---`: the hand-written half uses those as section rules, so a bare
# one cannot be told apart from a divider inside it.
SEPARATOR = ("\n\n---\n\n<!-- everything below is written by sysml.pipeline.examples.generate"
             " from a survey of the graph itself -->\n\n")

# What the model is told. It is handed the hand-written half in full, so the one
# instruction that matters is: do not write that again.
GUIDE = """You are writing the second half of a document that teaches a
language model to answer questions about one specific ArangoDB graph by writing
AQL. It is passed verbatim as the `aql_examples` argument of
`ReadOnlyArangoGraphQAChain.from_llm`.

You will be given two things: the first half, which is already written, and a survey
of the graph, which was run against it just now.

The first half describes the pipeline that builds any graph of this kind -- the
collections, every field, every edge label and its direction, how names are
resolved, how a composition walk has to cross typing, how a numeric rollup has to
group by unit. All of that is settled. **Do not restate any of it.** A document that
says the same thing twice teaches nothing twice and costs the reader the context
window it needed for the survey.

Your half is only what the first half cannot know: what is actually in THIS graph.

## What to write

A markdown document beginning with the heading `## What is in this graph`, and
nothing before it. No preamble, no sign-off, no outer code fence.

Cover, in this order:

1. **The corpus.** How many modules and what they are called, how many files, how
   many elements and stated relations. One short paragraph, from the survey.

2. **Which kinds actually occur**, with counts, and which of the closed list are
   absent. The absent ones matter as much: a question that names one of them has no
   answer here, and the reader should say so rather than return an empty result it
   presents as a fact.

3. **The attributes that exist**: their real names, their units, roughly how many
   elements carry each, and an example value. This is the single most useful thing
   in your half, because a question asks for "mass" and the source calls it
   something else. Where several names mean the same quantity, say so.

4. **Which relation labels are populated**, with counts, and which are empty. Same
   reasoning as the kinds.

5. **How names look in this corpus**: the real shape of `short_name`, whether
   `display` was disambiguated with owner prefixes and what those look like, and any
   naming convention a question would trip over.

6. **The composition roots** -- the elements with the largest subtrees underneath
   them, which is what a question about "the whole system" means here.

7. **Worked queries.** Between 8 and 16 ```aql blocks, each solving a question a
   person would actually ask *of this corpus*, using its real attribute names, real
   kinds and real labels. Prefer questions the first half does not already show:
   the rollups that matter here, the coverage question that matters here, the
   behavioural question that matters here.

   Vary the shape as well as the subject. A file of aggregates teaches totals and
   leaves the per-row shape unshown; a file of one-hop queries leaves traversal
   unshown. Between them the queries should cover a total, a per-member listing, a
   count, a ranking, an absence, and a walk of more than one hop.

## Rules for the queries

- Every query must be valid AQL for this graph and must return rows. Use real
  collection names, real field names, and literals that appear in the survey.
- **Never write a bind parameter.** The reader's query is executed exactly as it
  writes it, with no bind values supplied, so `@name` produces "no value specified
  for declared bind parameter" and the question comes back unanswered. Where a value
  would come from the question, put it in a `LET` at the top of the query as a string
  literal -- `LET wanted = "SomeName"` -- and use that. One place to substitute is
  the shape worth teaching; a bind parameter is a query that cannot run.
- Use REAL names, kinds and attribute names from the survey in your queries. The
  first half uses `"<NAME>"`-style placeholders because it cannot know any; you can,
  so do not write a placeholder anywhere.
- Names are case sensitive. `name`, `qualified`, `identity` and `short_name` keep
  the source's capitalisation and only `display` is upper case, so never lower-case
  a stored name before comparing it.
- Every query must be READ-ONLY. No `INSERT`, `UPDATE`, `REPLACE`, `REMOVE`,
  `UPSERT` or `TRUNCATE` anywhere, not even as an example of what not to do.
- Prefer `sysml_elements` and `sysml_declarations`. Only use `sysml_Entities` for a
  question about meaning.
- Under each query, one or two sentences on why it has to look like that here.

Ground every claim in the survey. If the survey does not show something, do not say
it exists, and do not guess at a number."""


# Every probe is written without knowing what the corpus is about, so the same list
# surveys one loaded next week. They lean on Layers 1 and 2 because that is where a
# question with a definite answer is answered.
def probes() -> list[tuple[str, str]]:
    E, D = config.ELEMENTS, config.DECLARATIONS
    return [
        ("the collections, by name and size", "FOR row IN [%s] RETURN row" % ", ".join(
            f'{{collection: "{n}", rows: LENGTH({n})}}' for n in config.ALL_COLLECTIONS)),

        ("modules, and what is in each", f"""
         FOR m IN {config.MODULES}
           LET elements = LENGTH(FOR e IN {E} FILTER e.module == m.name RETURN 1)
           LET relations = LENGTH(FOR r IN {D} FILTER r.module == m.name
                                    AND r.label NOT IN ["DECLARED_IN", "READ_AS"] RETURN 1)
           RETURN {{module: m.name, files: LENGTH(m.files), elements, relations}}"""),

        ("element kinds that occur, split by definition and usage", f"""
         FOR e IN {E}
           COLLECT kind = e.kind AGGREGATE definitions = SUM(e.is_definition ? 1 : 0),
                                           usages = SUM(e.is_definition ? 0 : 1)
           SORT definitions + usages DESC
           RETURN {{kind, definitions, usages}}"""),

        ("attribute names, with units, counts and an example", f"""
         FOR e IN {E}
           FOR field IN ATTRIBUTES(e.attributes)
             LET a = e.attributes[field]
             COLLECT name = field INTO rows = {{owner: e.display, a: a, module: e.module}}
             SORT LENGTH(rows) DESC LIMIT 40
             RETURN {{attribute: name, on_n_elements: LENGTH(rows),
                      units: UNIQUE(rows[*].a.unit),
                      numeric: LENGTH(FOR r IN rows FILTER IS_NUMBER(r.a.value) RETURN 1),
                      example: CONCAT(rows[0].owner, " = ", rows[0].a.text)}}"""),

        ("attributes that hold a formula rather than a number", f"""
         FOR e IN {E}
           FOR field IN ATTRIBUTES(e.attributes)
             FILTER e.attributes[field].expression != null
             LIMIT 10
             RETURN {{element: e.display, attribute: field,
                      expression: e.attributes[field].expression}}"""),

        ("relation labels that are populated", f"""
         FOR r IN {D}
           COLLECT label = r.label WITH COUNT INTO n SORT n DESC
           RETURN {{label, n}}"""),

        ("relation labels that are empty", f"""
         LET present = (FOR r IN {D} COLLECT label = r.label RETURN label)
         FOR label IN @all FILTER label NOT IN present RETURN label"""),

        ("how names were disambiguated", f"""
         FOR e IN {E}
           FILTER e.name != null AND e.display != UPPER(e.name)
           COLLECT AGGREGATE n = COUNT(1)
           RETURN {{elements_whose_display_carries_an_owner_prefix: n}}"""),

        ("a sample of disambiguated names", f"""
         FOR e IN {E}
           FILTER e.name != null AND e.display != UPPER(e.name)
           SORT e.depth DESC LIMIT 8
           RETURN {{name: e.name, display: e.display, kind: e.kind}}"""),

        ("short names, a sample", f"""
         FOR e IN {E} FILTER e.short_name != null
           LIMIT 10 RETURN {{short_name: e.short_name, name: e.name, kind: e.kind}}"""),

        ("the composition roots -- the largest subtrees", f"""
         FOR e IN {E}
           FILTER e.children > 0 AND e.kind IN ["part", "package", "item", "action", "state"]
           LET below = LENGTH(FOR v IN 1..8 OUTBOUND e {D}
                                OPTIONS {{bfs: true, uniqueVertices: "global"}}
                                FILTER IS_SAME_COLLECTION("{E}", v)
                                RETURN DISTINCT v)
           SORT below DESC LIMIT 12
           RETURN {{element: e.display, kind: e.kind, module: e.module,
                    elements_below: below, at: CONCAT(e.source_file, ":", e.source_line)}}"""),

        ("elements that carry the most numeric values", f"""
         FOR e IN {E}
           LET numbers = LENGTH(FOR f IN ATTRIBUTES(e.attributes)
                                  FILTER IS_NUMBER(e.attributes[f].value) RETURN 1)
           FILTER numbers > 0
           SORT numbers DESC LIMIT 10
           RETURN {{element: e.display, kind: e.kind, numbers,
                    attributes: e.attributes}}"""),

        ("constraints, and the bounds they state", f"""
         FOR e IN {E}
           FILTER e.kind == "constraint" AND e.expression != null
           LIMIT 10
           RETURN {{owner: e.owner, operator: e.expression.operator,
                    expression: e.expression.text}}"""),

        ("multiplicities in use", f"""
         FOR e IN {E} FILTER e.multiplicity != null
           COLLECT text = e.multiplicity.text WITH COUNT INTO n
           SORT n DESC LIMIT 12 RETURN {{multiplicity: text, n}}"""),

        ("modifiers in use", f"""
         FOR e IN {E} FOR m IN e.modifiers
           COLLECT modifier = m WITH COUNT INTO n SORT n DESC RETURN {{modifier, n}}"""),

        ("transitions, with their triggers", f"""
         FOR r IN {D} FILTER r.label == "transitionsTo" AND r.trigger != null
           LIMIT 8 RETURN {{description: r.description, trigger: r.trigger,
                            at: CONCAT(r.source_file, ":", r.source_line)}}"""),

        ("connections between elements", f"""
         FOR r IN {D} FILTER r.label == "connectedTo"
           LIMIT 8 RETURN {{description: r.description,
                            at: CONCAT(r.source_file, ":", r.source_line)}}"""),

        ("requirements, and how many are satisfied", f"""
         FOR e IN {E} FILTER e.kind == "requirement"
           LET met = LENGTH(FOR v, x IN 1..1 INBOUND e {D}
                              FILTER x.label IN ["satisfies", "verifies"] RETURN 1)
           COLLECT AGGREGATE total = COUNT(1), satisfied = SUM(met > 0 ? 1 : 0),
                             with_short_name = SUM(e.short_name != null ? 1 : 0),
                             with_doc = SUM(e.doc != null ? 1 : 0),
                             with_rationale = SUM(e.rationale != null ? 1 : 0)
           RETURN {{total, satisfied, with_short_name, with_doc, with_rationale}}"""),

        ("one element as stored", f"""
         FOR e IN {E}
           FILTER LENGTH(ATTRIBUTES(e.attributes)) > 0 AND e.doc != null
           LIMIT 1 RETURN UNSET(e, "_rev", "_id", "description")"""),

        ("one declaration edge as stored", f"""
         FOR r IN {D} FILTER r.label == "typedBy" LIMIT 1 RETURN UNSET(r, "_rev")"""),

        ("references that point outside the corpus", f"""
         FOR e IN {E}
           FOR ref IN APPEND(e.typed_by, e.specializes)
             FILTER CONTAINS(ref, "::")
             COLLECT namespace = FIRST(SPLIT(ref, "::")) WITH COUNT INTO n
             SORT n DESC LIMIT 10
             RETURN {{namespace, references: n}}"""),

        ("layer 3: entity types and what survived the prune", f"""
         LET types = (FOR e IN {config.ENTITIES}
                        COLLECT t = e.entity_type WITH COUNT INTO n
                        SORT n DESC LIMIT 12 RETURN {{entity_type: t, n}})
         LET kinds = (FOR r IN {config.RELATIONS}
                        COLLECT t = r.type WITH COUNT INTO n SORT n DESC
                        RETURN {{type: t, n}})
         LET inferred = (FOR r IN {config.RELATIONS} FILTER r.type == "RELATED_TO"
                           COLLECT t = r.relationship_type WITH COUNT INTO n
                           SORT n DESC LIMIT 12 RETURN {{relationship_type: t, n}})
         RETURN {{entity_types: types, edge_kinds: kinds, inferred_relations: inferred}}"""),

        ("layer 3: how much of it is joined back to layer 2", f"""
         LET linked = LENGTH(FOR r IN {D} FILTER r.label == "READ_AS" RETURN 1)
         LET entities = LENGTH({config.ENTITIES})
         LET communities = LENGTH({config.COMMUNITIES})
         RETURN {{entities, linked_to_a_declaration: linked, communities}}"""),

        ("the file-level layers", f"""
         LET domains = (FOR d IN {config.DOMAINS}
                          RETURN {{cluster: d._key, module: d.module, files: d.size}})
         LET similar = FIRST(FOR s IN {config.SIMILARITIES}
                               RETURN {{fields: ATTRIBUTES(s), example: s.similarity_score}})
         RETURN {{domains, similarity_edge: similar,
                  similarity_edges: LENGTH({config.SIMILARITIES})}}"""),
    ]


FENCE = re.compile(r"```(?:aql|AQL)\s*\n(.*?)```", re.S)
BIND = re.compile(r"@@?([A-Za-z_][A-Za-z0-9_]*)")


def survey(db) -> str:
    """Run every probe and render the answers as the model's evidence.

    Truncated hard. A survey is a description of the graph, and one that runs to
    thirty pages buries the numbers that matter -- the generated half starts
    quoting row counts back instead of writing queries.
    """
    from ..corpus.model import RELATIONS

    out = []
    for heading, query in probes():
        try:
            rows = list(db.aql.execute(" ".join(query.split()),
                                       bind_vars={"all": sorted(RELATIONS)}
                                       if "@all" in query else None,
                                       max_runtime=120))
        except Exception as exc:                 # a probe is not worth failing over
            out.append(f"### {heading}\n\n(unavailable: {type(exc).__name__}: {exc})")
            continue
        # One row per line rather than pretty-printed: the same facts in a third of
        # the tokens, and the survey has to leave room for the graph it describes.
        body = "\n".join(json.dumps(row, default=str) for row in rows)
        if len(body) > 4500:
            body = body[:body.rfind("\n", 0, 4500)] + "\n... truncated"
        out.append(f"### {heading}\n\n```json\n{body}\n```")
    return "\n\n".join(out)


def ask(prompt: str, model: str, previous: list[dict] | None = None) -> tuple[str, list[dict]]:
    """One turn with the strong model, carrying the conversation so far.

    A repair round has to see what it wrote and why it failed, and a chat history is
    the cheapest way to give it both without restating the guide.
    """
    from openai import OpenAI

    messages = list(previous or [{"role": "system", "content": GUIDE}])
    messages.append({"role": "user", "content": prompt})
    reply = OpenAI(api_key=config.openai_key()).chat.completions.create(
        model=model, messages=messages).choices[0].message.content or ""
    text = reply.strip()
    # A model asked for a markdown file sometimes hands back the whole file inside
    # one fence, which would put ``` around a document that itself contains fences.
    if text.startswith("```") and not text.startswith("```aql"):
        text = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", text).strip()
    return text, messages + [{"role": "assistant", "content": reply}]


def bind_values(db) -> dict[str, Any]:
    """Plausible values for the bind parameters an example is likely to use.

    A query written as an example is a template, and a template cannot be run
    without values. These are read off the graph rather than invented, so a query
    that comes back with no rows really has come back with no rows.

    Each value is chosen to be one that can actually exercise the shape it will be
    bound into, and that is not fussiness. An example bound to a leaf returns
    nothing, gets reported as broken, and goes back to the model as a complaint --
    which then rewrites a correct query until it is wrong. So the root is the
    element with the deepest composition subtree that also has numeric values
    somewhere under it, the attribute is the one most common *inside that subtree*,
    the state is one that actually has an outgoing transition, and so on. Anything
    that still cannot be exercised is left `null`, and a query bound to a null is
    reported as unproven rather than as failing.
    """
    def one(aql: str) -> Any:
        return next(iter(db.aql.execute(aql, max_runtime=180)), None)

    E, D = config.ELEMENTS, config.DECLARATIONS
    composition = '["owns", "typedBy", "subsets", "redefines"]'

    # The best root for a rollup: the largest composition subtree that has numbers
    # in it. Ranked by how many valued elements it reaches, not by size, because a
    # package with a thousand requirements under it is the biggest subtree in most
    # corpora and the worst rollup example in all of them.
    best = one(f"""
        FOR e IN {E}
          FILTER e.children > 1 AND e.kind IN ["part", "item", "package", "action"]
          LET below = (FOR v, x, p IN 1..6 OUTBOUND e {D}
                         OPTIONS {{bfs: true, uniqueVertices: "global"}}
                         FILTER p.edges[*].label ALL IN {composition}
                         RETURN DISTINCT v)
          LET valued = (FOR v IN below
                          FOR f IN ATTRIBUTES(v.attributes)
                            FILTER IS_NUMBER(v.attributes[f].value)
                            RETURN f)
          FILTER LENGTH(valued) > 0
          SORT LENGTH(valued) DESC, LENGTH(below) DESC LIMIT 1
          RETURN {{element: e, attribute: FIRST(
                     FOR f IN valued COLLECT name = f WITH COUNT INTO n
                       SORT n DESC RETURN name)}}""")
    root = (best or {}).get("element") or {}

    return {
        "name": root.get("name") or one(
            f"FOR e IN {E} FILTER e.name != null LIMIT 1 RETURN e.name"),
        "display": root.get("display"),
        "start": root.get("_id"),
        "attribute": (best or {}).get("attribute"),
        "module": one(f"FOR m IN {config.MODULES} LIMIT 1 RETURN m.name"),
        "kind": one(f"FOR e IN {E} COLLECT k = e.kind WITH COUNT INTO n "
                    "SORT n DESC LIMIT 1 RETURN k"),
        "short": one(f"FOR e IN {E} FILTER e.short_name != null "
                     "LIMIT 1 RETURN e.short_name"),
        # A state with a transition out of it, not merely a state: most states in a
        # machine are named in transitions written on their siblings.
        "state": one(f"""FOR r IN {D} FILTER r.label == "transitionsTo"
                           LIMIT 1 RETURN r._from"""),
        "element": one(f"""FOR r IN {D} FILTER r.label == "connectedTo"
                             LIMIT 1 RETURN r._from"""),
        "requirement": one(f"FOR e IN {E} FILTER e.kind == 'requirement' AND "
                           "e.doc != null LIMIT 1 RETURN e._id"),
        "file": one(f"FOR d IN {config.SOURCES} LIMIT 1 RETURN d.filename"),
        # A search word that is really in the corpus. Taken from the most common
        # attribute name, because that fragment appears both in an attribute name
        # and -- since Layer 3 makes an entity out of most declarations -- in an
        # entity name, which is what the two search examples need.
        # A fragment that is really in a Layer 3 entity name that is joined back to
        # a declaration -- which is what the two search examples need to return
        # anything at all.
        "word": one(f"""FOR d IN {D} FILTER d.label == "READ_AS"
                          LET e = DOCUMENT(d._to)
                          FILTER LENGTH(e.entity_name) > 5
                          LIMIT 1 RETURN SUBSTRING(e.entity_name, 0, 5)""") or one(
            f"FOR e IN {E} FILTER e.name != null AND LENGTH(e.name) > 4 "
            "SORT e.children DESC LIMIT 1 RETURN SUBSTRING(e.name, 0, 4)") or "a",
        "label": "owns",
        # A real state, not merely the source of a succession: an occurrence
        # timeline uses the same edge, and an example that filters on
        # `kind == "state"` cannot be checked with a snapshot.
        "state_name": one(f"""FOR r IN {D} FILTER r.label == "transitionsTo"
                                LET a = DOCUMENT(r._from)
                                FILTER a.kind == "state"
                                LIMIT 1 RETURN a.display"""),
        "part_name": one(f"""FOR r IN {D} FILTER r.label == "connectedTo"
                               LIMIT 1 RETURN DOCUMENT(r._from).display"""),
    }


def fill(names: list[str], values: dict[str, Any]) -> dict[str, Any]:
    """Guess what each bind parameter wants from what it is called."""
    wanted: dict[str, Any] = {}
    for name in names:
        lowered = name.lower().lstrip("@")
        if name.startswith("@"):                       # @@collection
            wanted[name] = config.ELEMENTS
        elif "element" in lowered or "node" in lowered:
            wanted[name] = values["element"] or values["start"]
        elif any(w in lowered for w in ("start", "root", "from", "vertex", "_id")):
            wanted[name] = values["start"]
        elif "state" in lowered:
            wanted[name] = values["state"] or values["start"]
        elif any(w in lowered for w in ("requirement", "req")):
            wanted[name] = values["requirement"]
        elif any(w in lowered for w in ("short", "identifier")):
            wanted[name] = values["short"]
        elif any(w in lowered for w in ("attribute", "property", "quantity", "measure")):
            wanted[name] = values["attribute"]
        elif any(w in lowered for w in ("module", "model")):
            wanted[name] = values["module"]
        elif any(w in lowered for w in ("kind", "type")):
            wanted[name] = values["kind"]
        elif "label" in lowered:
            wanted[name] = values["label"]
        elif "file" in lowered:
            wanted[name] = values["file"]
        elif any(w in lowered for w in ("word", "term", "search", "text")):
            wanted[name] = values["word"]
        else:
            wanted[name] = values["name"]
    return wanted


# The hand-written half cannot know a single real name, so its examples carry a
# placeholder where the value from the question goes. They are swapped for values
# read off the graph before a query is run, which is the only way to check that the
# shape around them is right.
PLACEHOLDERS = {
    "<NAME>": "name", "<ELEMENT>": "display", "<STATE>": "state_name",
    "<PART>": "part_name", "<ATTRIBUTE>": "attribute", "<WORD>": "word",
}


def substitute(query: str, values: dict[str, Any]) -> str:
    for token, key in PLACEHOLDERS.items():
        if token in query:
            query = query.replace(token, str(values.get(key) or ""))
    return query


def check(db, markdown: str, values: dict[str, Any] | None = None) -> list[dict]:
    """Parse, run and count every ```aql block in a document.

    A query that writes is refused rather than run. This is not a formality: an
    earlier version of this step ran every block it found, a draft included one that
    wrote, and it emptied most of the graph it was describing. Nothing a model wrote
    reaches the database without clearing `config.MUTATION` first.
    """
    values = values if values is not None else bind_values(db)
    results = []
    for i, block in enumerate(FENCE.findall(markdown), start=1):
        query = substitute(block.strip(), values)
        row = {"n": i, "query": query, "parses": False, "runs": False, "rows": 0,
               "error": None, "binds": sorted(set(BIND.findall(query)))}
        writes = config.MUTATION.search(query)
        if writes:
            row["error"] = (f"refused: writes to the graph ({writes.group(0).upper()}). "
                            "Every example must be a read-only query.")
            results.append(row)
            continue
        try:
            db.aql.validate(query)
            row["parses"] = True
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            results.append(row)
            continue
        binds = fill([("@" + n) if f"@@{n}" in query else n for n in row["binds"]], values)
        try:
            rows = list(db.aql.execute(query, bind_vars=binds or None, max_runtime=90))
            row["runs"], row["rows"] = True, len(rows)
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        results.append(row)
    return results


def invented(db, markdown: str) -> list[str]:
    """Collection names the document uses that the database does not have.

    Prose is not executable, so `check` cannot see it, and a wrong name written in a
    heading is followed just as readily as one written in a query.
    """
    real = {c["name"] for c in db.collections()}
    used = set(re.findall(rf"\b{config.PROJECT}_[A-Za-z][A-Za-z0-9_]*", markdown))
    return sorted(used - real)


def report(results: list[dict], missing: list[str] | None = None) -> str:
    ran = sum(1 for r in results if r["runs"])
    empty = sum(1 for r in results if r["runs"] and not r["rows"])
    broken = [r for r in results if not r["parses"]]
    line = (f"{len(results)} queries, {ran} run, {len(broken)} do not parse, "
            f"{ran - empty} return rows, {empty} return nothing")
    if missing:
        line += f", {len(missing)} invented collection names ({', '.join(missing)})"
    return line


def complaints(results: list[dict], missing: list[str] | None = None) -> str:
    """What to hand back to the model: the failures, with the query and the error."""
    lines = []
    for name in missing or []:
        lines.append(f"`{name}` is not a collection in this database. Nothing may "
                     "refer to it -- not a query, not a heading, not a sentence.")
    for r in results:
        if r["runs"] and r["rows"]:
            continue
        why = r["error"] or ("runs, but returns no rows -- an assumption in it is "
                             "wrong, usually a literal that is not in the graph")
        lines.append(f"Query {r['n']}:\n```aql\n{r['query']}\n```\n{why}")
    return "\n\n".join(lines)


def base() -> str:
    return config.AQL_EXAMPLES.read_text(encoding="utf-8")


def build(db=None, model: str | None = None, rounds: int = 2,
          out: Path | None = None) -> dict:
    """Survey, generate the corpus-specific half, check it, repair it, write both."""
    db = db if db is not None else config.db()
    model = model or config.EXAMPLES_MODEL
    out = out or config.AQL_EXAMPLES_BUILT

    values = bind_values(db)
    handwritten = base()
    facts = survey(db)

    # The hand-written half is checked too, and against the same graph. It claims
    # to be corpus-agnostic, which is exactly the claim that is easy to get wrong:
    # a shape that only works on the corpus it was written beside is the failure
    # this whole arrangement exists to prevent.
    first = check(db, handwritten, values)

    addendum, history = ask(
        "Here is the first half, already written:\n\n"
        f"{handwritten}\n\n---\n\nAnd here is the survey of the graph, run just "
        f"now:\n\n{facts}\n\nWrite the second half.", model)
    results, missing = check(db, addendum, values), invented(db, addendum)
    rounds_of = [report(results, missing)]

    for _ in range(rounds):
        bad = complaints(results, missing)
        if not bad:
            break
        addendum, history = ask(
            "Every query in your half was run against the graph. These did not "
            f"work:\n\n{bad}\n\nReturn your WHOLE half again with those fixed -- "
            "same structure, same prose, only what is listed above changed. A query "
            "that returns no rows is filtering on something that is not in the "
            "graph; consult the survey for what is. Markdown only.",
            model, history)
        results, missing = check(db, addendum, values), invented(db, addendum)
        rounds_of.append(report(results, missing))

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(handwritten + SEPARATOR + addendum, encoding="utf-8")
    return {"path": out, "model": model, "addendum": addendum, "survey": facts,
            "handwritten": first, "results": results, "invented": missing,
            "rounds": rounds_of}


def main(model: str | None = None, rounds: int = 2, out: Path | None = None,
         check_only: bool = False) -> None:
    db = config.db()
    if check_only:
        text = (config.AQL_EXAMPLES_BUILT if config.AQL_EXAMPLES_BUILT.is_file()
                else config.AQL_EXAMPLES).read_text(encoding="utf-8")
        results = check(db, text)
        print(f"  {report(results, invented(db, text))}")
        for r in results:
            if not (r["runs"] and r["rows"]):
                print(f"\n  query {r['n']}: {r['error'] or 'no rows'}\n"
                      + "\n".join("    " + line for line in r["query"].splitlines()))
        return

    built = build(db=db, model=model, rounds=rounds, out=out)
    print(f"  hand-written  {report(built['handwritten'])}")
    print(f"  {built['model']} wrote {len(built['addendum']):,} characters "
          f"from a {len(built['survey']):,}-character survey")
    for i, line in enumerate(built["rounds"]):
        print(f"  {'generated' if not i else f'repair {i}':>12}  {line}")
    print(f"  {built['path']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default=None, help=f"default {config.EXAMPLES_MODEL}")
    ap.add_argument("--rounds", type=int, default=2, help="repair rounds after the check")
    ap.add_argument("--out", type=Path, default=None,
                    help=f"default {config.AQL_EXAMPLES_BUILT}")
    ap.add_argument("--check-only", action="store_true",
                    help="run the queries in the existing file and report")
    args = ap.parse_args()
    main(model=args.model, rounds=args.rounds, out=args.out, check_only=args.check_only)
