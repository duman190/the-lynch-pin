"""Thread-safe JPEG derivatives of large chart PNGs (gallery + per-ticker plot previews)."""
import os
import tempfile
import threading

_locks = {}
_locks_guard = threading.Lock()


def _lock_for(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def jpeg_preview(src, out_dir, width, quality=85):
    """Returns the path of a ``width``-px JPEG copy of ``src`` (cached by file name + mtime).

    Generation is serialised per source so concurrent requests never see a partial file;
    older previews of the same source are removed. Returns None if ``src`` is missing.
    """
    try:
        mtime = int(os.path.getmtime(src))
    except OSError:
        return None
    stem = os.path.splitext(os.path.basename(src))[0]
    out = os.path.join(out_dir, f"{stem}-w{width}-{mtime}.jpg")
    if os.path.exists(out):
        return out
    with _lock_for(os.path.abspath(out_dir) + "/" + stem):
        if os.path.exists(out):  # another request finished it while we waited
            return out
        from PIL import Image
        os.makedirs(out_dir, exist_ok=True)
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((width, width * 2), Image.LANCZOS)
            fd, tmp = tempfile.mkstemp(dir=out_dir, suffix=".part")
            try:
                with os.fdopen(fd, "wb") as f:
                    im.save(f, "JPEG", quality=quality, optimize=True, progressive=True)
                os.replace(tmp, out)
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
        prefix = f"{stem}-w{width}-"
        for old in os.listdir(out_dir):
            if old.startswith(prefix) and old.endswith(".jpg") and os.path.join(out_dir, old) != out:
                try:
                    os.remove(os.path.join(out_dir, old))
                except OSError:
                    pass
    return out
