# Rust Directives

Rust-specific extras/overrides on top of the generic repo standard in
`$ENGINEERING_OVERLAY_ROOT/flows/REPO.md` — read that first. Only Rust-specific content here.

This file is packaging, tooling, CI, release and known traps.
**How a crate is designed, named, documented and tested is in
`$RUST_OVERLAY_ROOT/standards/`** — start at its `README.md`, which says which
standard applies to the work. A project's recorded exceptions to them are in its
`.agents/AGENTS.md`.

## Packaging and Layout

- **Source Layout**: Cargo's default layout (`src/lib.rs`/`src/main.rs`, `tests/`,
  `examples/`, `benches/`) already satisfies REPO.md's layout rule — don't invent
  extra nesting Cargo doesn't ask for. Build backend is Cargo itself.
  Literal manifest: `$RUST_OVERLAY_ROOT/references/Cargo.toml`.
- **MSRV**: `rust-version` in `Cargo.toml` (the `requires-python` equivalent) —
  cargo *refuses* to build below it, unlike npm's warn-only `engines.node`.
- **Optional Dependencies**: zero *required* runtime deps where feasible. The
  mechanism is Cargo **features** (`[features]` + `dep:crate`), not runtime import
  fallbacks. Set `[package.metadata.docs.rs] all-features = true` so docs.rs
  documents every feature.
- **README badges**: the exact row, its order and the README section order are
  fixed in `$RUST_OVERLAY_ROOT/standards/base/DOCS.md` — Version, Rust version,
  License, Docs (docs.rs), CI. Copy the row from there; do not compose one.

## Testing and Development

- **Tests**: `cargo test --all-features` covers unit (`#[cfg(test)]`) and integration
  (`tests/`) — no separate framework choice.
- **Lint/format in CI by default** (unlike Python/Node): `cargo fmt --check` and
  `cargo clippy --all-features -- -D warnings` — official, cheap, ecosystem-standard.
- **`.gitignore` language block** (source for the template placeholder): `/target/`,
  plus `Cargo.lock` for *libraries only* (binaries commit their lockfile).

## CI/CD: Implementing the Two-Workflow Split

Templates: `$RUST_OVERLAY_ROOT/references/workflows/rust/{test,release}.yaml`.
- Toolchain via `dtolnay/rust-toolchain` (not unmaintained `actions-rs`) +
  `Swatinem/rust-cache`. Matrix over OS × {`stable`, declared MSRV} — both must pass.
- **Publish**: `cargo publish --token ${{ secrets.CARGO_REGISTRY_TOKEN }}` baseline;
  crates.io is rolling out OIDC Trusted Publishing — verify the current setup at
  release time.
- Changelog scraper: the identical Python inline step from the python template
  (rationale in REPO.md) — never reimplement per language.

## Releases and Versioning

- `Cargo.toml` `version` is plain SemVer — no PEP 440-style mismatch. No leading `v`
  in the manifest; leading `v` on the git tag. Bump in the same commit as the
  changelog entry.

## Documentation Site

- **API reference is nearly free**: crates.io publish auto-builds docs on **docs.rs**
  — no docs workflow job needed for that. Add Pages docs jobs (mirroring the python
  template) only for a narrative guide beyond API docs (e.g. mdBook); the landing
  page stays hand-written (REPO.md rule).

## Documentation: follow `flows/REPO.md`, which already covers this

**`$ENGINEERING_OVERLAY_ROOT/flows/REPO.md` is the standard — read it, do not re-derive it.** Its rule
is the **packaging boundary**: the shipped agent header sits at the root of whatever
the packaging tool ships, beside the human `README.md`. Cargo packages the **crate
root** (`cargo package --list` shows `README.md` and everything beside it), so:

| File | Tracked? | Audience |
|---|---|---|
| `<crate>/README.md` | yes | **humans** — the package long-description, ships |
| `<crate>/AGENTS.md` | yes | **agents** — public-API header, ships beside the README |
| `<crate>/src/**/AGENTS.md` | yes | optional per-module headers for large surfaces |
| workspace-root `AGENTS.md` | yes, optional | contributor orientation — not an API header, doesn't ship |
| `.agents/AGENTS.md` | **no**, gitignored | **private working notes** |

