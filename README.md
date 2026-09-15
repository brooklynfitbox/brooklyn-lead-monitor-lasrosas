# Brooklyn Lead Monitor

Watches the Brooklyn Fitboxing admin portal (My Hit4Change) for new leads and
notifies you when one appears, by email, WhatsApp, or both at once — whichever
`NOTIFY_CHANNEL` selects (`email`, `whatsapp`, or `email,whatsapp`). Runs on
GitHub Actions, roughly every ten minutes, and never announces the same lead
twice.

It notifies about new leads in **`pre-order`** status — a lead that has come in
and not yet been picked up by staff. One message covering each batch of new
leads, never the same lead twice.

The email is **redacted by default**: it tells you how many leads arrived and
the exact time each came in, with a link to the portal, but carries no name,
email or phone — that data stays in the portal rather than travelling over
Gmail. Set `NOTIFY_INCLUDE_PERSONAL_DATA=true` to put the full contact details
in the email instead — and note that the same setting also controls what the
SQLite database stores, since it's committed to git (see below): leaving it
`false` means neither the email nor the repository's history ever holds a
name, email or phone, only IDs and timestamps. WhatsApp always sends only a
count and a portal link regardless of this setting — see "Getting started"
below for why.

There is a deliberate trade-off in that choice: `pre-order` is transient (staff
move a lead to `Nurturing` the moment they open it), so a lead created and
handled inside a single ten-minute polling gap leaves `pre-order` before the
monitor sees it, and is not notified. To close that gap and be told about
**every** new lead regardless of status, set `LEADS_STATUS_FILTER` blank. The
reasoning is in [ARCHITECTURE.md](ARCHITECTURE.md).

It also only notifies about **recent** leads. A `pre-order` lead that never
converted can sit on the portal for weeks, and the first time the database
meets one of those it would otherwise read as "new" and get mailed —
backlog noise, not a real alert. By default only leads created since
yesterday are notify-eligible (since Saturday if today is Monday, so the
weekend isn't missed — nobody is watching the portal on a Saturday). Older
matching leads are still recorded, just never emailed. Set
`NOTIFY_RECENT_LEADS_ONLY=false` to be told about every match regardless of
age.

## How it works

A run authenticates against the portal, fetches the current leads, keeps the
ones in `pre-order`, records any it has not seen before in SQLite, and sends a
single email covering them. The interesting parts are why each of those steps is
shaped the way it is, which is written up in [ARCHITECTURE.md](ARCHITECTURE.md).

Playwright is used only to authenticate. The portal attaches its credential to
each request from memory — it is in neither the cookies nor browser storage — so
the browser logs in, watches the app make its own API call, and lifts the auth
header off it. That header is cached and replayed over plain HTTP, so a normal
run is one request and no browser at all; a browser starts roughly once every
two hours, when the session ages out.

## Getting started

You need Python 3.11 or newer.

```bash
git clone <your repo url>
cd brooklyn-lead-monitor
pip install -e ".[dev]"
python -m playwright install chromium

cp .env.example .env   # then fill it in
```

### Step one: get the leads query

The portal is a GraphQL application with one endpoint at `/fs1`, and the query
that fetches the leads is already built in — it was recovered by probing the
live schema, so `LEADS_GRAPHQL_QUERY` can be left blank unless the application
is redeployed with renamed fields.

`lead-monitor introspect` exists for that case, but note that **introspection is
currently disabled on this server**, so it will report an error rather than a
schema. If the fields ever change, the practical route is the same one that
worked here: ask for a deliberately misspelled field and read the "Did you mean"
suggestion out of the error.

`LEADS_CLIENT=graphql` and `LEADS_API_PATH=/fs1` are already the defaults.

If you would rather see the raw network traffic, `lead-monitor discover` opens
the Leads page in a browser and records everything it fetches. Its output goes
to `discovery/`, which is gitignored because those captures contain real
personal data. Do not commit them.

### Step two: check the notification channel

```bash
lead-monitor test-notify
```

This sends one test message per channel `NOTIFY_CHANNEL` names and touches
nothing else, so a problem with a channel is diagnosed without involving the
portal. Set `NOTIFY_CHANNEL=email,whatsapp` to run both at once — it tries
each independently and reports each on its own line, so a broken WhatsApp
template doesn't hide whether email still works, or vice versa.

For Gmail, `SMTP_PASSWORD` must be an
[App Password](https://support.google.com/accounts/answer/185833), not your
account password, and your account needs 2FA enabled for that option to exist.

For WhatsApp (`NOTIFY_CHANNEL=whatsapp`): this uses the official **WhatsApp
Business Cloud API** through Meta Business Manager — the same platform a
WhatsApp Business account already gives you access to — not personal-account
automation, which risks the number being banned and needs a browser session
kept alive between runs that a stateless CI job doesn't have. Setup:

1. At [developers.facebook.com](https://developers.facebook.com), create an
   app of type "Business" and add the **WhatsApp** product to it. If the
   WhatsApp Business account is already verified for the club's own number,
   link that account to the app instead of using the Meta-provided test
   number, so alerts come from a number staff recognise.
2. On the app's **API Setup** page, copy the **Phone Number ID** (not the
   phone number itself) into `WHATSAPP_PHONE_NUMBER_ID`.
3. In Meta Business Manager, under **Business Settings → Users → System
   Users**, create a system user, generate a **permanent token** scoped to
   `whatsapp_business_messaging`, and put it in `WHATSAPP_ACCESS_TOKEN`. A
   temporary token from the API Setup page's "Temporary access token" field
   also works for a first test, but expires in 24 hours and will silently
   break the schedule — use the permanent one for anything left running.
4. Business-initiated messages — which this is, since nobody replies to the
   monitor — must use a template pre-approved in Meta Business Manager under
   **Account tools → Message templates**; a free-form message sent outside a
   24-hour customer reply window is rejected. Create a **Utility**-category
   template with exactly two body variables, for example:

   ```
   Brooklyn Fitboxing: {{1}}. Ver portal: {{2}}
   ```

   with sample values `3 leads nuevos en pre-order` and the portal URL.

   **If more than one deployment sends to this same `WHATSAPP_TO` number**
   (see "Running this for more than one center" below — this is the normal
   case whenever two centers share one WhatsApp Business account), use three
   variables instead, with the center name leading:

   ```
   Brooklyn Fitboxing {{1}}: {{2}}. Ver portal: {{3}}
   ```

   sample values `A Coruña — Pinar`, `3 leads nuevos en pre-order`, and the
   portal URL — and set `CENTER_NAME` in that deployment's config (see
   below). Get this decision right before requesting approval: the template
   shape is fixed once Meta approves it, and changing from two variables to
   three later means submitting a new template and re-approving, not editing
   the existing one.

   Approval is usually minutes, occasionally up to a day. Put the approved
   template's name in `WHATSAPP_TEMPLATE_NAME` and its language code (e.g.
   `es`) in `WHATSAPP_TEMPLATE_LANGUAGE`.
