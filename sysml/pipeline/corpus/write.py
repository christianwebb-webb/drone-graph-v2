"""models/*.sysml -> Layers 1 and 2, with nothing guessed.

This is the step the previous version of this project did not have, and its
absence is why so much of what the files state ended up either in a language
model's paraphrase or nowhere.

What it builds, in the platform's own shape:

  L1  `{project}_modules`      one module per model. The boundary nothing
                               crosses: no similarity edge between two, no
                               clustering across them, and no name in one
                               resolving against the other.
  L2  `{project}_sources`      one row per .sysml file, embedded once, the way
                               autograph's corpus builder writes any document.
      `{project}_similarities` SIMILAR_TO, found by autograph's own
                               SimilarityFinder -- vector kNN and BM25 fused.
      `{project}_domains`      the Leiden clusters of those files, by autograph's
                               own clustering, one run per module.
      `{project}_elements`     every declaration the parser read, with its values,
                               its multiplicity, its modifiers, its documentation
                               and the line it was written on.
      `{project}_declarations` every relation the syntax states, typed and
                               directed, with the line it was read from.

The last two are this project's addition and not autograph's. autograph's corpus
graph has one row per file and a closed set of fields, which is right for a corpus
of prose: there is nothing inside a PDF a parser could state, so the only
structure available at L2 is between whole documents. A .sysml file is not prose.
Every element, every value and every relation in it is written down exactly, and
that is L2 material by each property that makes L2 what it is -- deterministic, no
model involved, re-derivable from the file alone, and true before anything reads
it for meaning. So it goes in beside the sources, in the same named graph, under
the same naming convention.

One model is used in this step and it is an embedding model, on the sources only,
because that is what autograph's similarity search reads. Nothing here asks a
model what anything means, and no row below `sources` has a vector at all.

    python -m sysml.pipeline.corpus.write
"""

from __future__ import annotations

import json
import logging
import sys
from collections import defaultdict
from typing import Any

from ... import config
from . import model as sysml_model
from .model import Fact, Model, attributes_of, key_of, value_of
from .parse import Element

# What autograph reads off a source row, and what it truncates to. Its corpus
# builder embeds the first CHUNK_MAX_CHARS of a document and nothing else, so a
# file longer than that is compared on its head. That is autograph's design and
# not something to work around here: the whole file is still stored, and the part
# of it that matters for *structure* is in `elements`, which has no such bound.
CONTENT_LIMIT = 4800


def autograph():
    """autograph's corpus-graph classes, pointed at the local container.

    Its config classes read the environment in their class bodies, so the
    environment has to be set before the import rather than after it.
    """
    config.autograph_env()

    from corpus_graph.datastorage import DataStorage
    from corpus_graph.graph_builder import GraphBuilder
    from corpus_graph.leiden_algorithm import LeidenAlgorithm
    from corpus_graph.similarity_finding import SimilarityFinder

    for name in ("corpus_graph", "arango-graphrag", "vectordb"):
        log = logging.getLogger(name)
        log.handlers = [logging.StreamHandler(sys.stderr)]
        log.propagate = False
    return DataStorage, GraphBuilder, LeidenAlgorithm, SimilarityFinder


def embed(texts: list[str]) -> list[list[float]]:
    """The same embedding every other vector in this database was made with."""
    from openai import OpenAI

    client = OpenAI(api_key=config.openai_key())
    out: list[list[float]] = []
    for i in range(0, len(texts), 64):
        batch = client.embeddings.create(model=config.EMBED_MODEL, input=texts[i:i + 64])
        out.extend(d.embedding for d in batch.data)
    return out


def sources() -> dict[str, dict]:
    """Every .sysml file, in the shape autograph's `insert_documents_bulk` reads.

    `file_id` is the key a delete matches on and `citable_url` is what a citation
    in a retrieved answer resolves to, so both are filled even though nothing here
    deletes or cites -- they are the two fields that are impossible to add later
    without rebuilding.
    """
    rows: dict[str, dict] = {}
    for path in sorted(config.MODELS.rglob("*.sysml")):
        relative = path.relative_to(config.MODELS).as_posix()
        text = path.read_text(encoding="utf-8")
        rows[relative] = {
            "filename": relative,
            "content": text[:CONTENT_LIMIT],
            "citable_url": f"models/{relative}",
            "file_id": f"sysml:{relative}",
            "module": config.model_of(relative),
            "lines": text.count("\n") + 1,
            "characters": len(text),
        }
    return rows


