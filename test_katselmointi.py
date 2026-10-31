"""Katselmointitestit (30.10.2026): kirjaavat NYKYISEN kayttaytymisen, jotta rajatapaukset eivat muutu huomaamatta.
Eivat muuta logiikkaa. Kohdat, jotka ovat Tonyn paatettavia, on merkitty PAATETTAVA."""
import sqlite3
import unittest

import agentit
import kirjanpito
import retro
import riskivahti


class RiskivahtiRaja(unittest.TestCase):
    def test_drawdown_raja_on_inklusiivinen(self):
        self.assertTrue(riskivahti.tila([100.0, 75.0], False)[0])
        self.assertFalse(riskivahti.tila([100.0, 75.01], False)[0])

    def test_jarru_pysyy_lukossa_vaikka_arvo_palaa(self):
        self.assertTrue(riskivahti.tila([100.0, 50.0, 100.0], True)[0])

    def test_jattimaisen_painon_poikkeus_ei_ole_ehdotusvirhe(self):
        # PAATETTAVA: riskivahti nostaa OverflowError, ei Ehdotusvirhea. agentit.jasenna_analyytikko
        # sieppaa taman ennen riskivahtia, joten tuotannossa ei vuoda, mutta riskivahti ei yksin ole fail closed.
        with self.assertRaises(OverflowError):
            riskivahti.siisti_ehdotus({"BTCUSDT": 10 ** 400}, {"BTCUSDT"})

    def test_positiorajat_ja_puskuri(self):
        s = set("ABCDEFG")
        painot, _ = riskivahti.siisti_ehdotus({x: 0.15 for x in s}, s)
        self.assertAlmostEqual(sum(painot.values()), 0.90)
        self.assertTrue(all(w <= 0.15 for w in painot.values()))


class KriitikonKerroin(unittest.TestCase):
    def test_hylkaa_palauttaa_none(self):
        self.assertIsNone(agentit.sovella_kriitikko({"A": 0.1}, {"paatos": "hylkaa", "kerroin": 1.0}))

    def test_hyvaksy_ei_kasvata(self):
        self.assertEqual(agentit.sovella_kriitikko({"A": 0.1}, {"paatos": "hyvaksy", "kerroin": 0.3}), {"A": 0.1})

    def test_kerroin_rajat(self):
        for huono in ('1.5', '-0.1', 'true', '"0.5"', 'NaN'):
            with self.assertRaises(agentit.Vastausvirhe):
                agentit.jasenna_kriitikko('{"paatos": "pienenna", "kerroin": %s}' % huono)

    def test_pienenna_nolla_eroaa_hylkaasta(self):
        # PAATETTAVA: pienenna x0.0 antaa nollapainot (salkku myy kaiken), hylkaa antaa None (salkku pitaa nykyiset).
        self.assertEqual(agentit.sovella_kriitikko({"A": 0.1}, {"paatos": "pienenna", "kerroin": 0.0}), {"A": 0.0})


class RetroKriitikko(unittest.TestCase):
    def markkina(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE hinnat (symbol TEXT, ts INTEGER, open REAL, close REAL)")
        con.execute("INSERT INTO hinnat VALUES ('A', 1788220800000, 100, 110)")   # 2026-09-01, +10 %
        return con

    def test_hylkaa_lasketaan_nollapainoksi(self):
        # PAATETTAVA: oikeassa ajossa hylkaa = pida nykyiset, ei nollaa. Retro mallintaa sen nollana, joten arvo on -R(ehdotus).
        r = retro.kriitikon_vaikutus([("2026-09-01", '{"A": 0.1}', "hylkaa", 1.0)], self.markkina())
        self.assertAlmostEqual(r["arvo"], -1.0)   # -(0.1 * 10 %) = -1,0 %-yks

    def test_pienenna_arvo(self):
        r = retro.kriitikon_vaikutus([("2026-09-01", '{"A": 0.1}', "pienenna", 0.5)], self.markkina())
        self.assertAlmostEqual(r["arvo"], -0.5)


class FifoKirjanpito(unittest.TestCase):
    def test_osamyynti_kuluineen(self):
        r = kirjanpito.fifo([("2026-01-01", "A", "BUY", 1.0, 100.0, 0.1), ("2026-01-02", "A", "SELL", 0.5, 110.0, 0.05)])
        _, _, maara, tulo, meno, voitto = r[0]
        self.assertAlmostEqual(tulo, 54.95)
        self.assertAlmostEqual(meno, 50.05)
        self.assertAlmostEqual(voitto, 4.9)

    def test_vanhin_era_ensin(self):
        r = kirjanpito.fifo([("2026-01-01", "A", "BUY", 1.0, 100.0, 0.0), ("2026-01-02", "A", "BUY", 1.0, 200.0, 0.0),
                             ("2026-01-03", "A", "SELL", 1.0, 300.0, 0.0)])
        self.assertAlmostEqual(r[0][5], 200.0)

    def test_myynti_yli_omistuksen(self):
        with self.assertRaises(ValueError):
            kirjanpito.fifo([("2026-01-01", "A", "BUY", 1.0, 100.0, 0.0), ("2026-01-02", "A", "SELL", 1.001, 100.0, 0.0)])


if __name__ == "__main__":
    unittest.main()
