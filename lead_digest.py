#!/usr/bin/env python3
"""
RTR lead sourcing - daily digest of brands that send SMS to Austria.

Reads the Austrian RTR open-data register of alphanumeric SMS Sender IDs
(https://data.rtr.at/pages/open-data/tk-sms), groups the entries by the company
holding them, keeps European brands, ranks them, and posts the companies that
are new since the previous run to Slack.

A "lead" is a COMPANY, not a Sender ID: a brand with nine Sender IDs is one
lead with nine Sender IDs listed under it.

Stdlib only - no pip install needed.

Usage
-----
  python3 lead_digest.py --bootstrap     # seed the baseline, write the backlog, post a summary
  python3 lead_digest.py                 # post companies that are new since last run
  python3 lead_digest.py --dry-run       # print the digest, post nothing, save nothing
  python3 lead_digest.py --test-notify   # send a test message to Slack and exit
  python3 lead_digest.py --input f.json  # read the register from a file instead of the API

Environment (all optional)
--------------------------
  SLACK_WEBHOOK_URL    where the digest is posted
  WEBHOOK_TEXT_KEY     payload field name, default "text"
  CORE_MARKETS         comma-separated ISO codes listed first, default NL,BE,DE,AT
  EXCLUDE_SUBMITTERS   comma-separated provider names; registrations made
                       through them are dropped (your own customers),
                       default: none, every provider is kept
  EXCLUDE_HOLDERS      comma-separated text; holders containing it are dropped,
                       default "spryng,commify"
  MAX_PER_SECTION      leads shown in full per Slack section, default 15;
                       the rest are listed by name
  OWN_PROVIDER         provider Spryng/Commify register through, default
                       "MASS Response Service"; companies registered only
                       through it are treated as likely clients
"""

import argparse
import collections
import csv
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request

API = "https://data.rtr.at/api/v2/tables/tk-sms?mediaType=json&unpaged=true"
UA = "rtr-lead-sourcing/1.0 (open-data lead digest)"

# Column names are matched at runtime so a rename at RTR does not break the run.
SENDER_HINTS = ("kennung", "absenderkennung", "senderid", "sender")
HOLDER_HINTS = ("inhaber", "firma", "unternehmen", "company", "name")
ADDRESS_HINTS = ("anschrift", "adresse", "address")
EMAIL_HINTS = ("email", "e_mail", "mail")
REASON_HINTS = ("grund", "begruendung", "reason")
SUBMITTER_HINTS = ("eingetragen_durch", "einbringer", "submitted", "provider")
DATE_HINTS = ("eingetragen_am", "eingetragen", "datum", "created")
STATUS_HINTS = ("status",)

