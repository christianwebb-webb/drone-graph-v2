"""out/kg -> Layer 3, and Layer 3 joined to Layer 2.

`ImportGraphToADB` is the second half of graphrag_importer: it creates
`{project}_kg` and its five collections, reads the workbench artifacts and writes
Documents, Chunks, Entities, Communities and every edge between them, then builds
the vector indexes. It is the same class the platform's importer pods run, given
three things a pod gets from its surroundings instead: a database JWT (ArangoDB
issues one at /_open/auth), a no-op progress sink, and a local URL.

What is different here from the previous version of this project is what happens
*after* the import, and it is the whole point of the rewrite.

Before, this step re-read the .sysml files and wrote what it found onto the L3
entities: attribute values became an `attributes` map on an Entity row, and
containment, typing, specialisation and redefinition became `RELATED_TO` edges in
the L3 edge collection marked `stated: true`. That put facts the syntax states
exactly into the layer built by asking a model what text means, where they sat
next to the model's guesses at the same facts, indistinguishable except by a flag,
and had to be de-duplicated against them afterwards. It also meant the facts only
existed if the extraction ran, which costs money and is not reproducible.

Now those facts are Layer 2's, written by `corpus`, and this step does two things
instead:

  link    every L3 Entity is matched to the L2 element it names, and an edge is
          written between them. An entity is then a *reading* of a declaration and
          can be treated as one -- the description and the vector are the model's,
          the structure is the file's, and which is which is a collection name
          rather than a flag.
  prune   an inferred `RELATED_TO` edge is removed when it restates something L2
          already has: either its relation is one of the structural ones the parser
          reads exhaustively, or both its ends are readings of L2 elements that L2
          already relates. What is left is the model's own -- a relation a
          sentence in a `doc` comment states, or one whose far end is outside the
          corpus -- and that is worth keeping, because nothing else has it.

    python -m sysml.pipeline.knowledge.load
"""

from __future__ import annotations

import asyncio
import builtins
import logging
import re
import sys

from ... import config

# The relations the parser reads exhaustively, and that an inferred copy gets wrong
# in a particular way: it points backwards. `HARDWARECOMPONENT typedby
# SATURNVINSTRUMENTUNIT` inverts a specialisation, and a six-hop walk crossing one
# arrives somewhere the model never puts it. There is no case where a reading of
# prose knows something about containment or typing that the declaration does not
# say outright, so these go on sight rather than on a matching twin.
STRUCTURAL = {"owns", "typedby", "specializes", "subsets", "redefines",
              "referencesfeature", "conjugates", "variantof"}


def importer(model: str):
    """graphrag_importer's writer, pointed at the local container.

    Two shims, both planted as module globals because that is what the import path
    resolves against.

    `update_service_status` is a gRPC call to the platform's metadata service
    announcing progress. There is nothing here to announce it to, and left alone
    it retries against an address that does not resolve.

    `open` is replaced because the two halves disagree about encoding on Windows.
    The extraction half writes its artifacts as UTF-8 with `ensure_ascii=False`
    and the writer reads them back with a bare `open(path)`, which takes the
    platform default -- cp1252 here, UTF-8 on the Linux pods this normally runs
    on, which is why it has never shown up. The first non-ASCII character in a
    source file stops the import.
    """
    for name in ("graphrag", "arango-graphrag", "vectordb"):
        log = logging.getLogger(name)
        log.handlers = [logging.StreamHandler(sys.stderr)]
        log.propagate = False

    import graphrag.importer.import_graph_to_adb as writer

    async def no_status(*_args, **_kwargs) -> None:
        """Progress goes to the GenAI metadata store; nothing here watches it."""

    def utf8_open(file, mode="r", *args, **kwargs):
        if "b" not in mode:
            kwargs.setdefault("encoding", "utf-8")
        return builtins.open(file, mode, *args, **kwargs)

    writer.update_service_status = no_status
    writer.open = utf8_open

    from graphrag.graph_builder.builder._llm import openai_embedding

    config.openai_key()
    return writer.ImportGraphToADB(
        path_to_files=str(config.kg(model)),
        arangodb_url=config.ARANGO_URL,
        db_name=config.DB_NAME,
        import_number=config.import_number(model),
        project_name=config.PROJECT,
        embedding_func=openai_embedding,
        enable_edge_embeddings=False,
        enable_community_embeddings=True,
    )


