import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import db
import indeksidata
import passiivinen_kk as pk
import passiivinen_vertailu as pv
import rajat
import salkku

K = (1 + rajat.SLIPPAGE) * (1 + rajat.KULU)


def sarja(alku, n, avaus=100.0, sulku=100.0, ei_auki=()):
    """{open, close}-sanakirjat n paivalle; ei_auki: paivat joina porssi on kiinni (ei avaushintaa)."""
    av, su = {}, {}
    for i in range(n):
        d = alku + timedelta(days=i)
        if d in ei_auki:
            continue
        av[d] = avaus(d) if callable(avaus) else avaus
        su[d] = sulku(d) if callable(sulku) else sulku
    return {"open": av, "close": su}


def ms(p):
    return int(datetime(p.year, p.month, p.day, tzinfo=timezone.utc).timestamp() * 1000)


def markkina_kanta(polku, alku, n, avaus=100.0, sulku=lambda i: 100.0):
    con = db.avaa(polku)
    rivit = [("BTCUSDT", ms(alku + timedelta(days=i)), avaus, avaus, avaus, sulku(i), 1.0, 1.0, 1) for i in range(n)]
    db.lisaa_hinnat(con, "BTCUSDT", [r[1:] for r in rivit])
    con.close()


class SimulaattoriTesti(unittest.TestCase):
    def test_kulumalli_sama_kuin_salkku_py(self):
        """osto() antaa saman maaran ja kulun kuin salkku.suorita_paiva samalla summalla."""
        con = salkku.avaa(":memory:")
        mk = db.avaa(":memory:")
        p = date(2026, 1, 5)
        db.lisaa_hinnat(mk, "BTCUSDT", [(ms(p), 100.0, 100.0, 100.0, 100.0, 1.0, 1.0, 1)])
        salkku.suorita_paiva(con, mk, p, {"BTCUSDT": 0.1}, {"BTCUSDT"})
        q_sal = salkku.positiot(con)["BTCUSDT"]
        kulu_sal = con.execute("SELECT kulu FROM kaupat").fetchone()[0]
        q, hinta, kulu, slip = pk.osto(1000.0, 100.0)
        self.assertAlmostEqual(q, q_sal, 9)
        self.assertAlmostEqual(kulu, kulu_sal, 9)
        self.assertAlmostEqual(q * hinta + kulu, 1000.0, 9)

    def test_vakiohinta_12_eraa_vain_ostoja_ja_kulut_pienentavat(self):
        h = {"BTCUSDT": sarja(date(2026, 1, 1), 400)}
        r = pk.simuloi(h, {"BTCUSDT": 1.0}, date(2026, 1, 1), date(2026, 12, 31), 1000.0, erat=12, alkukassa=12000.0)
        self.assertEqual(len(r["kaupat"]), 12)
        self.assertTrue(all(k[2] == "BUY" for k in r["kaupat"]))
        self.assertEqual([k[0][:7] for k in r["kaupat"]], ["2026-%02d" % m for m in range(1, 13)])
        self.assertAlmostEqual(r["arvot"][-1][1], 12000.0 - 12 * (1000.0 - 1000.0 / K), 6)
        m = pk.tulosmittarit(r)
        self.assertLess(m["tuotto"], 0)
        self.assertAlmostEqual(m["tuotto"], 1 / K - 1, 9)

    def test_tuplaantuva_hinta(self):
        h = {"BTCUSDT": sarja(date(2026, 1, 1), 40, sulku=200.0)}
        r = pk.simuloi(h, {"BTCUSDT": 1.0}, date(2026, 1, 1), date(2026, 2, 9), 1000.0, erat=2, alkukassa=2000.0)
        self.assertAlmostEqual(r["arvot"][-1][1], 2 * 1000.0 / K * 2, 6)

    def test_porssi_kiinni_osto_siirtyy_seuraavaan_avoimeen_paivaan(self):
        kiinni = {date(2026, 3, 1), date(2026, 3, 2)}
        h = {"BTCUSDT": sarja(date(2026, 3, 1), 40), "SPX": sarja(date(2026, 3, 1), 40, ei_auki=kiinni)}
        r = pk.simuloi(h, {"BTCUSDT": 0.5, "SPX": 0.5}, date(2026, 3, 1), date(2026, 3, 31), 1000.0, erat=12, alkukassa=12000.0)
        self.assertEqual({k[0] for k in r["kaupat"]}, {"2026-03-03"})
        self.assertEqual(len(r["kaupat"]), 2)

    def test_puuttuva_sulkuhinta_omistetulle_on_virhe(self):
        h = {"BTCUSDT": sarja(date(2026, 1, 1), 3)}
        h["BTCUSDT"]["close"].pop(date(2026, 1, 1))
        with self.assertRaises(salkku.Datavirhe):
            pk.simuloi(h, {"BTCUSDT": 1.0}, date(2026, 1, 1), date(2026, 1, 3), 100.0, erat=1, alkukassa=100.0)

    def test_tasapainotus_myy_ylipainon_vuoden_kuluttua(self):
        alku = date(2026, 1, 1)
        h = {"BTCUSDT": sarja(alku, 800, avaus=lambda d: 100.0 if d < date(2026, 12, 1) else 400.0, sulku=lambda d: 100.0 if d < date(2026, 12, 1) else 400.0),
             "SPX": sarja(alku, 800)}
        r = pk.simuloi(h, {"BTCUSDT": 0.5, "SPX": 0.5}, alku, date(2027, 3, 31), 1000.0, erat=None, alkukassa=0.0, talletus=True, tasapaino_kk=12)
        myynnit = [k for k in r["kaupat"] if k[2] == "SELL"]
        self.assertTrue(myynnit)
        self.assertTrue(all(k[0] >= "2027-01-01" and k[1] == "BTCUSDT" for k in myynnit))
        vt = pk.verotapahtumat(r["kaupat"])
        self.assertEqual(vt["2027"][0], len(myynnit))
        self.assertEqual(vt["2026"][0], 0)

    def test_tasapainotus_pois_ei_myyntia(self):
        alku = date(2026, 1, 1)
        nousu = lambda d: 100.0 + (d - alku).days
        h = {"BTCUSDT": sarja(alku, 800, avaus=nousu, sulku=nousu), "SPX": sarja(alku, 800)}
        r = pk.simuloi(h, {"BTCUSDT": 0.5, "SPX": 0.5}, alku, date(2027, 3, 31), 1000.0, erat=None, alkukassa=0.0, talletus=True)
        self.assertFalse([k for k in r["kaupat"] if k[2] == "SELL"])

    def test_painot_summa_virhe(self):
        with self.assertRaises(ValueError):
            pk.simuloi({"A": sarja(date(2026, 1, 1), 2)}, {"A": 0.5}, date(2026, 1, 1), date(2026, 1, 2), 1.0)

    def test_pudotus_indeksi_ei_nae_talletusta(self):
        """Talletus ei saa nayttaa tuottona eika pienentaa pudotusta: tasainen hinta -> pudotus vain kuluista."""
        h = {"BTCUSDT": sarja(date(2026, 1, 1), 200)}
        r = pk.simuloi(h, {"BTCUSDT": 1.0}, date(2026, 1, 1), date(2026, 7, 19), 1000.0, talletus=True)
        m = pk.tulosmittarit(r)
        self.assertLess(m["max_dd"], 0.01)
        self.assertEqual(m["kauppoja"], 7)

    def test_tappiokuukaudet(self):
        h = {"BTCUSDT": sarja(date(2026, 1, 1), 95, sulku=lambda d: 100.0 if d < date(2026, 3, 1) else 50.0)}
        r = pk.simuloi(h, {"BTCUSDT": 1.0}, date(2026, 1, 1), date(2026, 4, 5), 1000.0, erat=12, alkukassa=12000.0)
        m = pk.tulosmittarit(r)
        self.assertEqual(m["kuukausia"], 4)
        self.assertGreaterEqual(m["tappiokuukausia"], 2)    # tammi (kulut) ... huhti: arvo alle sijoitetun

    def test_lisaa_kuukausia(self):
        self.assertEqual(pk.lisaa_kuukausia(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(pk.lisaa_kuukausia(date(2026, 11, 1), 3), date(2027, 2, 1))
        self.assertEqual(pk.lisaa_kuukausia(date(2026, 1, 1), 12), date(2027, 1, 1))


class ElavaSalkkuTesti(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mk = os.path.join(self.tmp.name, "m.db")
        self.sal = os.path.join(self.tmp.name, "s.db")

    def tearDown(self):
        self.tmp.cleanup()

    def aja(self, alku, n, sulku=lambda i: 100.0):
        markkina_kanta(self.mk, alku, n, sulku=sulku)
        mk = db.avaa(self.mk)
        con = salkku.avaa(self.sal)
        tulokset = [pk.aja_paiva(con, mk, alku + timedelta(days=i)) for i in range(n)]
        return con, tulokset

    def test_ostaa_kerran_kuussa_12_kertaa_eika_koskaan_myy(self):
        con, t = self.aja(date(2026, 1, 15), 500)
        kaupat = con.execute("SELECT paiva, puoli FROM kaupat ORDER BY id").fetchall()
        self.assertEqual(len(kaupat), 12)
        self.assertTrue(all(p == "BUY" for _, p in kaupat))
        self.assertEqual(kaupat[0][0], "2026-01-15")
        self.assertEqual(kaupat[1][0], "2026-02-01")
        self.assertEqual(t[-1]["eria"], 12)
        self.assertLess(salkku.kateinen(con), 1e-6)

    def test_toinen_ajo_samalle_paivalle_hylataan(self):
        con, t = self.aja(date(2026, 1, 15), 3)
        mk = db.avaa(self.mk)
        with self.assertRaises(salkku.Datavirhe):
            pk.aja_paiva(con, mk, date(2026, 1, 17))

    def test_puuttuva_hinta_on_datavirhe_eika_kirjaa_mitaan(self):
        con, _ = self.aja(date(2026, 1, 15), 2)
        mk = db.avaa(self.mk)
        ennen = con.execute("SELECT COUNT(*) FROM arvot").fetchone()[0]
        with self.assertRaises(salkku.Datavirhe):
            pk.aja_paiva(con, mk, date(2026, 6, 1))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM arvot").fetchone()[0], ennen)

    def test_halytys_kerran_kun_pudotus_ylittaa_rajan(self):
        # 12 eraa vakiohinnalla (360 pv), sitten hinta putoaa 40 %
        alku = date(2026, 1, 1)
        n = 420
        markkina_kanta(self.mk, alku, n, sulku=lambda i: 100.0 if i < 380 else 60.0)
        mk = db.avaa(self.mk)
        con = salkku.avaa(self.sal)
        t = []
        for i in range(n):
            x = pk.aja_paiva(con, mk, alku + timedelta(days=i))
            if x["halytys"]:
                pk.kuittaa_halytys(con)   # main kuittaa vasta onnistuneen lahetyksen jalkeen
            t.append(x)
        halytykset = [x["paiva"] for x in t if x["halytys"]]
        self.assertEqual(len(halytykset), 1)
        self.assertTrue(halytykset[0] >= (alku + timedelta(days=380)).isoformat())
        self.assertGreaterEqual(t[-1]["dd"], pk.HALYTYSRAJA)

    def test_ei_halytysta_pienella_pudotuksella(self):
        con, t = self.aja(date(2026, 1, 1), 420, sulku=lambda i: 100.0 if i < 380 else 85.0)
        self.assertFalse(any(x["halytys"] for x in t))

    def test_halytys_tarvitaan_hystereesi(self):
        self.assertEqual(pk.halytys_tarvitaan(0.31, False), (True, True))
        self.assertEqual(pk.halytys_tarvitaan(0.35, True), (False, True))
        self.assertEqual(pk.halytys_tarvitaan(0.25, True), (False, True))
        self.assertEqual(pk.halytys_tarvitaan(0.10, True), (False, False))
        self.assertEqual(pk.halytys_tarvitaan(0.10, False), (False, False))

    def test_main_ajaa_ja_tulostaa_raportin(self):
        markkina_kanta(self.mk, date(2026, 1, 1), 3)
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(rajat, "PYSAYTYS", os.path.join(self.tmp.name, "ei-ole")):
            self.assertEqual(self._main(), 0)
        self.assertIn("passiivinen_kk", out.getvalue())
        self.assertIn("erat 1/12", out.getvalue())

    def _main(self):
        with mock.patch("sys.argv", ["x", "--salkku-db", self.sal, "--markkina-db", self.mk, "--ei-tg"]):
            return pk.main()

    def test_main_kill_switch_ei_aja(self):
        markkina_kanta(self.mk, date(2026, 1, 1), 3)
        kytkin = os.path.join(self.tmp.name, "PYSAYTYS")
        open(kytkin, "w").close()
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(rajat, "PYSAYTYS", kytkin), \
                mock.patch("sys.argv", ["x", "--salkku-db", self.sal, "--markkina-db", self.mk, "--ei-tg"]):
            self.assertEqual(pk.main(), 0)
        self.assertFalse(os.path.exists(self.sal))

    def test_main_lahettaa_halytyksen_notifyn_kautta(self):
        alku = date(2026, 1, 1)
        markkina_kanta(self.mk, alku, 420, sulku=lambda i: 100.0 if i < 380 else 60.0)
        env = os.path.join(self.tmp.name, "tg.env")
        with open(env, "w") as f:
            f.write("TELEGRAM_BOT_TOKEN=x\nTELEGRAM_CHAT_ID=1\n")
        # salkku on ajettu valmiiksi viimeiseen paivaan asti ennen pudotusta, main ajaa loput
        con = salkku.avaa(self.sal)
        mk = db.avaa(self.mk)
        for i in range(379):
            pk.aja_paiva(con, mk, alku + timedelta(days=i))
        con.close()
        with redirect_stdout(io.StringIO()), mock.patch.object(rajat, "PYSAYTYS", os.path.join(self.tmp.name, "ei")), \
                mock.patch("passiivinen_kk.notify.laheta") as laheta, \
                mock.patch("sys.argv", ["x", "--salkku-db", self.sal, "--markkina-db", self.mk, "--tg-env", env]):
            self.assertEqual(pk.main(), 0)
        self.assertEqual(laheta.call_count, 1)
        self.assertIn("HALYTYS", laheta.call_args[0][0])

    def _halytysvalmis(self):
        alku = date(2026, 1, 1)
        markkina_kanta(self.mk, alku, 420, sulku=lambda i: 100.0 if i < 380 else 60.0)
        env = os.path.join(self.tmp.name, "tg.env")
        with open(env, "w") as f:
            f.write("TELEGRAM_BOT_TOKEN=x\nTELEGRAM_CHAT_ID=1\n")
        con = salkku.avaa(self.sal)
        mk = db.avaa(self.mk)
        for i in range(379):
            pk.aja_paiva(con, mk, alku + timedelta(days=i))
        con.close()
        return env

    def _aja_main(self, env, onnistuu):
        with redirect_stdout(io.StringIO()), mock.patch.object(rajat, "PYSAYTYS", os.path.join(self.tmp.name, "ei")), \
                mock.patch("passiivinen_kk.notify.laheta", return_value=onnistuu) as laheta, \
                mock.patch("sys.argv", ["x", "--salkku-db", self.sal, "--markkina-db", self.mk, "--tg-env", env]):
            self.assertEqual(pk.main(), 0)
        return laheta

    def test_epaonnistunutta_halytysta_ei_kuitata_ja_se_yritetaan_uudelleen(self):
        env = self._halytysvalmis()
        self._aja_main(env, onnistuu=False)
        con = salkku.avaa(self.sal)
        self.assertNotEqual(salkku._hae(con, "halytetty"), "1")
        con.close()
        # ei uutta paivaa, mutta kuittaamaton halytys yritetaan silti uudelleen ja onnistuu
        laheta = self._aja_main(env, onnistuu=True)
        self.assertEqual(laheta.call_count, 1)
        con = salkku.avaa(self.sal)
        self.assertEqual(salkku._hae(con, "halytetty"), "1")
        con.close()
        # kuitattu halytys ei lahde enaa
        laheta = self._aja_main(env, onnistuu=True)
        self.assertEqual(laheta.call_count, 0)

    def test_onnistunut_halytys_kuitataan_ja_lahtee_kerran(self):
        env = self._halytysvalmis()
        laheta = self._aja_main(env, onnistuu=True)
        self.assertEqual(laheta.call_count, 1)
        con = salkku.avaa(self.sal)
        self.assertEqual(salkku._hae(con, "halytetty"), "1")


class IndeksidataTesti(unittest.TestCase):
    def _csv(self, d, teksti):
        polku = os.path.join(d, "i.csv")
        with open(polku, "w") as f:
            f.write(teksti)
        return polku

    def test_lue_csv(self):
        with tempfile.TemporaryDirectory() as d:
            h = indeksidata.lue(self._csv(d, "paiva,open,close\n2026-01-05,9.0,9.5\n2026-01-06,10.0,11.0\n"))
        self.assertEqual(h["open"][date(2026, 1, 6)], 10.0)
        self.assertEqual(h["close"][date(2026, 1, 5)], 9.5)

    def test_virheelliset_tiedostot_nostavat_valueerrorin(self):
        huonot = ["paiva,open\n2026-01-05,9.0\n", "paiva,open,close\n", "paiva,open,close\n2026-01-05,0,9.5\n",
                  "paiva,open,close\nei-paiva,9.0,9.5\n", "paiva,open,close\n2026-01-05,x,9.5\n"]
        with tempfile.TemporaryDirectory() as d:
            for teksti in huonot:
                with self.assertRaises(ValueError, msg=teksti):
                    indeksidata.lue(self._csv(d, teksti))

    def test_moduuli_ei_kayta_verkkoa_eika_esittaudu_selaimeksi(self):
        import ast
        with open(indeksidata.__file__) as f:
            lahde = f.read()
        tuodut = set()
        for n in ast.walk(ast.parse(lahde)):
            if isinstance(n, ast.Import):
                tuodut |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                tuodut.add(n.module.split(".")[0])
        self.assertFalse(tuodut & {"urllib", "socket", "http", "requests", "ssl"}, tuodut)
        self.assertNotIn("Mozilla", lahde)
        self.assertNotIn("User-Agent", lahde)
        self.assertFalse(hasattr(indeksidata, "hae"))


class VertailuTesti(unittest.TestCase):
    def test_aloitukset_12_kpl_kolmen_kuukauden_valein(self):
        a = pv.aloitukset()
        self.assertEqual(len(a), 12)
        self.assertEqual(a[0], date(2022, 2, 1))
        self.assertEqual(a[1], date(2022, 5, 1))
        self.assertEqual(a[-1], date(2024, 11, 1))
        self.assertLessEqual(pk.lisaa_kuukausia(a[-1], 12), pv.holdout_loppu())

    def test_holdout_on_lukittu(self):
        with self.assertRaises(ValueError):
            pv.lue_btc("/ei/ole.db", date(*rajat.HOLDOUT_ALKU))

    def test_lue_btc_rajaa_holdoutin_ulkopuolelle(self):
        with tempfile.TemporaryDirectory() as d:
            polku = os.path.join(d, "h.db")
            con = db.avaa(polku)
            alku = date(2026, 4, 10)
            db.lisaa_hinnat(con, "BTCUSDT", [(ms(alku + timedelta(days=i)), 1.0, 1.0, 1.0, 2.0, 1.0, 1.0, 1) for i in range(10)])
            con.close()
            h = pv.lue_btc(polku, pv.holdout_loppu())
        self.assertEqual(max(h["close"]), pv.holdout_loppu())

    def test_aineisto_a_vakiohinnalla_tappio_on_kulut(self):
        h = {"BTCUSDT": sarja(date(2022, 1, 1), 800), "SPX": sarja(date(2022, 1, 1), 800)}
        for painot in ({"BTCUSDT": 1.0}, {"BTCUSDT": 0.8, "SPX": 0.2}):
            m = pv.kerro(pv.aja_aineisto_a(h, painot, date(2022, 2, 1)))
            self.assertAlmostEqual(m["tuotto"], 1 / K - 1, 9)
            self.assertAlmostEqual(m["kauppoja_v"], 12 * len(painot), 0)

    def test_yhteenveto(self):
        r = [{"tuotto": x, "max_dd": 0.1 * i, "tappiokuukausia": i, "kuukausia": 4, "kauppoja_v": 12.0, "kulut_pros": 0.002,
              "krypto_myynnit_v": 0.0, "krypto_ostot_v": 12.0, "muut_myynnit_v": 0.0} for i, x in enumerate([-0.1, 0.2, 0.4])]
        y = pv.yhteenveto(r)
        self.assertEqual(y["tuotto"], (0.2, -0.1, 0.4))
        self.assertEqual(y["tappiolla_aloituksia"], 1)
        self.assertAlmostEqual(y["tappio_osuus"], 3 / 12)


if __name__ == "__main__":
    unittest.main()