**Do not copy Python's `src/<pkg>/AGENTS.md` path.** That is Python's *answer* (the
wheel ships only `src/<pkg>/`, so the header must live inside it), not the rule. In
Rust the crate root *is* the shipped root, so the header lives beside
`Cargo.toml`/`README.md`; per-module headers below it (e.g. `src/client/security/`)
stay useful when the surface is large.

- The shipped `AGENTS.md` is **header-file-style**: exports with signatures, args,
  defaults, return-or-contract, env vars, gotchas — *"so a consuming agent skips the
  source"*. Keep it current with the API, in the same commit.
- **Record failures, not features.** Bar: *if rediscovering it would cost an hour, write
  it down.* A file restating what the source already says is worse than none.
- Add `readme = "README.md"` to `Cargo.toml`.

### `.agents/` must never ship — and `.gitignore` alone does not guarantee it

Cargo's packaging precedence: `include` (allow-list — overrides everything) →
`exclude` → else honour `.gitignore` inside a git repo. So gitignore protection dies
silently the day someone adds an `include` list or packages outside a git checkout.
Set both:

```toml
exclude = [".*"]
```

It drops `.agents/`, `.gitignore`, and dotfiles at every depth while
`AGENTS.md`/`README.md`/`src/` still ship. A category beats a list — `.claude`,
`.vscode`, `.env` and the next tool's dotdir come free, and nothing a crate
legitimately ships starts with a dot. Don't add build-output entries
prophylactically: `target-dir` is usually workspace-level, and `exclude` only
matters for paths that exist.

**One command verifies both properties**: `cargo package --list -p <crate>
--allow-dirty` must show the crate-root `AGENTS.md` and nothing under `.agents/`.
Run it whenever packaging config or docs layout changes. (Every ecosystem has the
same trap: an allow-list — npm `files`, a Python manifest — stops the ignore file
applying; check the packaging output, not the ignore file.)

### Two leak classes (rule + tool live in REPO.md / EXEC.md)

Run the leak scan (`$ENGINEERING_OVERLAY_ROOT/kb/LEAKS.md`) per crate. It finds:

- **`.agents/` citations in tracked source**: correct only while `.agents/AGENTS.md`
  is the sole agent-facing file; once a shipped header exists, cite that.
- **`Phase N` phrasing**: agents narrate their plan into doc comments; a `NOTICE.md`
  citing "Phase 2/3/4 additions" is the tell.

 Plans and findings default to the **workspace** root when work spans
crates (it usually does); a crate gets its own `.agents/plans/` only when the work is
genuinely contained.

## FFI: the rules that cost real bugs

The rules are in `$RUST_OVERLAY_ROOT/standards/specialized/FFI.md`; this section
holds the traps.

### `repr(C)`/`repr(transparent)` only on types that cross the ABI

A Rust-only wrapper needs neither, and **adding one implies a contract that does not
exist**. Only the `#[repr(C)]` structs genuinely handed to C need the attribute.

**Do not cast a pointer into a reference on that basis**: see the section below.

### Casting a pointer to a wrapper reference is a level-of-indirection trap

Given `struct Uri { raw: *mut sys::raw_uri }` — a wrapper **holding** a pointer:

```rust
// WRONG.
pub unsafe fn from_borrowed<'a>(raw: *const sys::raw_uri) -> &'a Uri {
    &*(raw as *const Uri)
}
```

`&*(raw as *const Uri)` means *"treat the bytes **at** `raw` as a `Uri`"*. So it reads
the first word of the real C struct and uses **that** as the wrapper's `raw` field —
one dereference too many. Every accessor then follows a garbage pointer.

It compiles, it type-checks, and `debug_assert!(!raw.is_null())` passes, because the
pointer *value* is fine — only its interpretation is wrong. Nothing catches it until a
real handle arrives.

**`#[repr(transparent)]` plus `transmute` does not fix it either.** That gets the
reference's *address* right and still dereferences the wrong memory.

**The fix is an ordinary struct literal, not a cast:**

