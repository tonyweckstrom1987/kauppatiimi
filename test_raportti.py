"""Raportin verorivi tasmaa kirjanpito.fifo:n kanssa samalla kauppadatalla."""
import os
import sqlite3
import tempfile
import unittest

import kirjanpito
import raportti
import retro
import salkku

KAUPAT = [("2025-12-30", "BTCUSDT", "BUY", 1.0, 100.0, 1.0),
          ("2026-01-02", "BTCUSDT", "BUY", 1.0, 200.0, 2.0),
          ("2026-01-03", "BTCUSDT", "SELL", 1.5, 300.0, 4.5),
          ("2026-02-01", "ETHUSDT", "BUY", 2.0, 10.0, 0.2),
          ("2026-02-02", "ETHUSDT", "SELL", 2.0, 9.0, 0.18)]


class TulosTesti(unittest.TestCase):
    def test_tasmaa_fifo_vuosiyhteenvetoon(self):
        odotettu = kirjanpito.vuosiyhteenveto(kirjanpito.fifo(KAUPAT))
        tulos, n, myynnit = raportti.tulos_vuodelle(KAUPAT, 2026)
        self.assertAlmostEqual(tulos, odotettu["2026"])
        self.assertEqual((n, myynnit), (4, 2))   # 2025:n osto ei kuulu 2026:n kauppoihin
        self.assertIn("%+.2f" % odotettu["2026"], raportti.rivi(KAUPAT, 2026))

    def test_tuntematon_vuosi_on_nolla(self):
        self.assertEqual(raportti.tulos_vuodelle(KAUPAT, 2030), (0.0, 0, 0))

    def test_eri_vuoden_realisointi_ei_sekoitu(self):
        k = [("2025-05-01", "A", "BUY", 1.0, 100.0, 0.0), ("2025-06-01", "A", "SELL", 1.0, 150.0, 0.0),
             ("2026-05-01", "A", "BUY", 1.0, 100.0, 0.0), ("2026-06-01", "A", "SELL", 1.0, 90.0, 0.0)]
        self.assertAlmostEqual(raportti.tulos_vuodelle(k, 2025)[0], 50.0)
        self.assertAlmostEqual(raportti.tulos_vuodelle(k, 2026)[0], -10.0)

    def test_ylimyynti_ei_kaada_raporttia(self):
        r = raportti.rivi([("2026-01-01", "A", "SELL", 1.0, 1.0, 0.0)], 2026)
        self.assertIn("ei saatavilla (ValueError)", r)

    def test_ylimaaraiset_sarakkeet_sallitaan(self):
        k7 = [k + (0.0,) for k in KAUPAT]
        self.assertEqual(raportti.tulos_vuodelle(k7, 2026), raportti.tulos_vuodelle(KAUPAT, 2026))


class KantaTesti(unittest.TestCase):
    def _kanta(self, hakem):
        polku = os.path.join(hakem, "s.db")
        con = salkku.avaa(polku)
        con.executemany("INSERT INTO kaupat (paiva,symbol,puoli,maara,hinta,kulu,slippage_kulu) VALUES (?,?,?,?,?,?,0.0)", KAUPAT)
        con.commit()
        con.close()
        return polku

    def test_vero_komento_ja_retro_nayttavat_saman_luvun(self):
        with tempfile.TemporaryDirectory() as h:
            polku = self._kanta(h)
            odotettu = "%+.2f" % kirjanpito.vuosiyhteenveto(kirjanpito.fifo(KAUPAT))["2026"]
            teksti = raportti.vero("2026", {"x": polku})
            self.assertIn(odotettu, teksti)
            self.assertIn("kauppoja 4", teksti)
            s = retro.lue_salkku(polku)
            self.assertEqual(len(s["kaupat"][0]), 7)
            self.assertIn(odotettu, raportti.rivi(s["kaupat"], "2026"))

    def test_puuttuva_kanta(self):
        self.assertIn("kantaa ei voi lukea (OperationalError)", raportti.vero("2026", {"x": "/ei/ole.db"}))

    def test_main_virheellinen_syote(self):
        self.assertEqual(raportti.main(["raportti", "vero", "abc"]), 2)
        self.assertEqual(raportti.main(["raportti"]), 2)


if __name__ == "__main__":
    unittest.main()
