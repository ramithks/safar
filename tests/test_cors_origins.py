"""
Which browsers may talk to this API.

`resolve_cors_origins` runs once, at import, and there is no second chance to
get it right: a list that is too narrow is an outage for travbuddy.in and for
every hotel front desk, and a list that is too wide lets any page on the
internet make credentialed calls to a hotel's API. Neither shows up in a
functional test — the first looks like "the site is down", the second looks
like nothing at all.

The case worth having a test for is the wildcard one. Production runs with
`APP_ENV` unset and no `ALLOWED_ORIGINS` in its environment, so
`settings.allowed_origins` is the `"*"` default from config.py. An earlier
version of this raised on that combination, which meant setting
`APP_ENV=production` — on its own, an obviously safe tidying change — refused
to boot and took the marketplace down. Hence `test_production_wildcard_*`:
the wildcard is still refused, and the refusal still leaves the site up.
"""

from app.main import PRODUCTION_ORIGINS, resolve_cors_origins


class TestOutsideProduction:
    def test_unset_is_open(self):
        """A fresh checkout runs without configuring anything."""
        assert resolve_cors_origins("", "development") == ["*"]

    def test_wildcard_is_honoured(self):
        assert resolve_cors_origins("*", "development") == ["*"]

    def test_explicit_list_still_wins(self):
        assert resolve_cors_origins(
            "http://localhost:5173,http://localhost:3000", "development"
        ) == ["http://localhost:5173", "http://localhost:3000"]


class TestInProduction:
    def test_explicit_list_is_used_verbatim(self):
        assert resolve_cors_origins(
            "https://partner.travbuddy.in,https://travbuddy.in", "production"
        ) == ["https://partner.travbuddy.in", "https://travbuddy.in"]

    def test_unset_falls_back_to_the_real_origins(self):
        assert resolve_cors_origins("", "production") == PRODUCTION_ORIGINS

    def test_production_wildcard_is_refused(self):
        assert "*" not in resolve_cors_origins("*", "production")

    def test_production_wildcard_still_serves_the_marketplace(self):
        """
        The refusal must not be an outage. travbuddy.in calls this API from
        Client Components (lib/search.ts, lib/recommend.ts); dropping it would
        break the traveller site to fix a header.
        """
        origins = resolve_cors_origins("*", "production")
        assert "https://travbuddy.in" in origins
        assert "https://app.hostos.in" in origins

    def test_the_old_pms_origin_survives_the_rename(self):
        """
        The PMS moved from `partner.travbuddy.in` to `app.hostos.in`, and the
        old host redirects — which is enough for a browser that makes a request
        and nothing at all for a desk that installed the PWA on the old origin.
        Its service worker answers navigations from cache, so it never sees the
        redirect and goes on calling this API from a hostname that appears
        nowhere else in the configuration. Dropping it here fails those desks
        on preflight, silently.
        """
        origins = resolve_cors_origins("*", "production")
        assert "https://partner.travbuddy.in" in origins

    def test_a_wildcard_hidden_in_a_list_is_refused_too(self):
        """`*` alongside real origins is still `*` to CORSMiddleware."""
        assert resolve_cors_origins(
            "https://travbuddy.in,*", "production"
        ) == PRODUCTION_ORIGINS

    def test_nothing_here_raises(self):
        """
        Every input produces a usable list. This process serves the traveller
        site, both mobile apps and the PMS from one container; refusing to start
        is never the proportionate response to a bad header.
        """
        for configured in ("", "*", " , ", "https://travbuddy.in", "not-a-url"):
            for env in ("production", "PRODUCTION", "prod", "development", ""):
                assert resolve_cors_origins(configured, env)

    def test_prod_is_matched_by_prefix(self):
        """Coolify's value could be `prod` or `production`; both are production."""
        assert resolve_cors_origins("*", "prod") == PRODUCTION_ORIGINS
        assert resolve_cors_origins("*", "Production") == PRODUCTION_ORIGINS
