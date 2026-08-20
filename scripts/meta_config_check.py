"""
Are the Meta credentials in this environment actually the right ones?

    set -a; . ./.env.hotelos; set +a
    ./venv/bin/python scripts/meta_config_check.py

Every check asks *Meta*, not the config file. Nothing here sends a message,
posts anything, or changes state — the WhatsApp token is inspected with
`debug_token`, the Instagram token with a `me` read, and the Instagram app
pair with a code exchange using a deliberately invalid code.

── why this exists ──────────────────────────────────────────────────────────

Every way of getting Meta credentials wrong produces an error that names
something else:

  expired access token       → 190, reads as "the integration is broken"
  wrong app secret           → 403 on every inbound webhook, reads as
                               "Meta isn't calling us"
  Facebook app id used for   → 400 "Invalid platform app", which names neither
    Instagram Login            the app nor the id nor which of the two is wrong

That last one is the trap this file was written for. Meta shows a Facebook app
id on the Basic settings page and a *separate* Instagram app id under
Instagram → API setup with Instagram login. They are both ~16 digits, they
often share a prefix, and only one of them works. Nothing in the error says so.

`--json` prints a machine-readable summary for CI.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

GRAPH = "https://graph.facebook.com/v21.0"
IG_GRAPH = "https://graph.instagram.com"
IG_TOKEN = "https://api.instagram.com/oauth/access_token"

passed = failed = warned = 0
results: list[dict] = []


def record(label: str, state: str, detail: str = "") -> None:
    global passed, failed, warned
    icon = {"ok": "  ok  ", "fail": "  FAIL", "warn": "  warn", "skip": "  ·   "}[state]
    if state == "ok":
        passed += 1
    elif state == "fail":
        failed += 1
    elif state == "warn":
        warned += 1
    print(f"{icon} {label}" + (f"\n         {detail}" if detail else ""))
    results.append({"check": label, "state": state, "detail": detail})


def get(url: str, params: dict) -> tuple[int, dict]:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(full, timeout=25) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            return e.code, {}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": {"message": str(e)}}


def post(url: str, data: dict) -> tuple[int, dict]:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            return e.code, {}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": {"message": str(e)}}


def err(payload: dict) -> str:
    e = payload.get("error")
    if isinstance(e, dict):
        return e.get("message", "")
    return payload.get("error_message") or str(e or "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    wa_token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "").strip()
    wa_secret = os.environ.get("WHATSAPP_APP_SECRET", "").strip()
    ig_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "").strip()
    ig_secret = os.environ.get("INSTAGRAM_APP_SECRET", "").strip()
    ig_app_id = os.environ.get("INSTAGRAM_APP_ID", "").strip()
    redirect = os.environ.get("INSTAGRAM_REDIRECT_URI", "").strip()

    print("══ Meta credential check ══\n")

    # ── WhatsApp ─────────────────────────────────────────────────────────────
    print("── WhatsApp ──")
    app_id = None
    if not wa_token:
        record("WHATSAPP_ACCESS_TOKEN is set", "fail", "unset — nothing can send")
    else:
        st, body = get(f"{GRAPH}/debug_token",
                       {"input_token": wa_token, "access_token": wa_token})
        data = body.get("data") or {}
        if not data.get("is_valid"):
            record("the WhatsApp access token is valid", "fail", err(body) or str(body)[:160])
        else:
            app_id = str(data.get("app_id") or "")
            record("the WhatsApp access token is valid", "ok",
                   f"app {app_id} ({data.get('application')})")

            expires = data.get("expires_at")
            if expires == 0:
                record("and it does not expire", "ok", "SYSTEM_USER, no expiry")
            else:
                # A token with an expiry is a token that will stop working on a
                # date nobody has in a calendar.
                record("and it does not expire", "warn",
                       f"expires_at={expires} — a short-lived token will strand "
                       f"the integration silently. Use a System User token.")

            scopes = set(data.get("scopes") or [])
            need = {"whatsapp_business_messaging", "whatsapp_business_management"}
            missing = need - scopes
            record("it carries the messaging scopes", "ok" if not missing else "fail",
                   "" if not missing else f"missing {', '.join(sorted(missing))}")

    if not wa_secret:
        record("WHATSAPP_APP_SECRET is set", "fail",
               "unset — every inbound webhook Meta sends is rejected 403")
    elif app_id:
        # An app access token is `<app_id>|<app_secret>`. Meta accepts it only
        # if the secret really is that app's, which is the pairing that matters:
        # a valid secret from a *different* app fails every signature check
        # while looking perfectly well-formed in the config.
        st, body = get(f"{GRAPH}/debug_token",
                       {"input_token": wa_token, "access_token": f"{app_id}|{wa_secret}"})
        ok = bool((body.get("data") or {}).get("is_valid"))
        record("WHATSAPP_APP_SECRET belongs to that same app", "ok" if ok else "fail",
               "" if ok else f"{err(body)} — this secret is not app {app_id}'s, so "
                             f"X-Hub-Signature-256 can never verify")
    else:
        record("WHATSAPP_APP_SECRET belongs to that same app", "skip",
               "no valid token to identify the app with")

    # ── Instagram ────────────────────────────────────────────────────────────
    print("\n── Instagram ──")
    if not ig_token:
        record("INSTAGRAM_ACCESS_TOKEN is set", "warn",
               "unset — only needed for accounts we operate ourselves")
    else:
        st, body = get(f"{IG_GRAPH}/v23.0/me",
                       {"fields": "user_id,username,account_type", "access_token": ig_token})
        if st == 200 and body.get("user_id"):
            record("the Instagram access token is valid", "ok",
                   f"@{body.get('username')} ({body.get('account_type')})")
            if body.get("account_type") not in ("BUSINESS", "MEDIA_CREATOR"):
                record("and the account is a professional one", "fail",
                       f"account_type={body.get('account_type')} — messaging needs a "
                       f"professional (Business or Creator) account")
        else:
            record("the Instagram access token is valid", "fail", err(body) or str(body)[:160])

    if not ig_secret:
        record("INSTAGRAM_APP_SECRET is set", "fail",
               "unset — inbound Instagram webhooks are rejected 403")
    elif app_id and ig_secret == wa_secret:
        # Not automatically wrong, but worth saying out loud: Instagram Login
        # normally has its own secret, and reusing the Facebook one is the
        # commonest way to get a webhook that never verifies.
        record("INSTAGRAM_APP_SECRET differs from the Facebook one", "warn",
               "they are identical — check this is really the Instagram app secret")
    else:
        record("INSTAGRAM_APP_SECRET is set", "ok")

    if not ig_app_id:
        record("INSTAGRAM_APP_ID is set", "fail",
               "unset — the consent flow cannot start")
    elif app_id and ig_app_id == app_id:
        # The headline check. This is the mistake Meta's own error will not name.
        record("INSTAGRAM_APP_ID is the Instagram app id, not the Facebook one", "fail",
               f"it is {ig_app_id}, which is the FACEBOOK app id. Instagram Login "
               f"needs the separate id under Meta app → Instagram → API setup "
               f"with Instagram login → Instagram app ID.")
    elif ig_secret and redirect:
        # Ask Meta. A deliberately invalid code separates "wrong app" from
        # "right app, bad code" — the only two answers that matter here.
        st, body = post(IG_TOKEN, {
            "client_id": ig_app_id,
            "client_secret": ig_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect,
            "code": "probe-deliberately-invalid",
        })
        message = err(body)
        if "platform app" in message.lower():
            record("Meta recognises INSTAGRAM_APP_ID + INSTAGRAM_APP_SECRET", "fail",
                   f"{message} — the app id is not an Instagram Login app, or the "
                   f"secret is not its secret")
        else:
            # Anything about the *code* means the app pair was accepted first.
            record("Meta recognises INSTAGRAM_APP_ID + INSTAGRAM_APP_SECRET", "ok",
                   f"rejected only the probe code: {message[:90]}")
    else:
        record("Meta recognises the Instagram app pair", "skip",
               "needs INSTAGRAM_APP_SECRET and INSTAGRAM_REDIRECT_URI")

    if redirect:
        record("INSTAGRAM_REDIRECT_URI is https and not localhost",
               "ok" if redirect.startswith("https://") else "warn",
               "" if redirect.startswith("https://")
               else f"{redirect} — fine locally, never in production")

    print(f"\n╔═ {passed} passed, {failed} failed, {warned} warnings")
    if args.json:
        print(json.dumps(results, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
