# PyInstaller spec for the `engram` binary.
#
# Bundles Python + engram + the native deps (sqlite-vec extension, onnxruntime,
# tokenizers) into one executable. The embedding MODEL is NOT bundled — fastembed
# downloads it (~50 MB) on first use; after that the binary runs offline.
#
# Build:  pyinstaller packaging/engram.spec --noconfirm --clean
# Output: dist/engram   (onefile)
#
# Note: onefile extracts to a temp dir on each launch (slower start). For faster
# startup, flip ONEFILE = False below to produce a dist/engram/ folder instead.
from PyInstaller.utils.hooks import collect_all

ONEFILE = True

# packages with native libraries / data files that must be collected explicitly
_NATIVE_PKGS = [
    "sqlite_vec",       # the SQLite vector extension
    "onnxruntime",      # the embedding runtime
    "tokenizers",       # native tokenizer
    "py_rust_stemmers", # native stemmer (fastembed dep)
    "fastembed",        # embedding wrapper + its data
    "huggingface_hub",  # model download on first run
]

datas, binaries, hiddenimports = [], [], []
for pkg in _NATIVE_PKGS:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# our own lazily-imported modules, so the analyzer doesn't miss them
hiddenimports += [
    "engram", "engram.cli.main", "engram.mcp.server",
    "engram.core.memory", "engram.embeddings.local", "engram.embeddings.hashing",
    "mcp", "mcp.server.fastmcp",
]

a = Analysis(
    ["engram_entry.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name="engram",
        console=True, strip=False, upx=False,
    )
else:
    exe = EXE(pyz, a.scripts, [], name="engram", console=True, strip=False, upx=False)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="engram")
