"""Prove that every relation label the vocabulary declares is actually produced.

`RELATIONS` is a promise: it is what `config.relation_types()` hands the extractor,
what the AQL examples document as traversable, and what a question is answered
from. A label in it that no syntax produces is worse than a missing one -- a query
filtering on it runs, returns nothing, and reads as "the model has none of those".

So this parses `coverage.sysml`, which is written to exercise every form once, and
reports three things:

  unproduced   a label in RELATIONS that the fixture never yields. Either the
               fixture is missing that form or nothing emits it. Both are bugs.
  undeclared   a label emitted but not in RELATIONS, which would reach the graph
               undocumented.
  keyword-named elements, the signature of a construct the parser does not know:
               a keyword ends up as the name of an element, so a file that says
               `unions` gets a part called "unions". Anything here is a false row,
               not a missing one.

    python -m sysml.pipeline.corpus.check
    python -m sysml.pipeline.corpus.check --verbose     # every label and its count
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from . import model, parse
from .model import RELATIONS

FIXTURE = Path(__file__).resolve().parent / "coverage.sysml"

# Words that are syntax, never a name. An element named one of these means the
# parser read a keyword as an identifier.
KEYWORDS = (
    set(parse.RELATIONSHIP_PREFIXES)
    | set(parse.SPECIALIZATION_WORDS)
    | parse.TAIL_WORDS
    | parse.MODIFIERS
    | parse.TRAILING_MODIFIERS
    | {word for phrase, _ in parse.TYPE_OPERATORS for word in phrase}
    | {word for phrase, _ in parse.KEYWORD_PHRASES for word in phrase}
)


def run(path: Path | None = None) -> dict:
    root = (path or FIXTURE).parent
    trees = {(path or FIXTURE).name: parse.parse_file(path or FIXTURE, root)}
    built = model.build(trees)
    counts = Counter(fact.type for fact in built.relations)
    named_after_keyword = sorted(
        {f"{e.kind}/{e.name} at line {e.line}" for e in built.elements
         if e.name and e.name in KEYWORDS})
    return {
        "counts": counts,
        "unproduced": sorted(label for label in RELATIONS if not counts[label]),
        "undeclared": sorted(label for label in counts if label not in RELATIONS),
        "keyword_named": named_after_keyword,
        "elements": len(built.elements),
        "relations": len(built.relations),
        "unresolved": [u for u in built.unresolved if not u.get("external")],
    }


def report(verbose: bool = False, path: Path | None = None) -> int:
    result = run(path)
    print(f"  {result['elements']} elements, {result['relations']} relations, "
          f"{len(result['counts'])}/{len(RELATIONS)} labels produced")

    if verbose:
        for label in sorted(RELATIONS):
            print(f"    {result['counts'][label]:>4}  {label}")

    ok = True
    for name, items in (("no syntax produces", result["unproduced"]),
                        ("not in RELATIONS", result["undeclared"]),
                        ("keyword read as a name", result["keyword_named"]),
                        ("unresolved inside the fixture",
                         [f"{u['type']} {u['reference']} at line {u['line']}"
                          for u in result["unresolved"]])):
        if items:
            ok = False
            print(f"\n  {name} ({len(items)}):")
            for item in items:
                print(f"    {item}")

    print("\n  " + ("every label is produced and no keyword became a name"
                    if ok else "FAILED"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verbose", action="store_true",
                    help="every label and its count")
    ap.add_argument("--file", type=Path, default=None,
                    help="a fixture other than the built-in")
    args = ap.parse_args()
    return report(args.verbose, args.file)


if __name__ == "__main__":
    sys.exit(main())
