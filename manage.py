"""
manage.py — Small CLI for ops tasks (no Flask-CLI dependency needed).

    python manage.py init-db        # create tables
    python manage.py provider-check # verify the configured provider is reachable
    python manage.py count          # print product count in SQLite
    python manage.py load           # bulk-load the catalog from the provider
    python manage.py refresh        # alias for load (re-sync + flag changes)
    python manage.py stats          # show catalog + flagged counts

CLI commands run with start_background=False so they never spin up the nightly
scheduler or trigger an implicit startup load.
"""

from __future__ import annotations

import os
import sys

from app import create_app
from app.extensions import db
from app.models import Product


def _app():
    return create_app(os.getenv("STL_ENV", "development"), start_background=False)


def cmd_init_db() -> int:
    app = _app()
    with app.app_context():
        db.create_all()
        print("OK: schema created/ensured.")
    return 0


def cmd_provider_check() -> int:
    app = _app()
    with app.app_context():
        provider = app.extensions["data_provider"]
        ok = provider.health_check()
        print(f"provider={provider.name} healthy={ok}")
        return 0 if ok else 1


def cmd_count() -> int:
    app = _app()
    with app.app_context():
        n = db.session.query(Product).count()
        print(f"products in SQLite: {n}")
    return 0


def cmd_load() -> int:
    from app.services.loader import RefreshInProgress, bulk_load_guarded

    app = _app()
    with app.app_context():
        provider = app.extensions["data_provider"]
        try:
            stats = bulk_load_guarded(
                provider,
                batch_size=app.config["BULK_LOAD_BATCH_SIZE"],
                price_change_threshold=app.config["PRICE_CHANGE_WARN_DELTA"],
                shorten_max_chars=app.config["LABEL_NAME_MAX_CHARS"],
            )
        except RefreshInProgress:
            print("ERROR: a refresh is already running.")
            return 1
        d = stats.as_dict()
        print(
            "load complete: "
            f"seen={d['total_seen']} inserted={d['inserted']} updated={d['updated']} "
            f"unchanged={d['unchanged']} flagged={d['flagged']} "
            f"duplicates={d['duplicates']} invalid={d['skipped_invalid']} "
            f"errors={d['errors']} in {d['duration_seconds']}s"
        )
        if d["duplicates"]:
            print(f"  duplicate UPCs (first 50): {d['duplicate_upcs']}")
    return 0


def cmd_stats() -> int:
    app = _app()
    with app.app_context():
        total = db.session.query(Product).count()
        flagged = (
            db.session.query(Product).filter(Product.price_flagged.is_(True)).count()
        )
        on_sale = db.session.query(Product).filter(Product.on_sale.is_(True)).count()
        clearance = (
            db.session.query(Product).filter(Product.clearance.is_(True)).count()
        )
        print(
            f"products={total} flagged={flagged} on_sale={on_sale} clearance={clearance}"
        )
    return 0


def cmd_search() -> int:
    """python manage.py search "<query>"  — fuzzy-find products by name/upc."""
    if len(sys.argv) < 3:
        print('usage: python manage.py search "<query>"')
        return 2
    query = sys.argv[2]
    app = _app()
    with app.app_context():
        search = app.extensions["search"]
        by = "upc" if query.isdigit() else "name"
        hits = (
            search.search_by_upc(query)
            if by == "upc"
            else search.search_by_name(query)
        )
        if not hits:
            print(f"no matches for {query!r} (by={by})")
            return 0
        print(f"matches for {query!r} (by={by}):")
        for h in hits:
            p = h.product
            print(f"  {h.score:5.1f}  {p.upc:<14} {p.name}")
    return 0


_COMMANDS = {
    "init-db": cmd_init_db,
    "provider-check": cmd_provider_check,
    "count": cmd_count,
    "load": cmd_load,
    "refresh": cmd_load,  # alias
    "stats": cmd_stats,
    "search": cmd_search,
}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in _COMMANDS:
        print("usage: python manage.py [%s]" % " | ".join(_COMMANDS))
        return 2
    return _COMMANDS[argv[1]]()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