```rust
pub struct BorrowedUri<'a> {
    raw: *const sys::raw_uri,
    _marker: PhantomData<&'a sys::raw_uri>,
}
// no Drop impl -- it physically cannot free
```

Share the accessors between owned and borrowed via a trait rather than duplicating them.
The borrowed type having **no `Drop`** makes the double-free unrepresentable rather than
merely prevented, and construction by literal means the pointer is *stored*, not
*reinterpreted*.

The general lesson: **when a wrapper holds a pointer, converting a raw pointer into a
reference-to-wrapper is not a cast — it is a construction.** Reach for a struct literal.
A cast is only correct when the wrapper *is* the pointer's pointee, which a handle
wrapper never is.

### Encode nullability in the type: `NonNull<T>` vs `Option<NonNull<T>>`

Both compile to the identical C signature (`T*`) — the null-pointer optimisation gives
`None` the same representation as `NULL`. So this is **free** and it is the difference
between a caller knowing and a caller guessing:

```rust
pub extern "C" fn uri_host(u: *const Uri) -> NonNull<c_char>;          // never null
pub extern "C" fn uri_option(u: *const Uri, k: *const c_char)
    -> Option<NonNull<c_char>>;                                        // null = absent
```

Declared as bare `*const c_char`, those two are **indistinguishable**, so the only safe
habit is to null-check everything — which then hides the checks that matter. The same
optimisation is already how `Option<extern "C" fn(…)>` expresses an optional vtable slot.

### Check for null only where null is possible

A blanket helper that null-checks every getter and returns `""` on null does not merely
waste a branch — **it erases the difference between "absent" and "impossible"**. A null
from a getter that cannot return null means the other side is broken; converting it to
an empty string hides that three layers away from the fault.

Split the helpers: one that `debug_assert!`s non-null (free in release), one returning
`Option`.

### Borrowed vs owned: return `&str`, not `String`

If the ABI documents a getter's `const char*` as **borrowed** and a builder's `char*` as
**caller-freed**, that maps exactly onto `-> &str` (tied to `&self`) and `-> String`.
Returning `String` everywhere allocates on every accessor and **erases the distinction
the ABI already drew**.

Two things to check first: any operation that can *modify* the handle must take
`&mut self`, or a borrowed `&str` could outlive validity; and `to_string_lossy()`
silently hides invalid UTF-8, so moving to `&str` forces an honest decision about it.

### `Drop` and borrowed handles

`Drop` runs on **owned** values; a `&T` never triggers it. So passing `&T` is safe, and
the trap is constructing an owned wrapper *to take a reference to* — its `Drop` then
frees a handle you never owned. `ManuallyDrop` or `mem::forget` avoids that, but puts
correctness in discipline at every call site.

**Prefer a separate borrowed type with no `Drop` impl at all.** Then the double-free is
unrepresentable rather than merely avoided: there is no code path from the borrowed type
to a `free`. Share accessors via a trait so they are written once.

Do **not** try to reach the borrowed view by casting a pointer to `&Wrapper` — see the
level-of-indirection trap above. That is the shortcut that looks cleanest and is wrong.

### Appended struct fields: binary vs source compatibility

A `struct_size`-first versioned struct keeps **binary** compatibility — an
already-compiled plugin keeps working, because the engine reads a missing field as
absent. It does **not** keep **source** compatibility: a Rust struct literal is
exhaustive, so every in-tree consumer must name the new field. Both are true; conflating
them misrepresents what the convention buys.

**Guard the floor check itself.** Comparing a caller's `struct_size` against the
*current* size rejects every older struct the moment a field is appended — inverting the
convention. Compare against `offset_of!(T, new_field)` instead.

### Demonstrate compatibility, do not argue it

"An old binary still works" is only established by loading an **actually old** binary —
built from stashed source, hash-verified — against the new engine. A recompiled tree
proves nothing. Where the test lives matters too: if the engine is also linkable as an
rlib, a test crate that links it gets a *second private registry*, so a plugin loaded
there registers somewhere the test cannot see.

## Don't hand-roll what a reputable crate already does

**Especially cryptography, parsers, and anything with a specification.** Prefer a
pure-Rust crate from a known source (RustCrypto, dtolnay, rust-lang, tokio-rs) over
sixty lines written here. They have the fuzzing, the audits, the side-channel work,
the assembly paths and the edge cases; a local version reproduces the published test
vectors and stops there — which is the easy half.

