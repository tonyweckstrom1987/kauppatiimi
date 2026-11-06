import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import agentit
import backtest as bt
import k7
import rajat
from test_k6 import SYMBOLIT, kanta

HYP = "Ylimyydyt kolikot palautuvat lyhyella aikavalilla likviditeettishokin jalkeen"


def spec_dict(**yli):
    s = {"nimi": "Palautuma Testi", "hypoteesi": HYP, "odotus": "ei toimi jos sharpe < random",
         "kiinteat": {"signaali": "momentum", "suunta": "matala", "osuus": 0.7, "suodin": "ei", "suodin_toiminto": "kateinen"},
         "ruudukko": {"ikkuna": [7, 14], "k": [5, 8], "reb": [7]}}
    s.update(yli)
    return s


class SpecTesti(unittest.TestCase):
    def test_validi(self):
        s = k7.tarkista_spec(spec_dict())
        self.assertEqual(s["nimi"], "palautuma_testi")
        self.assertEqual(s["ruudukon_koko"], 4)
        self.assertEqual(len(k7.ruudukko_lista(s)), 4)

    def test_hylkaykset(self):
        huonot = [
            spec_dict(hypoteesi="lyhyt"),
            spec_dict(nimi="  "),
            spec_dict(kiinteat={"signaali": "rsi", "suunta": "matala", "osuus": 0.7, "suodin": "ei", "suodin_toiminto": "kateinen"}),
            spec_dict(kiinteat={"signaali": "momentum", "suunta": "matala", "osuus": 0.99, "suodin": "ei", "suodin_toiminto": "kateinen"}),
            spec_dict(kiinteat={"suunta": "matala", "osuus": 0.7, "suodin": "ei", "suodin_toiminto": "kateinen"}),  # signaali puuttuu
            spec_dict(ruudukko={"ikkuna": [7], "k": [5], "reb": [7], "signaali": ["momentum"]}),  # signaalia ei saa ruudukoida
            spec_dict(ruudukko={"ikkuna": [7, 14, 30], "k": [5, 6, 8, 10], "reb": [3, 7]}),  # 24 > 12
            spec_dict(ruudukko={"ikkuna": [7], "k": [5], "reb": [7], "paino": [1]}),  # tuntematon avain
            spec_dict(ruudukko={"ikkuna": [7], "k": [True], "reb": [7]}),
            spec_dict(kiinteat={"signaali": "momentum", "suunta": "matala", "osuus": 0.7, "suodin": "ei", "suodin_toiminto": "kateinen", "ikkuna": 7}),  # ikkuna myos ruudukossa
            "ei olio",
        ]
        for h in huonot:
            with self.assertRaises(k7.Speksivirhe, msg=str(h)[:80]):
                k7.tarkista_spec(h)

    def test_hash_riippuu_vain_speksista(self):
        a = k7.tarkista_spec(spec_dict())
        b = k7.tarkista_spec(spec_dict(nimi="eri nimi", hypoteesi=HYP + " lisaa"))
        c = k7.tarkista_spec(spec_dict(ruudukko={"ikkuna": [7, 14], "k": [5], "reb": [7]}))
        self.assertEqual(k7.spec_hash(a), k7.spec_hash(b))
        self.assertNotEqual(k7.spec_hash(a), k7.spec_hash(c))


def par(**yli):
    p = {"signaali": "momentum", "suunta": "korkea", "ikkuna": 30, "k": 5, "reb": 7, "osuus": 0.8, "suodin": "ei", "suodin_toiminto": "kateinen"}
    p.update(yli)
    return p