# ISO code -> (name, address prefixes seen in registers). RTR writes addresses
# as "<COUNTRY>-<POSTCODE> <CITY>, <STREET>", and holders use ISO-2, ISO-3 or
# the old car-plate letter, so each country lists every plausible prefix.
EUROPE = {
    "AT": ("Austria", ("A", "AT", "AUT")),
    "BE": ("Belgium", ("B", "BE", "BEL")),
    "DE": ("Germany", ("D", "DE", "DEU", "GER")),
    "NL": ("Netherlands", ("NL", "NLD")),
    "AD": ("Andorra", ("AD", "AND")),
    "AL": ("Albania", ("AL", "ALB")),
    "BA": ("Bosnia and Herzegovina", ("BA", "BIH")),
    "BG": ("Bulgaria", ("BG", "BGR")),
    "CH": ("Switzerland", ("CH", "CHE")),
    "CY": ("Cyprus", ("CY", "CYP")),
    "CZ": ("Czechia", ("CZ", "CZE")),
    "DK": ("Denmark", ("DK", "DNK")),
    "EE": ("Estonia", ("EE", "EST")),
    "ES": ("Spain", ("E", "ES", "ESP")),
    "FI": ("Finland", ("FI", "FIN")),
    "FR": ("France", ("F", "FR", "FRA")),
    "GB": ("United Kingdom", ("GB", "UK", "GBR")),
    "GR": ("Greece", ("GR", "EL", "GRC")),
    "HR": ("Croatia", ("HR", "HRV")),
    "HU": ("Hungary", ("H", "HU", "HUN")),
    "IE": ("Ireland", ("IE", "IRL")),
    "IS": ("Iceland", ("IS", "ISL")),
    "IT": ("Italy", ("I", "IT", "ITA")),
    "LI": ("Liechtenstein", ("LI", "FL", "LIE")),
    "LT": ("Lithuania", ("LT", "LTU")),
    "LU": ("Luxembourg", ("L", "LU", "LUX")),
    "LV": ("Latvia", ("LV", "LVA")),
    "MC": ("Monaco", ("MC", "MCO")),
    "MD": ("Moldova", ("MD", "MDA")),
    "ME": ("Montenegro", ("ME", "MNE")),
    "MK": ("North Macedonia", ("MK", "MKD")),
    "MT": ("Malta", ("M", "MT", "MLT")),
    "NO": ("Norway", ("N", "NO", "NOR")),
    "PL": ("Poland", ("PL", "POL")),
    "PT": ("Portugal", ("P", "PT", "PRT")),
    "RO": ("Romania", ("RO", "ROU")),
    "RS": ("Serbia", ("RS", "SRB")),
    "SE": ("Sweden", ("S", "SE", "SWE")),
    "SI": ("Slovenia", ("SI", "SLO", "SVN")),
    "SK": ("Slovakia", ("SK", "SVK")),
    "SM": ("San Marino", ("SM", "SMR")),
    "UA": ("Ukraine", ("UA", "UKR")),
    "XK": ("Kosovo", ("XK", "RKS")),
}
PREFIX_TO_ISO = {p: iso for iso, (_n, prefixes) in EUROPE.items() for p in prefixes}
# Email ccTLD fallback, used only when the address carries no country prefix.
TLD_TO_ISO = {iso.lower(): iso for iso in EUROPE}
TLD_TO_ISO.update({"uk": "GB", "gr": "GR"})

DEFAULT_CORE = "NL,BE,DE,AT"
# Empty on purpose: every provider's registrations are kept, including MASS
# Response Service, which Spryng/Commify submit through.
DEFAULT_EXCLUDE_SUBMITTERS = ""
SLACK_LIMIT = 3500  # characters per message; longer digests are split


def env(name, default=None):
    """os.environ.get that treats an empty value as absent (GitHub Actions
    passes an unset variable through as an empty string)."""
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def csv_env(name, default=""):
    return [p.strip() for p in (env(name, default) or "").split(",") if p.strip()]


def norm(value):
    """Casefold and drop punctuation so 'G.m.b.H.' matches 'GmbH'."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


LEGAL_FORMS = (r"\b(gmbh|ag|bv|b v|nv|se|sa|srl|sro|s r o|as|a s|ltd|limited|kg|co|og|gesmbh|ges m b h|"
               r"gesellschaft|mbh|m b h|plc|inc|llc|aktiengesellschaft|ug|eg|egbr|kft|spa|s p a|ab|oy|"
               r"aps|asa|sarl|sas|bvba|und|&)\b")


def company_key(name):
    """Grouping key that ignores legal-form spelling, so 'Deutsche Lufthansa AG'
    and 'Deutsche Lufthansa Aktiengesellschaft' are one company."""
    text = re.sub(r"[^a-z0-9äöüß ]+", " ", str(name or "").casefold())
    text = re.sub(LEGAL_FORMS, " ", text)
    key = norm(text)
    return key or norm(name)


# The provider Spryng/Commify register their clients' Sender IDs through.
# Companies registered only through it are most likely existing clients.
DEFAULT_OWN_PROVIDER = "MASS Response Service"

SHORT_PROVIDER = {
    "massresponse": "MASS", "linkmobility": "LINK Mobility", "tmobile": "Magenta (T-Mobile)",
    "twilio": "Twilio", "a1telekom": "A1", "messagebird": "MessageBird (Bird)",
    "hutchisondrei": "Drei", "belgacom": "BICS", "vonage": "Vonage", "telnyx": "Telnyx",
    "lycamobile": "Lycamobile", "ipaustria": "IP Austria", "aicall": "aicall",
}


def short_provider(name):
    n = norm(name)
    for k, v in SHORT_PROVIDER.items():
        if k in n:
            return v
    return str(name).split(" ")[0]


def fetch(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def extract_rows(payload):
    """RTR wraps rows differently per endpoint version; find the list of dicts."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "content", "items", "results", "rows"):
            val = payload.get(key)
            if isinstance(val, list) and (not val or isinstance(val[0], dict)):
                return val
        for val in payload.values():
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return val
    raise ValueError("Could not find a list of records in the API response")