"Keeping this crate dependency-free" is a real goal, but it is **not** worth paying
for with a security primitive. A hash backing a trust decision (certificate pinning) is exactly
the thing to borrow, even where the crate otherwise takes one dependency, and the
crate is often already compiled in (`sha2` via `russh` and `ssh-key`).

Check before assuming a dependency is new: `cargo tree -i <crate>` and the lockfile
answer it in seconds, and a transitive dependency already in the build adds nothing.

The test suite should change purpose rather than disappear. The published vectors that
verified a local implementation still earn their place verifying the **wiring** — that
the right bytes are hashed and encoded the expected way — since a
wrong-but-consistent encoding agrees with itself while disagreeing with every value a
user could paste in.

## A stale build profile impersonates a code bug — and a test can load the *other* profile

Two distinct failures, same root: a dynamic library exists in both `debug/` and
`release/`, and nothing keeps them in step.

**1. Link failure.** Add an exported symbol, run `cargo test`, get `LNK2019:
unresolved external symbol` for a symbol plainly in the source. `cargo build
--release` refreshed the release DLL; `cargo test` builds **debug** and links a copy
from before the symbol existed.

**2. The nastier one: a debug test binary loading the release DLL.** If the test
runner puts a release directory on `PATH` — which a launcher doing
`runtime_env(release)` will — the loader finds `release/lib.dll` first, whatever
profile the test itself was built in. The test then runs against an ABI one build
old. Symptom: an *assertion* failure (not a link error) that appears only under the
project's own test wrapper, passes under bare `cargo test --test x`, and survives a
dozen clean repeat runs. It reads exactly like a flaky parallel-execution race, and
serialising "fixes" it by accident often enough to be misleading.

Distinguish them by timestamp:

```powershell
Get-Item _target/{debug,release}/lib.dll | Select FullName, LastWriteTime
```

If the one earlier on `PATH` is older than your change, that is the bug.

**Rule: after touching an exported ABI, build every profile the tests can reach**, not
just the one the launcher wraps. A project whose launcher pins one profile has this
latent permanently — the wrapped profile stays fresh while the other silently rots,
and which one a test *loads* is decided by `PATH`, not by how it was compiled.

## `unsafe` belongs in named files, listed by a test

Target: **`unsafe` appears only in the files a boundary needs** — the exports, the
loader, the raw bindings — every other module carries `#![forbid(unsafe_code)]`,
and a test lists the allowed files (`$RUST_OVERLAY_ROOT/standards/base/UNSAFE.md`).
That is checkable, unlike "improve ergonomics", and it holds in a workspace where
more than one crate meets a real OS or C boundary, which "one crate" does not.

An FFI crate abstracts **both sides** of the boundary: a safe layer only for
consumers leaves backend authors writing raw `unsafe`, where the bugs are a
stack-allocated descriptor the callee keeps a pointer to, a missing NULL array
sentinel, and a `#[repr(C)]` mirror missing the struct's leading field.

## Build-output naming: `_` prefix is a workspace-only disambiguator

A workspace root holds crates *and* machinery side by side, so `_target/`, `_build/`,
`_bin/` separate the two at a glance. **A crate directory has no such ambiguity** —
inside one, plain `target/`/`build/` apply. Copying the prefix down is a common reflex
and wrong twice: it implies a distinction that doesn't exist, and it makes each crate's
`.gitignore` disagree with what cargo produces.

Two traps in ignore rules:

- **Ignoring directories that don't exist.** With a workspace-level `target-dir` no
  crate has its own output dir, yet every crate ignored `/_build/` and `/target/`.
  Entries describing nothing read as load-bearing and propagate (into
  `Cargo.toml` `exclude` lists).
- **`/_*` as a catch-all** silently ignores *any* underscore-prefixed file, including
  legitimate source (`_private.rs`, `_internal/`). An ignore rule that can swallow
  source is worse than a missing one.

`cargo metadata --format-version 1` reports `target_directory` — check before writing
the rule.

## Workspace conventions

