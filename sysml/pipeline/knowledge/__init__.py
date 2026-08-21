"""Layer 3: what the sources mean, which only a language model can say.

  extract   the source files -> out/kg, graphrag_importer's own LLM extraction.
            `prompts` holds the ontology and the prompts it runs. This is the
            only expensive step; every answer is cached, so a re-run over
            unchanged sources is free.
  load      out/kg -> Layer 3 in ArangoDB, then joined back to Layer 2. Every
            entity that names exactly one declaration gets a READ_AS edge to it,
            and every inferred relation that merely restates a fact Layer 2
            already holds is dropped.
  analogy   cross-model similarity edges over the extracted entities. Carried
            over unchanged and not wired into build.py.

This runs after `..corpus`, never before. A question with a definite answer is
answered from Layer 2 and never depends on an extraction having run.
"""
