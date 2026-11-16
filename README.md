# rtr-lead-sourcing

A daily Slack digest of brands that send SMS to Austria.

Since 1 October 2026 every alphanumeric SMS Sender ID used towards Austrian
numbers has to be in the public RTR register. The register is open data, so it
doubles as a list of companies that are actively sending SMS into Austria,
together with the provider each one registered through. This repo turns that
into a ranked lead list.

## What you get

**In Slack, once a day**

- New European brands that registered a Sender ID, core markets
  (NL, BE, DE, AT) first, then the rest of Europe.
- Known brands that added more Sender IDs (a sign of growing SMS use).
- Per lead: company, country, score, Sender IDs, current provider,
  registration date, contact email.
- "No new European brands today" on quiet days, and a failure alert if a run
  breaks, so silence never has two meanings.

**In this repo**

- `leads_backlog.csv` - every European brand in the register, ranked.
  Rewritten on each run, so it is always current.
- `digests/YYYY-MM-DD.csv` - the leads from each day's digest.
- `state.json` - which companies have already been reported. Do not edit.

## What counts as a lead

One lead is one **company**, however many Sender IDs it holds.

Included: holders with an active or pending registration whose address is in
Europe.

Left out:

- Deleted registrations.
- Holders whose name contains `spryng` or `commify` (`EXCLUDE_HOLDERS`).
- Holders that are themselves one of the submitting providers (Twilio,
  LINK Mobility and so on). They are messaging companies, not brands.
- Anything registered through a provider listed in `EXCLUDE_SUBMITTERS`.
  **Empty by default** - see "Excluding your own customers" below.
- Companies outside Europe or with no recognisable country. They are counted
  in the digest footer but not listed.

Country comes from the address prefix RTR publishes (`NL-`, `D-`, `A-`). When
an address has no prefix, the email domain ending (`.nl`, `.de`) is used and
the CSV says so in `country_source`.

## Score (0-100)

| Signal | Points | Why |
|---|---|---|
| Core market (NL, BE, DE, AT) | 40 | Where Spryng sells |
| Rest of Europe | 15 | |
| Each Sender ID, up to 5 | 6 each | More Sender IDs, more traffic |
| Registered through 2+ providers | 15 | Already splits traffic, so open to another supplier |
| Has an entry still in the waiting period | 10 | Registered in the last few weeks |
| Contact email present | 5 | |

## Setup

1. **Slack webhook.** Repo -> Settings -> Secrets and variables -> Actions ->
   New repository secret. Name `SLACK_WEBHOOK_URL`, value the webhook URL of
   the channel the digest should land in.
2. **Test it.** Actions -> RTR lead digest -> Run workflow -> tick
   **test_notify** -> Run. A test line should appear in the channel.
3. **First run.** Run workflow again with **bootstrap** ticked. This reads the
   whole register, writes `leads_backlog.csv`, and posts one summary (totals by
   country and by provider) instead of thousands of leads.
4. Nothing else. It runs every morning from then on.

## Excluding your own customers

The register does not list Spryng or Commify as a submitting provider; Sender
IDs are submitted by one of about 13 licensed providers. To drop companies that
are already customers, set the repo **variable** `EXCLUDE_SUBMITTERS` to the
name of the provider Spryng/Commify registers through, for example
`MASS Response Service`. The bootstrap summary lists every provider and how
many brands each one has, which makes the right name easy to spot.

## Settings (repo variables, all optional)

| Variable | Default | Effect |
|---|---|---|
| `CORE_MARKETS` | `NL,BE,DE,AT` | ISO codes listed first and scored highest |
| `EXCLUDE_SUBMITTERS` | empty | Providers whose registrations are dropped |
| `EXCLUDE_HOLDERS` | `spryng,commify` | Holder names that are dropped |
| `MAX_PER_SECTION` | `15` | Leads shown per Slack section; the rest are in the CSV |

## Before contacting anyone

The email addresses are published by RTR for Sender ID disputes, usually a
generic company mailbox. Unsolicited marketing email is tightly regulated in
Austria and elsewhere in the EU, including to businesses. Treat the register
as a way to find and qualify accounts, and check outreach rules with sales or
legal before emailing those addresses directly.

## Running it by hand

```
python3 lead_digest.py --dry-run     # print today's digest, change nothing
python3 -m unittest -v               # tests
```