def write_modules(db, rows: dict[str, dict]) -> int:
    """L1. One row per model, listing the files in it.

    autograph writes this collection when a build is spread over several File
    Manager scopes; here the scope is the model, which is the same thing for the
    same reason -- it is the unit that may not be mixed with another.
    """
    if not db.has_collection(config.MODULES):
        db.create_collection(config.MODULES)
    by_module: dict[str, list[str]] = defaultdict(list)
    for relative, row in rows.items():
        by_module[row["module"]].append(relative)
    docs = [{"_key": f"module_{name}", "name": name,
             "files": sorted(files), "clusters": []}
            for name, files in sorted(by_module.items())]
    coll = db.collection(config.MODULES)
    coll.truncate()
    coll.import_bulk(docs, on_duplicate="replace")
    return len(docs)


def write_sources(db, DataStorage, rows: dict[str, dict]) -> dict[str, str]:
    """L2. The files themselves, embedded, through autograph's own writer.

    Going through `insert_documents_bulk` rather than writing the rows here is the
    point of the exercise: the `_key` it derives (`doc_<sha of module:filename>`)
    is what makes a re-run an update, and it is the key autograph's own deletion,
    staleness and similarity code all expect to find.
    """
    storage = DataStorage(db)
    storage.create_collections(drop_existing=True)
    coll = db.collection(config.SOURCES)

    vectors = embed([row["content"] for row in rows.values()])
    payload = {relative: {**row, "embedding": vector}
               for (relative, row), vector in zip(rows.items(), vectors)}
    ids = storage.insert_documents_bulk(coll, payload)

    # `insert_documents_bulk` writes a closed field set, so the two counts this
    # project cares about are added afterwards rather than smuggled in.
    coll.import_bulk(
        [{"_key": ids[relative].split("/")[1], "lines": row["lines"],
          "characters": row["characters"]}
         for relative, row in rows.items() if relative in ids],
        on_duplicate="update")

    storage.create_vector_index(coll)
    storage.create_arangosearch_view(coll, token=config.token())
    return ids


def write_similarities(db, SimilarityFinder, rows: dict[str, dict],
                       ids: dict[str, str], top_k: int = 8) -> int:
    """L2. SIMILAR_TO between files, inside a module and never across one.

    `module_doc_ids` is autograph's own way of confining the search, and it is
    applied before the fusion so a foreign document cannot consume the top_k
    window. Confining it is not an optimisation: a similarity edge between the
    Apollo mission and a drone would be a resemblance this layer is not entitled
    to state. Resemblance across models is what the analogy step is for, and it
    says so in its own label.
    """
    coll = db.collection(config.SOURCES)
    edges = db.collection(config.SIMILARITIES)
    finder = SimilarityFinder(db, coll, top_k=top_k)

    by_module: dict[str, dict[str, dict]] = defaultdict(dict)
    for relative, row in rows.items():
        if relative in ids:
            by_module[row["module"]][relative] = row

    total = 0
    for module, members in sorted(by_module.items()):
        if len(members) < 2:
            continue
        allowed = {ids[relative] for relative in members}
        vectors = {relative: db.document(ids[relative]).get(config.CORPUS_EMBEDDING_FIELD)
                   for relative in members}
        docs = {relative: {"id": ids[relative], "embedding": vectors[relative],
                           "content": row["content"]}
                for relative, row in members.items()}
        total += finder.create_similarity_relationships(
            docs, edges, top_k=top_k, module=module, module_doc_ids=allowed)
    return total


def write_domains(db, LeidenAlgorithm, DataStorage) -> int:
    """L2. The Leiden clusters of the files, one run per module.

    autograph clusters inside a module and prefixes the cluster key with it, so
    two modules can never land in one domain. Running it per module here is not a
    choice, it is the same call autograph makes.
    """
    clustering = LeidenAlgorithm(DataStorage(db))
    for module in config.MODEL_NAMES:
        clustering.perform_enhanced_leiden_clustering(module=module)
    # Counted off the collection rather than off the returned partition: what
    # comes back is the second-level partition, so counting it reports the number
    # of super-clusters and not the number of domains that were written.
    return db.collection(config.DOMAINS).count()


