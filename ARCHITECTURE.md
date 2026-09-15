# Architecture

This document explains why the code is shaped the way it is. The README covers
how to run it.

## The discovery question

The brief was to inspect the portal first and use an internal API if one exists,
falling back to browser automation only if it does not. That is the right
instinct — an HTTP request is a hundred times cheaper than a browser and does not
break when someone restyles a table — but it creates a dependency: the whole
design would seem to hinge on an answer nobody had yet.

It does not, because the answer only changes one module. `LeadsClient` is a
protocol with two implementations. `ApiLeadsClient` issues one HTTP request and
maps the JSON. `DomLeadsClient` renders the page and parses the table. Both
produce `Lead` objects, and storage, diffing and notification never learn which
one ran. Discovery therefore sets a configuration value rather than triggering a
rewrite, and switching later is an environment variable.

Discovery itself is committed as code (`discovery.py`, run via
`lead-monitor discover`) rather than performed once by hand in DevTools. When
the portal is redeployed, rerunning it gives a fresh answer in a minute. It
records every XHR the Leads page makes and scores each by JSON content type, an
array-of-objects shape, lead-like field names in English or Spanish, and whether
a Pre Order value appears in the body. The highest scorer is the recommendation.

### What discovery found

Run against `https://myh4c.brooklynfitzone.com/#/leads` on 2026-07-29, by
watching the live application rather than the automated capture.

The portal is a single-page application called My Hit4Change, and it does have
an internal API: a **GraphQL** endpoint at `POST /fs1`. Everything the
application does goes through it, wrapped in the standard
`{"operationName", "variables", "query"}` envelope, which is why every request
appeared to hit the same opaque URL. Authentication is a session cookie.

More useful than the endpoint is the traffic pattern: the Leads page issues
exactly **one** request on load and then does everything else in the browser.
The 30/60/90/ALL range buttons and the status filters trigger no further
network activity, which means the response carries the full lead set and the
client filters it locally. Volume is small — 41 leads across 90 days for this
franchise — so there is no pagination to handle and one request every ten
minutes is the entire cost of monitoring.

The table exposes Created At, Name, Email, Phone, Status, Channel, Last
Activity and Last Modified, all of which the existing field mapping already
covers.

### The schema, and how it was recovered

Introspection is **disabled** on this server: `__schema` returns an errors array
saying so. `lead-monitor introspect` therefore fails against this portal, and is
kept only for the case where it is ever re-enabled.

The schema was recovered instead from the validator's own error messages, which
are considerably more talkative than the introspection switch suggests. Asking
for a misspelled field produces "Did you mean X?", asking for a scalar with a
selection set names its type, and supplying an unknown argument enumerates the
real ones. A dozen deliberately wrong queries gave the whole shape:

```graphql
query Leads {
  leads {              # [Lead!]!, takes no arguments
    id                 # lowercase — the only field that is
    FirstName          # there is no single name field
    LastName
    Email
    Phone
    Status
    CreatedDate
    LastActivityDate
    LastModifiedDate
  }
}
```

Two details in there would each have produced quietly wrong results. Every field
is PascalCase **except** `id`, a mixture nobody would guess. And the name is
split across two fields, so a single-field lookup returns nothing and every
notification reads "(no name)" — the mapping layer joins them back together.
There are tests for both.

### The authentication scheme, and why the cookie is not enough

Replaying `{ leads { ... } }` with the session cookie alone makes the resolver
throw `Cannot read properties of null (reading 'district')` — a null user
dereferenced. The query is valid and reaches the resolver, so the request is
arriving unauthenticated. Confirmed by a control: the app's own `notifications`
query, which succeeds on every page load, returns **"Not Authorised!"** when
replayed the same way.

The credential is not in the cookies, not in localStorage and not in
sessionStorage — three separate probes found nothing. It lives in a JavaScript
variable in memory and is attached to each request explicitly, which is why it
vanishes on reload and cannot be lifted from stored state. A bare `fetch` from
inside the authenticated page fails too, so it is not a global request wrapper
either; the app's own HTTP client adds it per call.

