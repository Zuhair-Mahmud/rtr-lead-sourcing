"""Run with: python3 -m unittest -v"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import lead_digest as ld


def row(kennung, inhaber, anschrift, durch="Twilio Ireland Limited", email="info@example.com",
        status="gültig", am="2026-11-10"):
    return {"kennung": kennung, "inhaber": inhaber, "anschrift": anschrift, "email": email,
            "grund": "Markenname", "eingetragen_durch": durch, "eingetragen_am": am,
            "gueltig_ab": "", "status": status, "id_status": 2,
            "geloescht_am": "", "geloescht_durch": ""}


BASE = [
    row("BOLCOM", "bol.com B.V.", "NL-3528 Utrecht, Papendorpseweg 100"),
    row("BOL", "bol.com b.v.", "NL-3528 Utrecht, Papendorpseweg 100", durch="MessageBird B.V."),
    row("Zalando", "Zalando SE", "D-10243 Berlin, Valeska-Gert-Str. 5"),
    row("ACME", "Acme Inc", "USA-10001 New York, 5th Ave"),
    row("Mystery", "Mystery Ltd", "12 High Street", email="hi@mystery.com"),
    row("Twilio", "Twilio Ireland Limited", "IRL-D02 Dublin, 3 Dublin Landings"),
    row("Spryng", "Spryng B.V.", "NL-1012 Amsterdam, Damrak 1"),
    row("Gone", "Gone GmbH", "A-1010 Wien, Graben 1", status="gelöscht"),
]


class Grouping(unittest.TestCase):
    def setUp(self):
        f = ld.resolve_fields(BASE)
        self.leads, self.stats = ld.build_leads(BASE, f, [], ["spryng", "commify"])

    def test_company_is_one_lead_with_all_sender_ids(self):
        bol = self.leads[ld.company_key("bol.com B.V.")]
        self.assertEqual(bol["sender_ids"], ["BOLCOM", "BOL"])
        self.assertEqual(len(bol["providers"]), 2)
        self.assertEqual(bol["country"], "NL")

    def test_status_field_is_the_text_column(self):
        self.assertEqual(ld.resolve_fields(BASE)["status"], "status")

    def test_exclusions(self):
        self.assertNotIn(ld.company_key("Spryng B.V."), self.leads)          # own brand
        self.assertNotIn(ld.company_key("Twilio Ireland Limited"), self.leads)  # a provider
        self.assertNotIn(ld.company_key("Gone GmbH"), self.leads)            # deleted

    def test_regions(self):
        core = ["NL", "BE", "DE", "AT"]
        r = {l["company"]: ld.region_of(l, core) for l in self.leads.values()}
        self.assertEqual(r["Zalando SE"], "core")
        self.assertEqual(r["Acme Inc"], "outside")
        self.assertEqual(r["Mystery Ltd"], "unknown")

    def test_single_letter_prefix_is_exact(self):
        self.assertEqual(ld.address_country("D-10243 Berlin"), "DE")
        self.assertEqual(ld.address_country("DK-1050 København"), "DK")
        self.assertEqual(ld.address_country("2333 N STATE RD 7"), "")

    def test_email_fallback(self):
        rows = [row("X", "Fallback BV", "Keizersgracht 1, Amsterdam", email="info@fallback.nl")]
        leads, _ = ld.build_leads(rows, ld.resolve_fields(rows), [], [])
        lead = next(iter(leads.values()))
        self.assertEqual((lead["country"], lead["country_source"]), ("NL", "email domain"))

    def test_exclude_submitter(self):
        leads, stats = ld.build_leads(BASE, ld.resolve_fields(BASE), ["MessageBird"], [])
        self.assertEqual(leads[ld.company_key("bol.com B.V.")]["sender_ids"], ["BOLCOM"])
        self.assertEqual(stats["excluded_own_provider"], 1)

    def test_mass_response_registrations_are_kept_by_default(self):
        rows = [row("OURS", "Customer BV", "NL-1000 Amsterdam, X", durch="MASS Response Service GmbH")]
        self.assertEqual(ld.csv_env("NO_SUCH_VAR", ld.DEFAULT_EXCLUDE_SUBMITTERS), [])
        leads, _ = ld.build_leads(rows, ld.resolve_fields(rows), [], ["spryng", "commify"])
        self.assertEqual(len(leads), 1)

    def test_core_outranks_rest_of_europe(self):
        core = ["NL", "BE", "DE", "AT"]
        rows = [row("A", "Core BV", "NL-1000 Amsterdam, X"), row("B", "Other SA", "F-75001 Paris, Y")]
        leads, _ = ld.build_leads(rows, ld.resolve_fields(rows), [], [])
        s = {l["company"]: ld.score(l, core) for l in leads.values()}
        self.assertGreater(s["Core BV"], s["Other SA"])


class Priority(unittest.TestCase):
    OWN = [ld.norm(ld.DEFAULT_OWN_PROVIDER)]

    def test_status_and_order(self):
        rows = [row("A", "Comp BV", "NL-1000 Amsterdam, X"),
                row("B", "Client BV", "NL-1000 Amsterdam, X", durch="MASS Response Service GmbH"),
                row("C", "Both BV", "NL-1000 Amsterdam, X", durch="MASS Response Service GmbH"),
                row("D", "Both BV", "NL-1000 Amsterdam, X", durch="LINK Mobility Austria GmbH")]
        leads, _ = ld.build_leads(rows, ld.resolve_fields(rows), [], [])
        st = {l["company"]: ld.client_status(l, self.OWN) for l in leads.values()}
        self.assertEqual(st, {"Comp BV": "competitor", "Client BV": "client", "Both BV": "client_plus"})
        order = [l["company"] for l in ld.rank(list(leads.values()), ["NL"], self.OWN)]
        self.assertEqual(order, ["Comp BV", "Both BV", "Client BV"])

    def test_legal_form_spellings_merge(self):
        rows = [row("LH", "Deutsche Lufthansa AG", "D-50679 Koeln, X"),
                row("LH2", "Deutsche Lufthansa Aktiengesellschaft", "D-50679 Koeln, X")]
        leads, _ = ld.build_leads(rows, ld.resolve_fields(rows), [], [])
        self.assertEqual(len(leads), 1)
        self.assertEqual(next(iter(leads.values()))["company"], "Deutsche Lufthansa AG")

    def test_digest_puts_competitors_first_and_names_mass_clients(self):
        rows = [row("A", "Comp BV", "NL-1000 Amsterdam, X"),
                row("B", "Client BV", "NL-1000 Amsterdam, X", durch="MASS Response Service GmbH")]
        leads, _ = ld.build_leads(rows, ld.resolve_fields(rows), [], [])
        import datetime as dt
        totals = {"rows": 2, "leads": 2, "competitor": 1}
        text = "\n\n".join(ld.build_digest(list(leads.values()), [], [], ["NL"], self.OWN, 15,
                                            dt.datetime(2026, 11, 18), "", totals))
        self.assertLess(text.index("Comp BV"), text.index("Client BV"))
        self.assertIn("Uses *Twilio*", text)
        self.assertIn("New via MASS, likely our clients (1)*\nClient BV (NL)", text)


class Chunking(unittest.TestCase):
    def test_never_exceeds_limit_or_loses_blocks(self):
        blocks = [f"block {i} " + "x" * 300 for i in range(40)]
        msgs = ld.chunk(blocks, 1000)
        self.assertTrue(all(len(m) <= 1000 for m in msgs))
        self.assertEqual(sum(m.count("block ") for m in msgs), 40)


class EndToEnd(unittest.TestCase):
    def run_cli(self, tmp, rows, *flags):
        src = os.path.join(tmp, "in.json")
        with open(src, "w", encoding="utf-8") as fh:
            json.dump({"data": rows}, fh)
        env = {k: v for k, v in os.environ.items() if "WEBHOOK" not in k and not k.startswith("GITHUB_")}
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lead_digest.py")
        return subprocess.run([sys.executable, script, "--input", src, *flags],
                              cwd=tmp, env=env, capture_output=True, text=True)

    def test_bootstrap_then_new_then_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self.run_cli(tmp, BASE, "--bootstrap")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("baseline ready", first.stdout)
            self.assertTrue(os.path.exists(os.path.join(tmp, "leads_backlog.csv")))

            more = BASE + [row("Coolblue", "Coolblue B.V.", "NL-3012 Rotterdam, Weena 664"),
                           row("ZLND", "Zalando SE", "D-10243 Berlin, Valeska-Gert-Str. 5"),
                           row("NewUS", "NewCo LLC", "USA-94105 San Francisco, 1 Market")]
            second = self.run_cli(tmp, more)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("Coolblue B.V.", second.stdout)
            self.assertIn("Competitor accounts adding Sender IDs", second.stdout)
            self.assertIn("`ZLND`", second.stdout)
            self.assertNotIn("NewCo LLC", second.stdout)   # outside Europe
            self.assertNotIn("bol.com", second.stdout)     # already known
            self.assertEqual(len(os.listdir(os.path.join(tmp, "digests"))), 1)

            third = self.run_cli(tmp, more)
            self.assertIn("(nothing to post)", third.stdout)  # same day, nothing new

    def test_client_adding_a_competitor_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            mass = row("OURS", "Kunde GmbH", "D-10115 Berlin, X", durch="MASS Response Service GmbH")
            self.run_cli(tmp, BASE + [mass], "--bootstrap")
            moved = row("OURS2", "Kunde GmbH", "D-10115 Berlin, X", durch="Twilio Ireland Ltd")
            res = self.run_cli(tmp, BASE + [mass, moved])
            self.assertIn("Our clients also using a competitor (1)", res.stdout)
            self.assertIn("also uses Twilio", res.stdout)

    def test_dry_run_saves_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli(tmp, BASE, "--dry-run")
            self.assertFalse(os.path.exists(os.path.join(tmp, "state.json")))

    def test_schema_change_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = self.run_cli(tmp, [{"foo": 1, "bar": 2}])
            self.assertEqual(res.returncode, 1)
            self.assertIn("schema changed", res.stderr.lower())


if __name__ == "__main__":
    unittest.main()
