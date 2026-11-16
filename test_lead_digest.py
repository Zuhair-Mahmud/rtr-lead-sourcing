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
        bol = self.leads[ld.norm("bol.com B.V.")]
        self.assertEqual(bol["sender_ids"], ["BOLCOM", "BOL"])
        self.assertEqual(len(bol["providers"]), 2)
        self.assertEqual(bol["country"], "NL")

    def test_status_field_is_the_text_column(self):
        self.assertEqual(ld.resolve_fields(BASE)["status"], "status")

    def test_exclusions(self):
        self.assertNotIn(ld.norm("Spryng B.V."), self.leads)          # own brand
        self.assertNotIn(ld.norm("Twilio Ireland Limited"), self.leads)  # a provider
        self.assertNotIn(ld.norm("Gone GmbH"), self.leads)            # deleted

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
        self.assertEqual(leads[ld.norm("bol.com B.V.")]["sender_ids"], ["BOLCOM"])
        self.assertEqual(stats["excluded_own_provider"], 1)

    def test_default_drops_mass_response_and_none_keeps_all(self):
        rows = [row("OURS", "Customer BV", "NL-1000 Amsterdam, X", durch="MASS Response Service GmbH")]
        f = ld.resolve_fields(rows)
        dropped, _ = ld.build_leads(rows, f, [ld.DEFAULT_EXCLUDE_SUBMITTERS], [])
        kept, _ = ld.build_leads(rows, f, ["none"], [])
        self.assertEqual((len(dropped), len(kept)), (0, 1))

    def test_core_outranks_rest_of_europe(self):
        core = ["NL", "BE", "DE", "AT"]
        rows = [row("A", "Core BV", "NL-1000 Amsterdam, X"), row("B", "Other SA", "F-75001 Paris, Y")]
        leads, _ = ld.build_leads(rows, ld.resolve_fields(rows), [], [])
        s = {l["company"]: ld.score(l, core) for l in leads.values()}
        self.assertGreater(s["Core BV"], s["Other SA"])


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
            self.assertIn("Known brands adding Sender IDs", second.stdout)
            self.assertIn("`ZLND`", second.stdout)
            self.assertNotIn("NewCo LLC", second.stdout)   # outside Europe
            self.assertNotIn("bol.com", second.stdout)     # already known
            self.assertEqual(len(os.listdir(os.path.join(tmp, "digests"))), 1)

            third = self.run_cli(tmp, more)
            self.assertIn("(nothing to post)", third.stdout)  # same day, nothing new

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
