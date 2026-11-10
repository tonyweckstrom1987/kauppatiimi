"""Viikkokoonnin testit pienellä testikannalla. Aja repon juuresta:
python3 -m unittest discover -p 'test_*.py'"""
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from datetime import timedelta
from contextlib import redirect_stdout

from analyysi.seuranta import seuranta as s
from analyysi.seuranta import viikkokoonti as vk
from analyysi.seuranta.test_seuranta import ALKU, rakenna


def lisaa_paatokset(hakemisto, nimi, paivat, kutsut, loki):
    """paivat: indeksit, kutsut: {indeksi: [roolit]}, loki: {indeksi: paatos}."""
    c = sqlite3.connect("%s/%s.db" % (hakemisto, nimi))
    c.execute("create table paatokset (paiva text, hylatty text)")
    c.execute("create table llm_kutsut (paiva text, rooli text)")
    c.execute("create table kriitikko_loki (paiva text, paatos text)")
    for i in paivat:
        p = (ALKU + timedelta(days=i)).isoformat()
        c.execute("insert into paatokset values (?, 'x')", (p,))
        for r in kutsut.get(i, []):
            c.execute("insert into llm_kutsut values (?,?)", (p, r))
        if i in loki:
            c.execute("insert into kriitikko_loki values (?,?)", (p, loki[i]))
    c.commit()
    c.close()


class Luokka(unittest.TestCase):
    def test_luokat(self):
        self.assertEqual(vk.luokka([], None), "d")
        self.assertEqual(vk.luokka(["analyytikko", "kriitikko"], "hylkaa"), "a")
        self.assertEqual(vk.luokka(["analyytikko"], None), "c")
        self.assertEqual(vk.luokka(["analyytikko", "kriitikko"], None), "b")
        self.assertEqual(vk.luokka(["analyytikko", "kriitikko"], "hyvaksy"), "e")


class Koonti(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def rakenna(self, paivia=10, **kw):
        rakenna(self.d, paivia, hinta=lambda i: 100.0 + i, salkku=lambda i: 10000.0 + 10 * i,
                osuus=lambda i: 0.5, **kw)
        for n in s.SALKUT:
            lisaa_paatokset(self.d, n, [1, 2, 3, 4], {1: ["analyytikko", "kriitikko"], 2: ["analyytikko"],
                            3: ["analyytikko", "kriitikko"]}, {1: "hylkaa"})

    def test_tuloste_ja_luvut_seurannasta(self):
        self.rakenna()
        teksti = vk.koonti(self.d)
        t = s.laske(self.d)
        self.assertIn("10 pv / 90 (11 %)", teksti)
        self.assertIn(vk.pp(t["salkut"][0]["tuotto"]), teksti)
        self.assertIn("a kriitikko hylkäsi 1", teksti)
        self.assertIn("c kutsusarja päättyi analyytikkoon 1", teksti)
        self.assertIn("b analyytikko ja kriitikko, ei kriitikkolokia 1", teksti)
        self.assertIn("d ei LLM-kutsuja 1", teksti)
        self.assertIn("Ehdot 2, 4 ja 5: ei mitattu tällä komennolla.", teksti)
        self.assertIn("viive", teksti)
        self.assertLessEqual(len(teksti.splitlines()), vk.MAX_RIVEJA)

    def test_ei_suosituksia(self):
        self.rakenna()
        self.assertNotIn("suosit", vk.koonti(self.d).lower().replace("ei sijoitusneuvo", ""))

    def test_vanha_kanta_ilman_sijoitusastetta(self):
        self.rakenna(ilman_sarakkeita=True)
        teksti = vk.koonti(self.d)
        self.assertIn("| salkku_flash | ", teksti)
        self.assertIn("| ? |", teksti)

    def test_tyhja_paatostaulu_ei_kaada(self):
        rakenna(self.d, 10, osuus=lambda i: 0.5)
        for n in s.SALKUT:
            lisaa_paatokset(self.d, n, [], {}, {})
        self.assertIn("ei tyhjiä päiviä", vk.koonti(self.d))

    def test_paatostaulut_puuttuvat_antaa_kysymysmerkin(self):
        rakenna(self.d, 10, osuus=lambda i: 0.5)
        self.assertIn("- salkku_flash: ?", vk.koonti(self.d))

    def test_tyhja_data(self):
        rakenna(self.d, 0)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(vk.main([self.d]), 1)
        self.assertEqual(out.getvalue().strip(), "virhe: ValueError")

    def test_puuttuva_tiedosto(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(vk.main([self.d + "/ei-ole"]), 1)
        self.assertTrue(out.getvalue().startswith("virhe: "))
        self.assertEqual(len(out.getvalue().strip().splitlines()), 1)

    def test_puuttuva_paivitetty(self):
        self.rakenna()
        import os
        os.remove(self.d + "/PAIVITETTY")
        self.assertIn("Data: tuntematon", vk.koonti(self.d))

    def test_data_ei_muutu(self):
        self.rakenna()
        import hashlib
        h = lambda: [hashlib.sha256(Path("%s/%s.db" % (self.d, n)).read_bytes()).hexdigest()
                     for n in s.SALKUT]
        ennen = h()
        vk.koonti(self.d)
        self.assertEqual(ennen, h())


if __name__ == "__main__":
    unittest.main()
