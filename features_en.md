# Python 3.14 — Main New Features

> **Release Date:** October 7, 2025  
> **Latest Stable Release:** Python 3.14.6 (June 10, 2026)

---

## 1. Deferred Evaluation of Annotations (PEP 649 & PEP 749)

Annotations on functions, classes, and modules are no longer evaluated eagerly. Instead, they are stored in special **annotate functions** and evaluated only when needed.

- Reduces runtime overhead for defining annotations
- Forward references work naturally — no need to wrap them in strings
- New **`annotationlib`** module provides flexible annotation introspection with three formats:
  - `VALUE` — evaluates annotations to runtime values (like before)
  - `FORWARDREF` — replaces undefined names with `ForwardRef` markers
  - `STRING` — returns annotations as raw strings

```python
>>> from annotationlib import get_annotations, Format
>>> def func(arg: Undefined): pass
>>> get_annotations(func, format=Format.FORWARDREF)
{'arg': ForwardRef('Undefined', ...)}
```

---

## 2. Multiple Interpreters in the Standard Library (PEP 734)

The new **`concurrent.interpreters`** module brings subinterpreters to Python users without needing the C API.

- Run multiple isolated Python interpreters in the same process
- True multi-core parallelism (each interpreter has its own GIL)
- A more human-friendly concurrency model compared to threads
- Ideal for CPU-bound workloads where shared memory isn't required

---

## 3. Template Strings — t-strings (PEP 750)

A new secure string formatting feature using the `t` prefix. Unlike f-strings, t-strings evaluate to a **`string.templatelib.Template`** object that separates static text from interpolated values.

```python
>>> name = "World"
>>> t_string = t"Hello {name}"
>>> t_string.strings
('Hello ', '')
>>> t_string.values
('World',)
```

**Key benefits:**
- No inline code execution — safer than f-strings
- Ideal for preventing **SQL injection**, **XSS attacks**, and **sensitive data leaks** in logs
- Enables safe HTML templating, structured logging, and config file generation
- Can be combined with custom handlers for context-aware formatting

---

## 4. Free-threaded Python Officially Supported (PEP 779)

The free-threaded build (no-GIL) is no longer experimental — it is now an **officially supported** edition of Python.

- True multi-threaded parallelism by removing the Global Interpreter Lock
- The specializing adaptive interpreter is now active in free-threaded builds
- Single-threaded programs still run ~5–10% slower (varies by platform)
- Available as an opt-in build alongside the default GIL-enabled build

---

## 5. Experimental Just-in-Time (JIT) Compiler

- Ships in **Windows and macOS** binary releases, but **disabled by default**
- Can be enabled via `PYTHON_JIT=1` environment variable
- Replaces sequences of bytecode instructions with pre-generated machine code
- Still experimental — performance may vary; not yet available in free-threaded builds

---

## 6. `except` and `except*` Without Brackets (PEP 758)

You can now write exception handlers without parentheses for single exception types:

```python
# Python 3.13 and earlier
except (ValueError):

# Python 3.14+
except ValueError:
```

---

## 7. Control Flow Restrictions in `finally` Blocks (PEP 765)

Using `return`, `break`, or `continue` inside a `finally` block now emits a **`SyntaxWarning`**, as these statements can silently suppress exceptions:

```python
def foo():
    try:
        1 / 0
    except ZeroDivisionError:
        raise
    finally:
        return  # SyntaxWarning!
```

---

## 8. Safe External Debugger Interface (PEP 768)

A new safe external debugger interface for CPython, enabling better debugging tool integration without compromising runtime safety.

---

## 9. Zstandard Compression in the Standard Library (PEP 784)

A new **`compression.zstd`** module brings Zstandard (zstd) compression directly into Python's standard library, offering high-speed compression/decompression without third-party dependencies.

---

## 10. Improved Error Messages

Python 3.14 continues the trend of friendlier, more informative error messages for common mistakes, making debugging easier for developers of all experience levels.

---

## 11. REPL Enhancements

- **Syntax highlighting** in the default interactive shell
- **Import autocompletion** for a smoother interactive experience
- Color output in several standard library CLIs

---

## 12. New Windows Python Installation Manager

A completely rewritten installation manager for Windows that:
- Manages multiple Python versions more reliably
- Provides streamlined install, update, and removal workflows
- Lets you set default versions and invoke specific versions per command

---

## 13. Asyncio Introspection Capabilities

Enhanced introspection features for asyncio, making it easier to debug and monitor asynchronous applications.

---

## 14. Tail-Call-Compiled Interpreter (Performance Boost)

A new type of interpreter using tail-call compilation, delivering **3–5% performance improvement** on the `pyperformance` benchmark suite (requires Clang 19+).

---

## 15. Incremental Garbage Collection

Improvements to the garbage collector reduce pause times by allowing incremental collection cycles.

---

## 16. Emscripten Officially Supported (PEP 776)

Emscripten (WebAssembly) is now a tier-3 officially supported platform, enabling Python to run in web browsers more reliably.

---

## 17. Android Binary Releases

Official binary releases for Android are now provided alongside existing platforms.

---

## 18. PGP Signatures Discontinued (PEP 761)

Python no longer provides PGP signatures for release artifacts. **Sigstore** is the recommended replacement for verifying release authenticity.

---

## 19. Other Notable Changes

- **`typing.ByteString`** and **`collections.abc.ByteString`** deprecation delayed to Python 3.17
- **`functools.Placeholder`** — new API for placeholder objects
- **`concurrent.futures.InterpreterPoolExecutor`** — new executor based on subinterpreters
- Concurrent safe warnings control
- Various standard library improvements (argparse, ast, asyncio, pathlib, etc.)
- Python 3.9 reached end of life in October 2025

---

> **Reference:**  
> - [What's New in Python 3.14 (Official Docs)](https://docs.python.org/3.14/whatsnew/3.14.html)  
> - [PEP 745 — Python 3.14 Release Schedule](https://peps.python.org/pep-0745/)