def reset(db) -> None:
    """Empty the L3 collections this import is about to fill, and only those.

    L2 is not touched. It is built by a different step from a different input and
    costs nothing to keep; re-importing L3 over it is the normal case, because the
    structure does not change when the reading of it does.

    The vector indexes have to go first: ArangoDB refuses a document with no value
    in an indexed vector field, which is every row at the moment it is inserted.
    """
    for name in config.KG_COLLECTIONS:
        config.drop_vector_indexes(db, name)
        if db.has_collection(name):
            db.collection(name).truncate()


async def load() -> dict:
    db = config.db(create=True)
    reset(db)

    # One import per model, in MODEL_NAMES order so the import numbers are the
    # ones `config.import_number` promises. Each reads its own workbench and
    # writes the same collections; keys cannot collide because the number is in
    # them.
    imp = None
    for model in config.MODEL_NAMES:
        imp = importer(model)
        await imp.initialize(config.token())
        await imp.import_documents(config.ARTIFACTS.FULL_DOCS)
        # Deliberately without the chunk-embedding file -- see `chunk_vectors`.
        await imp.import_text_chunks(config.ARTIFACTS.TEXT_CHUNKS)
        chunk_vectors(db, imp)
        await imp.import_entities(config.ARTIFACTS.ENTITIES)
        await imp.import_relationships(config.ARTIFACTS.RELATIONSHIPS)
        await imp.import_community_reports(config.ARTIFACTS.COMMUNITY_REPORTS)

    label(db)
    linked = link(db)
    pruned = prune(db)

    for name in (config.ENTITIES, config.CHUNKS, config.COMMUNITIES):
        await imp.create_vector_index(collection_name=name,
                                      field=config.EMBEDDING_FIELD,
                                      index_name=config.VECTOR_INDEX)
    return {**{name: db.collection(name).count() for name in config.KG_COLLECTIONS},
            "entities linked to an L2 element": linked,
            "inferred edges dropped for an L2 twin": pruned}


def chunk_vectors(db, imp) -> int:
    """Attach the chunk embeddings, matched by id rather than by position.

    `import_text_chunks` will do this itself if handed `vdb_chunks.json`, but it
    indexes the embedding matrix with the chunk's `chunk_order_index`. That
    counter restarts at 0 in every document, while the matrix is in one flat vdb
    order -- so with more than one input file every chunk after the first document
    gets some other chunk's vector. It is silent: the rows are there, the vectors
    are there, and chunk search returns confident nonsense.

    The vdb records carry `__id__`, which is the same "chunk-<hash>" the importer
    derives the _key from, so matching on that is exact regardless of order.
    """
    rows, matrix = imp.get_data_and_embeddings("vdb_chunks.json")
    updates = [{"_key": imp._generate_key(row["__id__"].split("-")[1], apply_hash=False),
                config.EMBEDDING_FIELD: matrix[i].tolist()}
               for i, row in enumerate(rows)]
    coll = db.collection(config.CHUNKS)
    for i in range(0, len(updates), 500):
        coll.import_bulk(updates[i:i + 500], on_duplicate="update")
    return len(updates)


# Documents already carry the file they came from. Chunks reach one through
# PART_OF and entities reach it through MENTIONED_IN and then PART_OF, so both
# hops are resolved here and written down.
STAMP_DOCUMENTS = f"""
FOR d IN {config.DOCUMENTS}
  UPDATE d WITH {{files: [d.file_name], models: [@models[d.file_name]]}}
  IN {config.DOCUMENTS}"""

# Both walk the collection they then write to, so the traversal is finished and
# materialised before the first update runs. Left as one pass, ArangoDB reads a
# document the same query has already modified and fails the whole statement with
# a write-write conflict, which is timing-dependent and so does not show up on
# every run.
STAMP_CHUNKS = f"""
LET rows = (FOR c IN {config.CHUNKS}
  LET docs = (FOR d IN 1..1 OUTBOUND c {config.RELATIONS}
                FILTER IS_SAME_COLLECTION('{config.DOCUMENTS}', d) RETURN d)
  RETURN {{key: c._key, files: SORTED(UNIQUE(docs[*].file_name)),
           models: SORTED(UNIQUE(FLATTEN(docs[*].models)))}})
FOR row IN rows
  UPDATE row.key WITH {{files: row.files, models: row.models}} IN {config.CHUNKS}"""

