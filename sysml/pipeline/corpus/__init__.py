"""Layers 1 and 2: SysML source text -> the corpus graph. `write.main()` is the step.

  lex     characters -> tokens. Knows the operators, keeps block comments so a
          `doc /* */` survives, and strips the quotes off a quoted name.
  parse   tokens -> a tree of `Element`. Every declaration keyword, every
          specialisation operator, every relationship statement, multiplicities,
          modifiers, metadata annotations and expressions. Each element carries
          the file, line and column it was written at.
  model   the tree -> resolved facts. Resolves every reference the way SysML
          scopes it -- enclosing namespace outward, through inheritance, through
          imports, feature chains walking typing rather than containment -- and
          emits one `Fact` per relation the syntax states.
  write   those facts -> ArangoDB, using autograph's own classes: the modules,
          one source row per file, the similarity edges and the Leiden domains,
          plus the two collections this project adds -- `{project}_elements` and
          `{project}_declarations` -- spliced into the same `{project}_CorpusGraph`.
          autograph's shipped documents have a closed field set with nowhere to
          put per-element structure, which is why the extra collections exist.
  check   parses `coverage.sysml`, a fixture that uses every relationship form
          once, and fails if any label in the vocabulary has no syntax producing
          it, or if any element came out named after a keyword. Both faults are
          otherwise silent.

Nothing here interprets anything. Everything written is a function of the text, is
the same on every run, and every row can be pointed back at the line that stated
it. The only language model involved embeds each file, which is what autograph's
similarity search needs.
"""
