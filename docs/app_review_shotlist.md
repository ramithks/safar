# App Review — what the two videos must show

Meta grants advanced access to `whatsapp_business_messaging` and
`whatsapp_business_management` on the evidence of a **recording**, not of a
working system. Reviewers are looking for one thing in each: that *your
application* made the API call, and that the effect was real.

Both videos are driven by `scripts/app_review_evidence.py`, which prints each
request and each response with a pause long enough to read.

```
cd travbuddy_backend
set -a; . ./.env.hotelos; set +a
```

Do a dry run of each with `--fast` first. The real recording runs without it.

---

## Before you record

- [ ] **App icon, Privacy Policy URL and app category** are set in the App
      Dashboard. App Review will not open without them.
- [ ] `WHATSAPP_WABA_ID` is in `.env.hotelos`, or you have it to pass with
      `--waba`. Find it at **WhatsApp → API Setup → "WhatsApp Business Account
      ID"**, directly under the phone number id. It cannot be read from the
      token — a system user's `me/businesses` comes back empty.
- [ ] `./venv/bin/python scripts/meta_config_check.py` reports the WhatsApp
      token valid and the app secret matching.
- [ ] For Route A: the backend and PWA are running the current code. The demo
      hotel is already `connected`, so `WHATSAPP_ONBOARDING_ENABLED` does **not**
      need to be true for the card to appear — that flag only gates the *Connect*
      button for hotels that have no number yet.
- [ ] Screen recording is at a resolution where the text is legible when Meta
      scales the video down. Increase the font or browser zoom first.
- [ ] Nothing secret is on screen: no `.env` open in another window, no token in
      shell history you might scroll to. The script never prints one.

---

## Video 1 — `whatsapp_business_messaging`

> *"Record a video showing a message sending from your app to a WhatsApp number.
> The video should show your app sending the message, as well as the WhatsApp
> interface (either web or mobile app) receiving the same message."*
>
> *"You can use the Try it out tool… Embed the cURL command from step 2 into
> your website and record how you send a message."*

Read that last line carefully: Meta wants to see **your app** sending, not a
terminal. A terminal recording often passes, but it is the weaker submission and
it is the most common reason this video is sent back.

So there are two ways to record it, and the first is much stronger.

### Route A — from the product (recommended)

**Settings → Send a test message.** This is a real feature: a hotel that has
just connected presses it to check its number works. It sends one WhatsApp
message from the hotel's own number and prints the API call and Meta's reply on
screen — which is exactly what Meta means by embedding the cURL into your site.

**⚠ Route A needs a deploy first.** The card is new. `api.hostos.in` and
`app.hostos.in` are still running the code from before this work, so the button
is not on the live Settings page yet. Either deploy both, or record against a
local backend — a hotelier pressing a button in your app is what the reviewer is
grading, not which host served it.

Log in as **`owner@anandihouse.demo`**. That is the hotel that owns the real
verified number (+91 93184 79690, "Travbuddy"); Casa Mandala and the other seed
hotels are on sandbox connections and will not send anything.

Set up before pressing record: the Settings page on one side, WhatsApp Web
logged in as the recipient handset on the other, **both visible at once**.

| # | On screen | Why it matters |
| --- | --- | --- |
| 1 | The Settings page, with WhatsApp showing **Connected** and the real number | Establishes this is a product, not a script |
| 2 | Typing the recipient number into "Send a test message" | A deliberate user action |
| 3 | Pressing **Send** | The moment the app sends |
| 4 | The dark panel that appears: `POST …/messages`, the JSON body, and Meta's reply with the `wamid.…` id | **This is the API call Meta needs to see** |
| 5 | **Move to the WhatsApp window.** The message arriving | The other half of the requirement |
| 6 | Hold on it for ~3 seconds | Time to match the two |

One continuous take. A cut between step 4 and step 5 is the single most common
rejection — the reviewer cannot tell the two halves are related.

**It sends the `hello_world` template, not free text, and that is correct.**
Meta only allows free text inside the 24-hour window a guest's own message
opens. A hotel testing a fresh connection has no such window, so free text comes
back `131047` and the test would report a broken integration on a connection
that is fine. `hello_world` is on every WABA from the day it is created, which
is why Meta's own "Try it out" tool sends exactly that.

### Route B — from the terminal (fallback)

Use this if you would rather not deploy first, or would rather not show the
product. **It works right now, with no deploy.** It sends a free-text message
and therefore needs the recipient to have messaged the number within the last 24
hours — if it comes back `131047`, message the hotel's number from the handset
first and run it again.

```
./venv/bin/python scripts/app_review_evidence.py --messaging --wait 0
```

Same shot list: command → request → Meta's reply with the message id → switch to
WhatsApp → the message arriving. Same rule about one continuous take.

The message body carries a timestamp in both routes. Make sure it is legible in
both windows — that is what makes "the same message" verifiable.

---

## Video 2 — `whatsapp_business_management`

> *"Make API test calls and record a separate unique video of creating a message
> template."*

**Separate and unique** — do not reuse any footage from video 1.

```
./venv/bin/python scripts/app_review_evidence.py --management --waba <WABA_ID>
```

| # | On screen | Why it matters |
| --- | --- | --- |
| 1 | The command being typed and run | Same as above |
| 2 | Step 1 — the existing templates listed | Establishes the "before" |
| 3 | Step 2 — the `POST .../message_templates` body, with `name`, `category`, `components` | This is the call being reviewed |
| 4 | Step 3 — Meta's reply with the new template `id` | Proof it was created |
| 5 | Step 4 — the template read back, now on the account | The "after" |

The status will be `PENDING`. That is correct and does not weaken the evidence:
the permission is for *creating* a template, and Meta's own review of the
template's content is a separate process.

**Optional, and stronger:** after step 5, switch to WhatsApp Manager in a
browser and show the same template listed there. It costs twenty seconds and
removes any doubt that the API call had a real effect.

---

## After both videos

- [ ] Watch them back at the size Meta will. If you cannot read the JSON, redo
      them larger.
- [ ] Submit with a plain-English note saying what each video shows and at which
      timestamp the call happens.
- [ ] The template created for video 2 stays on the account. Leave it — deleting
      it locks its name for 30 days, and you may need to record again.

---

## If a run fails

| What you see | What it means |
| --- | --- |
| `131030` | The recipient is not on the test number's allowed list. Add it at WhatsApp → API Setup → "To". |
| `190` | The token has expired or been revoked. Mint a new one; `meta_config_check.py` will confirm it. |
| `200` on the template call | The app does not hold `whatsapp_business_management` yet — which is what you are applying for. Standard access is enough to create a template on **your own** WABA, so if this fails on your own account, check the token's scopes rather than App Review. |
| `No WABA id` | See the checklist above. It is not discoverable from the token. |
| The delivery receipt never arrives | Only affects `--wait`, not the video. It needs Meta's webhook pointed at this deployment. |