The consequence for the design is that **Playwright is not optional here** — no
reconstructed HTTP request can authenticate, because the token can only be
observed, never derived. So `auth.py` captures it the one way that works: during
the browser login it listens to the requests the app itself makes to `/fs1` and
lifts the `Authorization` (and any `x-*`) header verbatim off a real one. That
header is cached alongside the cookies and replayed by the HTTP client on
subsequent runs, so steady state is still a browserless request — the browser
runs only to mint the header, roughly every two hours, exactly as for any other
session.

If the capture comes up empty the run logs a warning naming the likely cause
rather than failing with an opaque "Not Authorised", and a 401 on any later
fetch forces a fresh browser login.

Note what the unauthenticated failure looks like from outside: **HTTP 200**. It
is the exact trap described below, met for real before the code shipped.

### The trap GraphQL sets for a monitor

A failed GraphQL query still returns **HTTP 200**. A permission error, a field
renamed by a redeploy, an expired scope — all arrive as a cheerful 200 with a
null `data` and an `errors` array. Code that checks only the status code reads
that as "no leads today" and goes quiet indefinitely, which for this system is
the worst available failure: it looks exactly like a working monitor on a slow
week.

So the client inspects `errors` before anything else and raises, including when
`errors` arrives *alongside* partial data. A run that could not fetch fails
loudly, is recorded as failed, and exits non-zero. It never reports zero leads
unless the portal genuinely said zero.

## Authentication is where Playwright earns its place

Login flows involve CSRF tokens, sometimes a JavaScript-driven form, occasionally
a redirect chain. Reverse-engineering that into raw HTTP calls is possible and
brittle. Letting a real browser do it once is neither.

So `auth.py` performs the login, exports Playwright's `storage_state`, and pulls
out both the cookies and any bearer token sitting in `localStorage`. Both are
collected because portals split roughly evenly between the two schemes and which
one this portal uses is unknown; carrying both costs nothing and removes a class
of works-locally-fails-in-CI surprise.

That session is cached to disk and reused until it ages out (two hours by
default) or the portal returns a 401. The practical effect is that a browser
starts about once every two hours rather than once every ten minutes, and the
other seventeen runs in that window are a single HTTP request each.

Login-form selectors are heuristic — find the password input, take the text field
before it, press Enter — with configuration overrides for when the heuristic
guesses wrong. A changed CSS class should be a settings edit, not a code change.

Success is verified by having navigated away from the login page. A form that
silently re-renders itself is the classic wrong-password response, and treating
it as success would mean cheerfully monitoring nothing forever.

## The status filter, and its one trade-off

The monitor notifies about leads in `pre-order` — the status an unhandled lead
shows before a member of staff opens it and moves it to `Nurturing`. That is the
moment worth alerting on: a fresh lead nobody has picked up yet.

This has one deliberate cost. `pre-order` is transient, so a lead created and
handled inside a single ten-minute polling gap has already left `pre-order` by
the time the monitor looks, and is never notified. That was judged acceptable
because a lead handled that fast has, by definition, already been seen by
someone — the alert's job is done. Its counterpart is documented and one setting
away: `LEADS_STATUS_FILTER` blank notifies about every new lead regardless of
status, trading a little noise (leads already in progress) for never missing one.

Matching is forgiving about case and punctuation, so `pre-order`, `Pre Order`
and `PREORDER` are equivalent — the portal spells it `pre-order`.

## The recency window: not every unseen lead is new

`record_seen`'s dedup guarantees a lead is never *announced twice*. It says
nothing about whether a lead deserves an announcement in the first place, and
those are different questions. A `pre-order` lead that never converted stays
in that status indefinitely — the portal has no automatic timeout that moves
it along — so the database can meet a lead for the first time weeks after it
actually arrived: a reseed, a schema change to a field the fingerprint reads,
a gap in coverage while the workflow was disabled. Every one of those reads,
correctly by the dedup logic and incorrectly by any human reading the alert,
as "new."