STAMP_ENTITIES = f"""
LET rows = (FOR e IN {config.ENTITIES}
  LET chunks = (FOR c IN 1..1 OUTBOUND e {config.RELATIONS}
                  FILTER IS_SAME_COLLECTION('{config.CHUNKS}', c) RETURN c)
  RETURN {{key: e._key, files: SORTED(UNIQUE(FLATTEN(chunks[*].files))),
           models: SORTED(UNIQUE(FLATTEN(chunks[*].models)))}})
FOR row IN rows
  UPDATE row.key WITH {{files: row.files, models: row.models}} IN {config.ENTITIES}"""

# An entity found in several chunks keeps one description per chunk, joined with
# GRAPH_FIELD_SEP. Nothing downstream splits on it -- not the retrievers, not the
# AQLizer -- so it survives only as a literal "<SEP>" in the middle of every
# answer built from a description.
UNJOIN = """
FOR d IN @@collection
  FILTER CONTAINS(d.description, '<SEP>')
  UPDATE d WITH {description: SUBSTITUTE(d.description, '<SEP>', ' ')}
  IN @@collection"""


def label(db) -> None:
    """Write `files` and `models` onto every Document, Chunk and Entity."""
    names = db.aql.execute(f"FOR d IN {config.DOCUMENTS} RETURN d.file_name")
    db.aql.execute(STAMP_DOCUMENTS,
                   bind_vars={"models": {n: config.model_of(n) for n in names}})
    db.aql.execute(STAMP_CHUNKS)
    db.aql.execute(STAMP_ENTITIES)
    for name in (config.ENTITIES, config.RELATIONS):
        db.aql.execute(UNJOIN, bind_vars={"@collection": name})
    for name in (config.DOCUMENTS, config.CHUNKS, config.ENTITIES):
        db.collection(name).add_index(
            {"type": "persistent", "fields": ["models[*]"], "name": "models_idx"})
    db.collection(config.ENTITIES).add_index(
        {"type": "persistent", "fields": ["entity_type"], "name": "entity_type_idx"})
    db.collection(config.RELATIONS).add_index(
        {"type": "persistent", "fields": ["relationship_type"], "name": "relationship_type_idx"})


def comparable(name: str) -> str:
    """The form two names are compared in: last segment, letters and digits only.

    Extraction upper-cases what it keeps and is inconsistent about the rest -- the
    same requirement arrives as `DE-REQ-3`, `DE-REQ-3 DURABILITY` and
    `FUNCTIONALREQUIREMENTSPACKAGE::'FLR-R002`. Dropping punctuation makes those
    one string. Only L2's declarations are matched against, so nothing is matched
    loosely against another guess.
    """
    return re.sub(r"[^A-Z0-9]", "", name.split("::")[-1].upper())


def link(db) -> int:
    """Join every L3 entity to the L2 element it is a reading of.

    An entity is matched to a declaration only when the pair is unambiguous:
    within a model, exactly one declaration answers to the name. Where several do
    -- `spacecraft` is declared 21 times in the Apollo model, once per mission
    snapshot -- the extraction made one fused row out of all of them and there is
    no fact of the matter about which it belongs to, so no edge is written. A
    wrong link is worse than none: it would put one snapshot's mass on another's
    row for anything that follows the edge.

    The edge lives in L2's collection and points from the declaration outward,
    because L2 is the layer that is true regardless of whether L3 was ever built.
    """
    rows = list(db.aql.execute(f"""
        FOR e IN {config.ENTITIES}
          RETURN {{key: e._key, name: e.entity_name, short: e.short_name,
                   models: e.models}}"""))
    declarations = list(db.aql.execute(f"""
        FOR x IN {config.ELEMENTS}
          RETURN {{key: x._key, name: x.name, display: x.display,
                   short: x.short_name, module: x.module}}"""))

    claimed: dict[tuple[str, str], list[str]] = {}
    for row in declarations:
        for written in (row["name"], row["display"], row["short"]):
            if not written:
                continue
            alias = comparable(written)
            if len(alias) > 1:
                claimed.setdefault((row["module"], alias), []).append(row["key"])

    edges = []
    for row in rows:
        for written in (row["name"], row["short"]):
            if not written:
                continue
            alias = comparable(written)
            for module in row["models"] or []:
                candidates = set(claimed.get((module, alias)) or ())
                if len(candidates) != 1:
                    continue
                element = next(iter(candidates))
                edges.append({
                    "_key": f"reads_{element}_{row['key']}",
                    "_from": f"{config.ELEMENTS}/{element}",
                    "_to": f"{config.ENTITIES}/{row['key']}",
                    "label": "READ_AS",
                    "module": module,
                    "description": f"the entity {row['name']} is a reading of this declaration",
                })
    unique = {edge["_key"]: edge for edge in edges}
    graph = db.graph(config.CORPUS_GRAPH)
    definitions = {d["edge_collection"]: d for d in graph.edge_definitions()}
    targets = definitions[config.DECLARATIONS]["to_vertex_collections"]
    if config.ENTITIES not in targets:
        graph.replace_edge_definition(
            edge_collection=config.DECLARATIONS,
            from_vertex_collections=definitions[config.DECLARATIONS]["from_vertex_collections"],
            to_vertex_collections=targets + [config.ENTITIES])
    coll = db.collection(config.DECLARATIONS)
    rows = list(unique.values())
    for i in range(0, len(rows), 1000):
        coll.import_bulk(rows[i:i + 1000], on_duplicate="replace")
    return len(rows)