class StrategiaTesti(unittest.TestCase):
    def test_sama_kuin_k6_momentum(self):
        d = bt.Data(kanta(300), 8)
        a, b = date(2026, 1, 1), date(2026, 3, 1)
        r1 = bt.aja(bt.momentum(30, 5, 0.8, 7), d, a, b)
        r2 = bt.aja(k7.rakenna(par()), d, a, b)
        self.assertEqual(r1["arvot"], r2["arvot"])

    def test_suunta_vaihtaa_valinnan(self):
        d = bt.Data(kanta(300), 8)
        a, b = date(2026, 1, 1), date(2026, 3, 1)
        r1 = bt.aja(k7.rakenna(par()), d, a, b)
        r2 = bt.aja(k7.rakenna(par(suunta="matala")), d, a, b)
        self.assertNotEqual(r1["arvot"], r2["arvot"])

    def test_kaikki_signaalit_ajavat(self):
        d = bt.Data(kanta(300), 8)
        for sig in k7.SALLITUT["signaali"]:
            r = bt.aja(k7.rakenna(par(signaali=sig)), d, date(2026, 1, 1), date(2026, 2, 1))
            self.assertIsNotNone(r["mittarit"])

    def _suodin_data(self, nouseva):
        d = bt.Data(kanta(300), 8)
        loppu = bt._ms(date(2026, 3, 1))
        for i in range(1, 300):
            ts = loppu - i * 86400000
            if ts in d.close["BTCUSDT"]:
                d.close["BTCUSDT"][ts] = (1000.0 - i) if nouseva else (100.0 + i)   # i = paivia taaksepain: nouseva -> tuorein korkein
        return d, loppu

    def test_suodin_pois_paalta_kateinen(self):
        d, _ = self._suodin_data(nouseva=False)    # BTC nousee paivittain? i kasvaa taaksepain -> arvo laskee myohemmin
        # nouseva=False: vanhin korkein, uusin pienin -> uusin < keskiarvo -> suodin pois
        strat = k7.rakenna(par(suodin="sma50"))()
        self.assertEqual(strat(d, date(2026, 3, 1), set(SYMBOLIT)), {})

    def test_suodin_pois_paalta_btc(self):
        d, _ = self._suodin_data(nouseva=False)
        strat = k7.rakenna(par(suodin="sma50", suodin_toiminto="btc"))()
        self.assertEqual(strat(d, date(2026, 3, 1), set(SYMBOLIT)), {"BTCUSDT": 0.8})

    def test_suodin_paalla_valitsee(self):
        d, _ = self._suodin_data(nouseva=True)
        strat = k7.rakenna(par(suodin="sma50"))()
        w = strat(d, date(2026, 3, 1), set(SYMBOLIT))
        self.assertEqual(len(w), 5)

    def test_suodin_data_puuttuu_fail_closed(self):
        d = bt.Data(kanta(300), 8)
        strat = k7.rakenna(par(suodin="sma200"))()
        self.assertEqual(strat(d, date(2025, 5, 15), set(SYMBOLIT)), {})   # alle 200 pv dataa ennen paivaa

    def test_ei_katso_tulevaisuuteen(self):
        import random
        T = date(2026, 3, 1)
        con1, con2 = kanta(300, seed=5), kanta(300, seed=5)
        rnd = random.Random(9)
        for s in SYMBOLIT:
            for (ts, c) in con2.execute("SELECT ts, close FROM hinnat WHERE symbol=? AND ts>?", (s, bt._ms(T))).fetchall():
                f = rnd.uniform(0.2, 5.0)
                con2.execute("UPDATE hinnat SET close=?, open=?, high=?, low=? WHERE symbol=? AND ts=?", (c * f, c * f, c * f * 1.01, c * f * 0.99, s, ts))
        for sig, suod in (("volatiliteetti", "sma50"), ("huippuetaisyys", "ei")):
            p = par(signaali=sig, suodin=suod, suunta="matala")
            r1 = bt.aja(k7.rakenna(p), bt.Data(con1, 8), date(2026, 1, 10), T)
            r2 = bt.aja(k7.rakenna(p), bt.Data(con2, 8), date(2026, 1, 10), T)
            self.assertEqual(r1["arvot"], r2["arvot"])

    def test_satunnainen_kayttaa_samaa_suodinta(self):
        d, _ = self._suodin_data(nouseva=False)
        strat = k7.rakenna(par(suodin="sma50"), k7.satunnaisvalitsin(1))()
        self.assertEqual(strat(d, date(2026, 3, 1), set(SYMBOLIT)), {})
        d2, _ = self._suodin_data(nouseva=True)
        strat2 = k7.rakenna(par(suodin="sma50"), k7.satunnaisvalitsin(1))()
        self.assertEqual(len(strat2(d2, date(2026, 3, 1), set(SYMBOLIT))), 5)

    def test_mediaaniparametrit(self):
        s = k7.tarkista_spec(spec_dict(ruudukko={"ikkuna": [7, 14, 30], "k": [5, 8], "reb": [7]}))
        p = k7.mediaaniparametrit(s)
        self.assertEqual(p["ikkuna"], 14)
        self.assertEqual(set(p), set(k7.PAKOLLISET))


