import json
import os
import tempfile
import unittest

import laaturaportti as lr


def rivi(ajo, ryhma, tulos, aika=1000, **k):
    d = {"aika": aika, "ajo": ajo, "ryhma": ryhma, "tulos": tulos}
    d.update(k)
    return d


class LaskeTesti(unittest.TestCase):
    def test_paiva_ryhmittain(self):
        rs = [rivi("paiva", "pro", "ok", keksitty=0), rivi("paiva", "pro", "virhe", virhekoodi="LLMVirhe"),
              rivi("paiva", "flash", "ok", keksitty=0), rivi("paiva", "flash", "ei_tietoa", keksitty=0)]
        m = lr.laske(rs)
        self.assertEqual(m[("paiva", "pro")]["virhe"], 1)
        self.assertEqual(m[("paiva", "pro")]["virhekoodit"], {"LLMVirhe": 1})
        self.assertAlmostEqual(m[("paiva", "flash")]["kelpo_osuus"], 1.0)

    def test_kaikki_hylatty_erillaan_ja_kuivan_portti_ennallaan(self):
        rs = [rivi("k7_kuiva", "pro", "kaikki_hylatty", ehdotettu=3, kelvollinen=0, hylatty=3, tuplia=0, hylkaykset="hyp_lyhyt:2,ei_olio:1"),
              rivi("k7_kuiva", "pro", "ok", ehdotettu=3, kelvollinen=3, hylatty=0, tuplia=0),
              rivi("k7_kuiva", "pro", "ei_tietoa", ehdotettu=0, kelvollinen=0, hylatty=0, tuplia=0, hylkaykset="rikki"),
              rivi("k7_kuiva", "flash", "ok", ehdotettu=3, kelvollinen=3, hylatty=0, tuplia=0)]
        m = lr.laske(rs)[("k7_kuiva", "pro")]
        self.assertEqual((m["ok"], m["ei_tietoa"], m["kaikki_hylatty"]), (1, 1, 1))
        self.assertEqual(m["hylkayssyyt"], {"hyp_lyhyt": 2, "ei_olio": 1})
        self.assertAlmostEqual(m["kelv_osuus"], 0.5)    # portti ennallaan: ehdotettu 6, kelvollinen 3
        mm = lr.laske(rs)
        self.assertIn("kaikki_hylatty 1", lr.teksti(mm, lr.portti(mm)))

    def test_ei_kuiva_k7_kaikki_hylatty_laskee_kelpo_osuutta(self):
        """Lukitsee nykykaytoksen (ei ota kantaa onko oikea): ei-kuivalla k7-ajolla kaikki_hylatty EI lasketa kelpoihin, joten portti voi sulkeutua."""
        verrokki = [rivi("k7", "pro", "ok") for _ in range(20)]
        uusi = [rivi("k7", "flash", "ok") for _ in range(17)] + [rivi("k7", "flash", "kaikki_hylatty", ehdotettu=3, kelvollinen=0, hylatty=3) for _ in range(3)]
        vanha = [rivi("k7", "flash", "ok") for _ in range(17)] + [rivi("k7", "flash", "ei_tietoa", ehdotettu=3, kelvollinen=0, hylatty=3) for _ in range(3)]
        m = lr.laske(verrokki + uusi)
        self.assertAlmostEqual(m[("k7", "flash")]["kelpo_osuus"], 0.85)     # (17 ok + 0 ei_tietoa) / 20
        p = lr.portti(m)[("k7", "flash")][0]
        self.assertEqual((p[0], p[1]), ("kelvollisten tulosten osuus >= 90 %", "EI"))
        m0 = lr.laske(verrokki + vanha)
        self.assertAlmostEqual(m0[("k7", "flash")]["kelpo_osuus"], 1.0)     # sama data vanhalla luokituksella: ei_tietoa laski kelpoihin
        self.assertEqual(lr.portti(m0)[("k7", "flash")][0][1], "ok")

    def test_vanhat_rivit_ilman_uusia_kenttia(self):
        m = lr.laske([rivi("paiva", "pro", "ok")])[("paiva", "pro")]
        self.assertEqual((m["kaikki_hylatty"], m["hylkayssyyt"]), (0, {}))

    def test_portti_paiva(self):
        rs = [rivi("paiva", "pro", "ok", keksitty=0)] * 20 + [rivi("paiva", "flash", "ok", keksitty=0)] * 18 + [rivi("paiva", "flash", "virhe", virhekoodi="X")] * 2
        p = lr.portti(lr.laske(rs))[("paiva", "flash")]
        tilat = {k: t for k, t, _ in p}
        self.assertEqual(tilat["kelvollisten tulosten osuus >= 90 %"], "ok")      # 90 % raja-arvolla
        self.assertEqual(tilat["keksittyja faktoja 0"], "ok")
        self.assertEqual(tilat["virheet <= verrokki + 5 pp"], "EI")               # 10 % vs 0 %

    def test_keksitty_estaa(self):
        rs = [rivi("paiva", "pro", "ok", keksitty=0), rivi("paiva", "flash", "ok", keksitty=2)]
        tilat = {k: t for k, t, _ in lr.portti(lr.laske(rs))[("paiva", "flash")]}
        self.assertEqual(tilat["keksittyja faktoja 0"], "EI")

    def test_pieni_otos_ei_ole_ok(self):
        rs = [rivi("paiva", "pro", "ok", keksitty=0)] * 5 + [rivi("paiva", "flash", "ok", keksitty=0)] * 5
        tilat = {k: t for k, t, _ in lr.portti(lr.laske(rs))[("paiva", "flash")]}
        self.assertEqual(tilat["otoskoko >= 20"], "ei_dataa")
        self.assertNotIn("ok", tilat.values())

    def test_ei_dataa_ei_ole_ok(self):
        rs = [rivi("paiva", "flash", "ok")]
        tilat = {k: t for k, t, _ in lr.portti(lr.laske(rs))[("paiva", "flash")]}
        self.assertEqual(tilat["keksittyja faktoja 0"], "ei_dataa")
        self.assertEqual(tilat["virheet <= verrokki + 5 pp"], "ei_dataa")

    def test_kuiva(self):
        rs = [rivi("k7_kuiva", "pro", "ok", ehdotettu=3, kelvollinen=3, tuplia=0),
              rivi("k7_kuiva", "flash", "ok", ehdotettu=3, kelvollinen=2, tuplia=1)]
        tilat = {k: t for k, t, _ in lr.portti(lr.laske(rs))[("k7_kuiva", "flash")]}
        self.assertEqual(tilat["kelvollisten ehdotusten osuus >= 90 %"], "EI")    # 67 %
        self.assertEqual(tilat["tuplia <= 20 %"], "EI")                           # 50 %


class LueTesti(unittest.TestCase):
    def test_rikkinaiset_rivit_ja_aikaraja(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.jsonl")
            with open(p, "w") as f:
                f.write(json.dumps(rivi("paiva", "pro", "ok", aika=100)) + "\n")
                f.write("ei jsonia\n")
                f.write(json.dumps(rivi("paiva", "pro", "ok", aika=500)) + "\n")
            self.assertEqual(len(lr.lue(p)), 2)
            self.assertEqual(len(lr.lue(p, alku=300)), 1)
        self.assertEqual(lr.lue("/ei/ole/tata"), [])

    def test_teksti_tyhja_ja_tayteen(self):
        self.assertIn("Ei tapahtumia", lr.teksti({}, {}))
        rs = [rivi("paiva", "pro", "ok", kesto_s=2.0, keksitty=0), rivi("paiva", "flash", "ok", kesto_s=1.0, keksitty=0)]
        m = lr.laske(rs)
        t = lr.teksti(m, lr.portti(m), 7)
        self.assertIn("paiva / flash", t)
        self.assertIn("viimeiset 7 pv", t)


if __name__ == "__main__":
    unittest.main()