def prune(db) -> int:
    """Drop the L3 edges that restate an L2 fact.

    Containment, typing, specialisation, connection, transition -- none of these
    is something a text discusses, they are things a declaration states, and L2
    reads all of them from the syntax with a file and a line. An inferred copy is
    therefore not extra coverage, it is the same fact without provenance, and the
    copies are wrong in a particular way: they point backwards.
    `HARDWARECOMPONENT typedby SATURNVINSTRUMENTUNIT` inverts a specialisation,
    and a six-hop walk crossing one arrives somewhere the model never puts it.

    What survives in L3 is what the syntax does not state: the prose relations a
    paragraph implies, the communities, and the descriptions.
    """
    structural = db.aql.execute(
        f"""FOR r IN {config.RELATIONS}
              FILTER r.type == "RELATED_TO"
                 AND LOWER(r.relationship_type) IN @structural
              REMOVE r IN {config.RELATIONS}
              COLLECT WITH COUNT INTO n RETURN n""",
        bind_vars={"structural": sorted(STRUCTURAL)})

    # And every remaining inferred edge whose two ends are both readings of L2
    # elements that L2 already relates. That is one fact stored twice: L2's copy
    # has a file and a line, this one has neither, and anything grouping by
    # relation counts both. Where only one end resolves, or where L2 says nothing
    # about the pair, the edge is the model's own reading and is kept -- a relation
    # a `doc` comment states in a sentence is real, and the parser cannot see it.
    duplicates = db.aql.execute(f"""
        LET reads = MERGE(FOR d IN {config.DECLARATIONS}
                            FILTER d.label == "READ_AS"
                            RETURN {{[PARSE_IDENTIFIER(d._to).key]: d._from}})
        FOR r IN {config.RELATIONS}
          FILTER r.type == "RELATED_TO"
          LET a = reads[PARSE_IDENTIFIER(r._from).key]
          LET b = reads[PARSE_IDENTIFIER(r._to).key]
          FILTER a != null AND b != null
          LET twin = FIRST(FOR d IN {config.DECLARATIONS}
                             FILTER d._from == a AND d._to == b AND d.label != "READ_AS"
                             LIMIT 1 RETURN 1)
          FILTER twin != null
          REMOVE r IN {config.RELATIONS}
          COLLECT WITH COUNT INTO n RETURN n""")

    # An edge from a row to itself is not a weak fact, it is not a fact. The
    # extraction writes a few, twelve of them `satisfies`, which is enough to
    # report twelve requirements as met by themselves.
    reflexive = db.aql.execute(
        f"""FOR r IN {config.RELATIONS}
              FILTER r.type == "RELATED_TO" AND r._from == r._to
              REMOVE r IN {config.RELATIONS} COLLECT WITH COUNT INTO n RETURN n""")
    return (next(iter(structural), 0) + next(iter(duplicates), 0)
            + next(iter(reflexive), 0))


def main() -> None:
    counts = asyncio.run(load())
    print(f"  loaded into {config.DB_NAME}")
    for name, n in counts.items():
        print(f"  {n:>6}  {name}")


if __name__ == "__main__":
    main()