class RekisteriTesti(unittest.TestCase):
    def test_duplikaatti_ja_kiintio(self):
        rek = k7.avaa(":memory:")
        tanaan = date(2026, 10, 15)
        s = k7.tarkista_spec(spec_dict())
        hid, nimi = k7.rekisteroi(rek, s, "llm", tanaan)
        self.assertIsNotNone(hid)
        self.assertTrue(nimi.startswith("k7-"))
        hid2, _ = k7.rekisteroi(rek, s, "llm", tanaan)
        self.assertIsNone(hid2)
        self.assertEqual(k7.perhe_koko(rek), 2)     # 1 hypoteesi + K6 momentum
        self.assertEqual(k7.kiintiota_jaljella(rek, tanaan), k7.K7_VIIKKO_KIINTIO - 1)
        self.assertEqual(k7.kiintiota_jaljella(rek, tanaan + timedelta(days=7)), k7.K7_VIIKKO_KIINTIO)

    def test_kasin_ei_kuluta_kiintiota_mutta_kasvattaa_perhetta(self):
        rek = k7.avaa(":memory:")
        s = k7.tarkista_spec(spec_dict())
        k7.rekisteroi(rek, s, "kasin", date(2026, 10, 15))
        self.assertEqual(k7.kiintiota_jaljella(rek, date(2026, 10, 15)), k7.K7_VIIKKO_KIINTIO)
        self.assertEqual(k7.perhe_koko(rek), 2)


class EhdotaTesti(unittest.TestCase):
    def hae(self, sisalto):
        def f(url, avain, payload):
            self.viimeisin = payload
            return {"choices": [{"message": {"content": sisalto}, "finish_reason": "stop"}], "usage": {}}
        return f

    ENV = {"LLM_BASE_URL": "http://x/v1", "LLM_API_KEY": "k"}

    def test_validit_ja_hylatyt(self):
        rek = k7.avaa(":memory:")
        hyva, huono = spec_dict(), spec_dict(hypoteesi="x")
        vast = json.dumps({"hypoteesit": [hyva, huono]})
        v, h = k7.ehdota(rek, 3, "m", self.ENV, self.hae(vast))
        self.assertEqual(len(v), 1)
        self.assertEqual(len(h), 1)

    def test_n_rajaa_maaran(self):
        rek = k7.avaa(":memory:")
        vast = json.dumps({"hypoteesit": [spec_dict(nimi="a"), spec_dict(nimi="b"), spec_dict(nimi="c")]})
        v, h = k7.ehdota(rek, 1, "m", self.ENV, self.hae(vast))
        self.assertEqual(len(v), 1)

    def test_roskaa_nostaa(self):
        rek = k7.avaa(":memory:")
        with self.assertRaises(agentit.Vastausvirhe):
            k7.ehdota(rek, 3, "m", self.ENV, self.hae("en osaa"))
        with self.assertRaises(agentit.Vastausvirhe):
            k7.ehdota(rek, 3, "m", self.ENV, self.hae(json.dumps({"hypoteesit": "ei lista"})))

    def test_prompt_ei_sisalla_hintadataa_mutta_listaa_sallitut(self):
        rek = k7.avaa(":memory:")
        self.hae(json.dumps({"hypoteesit": []}))
        f = self.hae(json.dumps({"hypoteesit": []}))
        k7.ehdota(rek, 3, "m", self.ENV, f)
        teksti = json.dumps(self.viimeisin["messages"])
        for k in k7.SALLITUT["signaali"] + k7.SALLITUT["suodin"]:
            self.assertIn(k, teksti)
        self.assertIn("NO price data", teksti)

    def test_aiemmat_nakyvat_promptissa(self):
        rek = k7.avaa(":memory:")
        k7.rekisteroi(rek, k7.tarkista_spec(spec_dict()), "llm", date(2026, 10, 15))
        f = self.hae(json.dumps({"hypoteesit": []}))
        k7.ehdota(rek, 3, "m", self.ENV, f)
        self.assertIn("palautuma_testi", json.dumps(self.viimeisin["messages"]))


