"""Paths, connection settings and the collection names of all three layers.

Every name here is imported from the repo that owns it rather than restated.
Restating them is how they drift: this file used to claim `HAS_PARENT` was part of
the importer's closed set, and it never was -- the importer's community-hierarchy
edge is `SUB_COMMUNITY_OF`, so every parent edge written under the old name was
invisible to an importer-side reader.

The layers are the platform's own, and which repo owns which is not a detail:

  L1  modules          a label on a document, and the boundary nothing crosses.
                       One module per model here.
  L2  the corpus graph autograph's `{project}_CorpusGraph`: sources, the
                       similarity edges between them, the Leiden domains they
                       fall into. Built without a language model.
  L3  the knowledge    graphrag_importer's `{project}_kg`: documents, chunks,
      graph            entities, communities. Built by asking a model what the
                       text means.

Two collections are added to L2 here, and they are this project's own rather than
autograph's: `{project}_elements` and `{project}_declarations`. autograph's corpus
graph has one row per file and a closed set of fields, which is right for a corpus
of prose -- there is nothing inside a PDF that a parser could state. A .sysml file
is not prose: every element, value and relation in it is written down exactly, and
that is L2 material by every property that makes L2 L2 (deterministic, no model,
re-derivable from the file alone). So it goes in beside the sources, in the same
named graph, under the same naming convention.

Everything points at a local ArangoDB container. Nothing here touches a shared or
hosted deployment.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from arango import ArangoClient
from arango.database import StandardDatabase

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
OUT = ROOT / "out"
KG = OUT / "kg"                  # GraphRAG's working_dir: extraction artifacts + LLM cache
# The AQLizer's examples, in two halves. The hand-written one says nothing about any
# particular corpus -- everything in it is a property of this pipeline, so it
# cannot go stale when the input changes. `pipeline.examples.generate` appends a second
# half written from a survey of the graph actually loaded, and writes the pair to
# OUT. That combined file is what the read side uses; the hand-written one alone
# is a fallback for a database this step has never run against.
AQL_EXAMPLES = Path(__file__).resolve().parent / "pipeline" / "examples" / "aql_examples.md"
AQL_EXAMPLES_BUILT = OUT / "aql_examples.md"


def aql_examples() -> Path:
    return AQL_EXAMPLES_BUILT if AQL_EXAMPLES_BUILT.is_file() else AQL_EXAMPLES


# The four Arango repos this project reads, cloned next to it.
IMPORTER_REPO = ROOT.parent / "graphrag_importer"
AUTOGRAPH_REPO = ROOT.parent / "autograph"
SERVICE_REPO = ROOT.parent / "natural-language-service"
RETRIEVER_REPO = ROOT.parent / "graphrag_retrievers"

ARANGO_URL = os.environ.get("ARANGO_URL", "http://localhost:8529")
ARANGO_USER = os.environ.get("ARANGO_USER", "root")
ARANGO_PASS = os.environ.get("ARANGO_PASSWORD", "testpass")
DB_NAME = os.environ.get("SYSML_DB", "dronegraph")

# Both repos build every collection name from the project name in a class body
# that reads the environment at import time, so this has to be set before either
# is imported.
PROJECT = os.environ.get("SYSML_PROJECT", "sysml")
os.environ.setdefault("GENAI_PROJECT_NAME", PROJECT)
for repo in (IMPORTER_REPO, AUTOGRAPH_REPO):
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))

try:
    from graphrag.naming import CollectionNames, FileNames, IndexNames, RelationshipTypes
except ImportError as exc:  # pragma: no cover - a missing clone, not a code path
    raise RuntimeError(
        f"cannot import graphrag.naming ({exc}). Clone graphrag_importer next to "
        "this project -- the L3 collection names and edge vocabulary come from it."
    ) from exc

try:
    from corpus_graph.naming import (
        CORPUS_DOMAINS_COLLECTION,
        CORPUS_GRAPH_NAME,
        CORPUS_MODULES_COLLECTION,
        CORPUS_RELATIONS_COLLECTION,
        CORPUS_SIMILARITIES_COLLECTION,
        CORPUS_SOURCES_COLLECTION,
        EDGE_LABEL_IN_DOMAIN,
        EDGE_LABEL_SIMILAR_TO,
        _add_project_prefix,
    )
except ImportError as exc:  # pragma: no cover - a missing clone, not a code path
    raise RuntimeError(
        f"cannot import corpus_graph.naming ({exc}). Clone autograph next to this "
        "project -- the L1/L2 collection names and edge labels come from it."
    ) from exc


# L1 and L2, autograph's own names.
MODULES = _add_project_prefix(CORPUS_MODULES_COLLECTION)
SOURCES = _add_project_prefix(CORPUS_SOURCES_COLLECTION)
SIMILARITIES = _add_project_prefix(CORPUS_SIMILARITIES_COLLECTION)
DOMAINS = _add_project_prefix(CORPUS_DOMAINS_COLLECTION)
CORPUS_RELATIONS = _add_project_prefix(CORPUS_RELATIONS_COLLECTION)
CORPUS_GRAPH = _add_project_prefix(CORPUS_GRAPH_NAME)

SIMILAR_TO = EDGE_LABEL_SIMILAR_TO
IN_DOMAIN = EDGE_LABEL_IN_DOMAIN
# autograph writes this one as a bare string rather than through `naming`
# (service.py, where the module vertex is joined to its clusters), so it is the
# one label that has to be spelled out. Spelled differently it is invisible to
# autograph's own deletion code, which matches on the literal.
HAS_CLUSTER = "HAS_CLUSTER"

# L2, this project's addition: the SysML model the sources actually contain.
ELEMENTS = _add_project_prefix("elements")
DECLARATIONS = _add_project_prefix("declarations")
DECLARED_IN = "DECLARED_IN"      # element -> the source file that declares it

# L3, the importer's own names.
DOCUMENTS = CollectionNames.DOCUMENTS
CHUNKS = CollectionNames.CHUNKS
ENTITIES = CollectionNames.ENTITIES
COMMUNITIES = CollectionNames.COMMUNITIES
RELATIONS = CollectionNames.get_edge_collection()
GRAPH = f"{PROJECT}_kg"

CORPUS_COLLECTIONS = (MODULES, SOURCES, DOMAINS, ELEMENTS,
                      SIMILARITIES, CORPUS_RELATIONS, DECLARATIONS)
KG_COLLECTIONS = (DOCUMENTS, CHUNKS, ENTITIES, COMMUNITIES, RELATIONS)
ALL_COLLECTIONS = CORPUS_COLLECTIONS + KG_COLLECTIONS

# The five artifacts extraction leaves in KG, named by the importer's own class so
# the two halves cannot disagree about what the files are called.
ARTIFACTS = FileNames

# The field the importer vector-indexes: "embedding", singular. autograph writes
# "embeddings", plural, on its own sources. Writing the wrong one is invisible --
# the rows are there, the vectors are there, and every vector query returns
# nothing.
EMBEDDING_FIELD = IndexNames.EMBEDDING_FIELD
CORPUS_EMBEDDING_FIELD = "embeddings"
VECTOR_INDEX = IndexNames.VECTOR_COSINE

# Fixed by the importer, not chosen here: `openai_embedding` calls
# text-embedding-3-small with no `dimensions` argument, so vectors are always
# 1536 wide. The retrievers have to be told the same number or their queries
# return nothing.
EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536
CHAT_MODEL = os.environ.get("CHAT_MODEL", "gpt-4o")
EXAMPLES_MODEL = os.environ.get("EXAMPLES_MODEL", "gpt-5.5")

SUB_COMMUNITY_OF = RelationshipTypes.SUB_COMMUNITY_OF
EDGE_TYPES = tuple(sorted(RelationshipTypes.get_expected_types() | {SUB_COMMUNITY_OF}))

# One model per top-level entry under models/: a directory is a model, and so is a
# loose .sysml file. Derived rather than listed, so adding a model is dropping a
# folder in rather than editing this file.
#
# The list is also the L1 module boundary. A module is what nothing crosses:
# autograph draws no similarity edge between two modules and clusters inside each
# one, and this project's parser resolves a name only against its own model. Two
# vehicles that both declare a `Battery` declare two different batteries.
MODEL_NAMES = tuple(sorted(
    p.name if p.is_dir() else p.stem
    for p in MODELS.iterdir() if p.is_dir() or p.suffix == ".sysml"
))


def model_of(relative_path: str) -> str:
    head = relative_path.split("/")[0]
    return head[: -len(".sysml")] if head.endswith(".sysml") else head


def import_number(model: str) -> int:
    """A model's slot in every L3 document key. Sorted, so it is stable across
    runs and only shifts if a model is added or removed."""
    return MODEL_NAMES.index(model)


def kg(model: str) -> Path:
    """One GraphRAG working directory per model: artifacts and LLM cache."""
    return KG / model


# The SysML ontology is not restated here. `pipeline.corpus.parse` owns the
# keywords and `.model` owns the relation vocabulary, both because they are what reads
# them and because a list kept in two places is a list that disagrees with itself.
def kinds() -> list[str]:
    from .pipeline.corpus.parse import DEFINITION_KINDS, USAGE_KINDS

    return sorted({*DEFINITION_KINDS.values(), *USAGE_KINDS.values()})


def relation_types() -> list[str]:
    from .pipeline.corpus.model import RELATIONS

    return sorted(RELATIONS)


def _env_file() -> dict[str, str]:
    """The `env` file sitting next to the cloned repos, as KEY=VALUE lines.

    Nothing is exported wholesale -- the file also carries a deployment password,
    and the only value wanted is the OpenAI key.
    """
    for name in ("env", ".env"):
        path = ROOT.parent / name
        if path.is_file():
            break
    else:
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip("\"'")
    return values


def openai_key() -> str:
    from_file = _env_file()
    key = (
        from_file.get("CHAT_API_KEY")
        or from_file.get("OPENAI_API_KEY")
        or os.environ.get("CHAT_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    if not key:
        raise RuntimeError(
            "no OpenAI key: put CHAT_API_KEY (or OPENAI_API_KEY) in the `env` file "
            "beside the cloned repos, or export it"
        )
    # The extraction half builds its own `AsyncOpenAI()` with no arguments, which
    # reads OPENAI_API_KEY and nothing else.
    os.environ["OPENAI_API_KEY"] = key
    return key


def autograph_env() -> None:
    """What autograph's config classes read at import time.

    They read the environment in their class bodies, so every one of these has to
    be set before `corpus_graph` is imported. `ARANGO_DEPLOYMENT_ENDPOINT` is
    assigned rather than defaulted: it is the variable a shell is most likely to
    already hold, pointing at a hosted deployment, and deferring to it sends
    autograph's HTTP calls at a database this project has no business writing to.
    """
    os.environ["EMBEDDING_DIM"] = str(EMBED_DIM)
    os.environ["ARANGO_DEPLOYMENT_ENDPOINT"] = ARANGO_URL
    os.environ["db_name"] = DB_NAME
    os.environ["ARANGODB_USER"] = ARANGO_USER
    os.environ["ARANGODB_PASSWORD"] = ARANGO_PASS
    os.environ["GENAI_PROJECT_NAME"] = PROJECT


# Anything that writes. Two places run AQL that a language model wrote -- `nl`
# runs what AQLizer generated for a question, and `pipeline.examples` runs the
# worked examples out of a generated file to check them -- and neither may be
# allowed to reach the database with a mutation. The service's own read-only gate
# is a substring test (`if op in query.upper()`), so it refuses "orbit INSERTion"
# and passes a `FOR c IN [...] TRUNCATE c`. This is the one that decides.
MUTATION = re.compile(r"\b(INSERT|UPDATE|REPLACE|REMOVE|UPSERT|TRUNCATE)\b", re.I)


def client() -> ArangoClient:
    return ArangoClient(hosts=ARANGO_URL)


def db(create: bool = False) -> StandardDatabase:
    c = client()
    if create:
        sys_db = c.db("_system", username=ARANGO_USER, password=ARANGO_PASS)
        if not sys_db.has_database(DB_NAME):
            sys_db.create_database(DB_NAME)
    return c.db(DB_NAME, username=ARANGO_USER, password=ARANGO_PASS)


def token() -> str:
    """A database JWT. Both the importer and autograph authenticate with one
    rather than a password, and ArangoDB issues an acceptable one itself."""
    import requests

    r = requests.post(f"{ARANGO_URL}/_open/auth", timeout=15,
                      json={"username": ARANGO_USER, "password": ARANGO_PASS})
    r.raise_for_status()
    return r.json()["jwt"]


def drop_vector_indexes(db, collection: str) -> int:
    """Remove any vector index on a collection before rewriting it.

    ArangoDB refuses to insert a document without the indexed vector field once a
    vector index exists, so a re-run of an earlier stage fails with a bare
    "bad parameter" unless the index is dropped first.
    """
    if not db.has_collection(collection):
        return 0
    coll = db.collection(collection)
    dropped = [i for i in coll.indexes() if i.get("type") == "vector"]
    for idx in dropped:
        coll.delete_index(idx["id"])
    return len(dropped)


def drop_database() -> bool:
    sys_db = client().db("_system", username=ARANGO_USER, password=ARANGO_PASS)
    if sys_db.has_database(DB_NAME):
        sys_db.delete_database(DB_NAME)
        return True
    return False