def element_row(element: Element, module: str) -> dict[str, Any]:
    """One declaration, with everything the file wrote on it.

    Nothing here is inferred and nothing is summarised. The fields that look
    redundant are the ones a question actually asks for: `abstract` is a modifier
    and also the difference between a part that can exist and one that only
    classifies; `rationale` is one of 294 `@Rationale` annotations and the only
    statement of *why* a requirement exists that these files contain; `attributes`
    repeats the values of the element's own child features so that a rollup is a
    field read rather than a traversal, while those children remain rows of their
    own with their own lines.
    """
    modifiers = set(element.modifiers)
    values = attributes_of(element)
    row: dict[str, Any] = {
        "_key": key_of(element.id),
        "identity": element.id,
        "name": element.name,
        "display": element.display,
        "qualified": element.qualified,
        "short_name": element.short_name,
        "kind": element.kind,
        "is_definition": element.is_definition,
        "anonymous": element.anonymous,
        "module": module,
        "source_file": element.file,
        "source_line": element.line,
        "source_column": element.column,
        "modifiers": sorted(modifiers),
        "visibility": element.visibility,
        "direction": element.direction,
        "abstract": "abstract" in modifiers,
        "variation": "variation" in modifiers,
        "variant": "variant" in modifiers,
        "individual": "individual" in modifiers,
        "reference": "ref" in modifiers,
        "end": "end" in modifiers,
        "conjugated": element.conjugated,
        "attributes": values,
        "doc": element.doc,
        "comments": element.comments,
        "owner": element.owner.id if element.owner else None,
        "depth": element.qualified.count("::"),
        "children": len(element.children),
        # Kept as written as well as resolved, so a reference whose far end is
        # outside the corpus -- every `ISQ::MassValue` in these files -- is still
        # readable rather than simply absent.
        "typed_by": [r.text for r in element.typed_by],
        "specializes": [r.text for r in element.supers],
        "redefines": [r.text for r in element.redefines],
        "references": [r.text for r in element.references],
    }
    if element.multiplicity:
        row["multiplicity"] = {k: v for k, v in element.multiplicity.items()
                               if k != "line"}
    if element.value:
        # The element's own value, which is not in its own `attributes` map --
        # see `attributes_of` for why putting it in both double-counts a rollup.
        row["value"] = value_of(element.value["expr"])
        row["value_operator"] = element.value["op"]
    if element.result is not None:
        row["expression"] = {"text": element.result.text, "operator": element.result.op}
    if element.annotations:
        row["annotations"] = [
            {"name": a.name.text,
             "values": {k: v.text for k, v in a.values.items()}}
            for a in element.annotations]
        for annotation in element.annotations:
            for name, value in annotation.values.items():
                if name == "text" and value.op == "string":
                    row["rationale"] = value.args[0]
    if element.relationship and element.relationship.get("form"):
        row["states"] = element.relationship["form"]
    row["description"] = describe(element, row)
    return row


def describe(element: Element, row: dict) -> str:
    """A plain sentence for an element, built from what the file says.

    Not decoration. This is the text a lexical index searches and the text L3's
    entity is matched against, and it is written from the declaration rather than
    generated, so it cannot say anything the file does not.
    """
    what = f"{'abstract ' if row['abstract'] else ''}{element.kind}"
    if element.is_definition:
        what += " definition"
    parts = [f"{element.display} is a {what} declared in {element.qualified}"]
    if element.typed_by:
        parts.append(f"typed by {', '.join(r.text for r in element.typed_by)}")
    if element.supers:
        word = "specializing" if element.is_definition else "subsetting"
        parts.append(f"{word} {', '.join(r.text for r in element.supers)}")
    if element.redefines:
        parts.append(f"redefining {', '.join(r.text for r in element.redefines)}")
    if row.get("multiplicity"):
        parts.append(f"with multiplicity [{row['multiplicity']['text']}]")
    if row.get("value") is not None:
        own = row["value"]
        shown = own.get("value", own.get("expression", own.get("reference")))
        unit = f" {own['unit']}" if own.get("unit") else ""
        parts.append(f"whose value is {shown}{unit}")
    for name, value in list(row["attributes"].items())[:6]:
        if "value" in value:
            unit = f" {value['unit']}" if value.get("unit") else ""
            parts.append(f"{name} = {value['value']}{unit}")
        elif "expression" in value:
            parts.append(f"{name} = {value['expression']}")
    sentence = ", ".join(parts) + f", at {element.file}:{element.line}."
    if element.short_name:
        sentence += f" Also written {element.short_name}."
    if element.doc:
        sentence += f" {element.doc}"
    if row.get("rationale"):
        sentence += f" Rationale: {row['rationale']}"
    return sentence


def declaration_row(fact: Fact, by_id: dict[str, Element]) -> dict[str, Any]:
    source, target = by_id.get(fact.source), by_id.get(fact.target)
    row = {
        "_key": key_of(f"{fact.type}|{fact.source}|{fact.target}"),
        "_from": f"{config.ELEMENTS}/{key_of(fact.source)}",
        "_to": f"{config.ELEMENTS}/{key_of(fact.target)}",
        "label": fact.type,
        "module": source.model if source else "",
        "source_file": fact.file,
        "source_line": fact.line,
        "description": (f"{source.display if source else fact.source} "
                        f"{fact.type} {target.display if target else fact.target}"),
    }
    row.update({k: (v if isinstance(v, (str, int, float, bool)) else str(v))
                for k, v in fact.detail.items()})
    return row