class TestausTesti(unittest.TestCase):
    WF = dict(alku=date(2025, 1, 1), loppu=date(2026, 4, 10), train_pv=150, testi_pv=60)

    def test_testaa_kokonaisuus(self):
        d = bt.Data(kanta(600), 8)
        rek = k7.avaa(":memory:")
        s = k7.tarkista_spec(spec_dict(ruudukko={"ikkuna": [14], "k": [5], "reb": [7, 14]}))
        a, t = k7.testaa(s, d, rek, "k7-x", perhe=3, satunnaisia=6, wf_kw=self.WF)
        self.assertEqual(a["kerroin"], 2 * k7.K7_PERHE_BUDJETTI)
        self.assertIn("korjaus %d" % (2 * k7.K7_PERHE_BUDJETTI), t)
        self.assertIn("TULOS:", t)
        self.assertEqual(a["satunnaisia"], 6)
        self.assertFalse(a["riittavasti_satunnaisia"])
        self.assertFalse(k7.kelpaa(a))    # 6 satunnaista ei riita -> ei voi kelvata

    def test_satunnaisia_kasvaa_perheen_mukana_ja_on_rajattu(self):
        # laskukaava: min(MAX, max(MIN, 25 x kerroin))
        f = lambda kerroin: min(k7.MAX_SATUNNAISIA, max(k7.MIN_SATUNNAISIA, 25 * kerroin))
        self.assertEqual(f(1), 200)
        self.assertEqual(f(12), 300)
        self.assertEqual(f(12 * 50), k7.MAX_SATUNNAISIA)

    def test_aja_hypoteesi_virhe_merkitaan(self):
        rek = k7.avaa(":memory:")
        s = k7.tarkista_spec(spec_dict())
        hid, nimi = k7.rekisteroi(rek, s, "kasin", date(2026, 10, 15))
        a, t = k7.aja_hypoteesi(rek, None, hid, nimi, s, 1)    # data=None -> poikkeus
        self.assertIsNone(a)
        self.assertEqual(rek.execute("SELECT tila FROM k7_hypoteesit WHERE id=?", (hid,)).fetchone()[0], "virhe")

    def test_aja_hypoteesi_merkitsee_valmiiksi(self):
        d = bt.Data(kanta(600), 8)
        rek = k7.avaa(":memory:")
        s = k7.tarkista_spec(spec_dict(ruudukko={"ikkuna": [14], "k": [5], "reb": [7]}))
        hid, nimi = k7.rekisteroi(rek, s, "kasin", date(2026, 10, 15))
        a, t = k7.aja_hypoteesi(rek, d, hid, nimi, s, 1, satunnaisia=4, wf_kw=TestausTesti.WF)
        tila, verd = rek.execute("SELECT tila, verdikti FROM k7_hypoteesit WHERE id=?", (hid,)).fetchone()
        self.assertEqual(tila, "valmis")
        self.assertEqual(verd, "EI KELPAA")

    def test_kelpaa_vaatii_kaikki(self):
        a = {"riittavasti_satunnaisia": True, "voittaa_satunnaiset": True, "voittaa_btc_tuotto": True, "voittaa_btc_sharpe": True}
        self.assertTrue(k7.kelpaa(a))
        for k in list(a):
            b = dict(a); b[k] = False
            self.assertFalse(k7.kelpaa(b), k)

    def test_ei_koske_holdoutiin(self):
        d = bt.Data(kanta(600), 8)
        rek = k7.avaa(":memory:")
        s = k7.tarkista_spec(spec_dict())
        with self.assertRaises(bt.Holdoutvirhe):
            k7.testaa(s, d, rek, "x", 1, satunnaisia=2, wf_kw=dict(alku=date(2025, 6, 1), loppu=date(2026, 6, 1), train_pv=100, testi_pv=50))
        self.assertEqual(rek.execute("SELECT COUNT(*) FROM holdout_kaytto").fetchone()[0], 0)


