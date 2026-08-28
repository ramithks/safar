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
- [ ] Screen recording is at a resolution where the terminal text is legible
      when Meta scales the video down. Increase the font size first.
- [ ] Nothing secret is on screen: no `.env` open in another window, no token in
      shell history you might scroll to. The script never prints one.

---

## Video 1 — `whatsapp_business_messaging`

> *"Record a video showing a message sending from your app to a WhatsApp number.
> The video should show your app sending the message, as well as the WhatsApp
> interface (either web or mobile app) receiving the same message."*

Both halves have to be in **one continuous take**. A cut between the send and
the receipt is the most common reason this video is rejected — the reviewer
cannot tell the two are related.

**Set up before pressing record:** terminal on one side, WhatsApp Web logged in
as the recipient handset on the other, both visible at once.

```
./venv/bin/python scripts/app_review_evidence.py --messaging --wait 0
```

| # | On screen | Why it matters |
| --- | --- | --- |
| 1 | The command being typed and run | Establishes that an application is doing this, not a console |
| 2 | Step 1 — the `POST .../messages` URL and the JSON body | The reviewer must see the API call itself |
| 3 | Step 2 — Meta's reply with the `wamid.…` message id | Proof Meta accepted it |
| 4 | **Move to the WhatsApp window.** The message arriving, with its timestamp | The other half of the requirement |
| 5 | Hold on the message for ~3 seconds | Gives the reviewer time to match the text |

The message body carries a timestamp — read it aloud or make sure it is legible
in both windows. That is what makes "the same message" verifiable.

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
