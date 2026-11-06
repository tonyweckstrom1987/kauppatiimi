import json
import os
import tempfile
import unittest

import tapahtumat as th


def rivit(polku):
    with open(polku, encoding="utf-8") as f:
        return [json.loads(x) for x in f]


class TapahtumatTesti(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.p = os.path.join(self.t, "tapahtumat.jsonl")

    def test_kirjaa_rivin_ja_lisaa_perakkain(self):
        self.assertTrue(th.kirjaa("paiva", "ok", polku=self.p, nyt=100, ryhma="flash", malli="deepseek-flash", tokens_in=10, tokens_out=5))
        self.assertTrue(th.kirjaa("paiva", "virhe", polku=self.p, nyt=200, ryhma="pro", virhekoodi="LLMVirhe"))
        r = rivit(self.p)
        self.assertEqual(len(r), 2)
        self.assertEqual((r[0]["ryhma"], r[0]["tokens_in"], r[0]["tulos"]), ("flash", 10, "ok"))
        self.assertEqual(r[1]["virhekoodi"], "LLMVirhe")

    def test_kaikki_hylatty_ja_hylkaykset_kirjataan(self):
        self.assertTrue(th.kirjaa("k7_kuiva", "kaikki_hylatty", polku=self.p, hylkaykset="nimi_puuttuu:3"))
        r = rivit(self.p)[0]
        self.assertEqual((r["tulos"], r["hylkaykset"]), ("kaikki_hylatty", "nimi_puuttuu:3"))

    def test_tuntematon_kentta_pudotetaan(self):
        th.kirjaa("paiva", "ok", polku=self.p, strategia="salainen", hinta=123.4, ryhma="pro")
        r = rivit(self.p)[0]
        self.assertNotIn("strategia", r)
        self.assertNotIn("hinta", r)
        self.assertEqual(r["ryhma"], "pro")

    def test_salaisuuden_nakoinen_siivotaan_ja_pitka_teksti_katkeaa(self):
        th.kirjaa("paiva", "virhe", polku=self.p, virhekoodi="sk-" + "a" * 24 + "x" * 300)
        r = rivit(self.p)[0]["virhekoodi"]
        self.assertNotIn("sk-a", r)
        self.assertLessEqual(len(r), 120)

    def test_virheellinen_tulos_ei_kirjaudu(self):
        self.assertFalse(th.kirjaa("paiva", "muu", polku=self.p))
        self.assertFalse(os.path.exists(self.p))

    def test_puuttuva_hakemisto_ei_kaada(self):
        self.assertFalse(th.kirjaa("paiva", "ok", polku="/ei/ole/tata/loki.jsonl"))

    def test_kirjoitusvirhe_ei_kaada(self):
        self.assertFalse(th.kirjaa("paiva", "ok", polku=self.t))  # hakemisto ei ole tiedosto

    def test_paivan_tulos_luokat(self):
        self.assertEqual(th.paivan_tulos({"virhe": "LLMVirhe: x"}), "virhe")
        self.assertEqual(th.paivan_tulos({"virhe": None, "analyytikko": {"en_tieda": True}}), "ei_tietoa")
        self.assertEqual(th.paivan_tulos({"virhe": None, "analyytikko": {"paino": {}}}), "ok")

    def test_virhekoodi_ei_sisalla_viestin_sisaltoa(self):
        self.assertEqual(th.virhekoodi("LLMVirhe: HTTP 429 salainen"), "LLMVirhe")
        self.assertIsNone(th.virhekoodi(None))

    def test_paivan_kirjaus_laskee_tokenit_salkun_taulusta(self):
        import sqlite3
        from datetime import date
        import paiva
        sal = sqlite3.connect(":memory:")
        sal.execute("CREATE TABLE llm_kutsut (paiva TEXT, rooli TEXT, malli TEXT, tokens_in INTEGER, tokens_out INTEGER)")
        sal.execute("INSERT INTO llm_kutsut VALUES ('2026-10-25','analyytikko','m',100,20)")
        sal.execute("INSERT INTO llm_kutsut VALUES ('2026-10-25','kriitikko','m',50,10)")
        sal.execute("INSERT INTO llm_kutsut VALUES ('2026-10-24','analyytikko','m',999,999)")
        vanha = th.POLKU
        th.POLKU = self.p
        try:
            paiva._kirjaa_tapahtuma(sal, date(2026, 10, 25), "flash", "deepseek-flash", {"virhe": None, "analyytikko": {"paino": {}}}, 3.14)
        finally:
            th.POLKU = vanha
        r = rivit(self.p)[0]
        self.assertEqual((r["tokens_in"], r["tokens_out"], r["ryhma"], r["tulos"], r["kesto_s"]), (150, 30, "flash", "ok", 3.1))


if __name__ == "__main__":
    unittest.main()