5. `WHATSAPP_TO` is the recipient's number in E.164 (e.g. `+34600123456`),
   comma-separated for more than one, same shape as `MAIL_TO`. Unlike a bot
   platform, the recipient does not need to message anything first — the
   approved template is what makes a business-initiated message allowed.

Because a template's placeholders are fixed in number at approval time, a
WhatsApp alert always carries a count and a portal link (plus the center
name when `CENTER_NAME` is set) — it cannot list each lead's name, email or
phone the way the redacted-off email can, so `NOTIFY_INCLUDE_PERSONAL_DATA`
has no effect on this channel.

### Step three: run it

```bash
lead-monitor run
```

The first run seeds. Every lead currently on the portal is recorded without an
email being sent, because a cold start would otherwise mail you the entire
backlog at once. Real notifications begin from the second run. Set
`SEED_WITHOUT_NOTIFYING=false` if you would rather be told about everything that
is already there.

`lead-monitor status` summarises what the database knows: how many leads, how
many are waiting to be announced, and whether the last run succeeded.

## Deploying to GitHub Actions

Push the repository, then add these under **Settings → Secrets and variables →
Actions**.

As **secrets**:

| Name | What it is |
| --- | --- |
| `PORTAL_BASE_URL` | Portal address, no trailing slash |
| `PORTAL_USERNAME` | Portal login |
| `PORTAL_PASSWORD` | Portal password |
| `SMTP_HOST` | e.g. `smtp.gmail.com` — only needed for `NOTIFY_CHANNEL=email` |
| `SMTP_USERNAME` | Sending account — email channel only |
| `SMTP_PASSWORD` | App password — email channel only |
| `MAIL_FROM` | Sender address — email channel only |
| `MAIL_TO` | Recipients, comma-separated — email channel only |
| `WHATSAPP_ACCESS_TOKEN` | Permanent System User token — only needed for `NOTIFY_CHANNEL=whatsapp` |
| `WHATSAPP_PHONE_NUMBER_ID` | Sending number's Phone Number ID — WhatsApp channel only |
| `WHATSAPP_TO` | Recipient number(s) in E.164, comma-separated — WhatsApp channel only |

Only the secrets for the channel(s) actually named by `NOTIFY_CHANNEL` need
values; an unused channel's fields can be left blank.