def pick_field(fields, hints):
    lowered = {f.lower(): f for f in fields}
    for hint in hints:
        if hint in lowered:
            return lowered[hint]
    for hint in hints:
        for low, original in lowered.items():
            if hint in low:
                return original
    return None


def resolve_fields(rows):
    names = []
    for row in rows[:200]:
        for k in row:
            if k not in names:
                names.append(k)
    f = {
        "sender": pick_field(names, SENDER_HINTS),
        "holder": pick_field(names, HOLDER_HINTS),
        "address": pick_field(names, ADDRESS_HINTS),
        "email": pick_field(names, EMAIL_HINTS),
        "reason": pick_field(names, REASON_HINTS),
        "submitter": pick_field(names, SUBMITTER_HINTS),
        "date": pick_field(names, DATE_HINTS),
    }
    # "status" must not resolve to id_status when the text column exists.
    status = [n for n in names if n.lower() == "status"]
    f["status"] = status[0] if status else pick_field(names, STATUS_HINTS)
    missing = [k for k in ("sender", "holder") if not f[k]]
    if missing:
        raise ValueError(f"Register schema changed: no column found for {missing}. "
                         f"Columns present: {names}")
    return f


def is_deleted(row, status_field):
    raw = str(row.get(status_field, "") if status_field else "").strip().casefold()
    return "gelösch" in raw or "geloesch" in raw or raw == "0"


def is_pending(raw):
    low = str(raw or "").casefold()
    return "wartefr" in low or low.strip() == "1"


def address_country(address):
    """ISO code from the '<COUNTRY>-<POSTCODE> ...' prefix, or ''."""
    head, sep, _rest = str(address or "").strip().partition("-")
    head = head.strip().upper()
    if not sep or not head.isalpha() or len(head) > 4:
        return ""
    return PREFIX_TO_ISO.get(head, "?" + head)  # "?US" = a prefix outside Europe


def email_country(email):
    tld = str(email or "").strip().lower().rpartition(".")[2]
    return TLD_TO_ISO.get(tld, "")


def build_leads(rows, f, exclude_submitters, exclude_holders):
    """Group active register rows into companies. Returns (leads, stats)."""
    stats = collections.Counter()
    submitter_names = {company_key(r.get(f["submitter"])) for r in rows} if f["submitter"] else set()
    submitter_names.discard("")
    ex_sub = [norm(s) for s in exclude_submitters if norm(s) != "none"]
    ex_hold = [norm(h) for h in exclude_holders]

    leads = {}
    for row in rows:
        stats["rows"] += 1
        if is_deleted(row, f["status"]):
            stats["deleted"] += 1
            continue
        holder = str(row.get(f["holder"]) or "").strip()
        key = company_key(holder)
        if not key:
            stats["no_holder"] += 1
            continue
        submitter = str(row.get(f["submitter"]) or "").strip() if f["submitter"] else ""
        nsub = norm(submitter)
        if any(x and x in nsub for x in ex_sub):
            stats["excluded_own_provider"] += 1
            continue
        if any(x and x in key for x in ex_hold):
            stats["excluded_own_brand"] += 1
            continue
        # A holder that is itself one of the submitting providers is a
        # messaging company registering its own name, not a brand.
        if key in submitter_names:
            stats["excluded_messaging_provider"] += 1
            continue

        lead = leads.setdefault(key, {
            "key": key, "company": holder, "sender_ids": [], "providers": [],
            "emails": [], "addresses": [], "reasons": [], "dates": [],
            "countries": collections.Counter(), "pending": 0,
        })
        if len(holder) < len(lead["company"]):
            lead["company"] = holder
        sid = str(row.get(f["sender"]) or "").strip()
        if sid and sid not in lead["sender_ids"]:
            lead["sender_ids"].append(sid)
        for bucket, field in (("providers", "submitter"), ("emails", "email"),
                              ("addresses", "address"), ("reasons", "reason"),
                              ("dates", "date")):
            val = str(row.get(f[field]) or "").strip() if f[field] else ""
            if val and val not in lead[bucket]:
                lead[bucket].append(val)
        if is_pending(row.get(f["status"]) if f["status"] else ""):
            lead["pending"] += 1
        c = address_country(row.get(f["address"])) if f["address"] else ""
        if c:
            lead["countries"][("address", c)] += 1
        else:
            e = email_country(row.get(f["email"])) if f["email"] else ""
            if e:
                lead["countries"][("email", e)] += 1

    for lead in leads.values():
        by_addr = [(n, c) for (src, c), n in lead["countries"].items() if src == "address"]
        by_mail = [(n, c) for (src, c), n in lead["countries"].items() if src == "email"]
        if by_addr:
            lead["country"], lead["country_source"] = max(by_addr)[1], "address"
        elif by_mail:
            lead["country"], lead["country_source"] = max(by_mail)[1], "email domain"
        else:
            lead["country"], lead["country_source"] = "", ""
        del lead["countries"]
        lead["first_registered"] = min(lead["dates"]) if lead["dates"] else ""
        lead["last_registered"] = max(lead["dates"]) if lead["dates"] else ""
    return leads, stats


