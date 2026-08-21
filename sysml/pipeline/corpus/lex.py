"""A token stream for SysML v2 source text.

The previous version of this project scanned a .sysml file with regexes over
statements chopped at `;` and `{`. That works until the file uses the language:
a quoted name containing a brace, a `doc /* */` that ends a statement without a
terminator, a body that continues after its closing brace (`connection : C { ... }
:> capabilityToGoals;`), or `137000 [kg]` where the bracket is a quantity and not
a multiplicity. Each of those cost a rule, and the rules started contradicting
each other.

So the reading happens twice, properly: this module turns text into tokens, and
`parse` turns tokens into elements. Nothing here knows what SysML means -- it
knows what a name, a number, a string and an operator look like, and it keeps
every one of them anchored to a line and a column so the model can say where a
fact came from.

Three things are worth knowing about the lexing:

Block comments are tokens, not whitespace. `doc /* ... */` attaches its text to
the element it is declared in, so throwing comments away in the lexer would throw
away the only prose the syntax states outright. A `/* */` that follows nothing is
dropped later, by the parser, which is the only place that can tell.

Names come in two spellings. `battery` and `'S-IC'` are both names; the quotes
are SysML's escape for an identifier that is not a plain word, and they are not
part of the name. `'DE-REQ-1'` and `DEREQ1` are different elements.

The operator table is matched longest-first and that ordering is load-bearing.
`::>` is reference-subsetting, `::` is a namespace separator and `:>` is
specialisation; matching them in the wrong order turns `end capa ::> goal` into
a namespace lookup of nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

NAME = "name"
NUMBER = "number"
STRING = "string"
OP = "op"
COMMENT = "comment"
END = "end"

# Longest first. Every prefix of a longer operator has to come after it, or the
# shorter one wins and the rest of the operator is read as separate tokens.
OPERATORS = (
    "::>", "!==", "===", ":>>",
    ":>", "::", ":=", "==", "!=", "<=", ">=", "..", "->", "?.", "**",
    ":", "=", "<", ">", "+", "-", "*", "/", "%", "^", "&", "|", "~", "!",
    "#", "@", ",", ";", ".", "(", ")", "[", "]", "{", "}", "?",
)


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    line: int
    column: int

    def is_name(self, *words: str) -> bool:
        return self.kind is NAME and self.text in words

    def is_op(self, *symbols: str) -> bool:
        return self.kind is OP and self.text in symbols

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"{self.kind}({self.text!r})@{self.line}:{self.column}"


class LexError(Exception):
    def __init__(self, message: str, line: int, column: int) -> None:
        super().__init__(f"{message} at line {line}, column {column}")
        self.line = line
        self.column = column


def _name_start(c: str) -> bool:
    return c.isalpha() or c == "_"


def _name_rest(c: str) -> bool:
    return c.isalnum() or c == "_"


def lex(source: str) -> list[Token]:
    """Every token in a .sysml file, plus a closing END.

    Quoted names keep their quotes off and their escapes resolved, so the text of
    a `name` token is the identifier itself whichever way it was written. String
    literals keep the same treatment: what a `string` token carries is the value,
    not the source spelling.
    """
    tokens: list[Token] = []
    i, line, column, n = 0, 1, 1, len(source)

    def advance(count: int) -> None:
        nonlocal i, line, column
        for c in source[i:i + count]:
            if c == "\n":
                line += 1
                column = 1
            else:
                column += 1
        i += count

    while i < n:
        c = source[i]

        if c in " \t\r\n\f\v":
            advance(1)
            continue

        if source.startswith("//", i):
            stop = source.find("\n", i)
            advance((n if stop < 0 else stop) - i)
            continue

        if source.startswith("/*", i):
            start_line, start_column = line, column
            stop = source.find("*/", i + 2)
            if stop < 0:
                raise LexError("unterminated block comment", start_line, start_column)
            body = source[i + 2:stop]
            advance(stop + 2 - i)
            tokens.append(Token(COMMENT, _undent(body), start_line, start_column))
            continue

        if c in "'\"":
            start_line, start_column = line, column
            value, length = _quoted(source, i, c)
            advance(length)
            tokens.append(Token(NAME if c == "'" else STRING,
                                value, start_line, start_column))
            continue

        if c.isdigit():
            start_line, start_column = line, column
            j = i
            while j < n and source[j].isdigit():
                j += 1
            # `1..*` is a multiplicity range and not the number 1. followed by .*,
            # so a dot only continues the number when a digit follows it.
            if j + 1 < n and source[j] == "." and source[j + 1].isdigit():
                j += 1
                while j < n and source[j].isdigit():
                    j += 1
            if j < n and source[j] in "eE":
                k = j + 1
                if k < n and source[k] in "+-":
                    k += 1
                if k < n and source[k].isdigit():
                    j = k
                    while j < n and source[j].isdigit():
                        j += 1
            text = source[i:j]
            advance(j - i)
            tokens.append(Token(NUMBER, text, start_line, start_column))
            continue

        if _name_start(c):
            start_line, start_column = line, column
            j = i + 1
            while j < n and _name_rest(source[j]):
                j += 1
            text = source[i:j]
            advance(j - i)
            tokens.append(Token(NAME, text, start_line, start_column))
            continue

        for symbol in OPERATORS:
            if source.startswith(symbol, i):
                start_line, start_column = line, column
                advance(len(symbol))
                tokens.append(Token(OP, symbol, start_line, start_column))
                break
        else:
            raise LexError(f"unexpected character {c!r}", line, column)

    tokens.append(Token(END, "", line, column))
    return tokens


def _quoted(source: str, start: int, quote: str) -> tuple[str, int]:
    """The value of a quoted name or string, and how many characters it spans."""
    out: list[str] = []
    i, n = start + 1, len(source)
    escapes = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
               "\\": "\\", "'": "'", '"': '"'}
    while i < n:
        c = source[i]
        if c == "\\" and i + 1 < n:
            out.append(escapes.get(source[i + 1], source[i + 1]))
            i += 2
            continue
        if c == quote:
            return "".join(out), i + 1 - start
        out.append(c)
        i += 1
    raise LexError("unterminated quoted text", 0, start)


def _undent(body: str) -> str:
    """A `doc /* ... */` body as prose.

    Documentation in these files is written as a block with a `*` down the left
    margin, which is decoration and not text. Left in, it survives into every
    description and every embedding of it.
    """
    lines = []
    for raw in body.splitlines():
        stripped = raw.strip()
        if stripped.startswith("*") and not stripped.startswith("**"):
            stripped = stripped[1:].strip()
        lines.append(stripped)
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).strip()
