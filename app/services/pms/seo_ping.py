"""
Telling travbuddy.in that a hotel's page has changed.

    publish / unpublish / rooms edited
              │
              └──► POST travbuddy.in/api/revalidate  {kind: stayos_hotel, slug}
                          │
                          ├─ purge /book/{slug} and its room pages
                          ├─ purge /sitemaps/stayos-*.xml and /sitemap.xml
                          └─ submit the URL to IndexNow

**Why a ping rather than a shorter cache.** The hotel page is ISR on a one-hour
revalidate and the two files a crawler actually reads — the section sitemap and
the `<sitemapindex>` carrying its `lastmod` — are on twenty-four hours. A hotel
that uploads its fourth photograph crosses the indexability gate at that
instant and would otherwise sit outside the sitemap until the next day. Turning
the windows down instead would make every hotel page more expensive to serve in
order to fix the few minutes after a change.

**Best effort, always.** Nothing here may fail a publish. A hotelier pressing
"Publish" has done their part whether or not a cache on another host cleared,
and a 500 from a CDN is not something they can act on. Every failure is a log
line.

**The website derives the paths.** This sends a kind and a slug and nothing
else — see `app/api/revalidate/route.ts`, where the paths are built from them.
A leaked secret buys the ability to purge one hotel's page, not the site.
"""
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5.0


async def hotel_changed(slug: str | None) -> None:
    """Purge a hotel's public pages on the website. Never raises."""
    if not slug:
        return

    base = (getattr(settings, "site_url", None) or "").rstrip("/")
    secret = getattr(settings, "revalidate_secret", None)
    if not (base and secret):
        # Not configured is a normal state — a development box, or a StayOS
        # deployment with no website in front of it. Said once, at debug.
        logger.debug("no revalidate hook configured; skipping SEO ping for %s", slug)
        return

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{base}/api/revalidate",
                json={"kind": "stayos_hotel", "slug": slug},
                headers={"x-revalidate-secret": secret},
            )
        if response.status_code >= 400:
            logger.warning("revalidate hook refused %s: %s %s",
                           slug, response.status_code, response.text[:200])
    except Exception:  # noqa: BLE001
        # A publish that worked must not report as a failure because a cache on
        # another host was slow.
        logger.warning("could not ping the website for %s", slug, exc_info=True)
