"""Shared keyword abbreviation matching for execution, help and completion."""

import itertools
import shlex

from .config import ConfigError


def paths(commands):
    for command in commands:
        words = command.split()
        alternatives = [w[1:-1].split("|") if w.startswith("<") and "|" in w else [w]
                        for w in words]
        for path in itertools.product(*alternatives):
            yield command, path


def match(commands, tokens, strict=False):
    candidates = [(label, path, []) for label, path in paths(commands)]
    for index, token in enumerate(tokens):
        available = [item for item in candidates if len(item[1]) > index]
        exact = {path[index] for _, path, _ in available if path[index] == token}
        keywords = exact or {path[index] for _, path, _ in available
                             if not path[index].startswith("<") and path[index].startswith(token)}
        if strict and len(keywords) > 1:
            raise ConfigError(f"ambiguous keyword '{token}': " + ", ".join(sorted(keywords)))
        candidates = [(label, path, expanded + [word if keywords else token])
                      for label, path, expanded in available
                      for word in [path[index]]
                      if word in keywords or (not keywords and word.startswith("<"))]
        if not candidates:
            break
    return candidates


def resolve(commands, tokens):
    candidates = match(commands, tokens, strict=True)
    complete = [expanded for _, path, expanded in candidates if len(path) == len(tokens)]
    if complete:
        return complete[0]
    if candidates:
        raise ConfigError("incomplete command; use '?' for help")
    raise ConfigError("unknown command; use '?' for commands in this mode")


def suggestions(commands, prefix, completion=False):
    try:
        tokens = shlex.split(prefix)
    except ValueError:
        return []
    candidates = match(commands, tokens)
    result = []
    for label, path, expanded in candidates:
        if completion:
            words = [shlex.quote(word) for word in expanded]
            for word in path[len(tokens):]:
                if word.startswith("<"):
                    break
                words.append(word)
            value = " ".join(words)
            if len(words) < len(path):
                value += " "
        else:
            value = label
        if value not in result:
            result.append(value)
    if not completion and any(len(path) == len(tokens) for _, path, _ in candidates):
        result.append("<cr>")
    return result
