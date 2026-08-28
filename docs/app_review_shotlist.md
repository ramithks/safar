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

**⚠ The card is new, so the live site does not have it yet.** `api.hostos.in`
and `app.hostos.in` are still running the code from before this work. Two ways
round it, and the quick one is fine:

*Record locally* (no deploy, ~2 minutes to set up). The reviewer is grading a
person pressing a button in your app and the message arriving — not which host
served the page.

```
# terminal 1 — the backend
cd travbuddy_backend
set -a; . ./.env.hotelos; set +a
./venv/bin/uvicorn app.main:app --port 8000

# terminal 2 — the app
cd Travbuddy_supply_os
npm run dev            # http://localhost:5173, already points at :8000
```

*Or deploy* both repos first, and record against `app.hostos.in`. Unambiguous,
and you need the deploy for real hotels anyway — but it is not required for the
video.

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
>
> *"You can use **WhatsApp Manager** to create a message template for your use
> case. Record a video of creating and saving the template."*

**This is two separate requirements, and only one of them is the video.**

### 2a. The video — record it in WhatsApp Manager, not in code

Meta's own instruction is to use the WhatsApp Manager UI. That is simpler than
driving the API on camera and it needs no WABA id, no deploy and no terminal.

Go to **business.facebook.com → WhatsApp Manager → Message templates → Create
template**, and record the whole thing in one take.

| # | On screen | Why it matters |
| --- | --- | --- |
| 1 | WhatsApp Manager, Message templates list | Establishes the "before" |
| 2 | Pressing **Create template**, choosing a category and name | The act being reviewed |
| 3 | Typing the body, including at least one `{{1}}` and its sample value | A template with no parameters looks trivial |
| 4 | Pressing **Submit** | The moment it is created |
| 5 | The template appearing in the list as **In review** | The "after" |

`In review` is the correct end state. Meta reviews the template's *content*
separately; the permission is for creating one.

Use a genuine hotel template — a booking confirmation reads as a real use case.
The six in `whatsapp/provisioning.py` are good sources of wording.

### 2b. The API test calls — separate, and they need the WABA id

> *"Go to our published Postman collection to complete the requirements. Keep in
> mind, testing data can take 24 hours to show in the App Review submission
> form."*

Meta wants to see that the app has actually **called** the API for this
permission. Two ways, and either counts:

- **Meta's Postman collection** — the link in their instructions. It will ask for
  your WABA id and a token.
- **Our own script**, which makes the same call against our own account:

  ```
  ./venv/bin/python scripts/app_review_evidence.py --management --waba <WABA_ID>
  ```

  It lists the templates, creates one, and reads it back — a real
  `POST /{waba_id}/message_templates`.

Either way you need `WHATSAPP_WABA_ID`. It is **not discoverable from the
token** — `me/businesses`, `assigned_whatsapp_business_accounts` and the phone
number node all come back empty or reject the field. Read it off **WhatsApp →
API Setup → "WhatsApp Business Account ID"**, under the phone number id.

**Do this first.** Meta says test data takes up to 24 hours to appear in the
submission form, so it is the long pole — make the calls today, record the
videos while it propagates.

---

## After both videos

- [ ] Watch them back at the size Meta will. If you cannot read the JSON, redo
      them larger.
- [ ] Submit with a plain-English note saying what each video shows and at which
      timestamp the call happens.
- [ ] The template created for video 2 stays on the account. Leave it — deleting
      it locks its name for 30 days, and you may need to record again.
- [ ] Check the API test calls have landed in the submission form before
      submitting. Meta says up to 24 hours.

---

## Submitting

App Dashboard → **App Review → Permissions and Features**.

1. Find **`whatsapp_business_messaging`** and **`whatsapp_business_management`**
   and request **Advanced Access** on each. That builds one submission
   containing both.
2. For each permission you are asked how your app uses it and for step-by-step
   instructions a reviewer could follow. Keep it plain and concrete:
   - *messaging* — "Hotels connect their own WhatsApp number. Guest messages
     reach our AI front desk, which answers with the hotel's own rooms, rates
     and policies, and hands anything unusual to staff. We send booking
     confirmations, check-in links and pre-arrival details."
   - *management* — "We create and manage message templates on each hotel's own
     WhatsApp Business Account on their behalf, and track approval status, so a
     hotel does not have to author WhatsApp templates itself."
3. Attach the video to the matching permission. **Do not attach the same file
   twice** — Meta asks for a separate, unique video per permission and rejects
   duplicates.
4. Check the API test calls have registered before you submit. Meta says up to
   24 hours.
5. Submit, and expect a few working days. If something is refused, the reply
   names the permission — re-record only that one.

**Before you press submit**, confirm all of: business verification complete;
app icon, Privacy Policy URL and category set; both videos attached to the right
permissions; test calls showing.

---

## If a run fails

| What you see | What it means |
| --- | --- |
| `131030` | The recipient is not on the test number's allowed list. Add it at WhatsApp → API Setup → "To". |
| `190` | The token has expired or been revoked. Mint a new one; `meta_config_check.py` will confirm it. |
| `200` on the template call | The app does not hold `whatsapp_business_management` yet — which is what you are applying for. Standard access is enough to create a template on **your own** WABA, so if this fails on your own account, check the token's scopes rather than App Review. |
| `No WABA id` | See the checklist above. It is not discoverable from the token. |
| The delivery receipt never arrives | Only affects `--wait`, not the video. It needs Meta's webhook pointed at this deployment. |
