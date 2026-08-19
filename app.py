"""
Library catalog server — serves ONE HTML file used both on the admin's
desktop and as the public website.

Architecture:
  Firestore (admin's data)  --live listener-->  in-memory cache (this process)
  Browser, NOT signed in    --HTTP GET-->  /api/books  --served from memory, HTTP-cached-->
  Browser, signed in        --direct Firebase Auth + Firestore, unchanged-->

The HTML only loads the Firebase SDK and makes its first Firebase call the
moment someone clicks "Sign in" — before that, it's a plain fetch() to
this server. This file's job is just: hand out the HTML, and answer
/api/books from an always-fresh in-memory copy kept live by a Firestore
watch (not polling). Nothing is written to a visitor's device — no
localStorage, no cookies.
"""
import json
import os
import threading

import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask, Response, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = "skk-library.html"

# Fields sent to the public API — deliberately excludes anything internal
# (deleted flag, importedFrom, createdBy, raw timestamps) that guests don't need.
PUBLIC_FIELDS = [
    "title", "author", "category", "accessionNo",
    "isbn", "pages", "year", "publisher", "copiesTotal", "copiesAvailable",
]

# How long browsers may serve a cached response before re-checking. The
# Firestore listener keeps our in-memory copy live regardless — this just
# controls how quickly *visitors* see a change without a fresh request.
CACHE_MAX_AGE_SECONDS = 60


def _init_firebase():
    """
    Requires a Firebase service account key, provided via the
    FIREBASE_SERVICE_ACCOUNT_JSON environment variable (the full JSON
    content as a single string) — see the deployment notes for how to
    get one. Never commit that JSON to a public repo.
    """
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError(
            "FIREBASE_SERVICE_ACCOUNT_JSON environment variable is not set. "
            "This must contain the full service account key JSON as a string."
        )
    cred = credentials.Certificate(json.loads(raw))
    firebase_admin.initialize_app(cred)
    return firestore.client()


db = _init_firebase()

_lock = threading.Lock()
_books_cache = []
_version = "0"


def _rebuild_cache(col_snapshot, changes, read_time):
    """Called by the Firestore SDK on every change to the books collection —
    including once immediately on startup with the full current collection."""
    global _books_cache, _version
    docs = []
    for doc in col_snapshot:
        data = doc.to_dict() or {}
        if data.get("deleted"):
            continue
        row = {"id": doc.id}
        for field in PUBLIC_FIELDS:
            row[field] = data.get(field, "")
        docs.append(row)
    with _lock:
        _books_cache = docs
        _version = str(read_time.timestamp())
    print(f"[catalog] cache updated: {len(docs)} books, version {_version}")


# Live watch — stays open for the life of the process; the SDK handles
# reconnects internally if the stream drops.
_watch = db.collection("books").on_snapshot(_rebuild_cache)

app = Flask(__name__)


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, HTML_FILE)


@app.route("/api/books")
def api_books():
    with _lock:
        books = _books_cache
        etag = _version

    if request.headers.get("If-None-Match") == etag:
        resp = Response(status=304)
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = f"public, max-age={CACHE_MAX_AGE_SECONDS}"
        return resp

    body = json.dumps(books, ensure_ascii=False)
    resp = Response(body, mimetype="application/json")
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = f"public, max-age={CACHE_MAX_AGE_SECONDS}"
    return resp


@app.route("/healthz")
def healthz():
    with _lock:
        count = len(_books_cache)
    return {"status": "ok", "books_cached": count}, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