The rule that closes this comes from Clarita, who works the leads day to day:
notify on leads from the last day. If today is Monday, also from the weekend
— nobody is watching the portal on a Saturday, so a lead that arrived then is
still the first thing worth seeing Monday morning, not backlog.

That translates to a cutoff, not a rolling window: the start of yesterday in
local time, or the start of Saturday if today is Monday. `recency.py` does
this calculation in `NOTIFY_TIMEZONE` (`Europe/Madrid` by default) rather
than in UTC, deliberately — "yesterday" and "Monday" are calendar concepts to
the person who defined the rule, and computing them in UTC would shift the
boundary by Madrid's UTC offset, including across the DST change twice a
year. A rolling "last 24 hours" was considered and rejected: it does not
special-case Monday at all, and grafting a weekend extension onto an elapsed
time rather than a calendar day produces a boundary nobody asked for and
nobody could explain by looking at the code.

Leads that fail the recency check are not simply skipped — they are recorded
with `already_notified=True`, the same mechanism the cold-start seed uses.
This matters: without it, a stale lead would still have `notified_at` NULL,
and it would resurface as "pending" and get emailed on some later run once it
happened to still be unseen, defeating the whole point. Recorded-and-silenced
means a lead is evaluated against the window exactly once, at first sight,
and its fate is then locked in by the ordinary dedup path forever after.