class KuivaTesti(unittest.TestCase):
    ENV = {"LLM_BASE_URL": "http://x/v1", "LLM_API_KEY": "k"}

    def _speksi(self, nimi):
        return spec_dict(nimi=nimi)

    def aja(self, d, vastaus, taulu=None):
        import tapahtumat
        loki = os.path.join(d, "t.jsonl")
        polku = os.path.join(d, "r.db")
        with mock.patch.object(tapahtumat, "POLKU", loki), mock.patch("llm.kysy", return_value=(vastaus, {})), \
                mock.patch("llm.lue_env", return_value=self.ENV):
            koodi = k7.main(["--ei-tg", "--kuiva", "--malli", "flash", "--rekisteri", polku])
        return koodi, loki, polku

    def test_kuiva_ei_rekisteroi_eika_kuluta_kiintiota(self):
        with tempfile.TemporaryDirectory() as d:
            vast = json.dumps({"hypoteesit": [self._speksi("a"), {"nimi": "vika"}]})
            koodi, loki, polku = self.aja(d, vast)
            self.assertEqual(koodi, 0)
            rek = k7.avaa(polku)
            self.assertEqual(rek.execute("SELECT COUNT(*) FROM k7_hypoteesit").fetchone()[0], 0)
            self.assertEqual(k7.kiintiota_jaljella(rek, datetime.now(timezone.utc).date()), k7.K7_VIIKKO_KIINTIO)
            r = [json.loads(x) for x in open(loki)][0]
            self.assertEqual((r["ajo"], r["ryhma"], r["tulos"], r["ehdotettu"], r["kelvollinen"], r["hylatty"], r["tuplia"]),
                             ("k7_kuiva", "flash", "ok", 2, 1, 1, 0))
            self.assertNotIn("momentum", open(loki).read())

    def test_kuiva_kaikki_hylatty_ei_ole_ei_tietoa(self):
        with tempfile.TemporaryDirectory() as d:
            vast = json.dumps({"hypoteesit": [{"nimi": "a"}, {"nimi": "b"}, 5]})
            koodi, loki, _ = self.aja(d, vast)
            self.assertEqual(koodi, 0)
            r = [json.loads(x) for x in open(loki)][0]
            self.assertEqual((r["tulos"], r["ehdotettu"], r["kelvollinen"], r["hylatty"]), ("kaikki_hylatty", 3, 0, 3))
            self.assertEqual(r["hylkaykset"], "hyp_lyhyt:2,ei_olio:1")

    def test_kuiva_tyhja_lista_on_ei_tietoa(self):
        with tempfile.TemporaryDirectory() as d:
            koodi, loki, _ = self.aja(d, json.dumps({"hypoteesit": []}))
            r = [json.loads(x) for x in open(loki)][0]
            self.assertEqual((r["tulos"], r["ehdotettu"]), ("ei_tietoa", 0))
            self.assertNotIn("hylkaykset", r)

    def test_kuiva_laskee_duplikaatit(self):
        with tempfile.TemporaryDirectory() as d:
            polku = os.path.join(d, "r.db")
            rek = k7.avaa(polku)
            sp = k7.tarkista_spec(self._speksi("a"))
            k7.rekisteroi(rek, sp, "kasin", datetime.now(timezone.utc).date())
            rek.close()
            koodi, loki, _ = self.aja(d, json.dumps({"hypoteesit": [self._speksi("a")]}))
            self.assertEqual(koodi, 0)
            self.assertEqual([json.loads(x) for x in open(loki)][0]["tuplia"], 1)

    def test_kuiva_llm_virhe_kirjataan_eika_kaada(self):
        import tapahtumat
        with tempfile.TemporaryDirectory() as d:
            loki = os.path.join(d, "t.jsonl")
            with mock.patch.object(tapahtumat, "POLKU", loki), mock.patch("llm.kysy", side_effect=k7.llm.LLMVirhe("HTTP 500")), \
                    mock.patch("llm.lue_env", return_value=self.ENV):
                koodi = k7.main(["--ei-tg", "--kuiva", "--rekisteri", os.path.join(d, "r.db")])
            self.assertEqual(koodi, 1)
            r = [json.loads(x) for x in open(loki)][0]
            self.assertEqual((r["tulos"], r["virhekoodi"]), ("virhe", "LLMVirhe: HTTP 500"))


