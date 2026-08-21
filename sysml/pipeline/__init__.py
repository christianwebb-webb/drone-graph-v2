"""The three stages that turn SysML source text into a graph you can question.

  corpus/     the text -> Layers 1 and 2. lex, parse and model read the files into
              a resolved model with no database and no language model involved;
              write puts it in Arango -- the modules, one source row per file, the
              similarity edges and the Leiden domains, and every declaration and
              relation the syntax states, each with a file and a line.
  knowledge/  the same files -> Layer 3, read for meaning by a language model,
              then joined back to Layer 2 by READ_AS and pruned of anything that
              merely restates a Layer 2 fact.
  examples/   the finished graph -> out/aql_examples.md, the worked AQL the
              question answerer is shown, plus the check that asks in English and
              verifies the answer against the graph.

The order is the argument. Layer 2 is complete, exact and reproducible before
anything is asked to interpret anything, so a question with a definite answer
never depends on an extraction having run. `build.py --no-l3` stops after
`corpus`, which is the fast path when only the parse has changed.

Reading the graph afterwards is `sysml.nl`, which is not part of this.
"""
