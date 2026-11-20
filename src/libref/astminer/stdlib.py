"""Language tables for classifying external standard-library references."""

from __future__ import annotations

import sys

STDLIB_TABLE_VERSION = "1"

C_FUNCTIONS = frozenset(
    {
        "abort",
        "abs",
        "assert",
        "atof",
        "atoi",
        "atol",
        "atoll",
        "bsearch",
        "calloc",
        "ceil",
        "clock",
        "cos",
        "ctime",
        "difftime",
        "div",
        "exit",
        "exp",
        "fabs",
        "fclose",
        "fgets",
        "floor",
        "fmod",
        "fopen",
        "fprintf",
        "fread",
        "free",
        "fscanf",
        "fseek",
        "ftell",
        "fwrite",
        "getchar",
        "getenv",
        "isalpha",
        "isdigit",
        "isspace",
        "labs",
        "ldiv",
        "log",
        "log10",
        "malloc",
        "memchr",
        "memcmp",
        "memcpy",
        "memmove",
        "memset",
        "pow",
        "printf",
        "putchar",
        "puts",
        "qsort",
        "rand",
        "realloc",
        "rewind",
        "scanf",
        "setlocale",
        "signal",
        "sin",
        "snprintf",
        "sprintf",
        "sqrt",
        "srand",
        "sscanf",
        "strcat",
        "strchr",
        "strcmp",
        "strcpy",
        "strerror",
        "strlen",
        "strncat",
        "strncmp",
        "strncpy",
        "strrchr",
        "strstr",
        "strtod",
        "strtol",
        "strtoul",
        "system",
        "tan",
        "time",
        "tolower",
        "toupper",
        "va_arg",
        "va_end",
        "va_start",
        "vfprintf",
    }
)

CPP_PREFIXES = ("std::", "std.")

RUST_PREFIXES = ("std::", "core::", "alloc::", "proc_macro::")
RUST_NAMES = frozenset(
    {
        "println",
        "print",
        "eprintln",
        "eprint",
        "format",
        "vec",
        "panic",
        "drop",
        "todo",
        "unimplemented",
        "assert",
        "assert_eq",
        "assert_ne",
        "debug_assert",
        "dbg",
    }
)

PYTHON_MODULES = frozenset(getattr(sys, "stdlib_module_names", ()))
NODE_BUILTINS = frozenset(
    {
        "assert",
        "buffer",
        "child_process",
        "cluster",
        "crypto",
        "dgram",
        "dns",
        "events",
        "fs",
        "http",
        "http2",
        "https",
        "module",
        "net",
        "os",
        "path",
        "process",
        "querystring",
        "readline",
        "stream",
        "string_decoder",
        "timers",
        "tls",
        "tty",
        "url",
        "util",
        "v8",
        "vm",
        "worker_threads",
        "zlib",
    }
)
JAVA_PREFIXES = ("java.", "javax.", "jdk.", "sun.", "com.sun.")
CSHARP_PREFIXES = ("System.",)
KOTLIN_PREFIXES = ("kotlin.", "kotlinx.")
GROOVY_PREFIXES = ("groovy.", "org.codehaus.groovy.")


def _head(name: str) -> str:
    return name.replace("::", ".").partition(".")[0]


def stdlib_category(
    name: str, language: str | None, table_version: str = STDLIB_TABLE_VERSION
) -> str:
    """Return the owning standard library category, or an empty string.

    A call whose name matches a target API is classified by resolution before
    this function is consulted, so same-named user APIs stay user APIs.
    """
    if not name or table_version != STDLIB_TABLE_VERSION or not language:
        return ""
    normalized = name.replace("::", ".")
    head = _head(name)
    if language in ("c", "cpp"):
        if language == "cpp" and normalized.startswith(("std.",)):
            return "cpp"
        if head in C_FUNCTIONS:
            return "c" if language == "c" else "cpp"
    if language == "cpp" and normalized.startswith(CPP_PREFIXES):
        return "cpp"
    if language == "rust":
        if name.startswith(RUST_PREFIXES) or normalized.startswith(
            tuple(prefix.replace("::", ".") for prefix in RUST_PREFIXES)
        ):
            return "rust"
        if head in RUST_NAMES:
            return "rust"
    if language == "python" and head in PYTHON_MODULES:
        return "python"
    if language in ("javascript", "typescript"):
        return "node" if head in NODE_BUILTINS or normalized.startswith("node:") else ""
    for prefix, category in (
        *((p, "java") for p in JAVA_PREFIXES),
        *((p, "csharp") for p in CSHARP_PREFIXES),
        *((p, "kotlin") for p in KOTLIN_PREFIXES),
        *((p, "groovy") for p in GROOVY_PREFIXES),
    ):
        if normalized.startswith(prefix):
            return category
    return ""


def is_stdlib(
    name: str, language: str | None, table_version: str = STDLIB_TABLE_VERSION
) -> bool:
    return bool(stdlib_category(name, language, table_version))