def region_of(lead, core):
    c = lead["country"]
    if c in core:
        return "core"
    if c in EUROPE:
        return "europe"
    if c.startswith("?"):
        return "outside"
    return "unknown"


def is_own(provider, own):
    n = norm(provider)
    return any(o and o in n for o in own)


def client_status(lead, own):
    """'competitor': only on competitors (the sales target).
    'client_plus': registered through our provider AND a competitor.
    'client': registered only through our provider, most likely already a client."""
    mine = [p for p in lead["providers"] if is_own(p, own)]
    other = [p for p in lead["providers"] if not is_own(p, own)]
    if mine and other:
        return "client_plus"
    if mine:
        return "client"
    return "competitor"


STATUS_ORDER = {"competitor": 0, "client_plus": 1, "client": 2}
STATUS_LABEL = {"competitor": "on a competitor", "client_plus": "our client, also on a competitor",
                "client": "our client (via MASS)"}


def score(lead, core):
    """Ranking inside a section. See README for the reasoning."""
    region = region_of(lead, core)
    s = {"core": 40, "europe": 15}.get(region, 0)
    s += min(len(lead["sender_ids"]), 5) * 6      # more Sender IDs = more traffic
    if len(lead["providers"]) > 1:
        s += 15                                   # already multi-sourcing
    if lead["pending"]:
        s += 10                                   # registered in the last few weeks
    if lead["emails"]:
        s += 5
    return min(s, 100)


def rank(leads, core, own):
    return sorted(leads, key=lambda l: (STATUS_ORDER[client_status(l, own)], -score(l, core),
                                        l["company"].casefold()))


def country_label(code):
    if code in EUROPE:
        return code
    return code.lstrip("?") or "??"