- **One crate per git repo** works well when crates have independent lifecycles, but
  cross-crate refactors then need `git mv` per repo and a commit per repo. Budget for it.
- **Plugin registries are per-linkage.** If a plugin depends on the engine crate rather
  than importing its dylib, it links a second copy and registers into a registry the host
  never reads — **with no error**. Verify with `dumpbin /DEPENDENTS` (or `ldd`): the host
  must list the dylib and export none of its symbols.
- **Stale build artefacts bite after a rename.** A renamed plugin leaves its old `.dll`
  behind and a loader will happily load both, producing a duplicate-registration failure
  that reads like a code bug. Clean the target directory as part of any rename.

## `cargo clippy --fix` is not a safe unattended edit

It exits 0, the code compiles, and the tests pass, while it does two things beyond
the fix. Both are invisible to the compiler, to the suite, and to a
whitespace-ignoring diff:

- **It writes LF into a CRLF file.** Where it only rewrites tokens *within* a line it
  preserves the file's endings; where it **reconstructs** lines — `items_after_test_module`
  physically moves an item — it writes `\n`. A CRLF file ends up
  mixed, which is the state that hurts (`$ENGINEERING_OVERLAY_ROOT/kb/EOL.md`).
- **Every let-chain it collapses is left mis-indented.** `collapsible_if` now produces
  `if a && let Some(x) = b`; the fix rewrites the condition and deletes the inner
  brace but does not re-indent the body, so the body and its closing brace sit one
  level too deep and the brace ends up *shallower* than what it closes.

**`cargo fmt` is usually not the repair.** On a tree that has never been rustfmt-clean,
`cargo fmt --all --check` reports thousands of diffs in files nothing touched, so
running it buries a reviewable lint cleanup in unrelated reformatting. Re-indent only
the collapsed blocks; leave every other byte alone.

Procedure, and steps 4–5 are the ones that are easy to skip and produce no symptom:

1. Snapshot the `.rs` files first. In a workspace of independent crate repos `git diff`
   cannot isolate the tool's changes (every crate is already dirty), and a workspace
   root that is not a repo makes `cargo fix` demand `--allow-no-vcs` anyway.
2. Run `--fix`. 3. Diff against the snapshot and read every hunk.
4. Check line endings **per file** — `crlf` and `lf` both non-zero means mixed.
5. Re-indent the collapsed let-chain blocks.

## Android — a separate note

Cross-compiling Rust for Android fails in Android-specific ways: cc-rs cannot find the NDK's API-versioned
clang wrapper and falls back to a bare `clang.exe` with no `--sysroot`
(`ring` reports `fatal error: 'assert.h' file not found`, which reads like a
broken NDK rather than a missing flag), bindgen needs its own
`BINDGEN_EXTRA_CLANG_ARGS_<target>` because it does not inherit `CC_*`, and
under Flutter/cargokit a failing target aborts the whole task while the
build still prints success — shipping an APK with no native library in it.

**Same class as the Windows ARM64 `clang`/`ring` failure below**: a missing
C toolchain detail surfacing as an error that names something else.

## Windows ARM64

- **Both rustls crypto providers need `clang`** on `aarch64-pc-windows-msvc`, and neither
  failure names it clearly: `ring` reports `ToolNotFound: clang`; `aws-lc-rs` dies at
  `LNK1181` on a missing `.o`, many steps from the cause. Install LLVM. `rustls-rustcrypto`
  is a pure-Rust fallback if a toolchain genuinely cannot have one, but it is
  alpha: a poor default for a library.
- **`PROCESSOR_ARCHITECTURE` lies** in an emulated process: it reports `AMD64` while the
  OS is ARM64. Use `rustc -vV`, or the OS's own report, not the environment variable.
- **x64 emulation is user-mode only.** No x64 *driver* can load on an ARM64 Windows
  machine, and no amount of certificate work changes that.
- **Keep `CARGO_TARGET_DIR` off NFS.** Under parallel load rustc's archive step memory-maps `.rlib` files it has just
  written, and on an NFS mount that races: "failed to map object file: memory map must have a non-zero length". Put sources,
  tarballs and logs on the share if space is short; keep the target on local disk.
