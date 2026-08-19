"""Create a benchmark input from a subset of a larger standard-json input.

Keeps the transitive import closure of the given root sources and requests
code generation (outputSelection) only for the roots.
"""

import json
import re
import sys

IMPORT_RE = re.compile(
    r'import\s+(?:'
    r'\*\s+as\s+\w+\s+from\s+'            # import * as X from "path";
    r'|\w+(?:\s+as\s+\w+)?\s+from\s+'     # import X [as Y] from "path";
    r'|\{[^}]*\}\s+from\s+'               # import {a, b as c} from "path";
    r')?'
    r'["\']([^"\']+)["\']'                # path string -> group 1
    r'(?:\s+as\s+\w+)?'                   # optional trailing alias (form 1 only)
    r'\s*;'
)

# Matches string literals or comments, so comments can be blanked out without
# being fooled by "//" or "/*" appearing inside an import path string.
_TOKEN_RE = re.compile(
    r'"(?:\\.|[^"\\])*"'
    r"|'(?:\\.|[^'\\])*'"
    r'|//[^\n]*'
    r'|/\*.*?\*/',
    re.DOTALL,
)


def strip_comments(content):
    def repl(m):
        text = m.group(0)
        if text[0] in "\"'":
            return text
        return "".join(c if c == "\n" else " " for c in text)

    return _TOKEN_RE.sub(repl, content)


def parse_remappings(settings):
    remaps = []
    for r in settings.get("remappings", []):
        context, _, rest = r.rpartition(":")
        prefix, _, target = rest.partition("=")
        remaps.append((context, prefix, target))
    # longest prefix first
    remaps.sort(key=lambda x: len(x[1]), reverse=True)
    return remaps


def resolve(importer, spec, remaps):
    if spec.startswith("."):
        base = importer.split("/")[:-1]
        for part in spec.split("/"):
            if part in (".", ""):
                continue
            elif part == "..":
                base = base[:-1]
            else:
                base.append(part)
        return "/".join(base)
    for context, prefix, target in remaps:
        if context and not importer.startswith(context):
            continue
        if spec.startswith(prefix):
            return (target + spec[len(prefix):]).replace("//", "/")
    return spec


def closure(sources, roots, remaps):
    seen = set()
    todo = list(roots)
    while todo:
        cur = todo.pop()
        if cur in seen:
            continue
        if cur not in sources:
            print(f"error: {cur} not in sources", file=sys.stderr)
            sys.exit(1)
        seen.add(cur)
        content = strip_comments(sources[cur]["content"])
        for m in IMPORT_RE.finditer(content):
            todo.append(resolve(cur, m.group(1), remaps))
    return seen


def main():
    parent_path, out_path, *roots = sys.argv[1:]
    with open(parent_path) as f:
        inp = json.load(f)
    sources = inp["sources"]
    remaps = parse_remappings(inp["settings"])

    # expand prefix roots (trailing /) to all matching sources
    expanded = []
    for r in roots:
        if r.endswith("/"):
            expanded += [s for s in sources if s.startswith(r)]
        else:
            expanded.append(r)
    missing = [r for r in expanded if r not in sources]
    assert not missing, f"roots not found: {missing}"

    keep = closure(sources, expanded, remaps)
    old_outsel = inp["settings"].get("outputSelection", {})
    template = next(iter(old_outsel.values()), {"*": ["abi", "evm.bytecode", "evm.deployedBytecode"]})
    inp["sources"] = {k: v for k, v in sources.items() if k in keep}
    inp["settings"]["outputSelection"] = {
        r: old_outsel.get(r, template) for r in expanded
    }
    with open(out_path, "w") as f:
        json.dump(inp, f)
    print(f"{out_path}: {len(expanded)} roots, {len(keep)} sources kept of {len(sources)}")


if __name__ == "__main__":
    main()