def esc(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def provider_line(lead, own):
    other = [short_provider(p) for p in lead["providers"] if not is_own(p, own)]
    status = client_status(lead, own)
    if status == "client":
        return "Our client (registered via MASS)"
    if status == "client_plus":
        return "Our client via MASS, *also uses " + esc(", ".join(other)) + "*"
    return "Uses *" + esc(", ".join(other)) + "*"


def lead_block(lead, core, own, new_ids=None):
    ids = new_ids if new_ids is not None else lead["sender_ids"]
    shown = ", ".join(f"`{esc(i)}`" for i in ids[:6])
    if len(ids) > 6:
        shown += f" +{len(ids) - 6} more"
    n = len(lead["sender_ids"])
    out = [f"*{esc(lead['company'])}*  ·  {country_label(lead['country'])}  ·  "
           f"{n} Sender ID{'s' if n != 1 else ''}",
           provider_line(lead, own),
           ("New Sender IDs: " if new_ids is not None else "Sender IDs: ") + shown]
    bits = []
    if lead["last_registered"]:
        bits.append("registered " + esc(lead["last_registered"][:10]))
    if lead["emails"]:
        bits.append("register contact " + esc(lead["emails"][0]))
    if bits:
        out.append("  ·  ".join(bits))
    return "\n".join(out)


def section(title, leads, core, own, cap, new_ids_by_key=None):
    if not leads:
        return []
    leads = rank(leads, core, own)
    blocks = [f"*{title} ({len(leads)})*"]
    for lead in leads[:cap]:
        blocks.append(lead_block(lead, core, own, (new_ids_by_key or {}).get(lead["key"])))
    if len(leads) > cap:
        rest = ", ".join(esc(l["company"]) for l in leads[cap:cap + 30])
        more = len(leads) - cap - 30
        blocks.append(f"_Also: {rest}{f' and {more} more (see CSV)' if more > 0 else ''}_")
    return blocks


def name_list(title, leads, core, limit=25):
    if not leads:
        return []
    leads = sorted(leads, key=lambda l: (region_of(l, core) != "core", -len(l["sender_ids"]),
                                         l["company"].casefold()))
    names = ", ".join(f"{esc(l['company'])} ({country_label(l['country'])})" for l in leads[:limit])
    more = f" and {len(leads) - limit} more" if len(leads) > limit else ""
    return [f"*{title} ({len(leads)})*\n{names}{more}"]


def build_digest(new_leads, expanded, switched, core, own, cap, when, csv_link, totals):
    """new_leads: companies not seen before. expanded: (lead, new Sender IDs) for
    known companies. switched: known clients that added a competitor."""
    st = lambda l: client_status(l, own)
    comp_core = [l for l in new_leads if st(l) == "competitor" and region_of(l, core) == "core"]
    comp_eu = [l for l in new_leads if st(l) == "competitor" and region_of(l, core) == "europe"]
    clients_on_comp = [l for l in new_leads if st(l) == "client_plus"] + switched
    new_clients = [l for l in new_leads if st(l) == "client"]
    comp_exp = [(l, ids) for l, ids in expanded if st(l) == "competitor"]
    date = when.strftime("%a %d %b %Y")
    head = (f":dart: *Austria SMS leads - {date}*\n"
            f"*{len(comp_core) + len(comp_eu)} new companies on a competitor* "
            f"({len(comp_core)} in {', '.join(core)})")
    if clients_on_comp:
        head += f"  ·  {len(clients_on_comp)} of our clients also on a competitor"
    if new_clients:
        head += f"  ·  {len(new_clients)} new via MASS (likely our clients)"
    blocks = [head]
    blocks += section("New on a competitor: " + ", ".join(core), comp_core, core, own, cap)
    blocks += section("New on a competitor: rest of Europe", comp_eu, core, own, cap)
    blocks += section("Our clients also using a competitor", clients_on_comp, core, own, cap)
    blocks += section("Competitor accounts adding Sender IDs", [l for l, _ in comp_exp], core, own,
                      max(5, cap // 3), {l["key"]: ids for l, ids in comp_exp})
    blocks += name_list("New via MASS, likely our clients", new_clients, core)
    foot = (f"Register: {totals['rows']} entries, {totals['leads']} European companies tracked "
            f"({totals['competitor']} on competitors only).")
    if csv_link:
        foot += f"  <{csv_link}|Today's CSV>"
    blocks.append(f"_{foot}_")
    return blocks


def quiet_digest(when, totals):
    return [f":dart: *Austria SMS leads - {when.strftime('%a %d %b %Y')}*\n"
            f"No new companies today. {totals['leads']} tracked, "
            f"{totals['competitor']} of them on competitors only."]


def chunk(blocks, limit=SLACK_LIMIT):
    """Join blocks into messages no longer than `limit`, never splitting a lead."""
    messages, current = [], ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit and current:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def post_webhook(text):
    """Returns (ok, detail); ok is None when no webhook is configured."""
    url = (env("SLACK_WEBHOOK_URL") or env("WEBHOOK_URL") or "").strip()
    if not url:
        return None, "no webhook URL configured"
    if not url.lower().startswith("http"):
        return False, "the configured URL does not start with http - check the secret value"
    body = json.dumps({env("WEBHOOK_TEXT_KEY", "text"): text}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return True, resp.read().decode("utf-8", errors="replace").strip() or "ok"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:200]}"
    except Exception as exc:  # network errors must not lose the baseline
        return False, f"{type(exc).__name__}: {exc}"


def post_all(blocks):
    """Posts every chunk. Returns True if Slack accepted all of them, None if
    no webhook is set, False on any failure."""
    result = True
    for message in chunk(blocks):
        ok, detail = post_webhook(message)
        if ok is None:
            print("[info] no SLACK_WEBHOOK_URL set - digest printed only")
            return None
        if not ok:
            print(f"[warn] Slack post failed: {detail}", file=sys.stderr)
            result = False
    return result


CSV_COLUMNS = ["priority", "client_status", "company", "country", "region", "sender_id_count",
               "competitors", "sender_ids", "all_providers", "emails", "address", "reason",
               "first_registered", "last_registered", "score", "country_source"]


def write_csv(path, leads, core, own):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for i, l in enumerate(rank(leads, core, own), 1):
            w.writerow([i, STATUS_LABEL[client_status(l, own)], l["company"],
                        country_label(l["country"]), region_of(l, core), len(l["sender_ids"]),
                        ", ".join(short_provider(p) for p in l["providers"] if not is_own(p, own)),
                        " | ".join(l["sender_ids"]), " | ".join(l["providers"]),
                        " | ".join(l["emails"]),
                        l["addresses"][0] if l["addresses"] else "",
                        l["reasons"][0] if l["reasons"] else "",
                        l["first_registered"][:10], l["last_registered"][:10],
                        score(l, core), l["country_source"]])


def load_state(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=0, sort_keys=True)
    os.replace(tmp, path)


def gh_output(**pairs):
    out = env("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            for k, v in pairs.items():
                fh.write(f"{k}={v}\n")


def bootstrap_summary(europe_leads, core, own, totals, providers, backlog_link, top=15):
    st = lambda l: client_status(l, own)
    comp_core = [l for l in europe_leads if st(l) == "competitor" and region_of(l, core) == "core"]
    plus = [l for l in europe_leads if st(l) == "client_plus"]
    clients = sum(1 for l in europe_leads if st(l) == "client")
    top_p = ", ".join(f"{short_provider(p)} {n}" for p, n in providers.most_common()
                      if not is_own(p, own))
    head = (f":dart: *Austria SMS leads - baseline ready*\n"
            f"{totals['leads']} European companies hold a Sender ID for Austria:\n"
            f"• *{totals['competitor']} only on competitors* ({len(comp_core)} in {', '.join(core)})\n"
            f"• {len(plus)} of our clients also use a competitor\n"
            f"• {clients} registered only via MASS (likely our clients)\n"
            f"Competitors by number of companies: {top_p}")
    blocks = [head]
    blocks += section(f"Top {top} companies on a competitor: {', '.join(core)}",
                      rank(comp_core, core, own)[:top], core, own, top)
    blocks += section("Our clients also using a competitor", plus, core, own, 10)
    tail = "From tomorrow this channel gets one digest a day: new companies on competitors first."
    if backlog_link:
        tail += f"\n<{backlog_link}|Full ranked list (CSV), companies on competitors first>"
    blocks.append(tail)
    return blocks


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--backlog", default="leads_backlog.csv")
    ap.add_argument("--digest-dir", default="digests")
    ap.add_argument("--input", help="read the register from a JSON file instead of the API")
    ap.add_argument("--bootstrap", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-notify", action="store_true")
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    today = now.strftime("%Y-%m-%d")

    if args.test_notify:
        ok, detail = post_webhook(":dart: RTR lead sourcing - test message. Slack is connected.")
        print(f"test notification: {'sent' if ok else 'NOT sent'} ({detail})")
        return 0 if ok else 1

    core = [c.upper() for c in csv_env("CORE_MARKETS", DEFAULT_CORE)]
    own = [norm(o) for o in csv_env("OWN_PROVIDER", DEFAULT_OWN_PROVIDER)]
    cap = int(env("MAX_PER_SECTION", "15"))
    try:
        if args.input:
            with open(args.input, encoding="utf-8") as fh:
                payload = json.load(fh)
        else:
            payload = fetch(API)
        rows = extract_rows(payload)
        if not rows:
            raise ValueError("the register came back empty")
        f = resolve_fields(rows)
    except Exception as exc:
        print(f"[error] could not read the register: {exc}", file=sys.stderr)
        return 1

    leads, stats = build_leads(rows, f, csv_env("EXCLUDE_SUBMITTERS", DEFAULT_EXCLUDE_SUBMITTERS),
                               csv_env("EXCLUDE_HOLDERS", "spryng,commify"))
    europe = [l for l in leads.values() if region_of(l, core) in ("core", "europe")]
    totals = {
        "rows": stats["rows"], "leads": len(europe),
        "outside": sum(1 for l in leads.values() if region_of(l, core) == "outside"),
        "unknown": sum(1 for l in leads.values() if region_of(l, core) == "unknown"),
        "competitor": sum(1 for l in europe if client_status(l, own) == "competitor"),
    }
    providers = collections.Counter(p for l in europe for p in l["providers"])
    print(f"register rows: {stats['rows']}  deleted: {stats['deleted']}  "
          f"companies: {len(leads)}  european: {len(europe)}  "
          f"outside europe: {totals['outside']}  no country: {totals['unknown']}")
    for k in ("excluded_own_provider", "excluded_own_brand", "excluded_messaging_provider"):
        print(f"{k}: {stats[k]} rows")
    print("submitting providers (european brands each):")
    for p, n in providers.most_common():
        print(f"  {n:6d}  {p}")

    repo = env("GITHUB_REPOSITORY")
    base = f"{env('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/blob/{env('GITHUB_REF_NAME', 'main')}" if repo else ""

    state = load_state(args.state)
    first = state is None or args.bootstrap
    known = {} if first else state.get("companies", {})

    new_leads, expanded, switched = [], [], []
    if not first:
        for l in europe:
            prev = known.get(l["key"])
            if prev is None:
                new_leads.append(l)
                continue
            added = [s for s in l["sender_ids"] if s not in set(prev.get("sender_ids", []))]
            if added:
                expanded.append((l, added))
            # A known client that now also registers through a competitor. Only
            # judged when the previous run recorded providers, so upgrading the
            # state file does not flood the channel.
            if "providers" in prev and client_status(l, own) == "client_plus":
                had_competitor = any(not is_own(p, own) for p in prev["providers"])
                if not had_competitor:
                    switched.append(l)

    if first:
        blocks = bootstrap_summary(europe, core, own, totals, providers,
                                   f"{base}/{args.backlog}" if base else "")
    elif new_leads or expanded or switched:
        digest_path = os.path.join(args.digest_dir, f"{today}.csv")
        blocks = build_digest(new_leads, expanded, switched, core, own, cap, now,
                              f"{base}/{digest_path}" if base else "", totals)
    elif state.get("last_post_date") != today:
        blocks = quiet_digest(now, totals)
    else:
        blocks = []  # a second quiet run on the same day says nothing

    print("\n" + "\n\n".join(blocks) if blocks else "\n(nothing to post)")
    gh_output(new_count=len(new_leads), expanded_count=len(expanded), switched_count=len(switched))
    if args.dry_run:
        return 0

    posted = post_all(blocks) if blocks else True
    if posted is False and not first:
        # Keep the old baseline so the same leads are retried on the next run.
        print("[error] Slack rejected the digest; baseline left unchanged", file=sys.stderr)
        return 1

    write_csv(args.backlog, europe, core, own)
    if new_leads or expanded or switched:
        path = os.path.join(args.digest_dir, f"{today}.csv")
        todays = new_leads + [l for l, _ in expanded] + [l for l in switched if l not in new_leads]
        if os.path.exists(path):  # an earlier run today already wrote some
            with open(path, encoding="utf-8") as fh:
                earlier = {r["company"] for r in csv.DictReader(fh)}
            todays += [l for l in europe if l["company"] in earlier and l not in todays]
        write_csv(path, todays, core, own)

    companies = dict(known)
    for l in europe:
        entry = companies.get(l["key"], {"first_seen": today})
        entry["sender_ids"] = sorted(set(entry.get("sender_ids", [])) | set(l["sender_ids"]))
        entry["providers"] = sorted(set(entry.get("providers", [])) | set(l["providers"]))
        companies[l["key"]] = entry
    save_state(args.state, {
        "version": 2, "source": API, "last_run": now.isoformat(timespec="seconds"),
        "last_post_date": today if blocks else (state or {}).get("last_post_date", ""),
        "companies": companies,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