def write_model(db, corpus: Model, ids: dict[str, str]) -> dict[str, int]:
    """L2. The declarations and the relations between them.

    `DECLARED_IN` joins each element to the source row of the file it was written
    in, so provenance is a graph hop and not only a string field -- which is what
    lets a question start from a document and reach its declarations, and what
    ties this project's two collections into autograph's graph rather than leaving
    them beside it.
    """
    for name, edge in ((config.ELEMENTS, False), (config.DECLARATIONS, True)):
        if not db.has_collection(name):
            db.create_collection(name, edge=edge)
    db.collection(config.ELEMENTS).truncate()
    # Everything this step wrote last time, and nothing else. `load` puts its
    # READ_AS edges in the same collection, and truncating wholesale deleted them
    # -- silently, because the next thing to notice is a Layer 3 question with no
    # answer, several steps later. Element keys are deterministic, so a READ_AS
    # edge written before this rebuild still points at the right row afterwards.
    db.aql.execute(f"""FOR e IN {config.DECLARATIONS}
                         FILTER e.label != "READ_AS"
                         REMOVE e IN {config.DECLARATIONS}""")

    elements = db.collection(config.ELEMENTS)
    rows = [element_row(element, element.model) for element in corpus.elements]
    for i in range(0, len(rows), 500):
        elements.import_bulk(rows[i:i + 500], on_duplicate="replace")

    edges = db.collection(config.DECLARATIONS)
    facts = [declaration_row(fact, corpus.by_id) for fact in corpus.relations]
    for i in range(0, len(facts), 1000):
        edges.import_bulk(facts[i:i + 1000], on_duplicate="replace")

    provenance = [{
        "_key": key_of(f"declared|{element.id}"),
        "_from": f"{config.ELEMENTS}/{key_of(element.id)}",
        "_to": ids[element.file],
        "label": config.DECLARED_IN,
        "module": element.model,
        "source_line": element.line,
    } for element in corpus.elements if element.file in ids]
    for i in range(0, len(provenance), 1000):
        edges.import_bulk(provenance[i:i + 1000], on_duplicate="replace")

    # A READ_AS edge whose declaration this rebuild no longer produces has nothing
    # at one end, and a traversal that crosses it lands on null.
    db.aql.execute(f"""FOR e IN {config.DECLARATIONS}
                         FILTER e.label == "READ_AS" AND DOCUMENT(e._from) == null
                         REMOVE e IN {config.DECLARATIONS}""")

    for field in ("kind", "module", "source_file", "name", "qualified", "display"):
        elements.add_index({"type": "persistent", "fields": [field],
                            "name": f"{field}_idx"})
    edges.add_index({"type": "persistent", "fields": ["label"], "name": "label_idx"})
    return {"elements": len(rows), "relations": len(facts),
            "provenance": len(provenance)}


def write_graph(db, GraphBuilder) -> None:
    """One named graph over all of L2, autograph's plus this project's two.

    `create_named_graph` builds it with autograph's own edge definitions; the two
    added collections are spliced in afterwards, which is the same thing autograph
    does for `rags` when the strategizer creates it later than the graph.
    """
    GraphBuilder(db).create_named_graph()
    graph = db.graph(config.CORPUS_GRAPH)
    existing = {d["edge_collection"] for d in graph.edge_definitions()}
    if config.DECLARATIONS not in existing:
        graph.create_edge_definition(
            edge_collection=config.DECLARATIONS,
            from_vertex_collections=[config.ELEMENTS],
            to_vertex_collections=[config.ELEMENTS, config.SOURCES])


def build() -> dict[str, Any]:
    DataStorage, GraphBuilder, LeidenAlgorithm, SimilarityFinder = autograph()
    db = config.db(create=True)

    corpus = sysml_model.read(config.MODELS)
    rows = sources()

    modules = write_modules(db, rows)
    ids = write_sources(db, DataStorage, rows)
    similar = write_similarities(db, SimilarityFinder, rows, ids)
    domains = write_domains(db, LeidenAlgorithm, DataStorage)
    counts = write_model(db, corpus, ids)
    write_graph(db, GraphBuilder)

    outside = sum(1 for u in corpus.unresolved if u["external"])
    report = {
        "modules (L1)": modules,
        "sources (L2)": len(ids),
        "similarity edges": similar,
        "domains": domains,
        "elements": counts["elements"],
        "stated relations": counts["relations"],
        "provenance edges": counts["provenance"],
        "references outside the corpus": outside,
        "references unresolved inside it": len(corpus.unresolved) - outside,
    }
    config.OUT.mkdir(parents=True, exist_ok=True)
    (config.OUT / "corpus.json").write_text(
        json.dumps({"counts": report, "unresolved": corpus.unresolved}, indent=1),
        encoding="utf-8")
    return report


def main() -> None:
    for name, n in build().items():
        print(f"  {n:>6}  {name}")


if __name__ == "__main__":
    main()
