"""Build the whole graph: corpus (L1/L2) -> extract -> load (L3) -> examples.

    python build.py                # everything
    python build.py --reset        # drop the database first
    python build.py --only corpus  # one step
    python build.py --no-l3        # stop after Layers 1 and 2

Needs a local ArangoDB with the experimental vector index enabled:

    docker run -d --name christian-webb-drone-arango -p 8529:8529 \
      -e ARANGO_ROOT_PASSWORD=testpass arangodb:3.12.9.4 \
      arangod --experimental-vector-index=true

and an OpenAI key as CHAT_API_KEY in the `env` file one directory up (an exported
CHAT_API_KEY / OPENAI_API_KEY also works).

`check` runs first and needs nothing: it parses a fixture written to use every
relationship form once and reports any label in the vocabulary that no syntax
produces, plus any element that ended up named after a keyword. Both are silent
faults otherwise -- a label nothing emits answers "none of those" to every question
about it, and a keyword read as a name inserts a row that is false rather than
missing.

The order is the point. `corpus` parses the sources and writes everything the
syntax states -- every element, every value, every relation, with a file and a
line. It needs no language model beyond an embedding of each file, it is the same
on every run, and it is complete before anything is asked to interpret anything.
Only then does `extract` read the same files for meaning and `load` import that as
Layer 3, joined back to the declarations it is a reading of. A question with a
definite answer never touches Layer 3.

`extract` caches every LLM answer in out/kg, so a re-run over unchanged sources
costs nothing. `--no-l3` skips both LLM steps entirely, which is the fast path when
only the parse has changed.

`examples` writes out/aql_examples.md: the hand-written, corpus-agnostic examples
plus a second half generated from a survey of the graph just built. `probe` then
asks the graph questions in English and checks the answers against the graph.
"""

from __future__ import annotations

import argparse
import time

from sysml import config

STEPS = ("check", "corpus", "extract", "load", "examples", "probe")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true", help="drop the database first")
    ap.add_argument("--only", choices=STEPS, default=None, help="run one step")
    ap.add_argument("--no-l3", dest="l3", action="store_false",
                    help="skip extraction and the Layer 3 import")
    ap.add_argument("--no-examples", dest="examples", action="store_false",
                    help="skip writing the AQLizer examples")
    ap.add_argument("--no-probe", dest="probe", action="store_false",
                    help="skip the English-question check")
    args = ap.parse_args()

    if args.reset and config.drop_database():
        print(f"dropped {config.DB_NAME}")

    wanted = [args.only] if args.only else [
        step for step in STEPS
        if (step not in ("extract", "load") or args.l3)
        and (step != "examples" or args.examples)
        and (step != "probe" or args.probe)]

    started = time.time()
    for step in wanted:
        print(f"\n== {step} ==")
        if step == "check":
            from sysml.pipeline.corpus import check
            check.report()
        elif step == "corpus":
            from sysml.pipeline.corpus import write
            write.main()
        elif step == "extract":
            from sysml.pipeline.knowledge import extract
            extract.main()
        elif step == "load":
            from sysml.pipeline.knowledge import load
            load.main()
        elif step == "examples":
            from sysml.pipeline.examples import generate
            generate.main()
        elif step == "probe":
            from sysml.pipeline.examples import probe
            probe.main()

    print(f"\ndone in {time.time() - started:.0f}s -- database {config.DB_NAME}")


if __name__ == "__main__":
    main()
