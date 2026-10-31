"""No-trade band (kynnys.py): oletus pois, pienet muutokset ohitetaan, poistuminen ei esty, salkku.py koskematon."""
import unittest
from datetime import timedelta

import kynnys
import salkku
from test_k3 import ALKU, S, markkina


def ajo(ehdotukset, kp):
    mk = markkina({s: [(100, 100)] * 6 for s in S})
    con = salkku.avaa(":memory:", alkupaoma=10000.0)
    ulos = []
    for i, e in enumerate(ehdotukset):
        p = ALKU + timedelta(days=i)
        e = kynnys.suodata(con, mk, p, e, S, kp)
        ulos.append(salkku.suorita_paiva(con, mk, p, e, S))
    return ulos


class KynnysTesti(unittest.TestCase):
    def test_oletus_pois(self):
        self.assertEqual(kynnys.KYNNYS_PP, 0.0)
        e = {"BTCUSDT": 0.108}
        self.assertIs(kynnys.suodata(None, None, ALKU, e, S), e)

    def test_nolla_ei_muuta(self):
        e = [{"BTCUSDT": 0.10}, {"BTCUSDT": 0.108}]
        self.assertEqual(len(ajo(e, 0.0)[1]["kaupat"]), 1)

    def test_pieni_muutos_ohitetaan(self):
        t = ajo([{"BTCUSDT": 0.10}, {"BTCUSDT": 0.108}], 2.0)
        self.assertEqual(t[1]["kaupat"], [])

    def test_iso_muutos_menee_lapi(self):
        t = ajo([{"BTCUSDT": 0.10}, {"BTCUSDT": 0.14}], 2.0)
        self.assertEqual(len(t[1]["kaupat"]), 1)

    def test_taysi_poistuminen_ei_esty(self):
        t = ajo([{"BTCUSDT": 0.10}, {}], 5.0)
        self.assertEqual([k[2] for k in t[1]["kaupat"]], ["SELL"])
        t = ajo([{"BTCUSDT": 0.10}, {"BTCUSDT": 0.0}], 5.0)
        self.assertEqual([k[2] for k in t[1]["kaupat"]], ["SELL"])

    def test_pienta_uutta_ei_avata(self):
        t = ajo([{"BTCUSDT": 0.10}, {"BTCUSDT": 0.10, "ETHUSDT": 0.01}], 2.0)
        self.assertEqual(t[1]["kaupat"], [])

    def test_virheellinen_ehdotus_ennallaan(self):
        e = {"XRPUSDT": 0.1}
        self.assertIs(kynnys.suodata(None, None, ALKU, e, S, 2.0), e)
        self.assertIsNone(kynnys.suodata(None, None, ALKU, None, S, 2.0))


if __name__ == "__main__":
    unittest.main()
