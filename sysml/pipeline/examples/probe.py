"""Ask the graph questions in English and check the answers against the graph.

`generate` proves that every query it writes runs and returns rows.
That is a lower bar than it sounds, and passing it says nothing about the thing
the examples exist for: whether a question asked in English comes back with the
right number.

So this asks. Each case is a question, an AQL query that computes the truth
directly, and a test on the answer. The truth query is written here rather than
generated, and it is deliberately not the query the examples teach -- if both were
wrong in the same way the test would pass.

The cases are written without naming anything in any particular corpus: each one
finds its own subject by querying for it first (the element with the largest
composition subtree, the most common numeric attribute, a requirement with no
inbound satisfaction). So the same suite runs against a corpus loaded next week
and still asks a hard question of it.

    python -m sysml.pipeline.examples.probe
    python -m sysml.pipeline.examples.probe --bare        # the same questions, no examples
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ... import config

COMPOSITION = '["owns", "typedBy", "subsets", "redefines"]'


@dataclass
class Case:
    name: str
    question: Callable[[dict], str]
    truth: Callable[[Any, dict], Any]
    passes: Callable[[str, list, Any], bool]
    why: str = ""


def subjects(db) -> dict[str, Any]:
    """What the questions are about, found by asking the graph rather than by
    knowing the corpus."""
    E, D = config.ELEMENTS, config.DECLARATIONS

    def one(aql: str, **binds) -> Any:
        return next(iter(db.aql.execute(aql, bind_vars=binds or None, max_runtime=180)), None)

    # The element whose composition subtree holds the most numeric values of one
    # name -- the natural subject of a "what is the total X of Y" question.
    best = one(f"""
        FOR e IN {E}
          FILTER e.children > 1
          LET below = (FOR v, x, p IN 1..8 OUTBOUND e {D}
                         OPTIONS {{bfs: true, uniqueVertices: "global"}}
                         FILTER p.edges[*].label ALL IN {COMPOSITION}
                         RETURN DISTINCT v)
          LET counted = (FOR v IN below FOR f IN ATTRIBUTES(v.attributes)
                           FILTER IS_NUMBER(v.attributes[f].value)
                           COLLECT name = f WITH COUNT INTO n
                           SORT n DESC LIMIT 1 RETURN {{name, n}})
          FILTER LENGTH(counted) > 0 AND FIRST(counted).n > 2
          SORT FIRST(counted).n DESC LIMIT 1
          RETURN {{root: e, attribute: FIRST(counted).name, contributors: FIRST(counted).n}}""")

    unsatisfied = one(f"""
        FOR r IN {E} FILTER r.kind == "requirement" AND r.doc != null
          LET met = LENGTH(FOR v, x IN 1..1 INBOUND r {D}
                             FILTER x.label IN ["satisfies", "verifies"] RETURN 1)
          FILTER met == 0 LIMIT 1 RETURN r""")
    satisfied = one(f"""
        FOR x IN {D} FILTER x.label == "satisfies"
          LIMIT 1 RETURN DOCUMENT(x._to)""")
    transition = one(f"""
        FOR x IN {D} FILTER x.label == "transitionsTo" AND x.trigger != null
          LIMIT 1 RETURN {{state: DOCUMENT(x._from), trigger: x.trigger}}""")
    identified = one(f"FOR e IN {E} FILTER e.short_name != null LIMIT 1 RETURN e")
    connected = one(f"""
        FOR x IN {D} FILTER x.label == "connectedTo"
          LIMIT 1 RETURN DOCUMENT(x._from)""")

    # An element that really owns several parts, for the containment question. The
    # rollup root usually is not one: the subtree with the most numbers under it is
    # often reached through typing rather than by owning anything directly.
    container = one(f"""
        FOR e IN {E}
          LET parts = LENGTH(FOR v, x IN 1..1 OUTBOUND e {D}
                               FILTER x.label == "owns" AND v.kind == "part" RETURN 1)
          FILTER parts >= 3
          SORT parts DESC LIMIT 1 RETURN e""")

    return {"best": best, "unsatisfied": unsatisfied, "satisfied": satisfied,
            "transition": transition, "identified": identified,
            "connected": connected, "container": container}


def numbers_in(text: str) -> set[float]:
    """Every number a summary states, with separators removed.

    A model writes 188650 as `188,650`, `188 650`, `188650 kg` and
    `1.8865e5`; all of them are the same claim and all of them have to count as a
    match, or the test measures formatting.
    """
    found: set[float] = set()
    for token in re.findall(r"-?\d[\d,_ ]*\.?\d*(?:[eE][-+]?\d+)?", text):
        cleaned = token.replace(",", "").replace("_", "").replace(" ", "")
        try:
            found.add(float(cleaned))
        except ValueError:
            continue
    return found


def close(found: set[float], wanted: float, tolerance: float = 0.005) -> bool:
    return any(abs(v - wanted) <= max(abs(wanted) * tolerance, 0.5) for v in found)


def cases() -> list[Case]:
    E, D = config.ELEMENTS, config.DECLARATIONS

    def rollup_question(s: dict) -> str:
        root, attribute = s["best"]["root"], s["best"]["attribute"]
        return (f"What is the total {attribute} of everything that makes up "
                f"{root['display']}? Give me the sum.")

    def rollup_truth(db, s: dict) -> Any:
        root, attribute = s["best"]["root"], s["best"]["attribute"]
        return next(iter(db.aql.execute(f"""
            LET sub = (FOR v, x, p IN 0..8 OUTBOUND @root {D}
                         OPTIONS {{bfs: true, uniqueVertices: "global"}}
                         FILTER p.edges[*].label ALL IN {COMPOSITION}
                         RETURN DISTINCT v)
            FOR v IN sub FOR f IN ATTRIBUTES(v.attributes)
              LET a = v.attributes[f]
              FILTER f == @attribute AND IS_NUMBER(a.value)
              COLLECT unit = a.unit AGGREGATE total = SUM(a.value), n = LENGTH(1)
              RETURN {{unit, total, contributors: n}}""",
            bind_vars={"root": root["_id"], "attribute": attribute},
            max_runtime=180)), None)

    def count_question(s: dict) -> str:
        return ("How many requirements are declared in the model, and how many of "
                "them are satisfied or verified by something?")

    def count_truth(db, s: dict) -> Any:
        return next(iter(db.aql.execute(f"""
            FOR r IN {E} FILTER r.kind == "requirement"
              LET met = LENGTH(FOR v, x IN 1..1 INBOUND r {D}
                                 FILTER x.label IN ["satisfies", "verifies"] RETURN 1)
              COLLECT AGGREGATE total = LENGTH(1), satisfied = SUM(met > 0 ? 1 : 0)
              RETURN {{total, satisfied}}""", max_runtime=180)), None)

    def parts_question(s: dict) -> str:
        return f"What parts are declared directly inside {s['container']['display']}? List them."

    def parts_truth(db, s: dict) -> Any:
        return list(db.aql.execute(f"""
            FOR v, x IN 1..1 OUTBOUND @root {D}
              FILTER x.label == "owns" AND v.kind == "part"
              RETURN v.display""",
            bind_vars={"root": s["container"]["_id"]}, max_runtime=180))

    def where_question(s: dict) -> str:
        e = s["identified"]
        return (f"Where is {e['short_name']} declared -- which file and which line?")

    def where_truth(db, s: dict) -> Any:
        e = s["identified"]
        return {"file": e["source_file"], "line": e["source_line"],
                "name": e["display"]}

    def satisfies_question(s: dict) -> str:
        r = s["satisfied"]
        return f"What satisfies the requirement {r['display']}?"

    def satisfies_truth(db, s: dict) -> Any:
        return list(db.aql.execute(f"""
            FOR v, x IN 1..1 INBOUND @req {D}
              FILTER x.label == "satisfies" RETURN v.display""",
            bind_vars={"req": s["satisfied"]["_id"]}, max_runtime=180))

    def transition_question(s: dict) -> str:
        t = s["transition"]
        return (f"When the system is in the state {t['state']['display']}, what "
                f"event moves it to another state, and which state does it go to?")

    def transition_truth(db, s: dict) -> Any:
        return list(db.aql.execute(f"""
            FOR v, x IN 1..1 OUTBOUND @state {D}
              FILTER x.label == "transitionsTo"
              RETURN {{to: v.display, trigger: x.trigger}}""",
            bind_vars={"state": s["transition"]["state"]["_id"]}, max_runtime=180))

    def unsatisfied_question(s: dict) -> str:
        return ("Which requirements are not satisfied or verified by anything? "
                "Just tell me how many.")

    def unsatisfied_truth(db, s: dict) -> Any:
        return next(iter(db.aql.execute(f"""
            RETURN LENGTH(FOR r IN {E} FILTER r.kind == "requirement"
                            LET met = LENGTH(FOR v, x IN 1..1 INBOUND r {D}
                                               FILTER x.label IN ["satisfies", "verifies"]
                                               RETURN 1)
                            FILTER met == 0 RETURN 1)""", max_runtime=180)), None)

    def biggest_question(s: dict) -> str:
        a = s["best"]["attribute"]
        return f"Which element has the largest {a}, and what is its value?"

    def biggest_truth(db, s: dict) -> Any:
        return next(iter(db.aql.execute(f"""
            FOR e IN {E} FOR f IN ATTRIBUTES(e.attributes)
              LET a = e.attributes[f]
              FILTER f == @attribute AND IS_NUMBER(a.value)
              SORT a.value DESC LIMIT 1
              RETURN {{element: e.display, value: a.value, unit: a.unit}}""",
            bind_vars={"attribute": s["best"]["attribute"]}, max_runtime=180)), None)

    return [
        Case("rollup", rollup_question, rollup_truth,
             lambda text, rows, truth: bool(truth) and close(numbers_in(text), truth["total"]),
             "the sum over a composition subtree, which needs the typing hop"),
        Case("counts", count_question, count_truth,
             lambda text, rows, truth: bool(truth) and close(numbers_in(text), truth["total"])
             and close(numbers_in(text), truth["satisfied"]),
             "two counts in one answer, both computed in AQL"),
        Case("children", parts_question, parts_truth,
             lambda text, rows, truth: bool(truth) and sum(
                 1 for name in truth if name.split("_")[-1].lower() in text.lower()
             ) >= max(1, len(truth) // 2),
             "one hop on a named label"),
        Case("provenance", where_question, where_truth,
             lambda text, rows, truth: truth["file"].split("/")[-1] in text
             and str(truth["line"]) in text,
             "the short-name lookup, and the file and line"),
        Case("satisfies", satisfies_question, satisfies_truth,
             lambda text, rows, truth: bool(truth) and any(
                 name.split("_")[-1].lower() in text.lower() for name in truth),
             "an inbound edge, against the direction the English suggests"),
        Case("transition", transition_question, transition_truth,
             lambda text, rows, truth: bool(truth) and any(
                 (t["trigger"] or "").lower() in text.lower() for t in truth),
             "a field on the edge rather than on either vertex"),
        Case("anti-join", unsatisfied_question, unsatisfied_truth,
             lambda text, rows, truth: truth is not None and close(numbers_in(text), truth),
             "an absent inbound edge, counted"),
        Case("extreme", biggest_question, biggest_truth,
             lambda text, rows, truth: bool(truth)
             and truth["element"].split("_")[-1].lower() in text.lower()
             and close(numbers_in(text), truth["value"]),
             "ranking by a value inside a map"),
    ]


def run(with_examples: bool = True, only: str | None = None) -> list[dict]:
    from ... import nl

    db = config.db()
    found = subjects(db)
    aqlizer = nl.instance()
    out = []
    for case in cases():
        if only and case.name != only:
            continue
        try:
            question = case.question(found)
            truth = case.truth(db, found)
        except Exception as exc:
            out.append({"case": case.name, "skipped": f"{type(exc).__name__}: {exc}"})
            continue
        answer = aqlizer.ask(question, with_examples=with_examples)
        text = answer.answer or ""
        try:
            ok = bool(case.passes(text, answer.rows, truth))
        except Exception:
            ok = False
        out.append({"case": case.name, "question": question, "truth": truth,
                    "answer": text, "aql": answer.aql, "rows": len(answer.rows or []),
                    "error": answer.error, "ok": ok, "why": case.why})
    return out


def main(with_examples: bool = True, only: str | None = None,
         verbose: bool = False) -> None:
    results = run(with_examples=with_examples, only=only)
    passed = sum(1 for r in results if r.get("ok"))
    print(f"  {passed}/{len(results)} correct "
          f"({'with examples' if with_examples else 'no examples'})\n")
    for r in results:
        if "skipped" in r:
            print(f"  SKIP  {r['case']}: {r['skipped']}")
            continue
        print(f"  {'PASS' if r['ok'] else 'FAIL'}  {r['case']:<12} {r['why']}")
        print(f"        Q  {r['question']}")
        print(f"        truth  {json.dumps(r['truth'], default=str)[:160]}")
        print(f"        said   {(r['answer'] or r['error'] or '').strip()[:300]}")
        if verbose or not r["ok"]:
            aql = (r["aql"] or "").strip()
            print("        aql    " + "\n               ".join(aql.splitlines()))
        print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bare", action="store_true",
                    help="ask without the examples, to see what they are worth")
    ap.add_argument("--only", default=None, help="run one case by name")
    ap.add_argument("--verbose", action="store_true", help="show the AQL for passes too")
    args = ap.parse_args()
    main(with_examples=not args.bare, only=args.only, verbose=args.verbose)