A lead with no `created_at` is treated as recent rather than dropped. The
GraphQL client always gets `CreatedDate` from the portal, so this should not
happen in practice; if it ever does, the two failure directions are not
symmetric — treating a missing timestamp as old risks silently losing a real
lead, while treating it as recent risks one extra email. The system already
fails toward extra emails over lost leads elsewhere (see "Never notifying
twice" below), and this follows the same rule.

The whole thing is a single toggle, `NOTIFY_RECENT_LEADS_ONLY` (default
`true`), for the case where someone deliberately wants the full backlog —
after a manual reseed with seeding turned off, for instance.

## The login form has two shapes, and a decoy input

Two live GitHub Actions failures, on two different lines, came from the same
root cause: the login form was investigated in a browser that was not
actually clean.

Clearing localStorage, sessionStorage and every cookie reachable from
JavaScript still left an `httpOnly` server-side session cookie in place —
JavaScript cannot read or clear those. With it present, the form shows the
account as a fixed block (`A-CORUNA / finisterre@brooklynfitboxing.com`) and
only a password field. Without it — genuinely never visited, which is every
GitHub Actions run — the form shows an editable e-mail field *and* the
password field together, on one screen. Reloading the same browser produced
both versions at different times, which is what exposed the mistake: this is
not a two-step wizard, it is one form whose first field the server fills in
for you when it recognises the browser.

The email field's accessible name is `"Your e-mail address *"`, `type="text"`,
not `type="email"` — worth knowing if `LOGIN_USERNAME_SELECTOR` is ever set
explicitly.

The first failure (15s timeout waiting for the password field) is consistent
with a cold GitHub Actions runner taking longer than a warmed browser to
hydrate the SPA bundle; `LOGIN_TIMEOUT_SECONDS` moved from 15 to 45 to cover
it. The second failure was sharper: `_fill_login_form`'s heuristic picked the
first element matching `input:not([type])`, which is not the email field but
a readonly accessibility focus-target behind the "Your language" combobox —
same page, sorts earlier in the DOM. It is genuinely visible, so an
`is_visible()` guard did not catch it; Playwright spent 30 seconds retrying a
fill on an element that will never become editable before giving up. The
selector now excludes `[readonly]` directly, and `is_editable()` backs it up
for any other portal whose equivalent field is disabled rather than readonly
instead. `_fill_login_form` fills the email field when the selector finds one
and skips straight to the password field when it does not — both are correct
depending on which of the two shapes the run happens to see.

## Never notifying twice

This is the requirement everything else bends around, and the naive
implementation gets it wrong. Fetch leads, insert the new ones, send an email:
if the process dies between the insert and the send, the lead is recorded as
seen and no email ever goes out. The next run sees nothing new. The lead is lost
silently, which is the worst possible failure for a system whose entire job is
telling you about leads.

So the write is split in two. `record_seen` inserts with `notified_at` NULL.
`mark_notified` stamps it, and is called only after the mail server has accepted
the message. `pending_notifications` returns everything unstamped, including
leads left over from earlier failed runs, which is what makes the guarantee
survive a crash.

Walking the failure cases: a crash before the insert means the lead is still on
the portal and is picked up next run. A crash between insert and send leaves it
unstamped and queued. A failed send leaves it unstamped and queued, with a
failure counter incremented so a permanently stuck lead is visible rather than
silent. A crash after the mail server accepted but before the stamp lands causes
exactly one duplicate.

That last window is the only one that produces a repeat, and it is deliberate.
The alternative ordering — stamp first, then send — closes it by opening a worse
one where leads vanish. A duplicate email is an annoyance; a missed lead is lost
revenue. The system fails toward annoyance.

Identity is `external_id`, the portal's own key, with a `fingerprint` over name,
email and normalised phone as a secondary unique constraint. The fingerprint
deliberately excludes the identifier — including it would make the hash differ in
exactly the renumbering case the fingerprint exists to catch — and excludes
status, since a lead moving out of Pre Order is the same person. Phone numbers
are reduced to their trailing national digits so `+34 600 123 456`,
`0034600123456` and `600123456` agree.

## The database is committed to git, so it gets the same redaction the email does

For a while this wasn't true, and it was a real gap: the notifier redacts
personal data from the email by default (see "The status filter" section and
notifier.py's docstring — name, email and phone stay in the portal, out of
the message), but `record_seen` was writing all three, plus the raw API
payload, into the `leads` table regardless. That table sits in a SQLite file
that gets committed to the `state` branch and pushed to GitHub on every run.
A redacted email travelling over Gmail was never the actual attack surface —
the repository's own history was, and it held the full contact list in plain
text the whole time.

`LeadStore` now takes a `store_personal_data` flag. `record_seen` writes
empty strings for `name`, `email` and `phone` and `{}` for `raw` when it's
off, and `monitor.run_once` wires it straight to
`Settings.notify_include_personal_data` — the same switch that controls the
email. One setting, one meaning: redacted means redacted everywhere the data
would otherwise land, not just in the message body. It defaults to `True` at
the `LeadStore` level (not `False`) so a direct construction — mainly tests,
and the read-only `status`/`init-db` CLI commands that never call
`record_seen` anyway — isn't silently changed by adding the parameter;
production is what forces the value, via `run_once`.

Two things this does *not* do. It doesn't touch the fingerprint, which stays
a one-way hash either way — dedup across a renumbered `external_id` keeps
working with personal data storage off, because the hash never needed the
plaintext to survive, only to have existed once at insert time. And it
doesn't retroactively scrub rows already written before this flag existed —
a `state` branch created by an older build still has full contact details in
whatever commits came before the fix. The forward fix stops the bleeding;
cleaning up already-committed history means re-seeding (`reseed` on the
workflow, or deleting the `state` branch) so the next single-commit,
force-pushed snapshot is redaction-clean. Even then, the old commits with
personal data become unreachable rather than instantly gone — GitHub garbage
collects unreferenced objects on its own schedule, not on request — which
matters if the repository is ever made public or shared beyond people who
already had access.

## A second channel: WhatsApp Business, and why it isn't just "email but different"

Email was the only channel for a while, and that was fine until the person
actually meant to receive these alerts asked for something that reaches her
phone directly rather than an inbox she doesn't have open. The first design
here used Telegram: an unofficial WhatsApp automation against a personal
number was ruled out early (against WhatsApp's terms, real ban risk, and a
poor fit besides — it wants a live, logged-in session between runs, which a
stateless CI job restarting every ten minutes does not have), and Telegram's
Bot API was the closest thing to it that's actually official — a plain
HTTPS POST with a token, no session to keep warm.

That changed once it turned out the club already has a WhatsApp Business
account. WhatsApp Business ships an equivalent official channel — the
**WhatsApp Business Cloud API**, reached through Meta Business Manager — so
the whole point of Telegram (avoid unofficial automation) is available on
the platform the recipient already uses, and Telegram was dropped in favour
of it rather than kept alongside it: one working channel to maintain and
document beats two, and the project's own dependency count is a value it
states elsewhere (see "Honest limits" below). `NOTIFY_CHANNEL=whatsapp`
selects it.

`NotifyChannel` is a two-value enum and `build_notifier` reads it to return
either `SmtpNotifier` or `WhatsAppNotifier`, the same factory shape
`clients/__init__.py` already uses to pick a `LeadsClient`. Nothing upstream
of the notifier — fetching, diffing, storing — knows or cares which channel
is live; `monitor.run_once` calls `notifier.send(leads)` exactly as before.

Settings enforces the channel choice at startup, not at send time. The two
channels' required fields used to be simply required; now they are required
*conditionally*, checked by a `model_validator` that inspects
`notify_channel` and lists by name whatever the chosen channel is missing —
`SMTP_HOST`, `WHATSAPP_ACCESS_TOKEN`, and so on — before the run has fetched
anything. The alternative, discovering a missing token only when `send()`
first runs, would mean a run that authenticates, fetches, records leads as
seen, and only then fails to tell anyone — the exact silent-loss shape this
whole project exists to avoid elsewhere.

WhatsApp's Cloud API has one structural constraint neither email nor a plain
bot API has: it will not deliver free-form text unless the recipient
messaged the business number within the last 24 hours, which nothing about
an unattended ten-minute schedule can promise. A business-initiated message
like this one must instead use a **template** pre-approved in Meta Business
Manager — fixed text with a fixed number of `{{n}}` placeholders, submitted
for review ahead of time (see the README for the exact template text this
project expects: two placeholders, a heading and a portal link). This is why
`WHATSAPP_TEMPLATE_NAME` exists as a required field for the channel and
`whatsapp_notifier.py` never constructs free-form message bodies the way
`notifier.py` builds an email — there is nothing to construct beyond filling
in the two approved placeholders.

That same fixed-shape constraint is also why `NOTIFY_INCLUDE_PERSONAL_DATA`
has no effect on WhatsApp. A template's parameter count is fixed at approval
time; it cannot grow to fit an arbitrary per-lead list of names, emails and
phones the way the email body can. So the WhatsApp message always carries
only a count and a portal link — the same content the *redacted* email mode
carries, always, regardless of the setting. This was a deliberate choice to
document rather than route around (a two-parameter template that omits
contact details, versus registering a second, larger template for "full"
mode and keeping two templates in sync with Meta's review process for
marginal benefit): staff already open the portal to work a lead, so the
notification's job is "tell me now," not "give me everything," and settling
for the always-redacted shape keeps one template, one approval, one thing to
maintain.

The access token is logging-redaction-listed unconditionally, even when
`NOTIFY_CHANNEL=email` and WhatsApp never fires — it is one `httpx`
exception message or debug log line away from landing in a public CI log
otherwise. `Settings.secret_values()` includes it unconditionally for that
reason. It travels as an `Authorization: Bearer` header rather than embedded
in the URL the way Telegram's token was, which is a smaller leak surface but
not a zero one — request objects and their headers still end up in
tracebacks and verbose HTTP logs from time to time.

One trade-off carries over unchanged from the Telegram design it replaced.
`smtplib`'s `send_message` hands every recipient to the mail server in one
call — it either accepts the whole envelope or it doesn't. The Cloud API has
no equivalent: each recipient number is a separate HTTP request, in a loop.
If the first of two configured recipients succeeds and the second then
fails, `send()` still raises — correctly, since `mark_notified` must not run
on a partial failure — but a retry on the next pass re-sends to both,
including the one that already got the message. Accepted for the same
reason the crash-window duplicate on the email side is accepted: the failure
mode a duplicate message causes is mild annoyance, and the alternative is a
recipient who silently never hears about a lead. In practice this only bites
multi-recipient `WHATSAPP_TO` setups; a single recipient behaves exactly
like the all-or-nothing SMTP case.

## State on GitHub Actions

Every scheduled run gets a fresh runner with an empty disk. If the database does
not persist, every run sees every lead as new and mails the entire backlog every
ten minutes.

`actions/cache` is the obvious answer and the wrong one. Caches are evicted under
repository pressure and after seven days of inactivity. When that happens there
is no error — the run simply starts with no database and re-announces everything.
A failure mode that is silent, delayed and mails your whole lead list is not a
reasonable thing to build on.

The database therefore lives on a dedicated orphan branch called `state`,
written by the workflow using the built-in `GITHUB_TOKEN`. It is durable, costs
nothing, and leaves an audit trail. It is force-pushed as a single commit each
time because the database is state rather than history; keeping every
ten-minute snapshot would grow the repository without bound for no benefit.

The persist step runs even when the monitor failed, for the same reason the two
phase write exists: a lead recorded but not yet emailed has to survive to the
next run.

Concurrency is queued rather than cancel-in-progress. Two runs sharing one
database would race on the state branch, and cancelling a run mid-flight would
deliberately manufacture the crash window described above.

## Four centers, one inbox: CENTER_NAME and one repo per club

The brief moved from monitoring one club to four, all notifying the same
internal address. The deployment model this project already had — a single
GitHub repo is one monitor, with its own Secrets, its own `state` branch, its
own schedule — extends to four centers by being copied four times rather than
made multi-tenant inside one repo. That was a deliberate choice over the
alternative (one repo, a build matrix over four sets of credentials, four
logical databases inside it): the whole point of this project's `state`
branch and two-phase notification design is that one club's failure — a
changed password, a redesigned login form, a renamed GraphQL field — never
touches another club's monitoring. A shared repo with four credential sets
threads that same failure-isolation requirement through matrix configuration
and multiple database paths instead of getting it for free from separate
processes, for no benefit this project's scale needs. Four small,
independent, boringly-identical repos are easier to reason about than one
clever one.

That copy-and-reconfigure model has one real gap: nothing about the alert
itself said which club it was about. Before four centers existed, the email's
subject was always exactly "N leads nuevos en pre-order" and its sender was
always "Brooklyn Lead Monitor" — fine when there is one deployment and one
recipient, silently useless the moment four deployments feed the same inbox,
because every alert looks identical until it's opened and the portal link's
domain is read. `CENTER_NAME` closes that gap: a plain string, blank by
default (so a single-center deployment is unaffected), that `SmtpNotifier`
folds into the subject prefix and the `From` display name — the two things
visible without opening the message — and, for anyone who does open it, into
the HTML header and the plain-text body's first line too.

It was initially left out of `WhatsAppNotifier` on the assumption that
WhatsApp would only ever be one deployment to one number, so nothing to
disambiguate — wrong once it turned out two of the four centers (both in A
Coruña) have separate portal logins but share one WhatsApp Business account.
Sharing a destination is exactly the condition `CENTER_NAME` exists for,
regardless of channel, so it now also becomes the template's leading
parameter when set — see `whatsapp_notifier.py`. Unlike email, this changes
the template's *shape*, not just its content: a template approved with two
placeholders cannot suddenly take three, so a deployment that sets
`CENTER_NAME` needs its own template approved with three placeholders from
the start (see README). Left unset, nothing changes — same two-placeholder
template as before this existed.

## Both channels at once: CompositeNotifier

Every channel decision up to this point was framed as a choice — email *or*
Telegram, then email *or* WhatsApp — because `NOTIFY_CHANNEL` held exactly
one value and `build_notifier` returned exactly one `Notifier`. That stopped
being the whole story once some centers wanted an email to a shared inbox
*and* a WhatsApp alert to a phone for the same lead, not a choice between
them.

`NOTIFY_CHANNEL` became a comma-separated list (`Settings.notify_channels`)
rather than gaining a second setting like `NOTIFY_CHANNEL_2`, for the same
reason `MAIL_TO` and `WHATSAPP_TO` are already comma-separated lists rather
than numbered fields: one shape, reused, instead of a new one invented per
case. A single value parses to a one-element list and behaves exactly as
before — nothing about single-channel deployments changed.

`build_notifier` now builds one notifier per named channel and, when there's
more than one, wraps them in `CompositeNotifier`. The one-channel case still
returns that channel's notifier directly rather than a one-element
`CompositeNotifier` — no reason to add a layer of indirection to the
overwhelmingly common case, and it keeps `isinstance(notifier, SmtpNotifier)`
in the existing tests meaningful.

`CompositeNotifier.send` deliberately does not stop at the first failing
channel. It tries every one of them regardless of earlier failures, then
raises at the end if any failed. The alternative — stop at the first
exception, Python's default `for` loop behaviour — would mean a broken
WhatsApp template silently suppresses an email that would otherwise have
gone out, purely because of iteration order. Given the choice between "try
the working channel too" and "give up because one channel is down," this
project has picked the first option every time something similar has come
up (see "Never notifying twice" above), and this is the same choice again.

The cost of attempting every channel every time is the same duplicate-risk
trade-off already accepted for a single channel's multiple recipients: if
any channel failed, the whole call raises, so `monitor.run_once` never
marks the batch notified, and the next run retries *every* channel —
including the one that already succeeded. A repeat email or a repeat
WhatsApp message is the accepted cost of never letting a working channel go
quiet because a different one broke.

`cli.cmd_test_notify` follows the same "try everything, report everything"
shape rather than delegating to `CompositeNotifier` directly: it calls
`build_channel_notifier` once per channel and prints a pass/fail line for
each, so `test-notify` with both channels configured tells you which one is
actually broken instead of just "failed" — the diagnostic purpose the
command exists for (see cli.py's module docstring) would be weaker if a
composite failure hid which half worked.

## Honest limits

GitHub's cron is best-effort. Ten-minute schedules drift under load, commonly to
fifteen or twenty-five minutes, and ticks are occasionally skipped. Anything
needing a real ten-minute guarantee belongs on a host with a real scheduler.

Scheduled workflows are auto-disabled after 60 days of repository inactivity.

The DOM fallback is genuinely more fragile than the API path. It keys columns by
header text rather than position, so an inserted column does not shift every
field, but a redesign will still break it. It is insurance, not a plan.

The trailing-digits phone rule can collide across country codes in principle.
The fingerprint also carries name and email, so a false match needs two different
people sharing all three.

## Layout

```
src/lead_monitor/
  config.py       typed settings, validated eagerly at startup
  logging_setup.py  JSON logs to stdout, secrets scrubbed from every record
  models.py       Lead and its identity rules
  retry.py        exponential backoff with full jitter, stdlib only
  auth.py         browser login, session capture and caching
  discovery.py    endpoint discovery and scoring
  introspect.py   probes the GraphQL schema via validator error messages
  store.py        SQLite, two-phase notification marking
  notifier.py     SMTP digest email
  whatsapp_notifier.py  WhatsApp Business Cloud API digest message
  recency.py      the Clarita window: which unseen leads are worth an email
  monitor.py      the run loop
  cli.py          run / discover / introspect / test-notify / status / init-db
  clients/
    base.py       LeadsClient protocol and schema-tolerant field mapping
    graphql.py    GraphQL client, preferred — the portal's actual API
    api.py        REST-style HTTP client fallback
    dom.py        Playwright fallback
```

Configuration is validated before anything else happens, so a missing SMTP
password fails immediately rather than after the database has already recorded
leads as seen. Logs are JSON on stdout with known secret values replaced, because
CI logs on a public repository are world-readable and passwords leak through
exception messages and dependency debug output.

Third-party dependencies are kept to four: httpx, playwright, pydantic and
pydantic-settings. The retry helper is sixty lines of standard library rather
than a fifth. For something that runs unattended every ten minutes, each
dependency is a thing that can break a run nobody is watching.
