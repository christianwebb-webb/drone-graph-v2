"""What the question-answering side is told about the graph, and how it is checked.

  aql_examples.md   the hand-written half. Corpus-agnostic: it may describe the
                    collections, the fields, the labels and how to traverse them,
                    because all of that follows from the parse, but it may not
                    mention anything about the models that happen to be loaded.
                    Its example queries use placeholders, never bind parameters.
  generate          surveys the graph that was just built, has a model write a
                    second half from real names in it, runs and repairs every
                    query in both halves, and writes out/aql_examples.md. That
                    built file is the only one the read side loads.
  probe             asks the graph questions in English and checks each answer
                    against a query written independently of the one the model
                    produces. `--bare` runs the same questions with the
                    examples withheld, which is what makes the score mean
                    anything.

Reading the graph itself is `sysml.nl`, which is not part of the build.
"""
