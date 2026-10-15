"""Thread-safe JPEG derivatives of large chart PNGs (gallery + per-ticker plot previews)."""
import os
import tempfile
import threading

PLOT_PREVIEW_WIDTH = 1100  # inline preview of a per-ticker chart; the PNG itself is the lightbox

_locks = {}
_locks_guard = threading.Lock()


def _lock_for(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def plot_preview_dir(chart):
    """Previews of a per-ticker chart live next to the day's charts, so the LFU day-rollover sweep removes both."""
    return os.path.join(os.path.dirname(chart), "previews")


def _preview_name(src, out_dir, width):
    """(path of the preview of ``src`` as it is now, its stem), or None if ``src`` is missing."""
    try:
        mtime = int(os.path.getmtime(src))
    except OSError:
        return None
    stem = os.path.splitext(os.path.basename(src))[0]
    return os.path.join(out_dir, f"{stem}-w{width}-{mtime}.jpg"), stem


def _drop_older(out_dir, stem, width, keep):
    prefix = f"{stem}-w{width}-"
    for old in os.listdir(out_dir):
        if old.startswith(prefix) and old.endswith(".jpg") and os.path.join(out_dir, old) != keep:
            try:
                os.remove(os.path.join(out_dir, old))
            except OSError:
                pass


def jpeg_preview(src, out_dir, width, quality=85):
    """Returns the path of a ``width``-px JPEG copy of ``src`` (cached by file name + mtime).

    Generation is serialised per source so concurrent requests never see a partial file;
    older previews of the same source are removed. Returns None if ``src`` is missing.
    """
    named = _preview_name(src, out_dir, width)
    if named is None:
        return None
    out, stem = named
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
        _drop_older(out_dir, stem, width, out)
    return out


def adopt_preview(src, out_dir, width, rendered):
    """Moves ``rendered`` — a ``width``-px JPEG drawn together with ``src`` — to where ``jpeg_preview`` looks for
    the preview of ``src``, so the first request finds it instead of shrinking the PNG. Returns its path."""
    named = _preview_name(src, out_dir, width)
    if named is None:
        os.remove(rendered)
        return None
    out, stem = named
    with _lock_for(os.path.abspath(out_dir) + "/" + stem):
        os.replace(rendered, out)
        _drop_older(out_dir, stem, width, out)
    return out