As **variables** (not secret, and each has a working default):
`CENTER_NAME`, `LEADS_CLIENT`, `LEADS_API_PATH`, `LEADS_GRAPHQL_QUERY`,
`LEADS_GRAPHQL_OPERATION`, `LEADS_GRAPHQL_VARIABLES`, `LEADS_STATUS_FILTER`,
`NOTIFY_CHANNEL`, `NOTIFY_INCLUDE_PERSONAL_DATA`, `NOTIFY_RECENT_LEADS_ONLY`,
`NOTIFY_TIMEZONE`, `PORTAL_LOGIN_PATH`, `PORTAL_LEADS_PATH`, `SMTP_PORT`,
`WHATSAPP_TEMPLATE_NAME`, `WHATSAPP_TEMPLATE_LANGUAGE`, `WHATSAPP_API_VERSION`.
`NOTIFY_CHANNEL` defaults to `email`; set it to `whatsapp` to switch entirely,
or `email,whatsapp` to fire both for every batch (see "Both channels at
once" below). Unlike the secrets above, `WHATSAPP_TEMPLATE_NAME` is a plain
variable — a template name isn't sensitive — but it still only matters when
`whatsapp` is one of the selected channels.
`CENTER_NAME` is blank by default; see "Running this for more than one
center" below for when it matters.

The workflow keeps the SQLite database on a branch called `state`, which it
creates on the first run. You never need to touch that branch, but it is worth
knowing it exists: it is why a lead is not announced twice, and deleting it
would cause the next run to re-seed.

To start over, run the workflow manually from the Actions tab with **reseed**
ticked. That discards the stored state and re-seeds silently.

To confirm the notification channel itself works — credentials, how the
message actually renders, spam filtering for email — run the workflow
manually from the Actions tab with **mode** set to `test-notify`. It sends
one synthetic message per channel `NOTIFY_CHANNEL` names and does not touch
the database or the portal, so it is safe to run any time, as often as you
like.

## Both channels at once

`NOTIFY_CHANNEL=email,whatsapp` sends every batch to both — an email to a
shared inbox and a WhatsApp alert straight to a phone, say. Fill in both
channels' fields (the email ones from "Deploying to GitHub Actions" above,
the WhatsApp ones from "Step two" above); each is validated independently at
startup, so a half-configured second channel fails immediately rather than
silently sending only the first.

The two channels are attempted independently every run: a broken WhatsApp
template doesn't stop the email from going out, and vice versa. If either
one fails, the whole run is treated as failed — nothing gets marked
notified — so the next run retries *both* channels, including whichever one
already succeeded. That can mean a duplicate on the channel that worked;
accepted for the same "duplicate beats silently dropped" reasoning used
everywhere else in this project (see ARCHITECTURE.md).

## Running this for more than one center

Each center gets its own copy of this repository — its own GitHub repo, own
Secrets and Variables, own `state` branch, own schedule. That isolation is
deliberate: a login form that breaks at one club, or a portal that changes its
GraphQL schema, then only ever breaks that one club's monitor, not all of
them, and each center's lead history stays in its own database rather than
four clubs' data interleaved in one.

To add a center: copy this repository (a GitHub template repo, or `git clone`
and push to a new remote, either works), then set that copy's own Secrets and
Variables. Two groups:

**Different per center**, always: `PORTAL_BASE_URL`, `PORTAL_USERNAME`,
`PORTAL_PASSWORD` — each club logs into its own portal instance under its own
account, even when every center runs the same "My Hit4Change" software.
`CENTER_NAME` is different per center too — set it to something short and
recognisable (the town, the franchise code) the moment two or more centers'
monitors notify the same inbox, which is the normal case once you're past one
center. Left unset, every center's alerts look identical in the inbox except
for the portal link buried in the body.

**The same across centers**, usually: everything else — `LEADS_CLIENT`,
`LEADS_API_PATH`, and the GraphQL query variables, since they describe the
shared platform's API rather than any one club's data; `LEADS_STATUS_FILTER`,
`NOTIFY_RECENT_LEADS_ONLY`, `NOTIFY_TIMEZONE`, `NOTIFY_INCLUDE_PERSONAL_DATA`,
since they're policy decisions, not per-club facts; `NOTIFY_CHANNEL` and
whichever channel's non-recipient settings it needs (`SMTP_HOST` /
`SMTP_USERNAME` / `SMTP_PASSWORD` if every center sends through the same
mailbox). `MAIL_TO` (or `WHATSAPP_TO`) can also be identical across every
center if they all land in one shared inbox — that's exactly the setup
`CENTER_NAME` exists for.

Worth confirming before assuming the GraphQL query travels unchanged:
`LEADS_GRAPHQL_QUERY` was recovered for *this* portal instance by probing its
validator (see ARCHITECTURE.md), and every "My Hit4Change" deployment should
share the same schema since it's the vendor's platform code rather than each
franchise's own — but a first `lead-monitor run` (or `lead-monitor discover`)
against the new center confirms it in one run rather than assuming it.

## Things worth knowing

GitHub's scheduler is best-effort. Under load, ten-minute crons drift — fifteen
to twenty-five minutes is common — and an occasional tick is skipped. If you
need a hard ten-minute guarantee, this needs to run somewhere other than Actions.

Scheduled workflows are disabled automatically after 60 days without repository
activity. GitHub emails you before it happens.

Duplicate emails are possible but bounded. If a run is killed in the window
between the mail server accepting a message and the database recording that it
did, that batch is re-sent next time. This is deliberate: the alternative
ordering loses leads silently, and a repeat is easier to live with than a miss.

## Development

```bash
pytest                 # or: python -m unittest discover -s tests
ruff check .
mypy
```

Tests are written as `unittest.TestCase`, so they run under the standard library
alone as well as under pytest. Nothing in the suite needs a network, a portal or
a mail server.