class HylkaysKoodiTesti(unittest.TestCase):
    def test_jokainen_speksivirhe_saa_koodin_eika_sisaltoa(self):
        h = ["nimi puuttuu", "arvo 'momentum' ei sallittu parametrille k", "ruudukko k tyhja", "ruudukko liian suuri (99 > 12)",
             "tuntematon parametri 'x'", "outo uusi virhe"]
        k = k7.hylkaykset_koodeina(h)
        self.assertEqual(sorted(x.split(":")[0] for x in k.split(",")),
                         sorted(["nimi_puuttuu", "arvo_ei_sallittu", "ruudukko_tyhja", "ruudukko_suuri", "tuntematon_param", "muu"]))
        self.assertNotIn("momentum", k)

    def test_jokainen_tarkista_spec_viesti_osuu_omaan_koodiinsa(self):
        """Ajaa tarkista_spec:n oikeilla virheilla: jokaisen oikean viestin pitaa saada oma koodinsa (ei 'muu', ei vaaraa koodia)."""
        kiint = {"signaali": "momentum", "suunta": "korkea", "ikkuna": 30, "k": 5, "reb": 7, "osuus": 0.5, "suodin": "ei", "suodin_toiminto": "kateinen"}

        def sp(poista=(), **muut):
            kk = {a: b for a, b in kiint.items() if a not in poista}
            o = {"nimi": "a", "hypoteesi": "x" * 25, "kiinteat": kk, "ruudukko": {}}
            o.update(muut)
            return o
        tapaukset = [
            ("ei_olio", 5),
            ("nimi_puuttuu", sp(nimi="")),
            ("hyp_lyhyt", sp(hypoteesi="lyhyt")),
            ("kentat_ei_olioita", sp(ruudukko=[1])),
            ("tuntematon_param", sp(ruudukko={"foo": [1, 2]})),
            ("ruudukointi_kielletty", sp(ruudukko={"signaali": ["momentum", "volatiliteetti"]}, poista=("signaali",))),
            ("param_kahdesti", sp(ruudukko={"k": [5, 6]})),
            ("param_puuttuu", sp(poista=("k",))),
            ("arvo_ei_sallittu", sp(ruudukko={"ikkuna": [7, 999]}, poista=("ikkuna",))),
            ("ruudukko_tyhja", sp(ruudukko={"k": []}, poista=("k",))),
            ("ruudukko_suuri", sp(ruudukko={"ikkuna": [7, 14, 30], "reb": [3, 7, 14]}, poista=("ikkuna", "reb"))),
        ]
        for odotettu, o in tapaukset:
            with self.subTest(odotettu):
                with self.assertRaises(k7.Speksivirhe) as c:
                    k7.tarkista_spec(o)
                self.assertEqual(k7.hylkaykset_koodeina([str(c.exception)]), odotettu + ":1")
        # kiinteiden arvon virhe (ei ruudukossa) tunnistetaan samoin
        o = sp()
        o["kiinteat"]["k"] = 99
        with self.assertRaises(k7.Speksivirhe) as c:
            k7.tarkista_spec(o)
        self.assertEqual(k7.hylkaykset_koodeina([str(c.exception)]), "arvo_ei_sallittu:1")

    def test_tyhja(self):
        self.assertEqual(k7.hylkaykset_koodeina([]), "")
        self.assertEqual(k7.ajon_tulos([], []), "ei_tietoa")
        self.assertEqual(k7.ajon_tulos([], ["x"]), "kaikki_hylatty")
        self.assertEqual(k7.ajon_tulos([{}], ["x"]), "ok")


class MainTesti(unittest.TestCase):
    def test_kill_switch(self):
        with tempfile.TemporaryDirectory() as d:
            stop = os.path.join(d, "STOP"); open(stop, "w").close()
            with mock.patch.object(rajat, "PYSAYTYS", stop), mock.patch("llm.kysy") as kys:
                self.assertEqual(k7.main(["--ei-tg", "--rekisteri", os.path.join(d, "r.db")]), 0)
                kys.assert_not_called()

    def test_kiintio_taynna_ei_kutsu_llm(self):
        with tempfile.TemporaryDirectory() as d:
            polku = os.path.join(d, "r.db")
            rek = k7.avaa(polku)
            tanaan = datetime.now(timezone.utc).date()
            for i in range(k7.K7_VIIKKO_KIINTIO):
                s = k7.tarkista_spec(spec_dict(ruudukko={"ikkuna": [7], "k": [5], "reb": [[3, 7, 14][i]]}))
                k7.rekisteroi(rek, s, "llm", tanaan)
            rek.close()
            with mock.patch("llm.kysy") as kys:
                self.assertEqual(k7.main(["--ei-tg", "--rekisteri", polku, "--llm-env", os.path.join(d, "ei")]), 0)
                kys.assert_not_called()

    def test_huono_kasinspec_hylataan(self):
        with tempfile.TemporaryDirectory() as d:
            sp = os.path.join(d, "s.json")
            with open(sp, "w") as f:
                json.dump(spec_dict(hypoteesi="x"), f)
            self.assertEqual(k7.main(["--ei-tg", "--rekisteri", os.path.join(d, "r.db"), "--spec-tiedosto", sp]), 2)


if __name__ == "__main__":
    unittest.main()
