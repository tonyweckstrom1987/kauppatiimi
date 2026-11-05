"""Oikean rahan suojat: avaintarkistus, hatapysaytys ja verokirjanpito feikki-Binancea vastaan (ei verkkoa)."""
import csv
import hashlib
import hmac
import io
import os
import stat
import tempfile
import unittest
import urllib.parse
from unittest import mock
from contextlib import redirect_stdout
from decimal import Decimal

import avaintarkistus
import hatapysaytys
import kirjanpito
import porssi
import verokirjanpito

AVAIN = "testiavain-ei-oikea"
SALAISUUS = "testisalaisuus-ei-oikea"
HYVAT_OIKEUDET = {
    "ipRestrict": True, "createTime": 1, "enableInternalTransfer": False, "enableFutures": False,
    "enablePortfolioMarginTrading": False, "enableVanillaOptions": False, "permitsUniversalTransfer": False,
    "enableReading": True, "enableSpotAndMarginTrading": True, "enableWithdrawals": False, "enableMargin": False,
}


class FeikkiBinance:
    """Pieni Binancen REST-jaljitelma: tarkistaa allekirjoituksen ja pitaa avoimia toimeksiantoja muistissa."""

    def __init__(self, oikeudet=None, peru_virhe=False):
        self.oikeudet = dict(HYVAT_OIKEUDET if oikeudet is None else oikeudet)
        self.avoimet = []
        self.kutsut = []
        self.peru_virhe = peru_virhe
        self._seuraava = 1

    def __call__(self, metodi, url, otsikot, runko):
        osat = urllib.parse.urlsplit(url)
        kysely = osat.query
        raaka, _, allek = kysely.rpartition("&signature=")
        odotettu = hmac.new(SALAISUUS.encode(), raaka.encode(), hashlib.sha256).hexdigest()
        if otsikot.get("X-MBX-APIKEY") != AVAIN or allek != odotettu:
            return 401, None
        p = dict(urllib.parse.parse_qsl(raaka))
        self.kutsut.append((metodi, osat.path, p.get("symbol")))
        if osat.path == "/sapi/v1/account/apiRestrictions":
            return 200, self.oikeudet
        if osat.path == "/api/v3/openOrders" and metodi == "GET":
            return 200, [o for o in self.avoimet if p.get("symbol") in (None, o["symbol"])]
        if osat.path == "/api/v3/openOrders" and metodi == "DELETE":
            if self.peru_virhe:
                return 503, None
            peruttu = [o for o in self.avoimet if o["symbol"] == p["symbol"]]
            self.avoimet = [o for o in self.avoimet if o["symbol"] != p["symbol"]]
            return 200, peruttu
        if osat.path == "/api/v3/order" and metodi == "POST":
            o = {"symbol": p["symbol"], "orderId": self._seuraava, "side": p["side"]}
            self._seuraava += 1
            self.avoimet.append(o)
            return 200, o
        return 404, None


class Pohja(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.td = self._td.name
        self.pysaytys = os.path.join(self.td, "data", "PYSAYTYS")
        self.avain = os.path.join(self.td, "salaiset", "binance.key")
        os.makedirs(os.path.dirname(self.avain), mode=0o700)
        fd = os.open(self.avain, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("BINANCE_API_KEY=%s\nBINANCE_API_SECRET=%s\n" % (AVAIN, SALAISUUS))
        os.chmod(self.avain, 0o600)

    def tearDown(self):
        self._td.cleanup()

    def kaynnista(self, feikki):
        tehdas = lambda a, s, t: porssi.Asiakas(a, s, hae=feikki, kello=lambda: 1700000000000, pysaytys_tarkistus=t)
        return avaintarkistus.kaynnistys(self.avain, self.pysaytys, asiakas_tehdas=tehdas)


class TestAvaintarkistus(Pohja):
    def test_hyva_avain_kaynnistyy(self):
        a = self.kaynnista(FeikkiBinance())
        self.assertNotIn(AVAIN, repr(a))

    def test_nosto_oikeus_kieltaa(self):
        with self.assertRaises(avaintarkistus.Kieltaytyy) as cm:
            self.kaynnista(FeikkiBinance(dict(HYVAT_OIKEUDET, enableWithdrawals=True)))
        self.assertIn("enableWithdrawals", str(cm.exception))

    def test_muut_vaaralliset_oikeudet_kieltavat(self):
        for kentta, arvo in [("ipRestrict", False), ("enableInternalTransfer", True),
                             ("permitsUniversalTransfer", True), ("enableMargin", True), ("enableFutures", True)]:
            with self.subTest(kentta=kentta), self.assertRaises(avaintarkistus.Kieltaytyy):
                self.kaynnista(FeikkiBinance(dict(HYVAT_OIKEUDET, **{kentta: arvo})))

    def test_puuttuva_tai_vaaran_tyyppinen_kentta_kieltaa(self):
        o = dict(HYVAT_OIKEUDET); del o["enableWithdrawals"]
        with self.assertRaises(avaintarkistus.Kieltaytyy):
            self.kaynnista(FeikkiBinance(o))
        with self.assertRaises(avaintarkistus.Kieltaytyy):
            self.kaynnista(FeikkiBinance(dict(HYVAT_OIKEUDET, enableWithdrawals="false")))
        self.assertTrue(avaintarkistus.tarkista_rajoitukset(None))

    def test_http_virhe_kieltaa_eika_vuoda_avainta(self):
        def rikki(*_):
            return 500, None
        tehdas = lambda a, s, t: porssi.Asiakas(a, s, hae=rikki, pysaytys_tarkistus=t)
        with self.assertRaises(avaintarkistus.Kieltaytyy) as cm:
            avaintarkistus.kaynnistys(self.avain, self.pysaytys, asiakas_tehdas=tehdas)
        self.assertIn("HTTP 500", str(cm.exception))
        self.assertNotIn(AVAIN, str(cm.exception)); self.assertNotIn(SALAISUUS, str(cm.exception))

    def test_vaara_salaisuus_hylataan_feikissa(self):
        a = porssi.Asiakas(AVAIN, "vaara", hae=FeikkiBinance())
        with self.assertRaises(porssi.PorssiVirhe) as cm:
            a.avaimen_rajoitukset()
        self.assertEqual(cm.exception.tila, 401)

    def test_kuljetuksen_poikkeus_ei_vuoda(self):
        def kaatuu(metodi, url, *_):
            raise RuntimeError(url)   # url sisaltaa allekirjoituksen
        a = porssi.Asiakas(AVAIN, SALAISUUS, hae=kaatuu)
        with self.assertRaises(porssi.PorssiVirhe) as cm:
            a.avaimen_rajoitukset()
        self.assertNotIn("signature", str(cm.exception))
        self.assertIsNone(cm.exception.__cause__)

    def test_tiedoston_oikeudet(self):
        for tila in (0o644, 0o640, 0o400, 0o700):
            os.chmod(self.avain, tila)
            with self.subTest(tila=oct(tila)), self.assertRaises(avaintarkistus.Kieltaytyy):
                avaintarkistus.lue_avain(self.avain)
        os.chmod(self.avain, 0o600)
        self.assertEqual(avaintarkistus.lue_avain(self.avain), (AVAIN, SALAISUUS))

    def test_symlinkki_ja_git_tyopuu_kieltavat(self):
        linkki = os.path.join(self.td, "linkki.key")
        os.symlink(self.avain, linkki)
        with self.assertRaises(avaintarkistus.Kieltaytyy):
            avaintarkistus.lue_avain(linkki)
        repo = os.path.join(self.td, "repo"); os.makedirs(os.path.join(repo, ".git"))
        p = os.path.join(repo, "binance.key")
        with open(p, "w") as f:
            f.write("BINANCE_API_KEY=a\nBINANCE_API_SECRET=b\n")
        os.chmod(p, 0o600)
        with self.assertRaises(avaintarkistus.Kieltaytyy) as cm:
            avaintarkistus.lue_avain(p)
        self.assertIn("git", str(cm.exception))

    def test_puuttuva_tiedosto_ja_puuttuva_salaisuus(self):
        with self.assertRaises(avaintarkistus.Kieltaytyy):
            avaintarkistus.lue_avain(os.path.join(self.td, "ei-ole"))
        with open(self.avain, "w") as f:
            f.write("BINANCE_API_KEY=a\n")
        with self.assertRaises(avaintarkistus.Kieltaytyy):
            avaintarkistus.lue_avain(self.avain)

    def test_pysaytys_estaa_kaynnistyksen(self):
        hatapysaytys.kirjoita_tiedosto("testi", self.pysaytys)
        with self.assertRaises(avaintarkistus.Kieltaytyy):
            self.kaynnista(FeikkiBinance())

    def test_cli_ei_tulosta_avainta(self):
        for oikeudet, odotus in ((HYVAT_OIKEUDET, 0), (dict(HYVAT_OIKEUDET, enableWithdrawals=True), 2)):
            ulos = io.StringIO()
            with mock.patch.object(porssi, "_urllib_hae", FeikkiBinance(oikeudet)), redirect_stdout(ulos):
                rc = avaintarkistus.main(["--avain", self.avain, "--pysaytys", self.pysaytys])
            self.assertEqual(rc, odotus)
            self.assertNotIn(AVAIN, ulos.getvalue()); self.assertNotIn(SALAISUUS, ulos.getvalue())


class TestHatapysaytys(Pohja):
    def test_pysaytys_pysayttaa_kaupankaynnin_ja_peruu_avoimet(self):
        feikki = FeikkiBinance()
        a = self.kaynnista(feikki)
        a.uusi_toimeksianto("BTCUSDT", "BUY", "LIMIT", quantity="0.001", price="10000", timeInForce="GTC")
        a.uusi_toimeksianto("ETHUSDT", "SELL", "LIMIT", quantity="0.01", price="9000", timeInForce="GTC")
        self.assertEqual(len(feikki.avoimet), 2)
        r = hatapysaytys.pysayta("testi", a, self.pysaytys)
        self.assertEqual(r["peruttu"], ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(r["jaljella"], 0)
        self.assertEqual(r["virheet"], [])
        self.assertEqual(feikki.avoimet, [])
        posteja = sum(1 for k in feikki.kutsut if k[0] == "POST")
        with self.assertRaises(hatapysaytys.Pysaytetty):
            a.uusi_toimeksianto("BTCUSDT", "BUY", "MARKET", quantity="0.001")
        self.assertEqual(sum(1 for k in feikki.kutsut if k[0] == "POST"), posteja)   # mitaan ei lahetetty

    def test_tiedosto_kasin_pysayttaa_heti(self):
        feikki = FeikkiBinance()
        a = self.kaynnista(feikki)
        os.makedirs(os.path.dirname(self.pysaytys), exist_ok=True)
        open(self.pysaytys, "w").close()   # Tony: touch PYSAYTYS
        with self.assertRaises(hatapysaytys.Pysaytetty):
            a.uusi_toimeksianto("BTCUSDT", "BUY", "MARKET", quantity="0.001")
        self.assertFalse([k for k in feikki.kutsut if k[0] == "POST"])

    def test_toimeksianto_ilman_tarkistinta_kielletty(self):
        a = porssi.Asiakas(AVAIN, SALAISUUS, hae=FeikkiBinance())
        with self.assertRaises(porssi.PorssiVirhe):
            a.uusi_toimeksianto("BTCUSDT", "BUY", "MARKET", quantity="0.001")

    def test_peruutusvirhe_raportoidaan_tiedosto_silti_paalla(self):
        feikki = FeikkiBinance(peru_virhe=True)
        a = self.kaynnista(feikki)
        a.uusi_toimeksianto("BTCUSDT", "BUY", "LIMIT", quantity="0.001", price="1", timeInForce="GTC")
        r = hatapysaytys.pysayta("testi", a, self.pysaytys)
        self.assertTrue(hatapysaytys.pysaytetty(self.pysaytys))
        self.assertEqual(r["jaljella"], 1)
        self.assertEqual(r["virheet"], ["peru BTCUSDT: PorssiVirhe"])

    def test_telegram_vain_tonyn_chat_id(self):
        tony = 123456
        def viesti(teksti, chat=tony, lahettaja=tony):
            return {"chat": {"id": chat}, "from": {"id": lahettaja}, "text": teksti}
        self.assertTrue(hatapysaytys.on_seis_komento(viesti("seis kauppa"), tony))
        self.assertTrue(hatapysaytys.on_seis_komento(viesti("  Seis   KAUPPA "), str(tony)))
        for v in (viesti("seis kauppa", chat=999), viesti("seis kauppa", lahettaja=999),
                  viesti("seis kauppa nyt"), viesti("seis"), viesti(None), {"text": "seis kauppa"}, None):
            self.assertFalse(hatapysaytys.on_seis_komento(v, tony))
        self.assertFalse(hatapysaytys.on_seis_komento(viesti("seis kauppa"), None))
        self.assertFalse(hatapysaytys.on_seis_komento(viesti("seis kauppa", chat=True, lahettaja=True), True))

    def test_telegram_pysaytys_peruu_ja_vieras_ei(self):
        feikki = FeikkiBinance()
        a = self.kaynnista(feikki)
        a.uusi_toimeksianto("BTCUSDT", "BUY", "LIMIT", quantity="0.001", price="1", timeInForce="GTC")
        vieras = {"chat": {"id": 5}, "from": {"id": 5}, "text": "seis kauppa"}
        self.assertIsNone(hatapysaytys.kasittele_telegram(vieras, 42, a, self.pysaytys))
        self.assertFalse(hatapysaytys.pysaytetty(self.pysaytys))
        tony = {"chat": {"id": 42}, "from": {"id": 42}, "text": "seis kauppa"}
        r = hatapysaytys.kasittele_telegram(tony, 42, a, self.pysaytys)
        self.assertEqual(r["jaljella"], 0)
        with open(self.pysaytys) as f:
            self.assertIn("telegram", f.read())

    def test_cli_kirjoittaa_tiedoston(self):
        with redirect_stdout(io.StringIO()):
            rc = hatapysaytys.main(["--syy", "testi; rm -rf /", "--polku", self.pysaytys])
        self.assertEqual(rc, 0)
        with open(self.pysaytys) as f:
            sisalto = f.read()
        self.assertNotIn(";", sisalto); self.assertNotIn("/", sisalto)

    def aja_cli(self, *argumentit, feikki=None):
        """Ajaa CLI:n niin, etta porssi.Asiakas kayttaa feikki-Binancea (ei verkkoa)."""
        alkup = porssi.Asiakas
        tehdas = lambda k, s, **kw: alkup(k, s, hae=feikki or FeikkiBinance(), **kw)
        ulos = io.StringIO()
        with mock.patch.object(porssi, "Asiakas", side_effect=tehdas), redirect_stdout(ulos):
            rc = hatapysaytys.main(["--polku", self.pysaytys] + list(argumentit))
        return rc, ulos.getvalue()

    def test_cli_hylatty_avain_palauttaa_virhekoodin(self):
        os.chmod(self.avain, 0o644)   # avaintarkistus hylkaa (Kieltaytyy)
        rc, ulos = self.aja_cli("--avain", self.avain)
        self.assertEqual(rc, hatapysaytys.AVAIN_HYLATTY)
        self.assertNotEqual(rc, 0)
        self.assertTrue(hatapysaytys.pysaytetty(self.pysaytys))   # tiedosto kirjoitetaan silti
        self.assertIn("EI peruttu", ulos)

    def test_cli_puuttuva_avaintiedosto_palauttaa_virhekoodin(self):
        rc, ulos = self.aja_cli("--avain", os.path.join(self.td, "ei-ole.key"))
        self.assertEqual(rc, hatapysaytys.AVAIN_HYLATTY)
        self.assertTrue(hatapysaytys.pysaytetty(self.pysaytys))

    def test_cli_odottamaton_virhe_avaimessa_ei_vuoda(self):
        with mock.patch("avaintarkistus.lue_avain", side_effect=RuntimeError(SALAISUUS)):
            rc, ulos = self.aja_cli("--avain", self.avain)
        self.assertEqual(rc, hatapysaytys.AVAIN_HYLATTY)
        self.assertIn("RuntimeError", ulos)
        self.assertNotIn(SALAISUUS, ulos)

    def test_cli_onnistunut_peruutus_nolla(self):
        feikki = FeikkiBinance()
        a = self.kaynnista(feikki)
        a.uusi_toimeksianto("BTCUSDT", "BUY", "LIMIT", quantity="0.001", price="1", timeInForce="GTC")
        rc, ulos = self.aja_cli("--avain", self.avain, feikki=feikki)
        self.assertEqual(rc, hatapysaytys.OK)
        self.assertEqual(feikki.avoimet, [])
        self.assertNotIn(AVAIN, ulos); self.assertNotIn(SALAISUUS, ulos)

    def test_cli_peruutusvirhe_palauttaa_virhekoodin(self):
        feikki = FeikkiBinance(peru_virhe=True)
        a = self.kaynnista(feikki)
        a.uusi_toimeksianto("BTCUSDT", "BUY", "LIMIT", quantity="0.001", price="1", timeInForce="GTC")
        rc, _ = self.aja_cli("--avain", self.avain, feikki=feikki)
        self.assertEqual(rc, hatapysaytys.PERUUTUS_KESKEN)

    def test_cli_tiedoston_kirjoitus_epaonnistuu(self):
        este = os.path.join(self.td, "tiedosto")
        open(este, "w").close()   # hakemiston paikalla on tiedosto -> makedirs epaonnistuu
        ulos = io.StringIO()
        with redirect_stdout(ulos):
            rc = hatapysaytys.main(["--polku", os.path.join(este, "PYSAYTYS")])
        self.assertEqual(rc, hatapysaytys.TIEDOSTO_EPAONNISTUI)
        self.assertIn("EI ole paalla", ulos.getvalue())

    def test_cli_asiakas_ei_voi_lahettaa_toimeksiantoa(self):
        luodut = []
        alkup = porssi.Asiakas
        def tehdas(k, s, **kw):
            luodut.append(alkup(k, s, hae=FeikkiBinance(), **kw))
            return luodut[-1]
        with mock.patch.object(porssi, "Asiakas", side_effect=tehdas), redirect_stdout(io.StringIO()):
            hatapysaytys.main(["--polku", self.pysaytys, "--avain", self.avain])
        os.remove(self.pysaytys)   # vaikka tiedosto poistettaisiin, hatapysaytyksen asiakas ei laheta
        with self.assertRaises(hatapysaytys.Pysaytetty):
            luodut[0].uusi_toimeksianto("BTCUSDT", "BUY", "MARKET", quantity="0.001")


def kauppa(i, symbol="BTCUSDT", ostaja=True, qty="0.01", price="60000", komissio="0.6", komissio_val="USDT",
           aika=1767225600000):
    return {"symbol": symbol, "id": i, "orderId": 1000 + i, "price": price, "qty": qty,
            "quoteQty": str(Decimal(qty) * Decimal(price)), "commission": komissio,
            "commissionAsset": komissio_val, "time": aika + i, "isBuyer": ostaja}


KURSSIT = {"USDT": Decimal("0.92"), "USDC": Decimal("0.92"), "BNB": Decimal("550"), "EUR": Decimal("1")}


def eur_hinta(valuutta, _aika):
    return KURSSIT[valuutta]


class TestVerokirjanpito(Pohja):
    def polku(self):
        return os.path.join(self.td, "yksityinen", "verokirjanpito.csv")

    def test_jokainen_kauppa_rivi_jossa_kaikki_kentat(self):
        vk = verokirjanpito.Verokirja(self.polku())
        n = vk.kirjaa_binance([kauppa(1), kauppa(2, ostaja=False, price="61000", komissio="0.001",
                                                 komissio_val="BNB")], eur_hinta)
        self.assertEqual(n, 2)
        with open(self.polku(), newline="") as f:
            rivit = list(csv.DictReader(f))
        self.assertEqual(len(rivit), 2)
        r = rivit[0]
        self.assertEqual(r["aika_utc"], "2026-01-01T00:00:00.001Z")
        self.assertEqual((r["pari"], r["puoli"], r["maara"], r["hinta"]), ("BTCUSDT", "BUY", "0.01", "60000"))
        self.assertEqual((r["kulu"], r["kulu_valuutta"]), ("0.6", "USDT"))
        self.assertEqual(r["arvo_eur"], "552.00")        # 600 USDT * 0.92
        self.assertEqual(r["kulu_eur"], "0.5520")
        self.assertEqual(rivit[1]["kulu_eur"], "0.5500")  # 0.001 BNB * 550
        self.assertEqual(rivit[1]["puoli"], "SELL")

    def test_oikeudet_600_ja_700(self):
        vk = verokirjanpito.Verokirja(self.polku())
        vk.kirjaa_binance([kauppa(1)], eur_hinta)
        self.assertEqual(stat.S_IMODE(os.stat(self.polku()).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.polku())).st_mode), 0o700)
        os.chmod(self.polku(), 0o644)
        with self.assertRaises(verokirjanpito.Kirjausvirhe):
            vk.kirjaa_binance([kauppa(2)], eur_hinta)

    def test_ei_git_tyopuuhun(self):
        repo = os.path.join(self.td, "repo"); os.makedirs(os.path.join(repo, ".git"))
        with self.assertRaises(verokirjanpito.Kirjausvirhe):
            verokirjanpito.Verokirja(os.path.join(repo, "data", "vero.csv"))

    def test_sama_kauppa_vain_kerran(self):
        vk = verokirjanpito.Verokirja(self.polku())
        self.assertEqual(vk.kirjaa_binance([kauppa(1)], eur_hinta), 1)
        self.assertEqual(vk.kirjaa_binance([kauppa(1), kauppa(2)], eur_hinta), 1)
        self.assertEqual(len(vk.kirjatut()), 2)

    def test_virheellinen_kauppa_ei_kirjaa_mitaan(self):
        vk = verokirjanpito.Verokirja(self.polku())
        huonot = [dict(kauppa(2), qty="-1"), dict(kauppa(2), symbol="BTC,USDT"), dict(kauppa(2), isBuyer=None),
                  dict(kauppa(2), symbol="BTCXYZ"), dict(kauppa(2), time="eilen"), dict(kauppa(2), price="nan")]
        for h in huonot:
            with self.subTest(h=h), self.assertRaises(verokirjanpito.Kirjausvirhe):
                vk.kirjaa_binance([kauppa(1), h], eur_hinta)
        self.assertFalse(os.path.exists(self.polku()))

    def test_fifo_euroina(self):
        vk = verokirjanpito.Verokirja(self.polku())
        vk.kirjaa_binance([kauppa(1, qty="0.02", price="50000", komissio="1"),
                           kauppa(2, symbol="BTCUSDC", ostaja=False, qty="0.01", price="60000",
                                  komissio="0.6", komissio_val="USDC")], eur_hinta)
        tulos = kirjanpito.fifo(vk.fifo_syote())
        self.assertEqual(len(tulos), 1)
        paiva, kohde, maara, tulo, meno, voitto = tulos[0]
        self.assertEqual(kohde, "BTC")
        # myynti 600*0.92 - 0.6*0.92 = 551.448; hankinta puolet (1000*0.92 + 0.92) = 460.46
        self.assertAlmostEqual(tulo, 551.448, places=6)
        self.assertAlmostEqual(meno, 460.46, places=6)
        self.assertAlmostEqual(voitto, 90.988, places=6)

    def test_fifo_pieni_kauppa_ei_sentin_pyoristysta(self):
        vk = verokirjanpito.Verokirja(self.polku())
        vk.kirjaa_binance([kauppa(1, qty="0.00001", price="60000", komissio="0")], eur_hinta)
        # arvo 0.6 USDT * 0.92 = 0.552 EUR; arvo_eur-sarakkeessa 0.55, mutta hinta lasketaan pyoristamattomasta
        hinta_eur = vk.fifo_syote()[0][4]
        self.assertAlmostEqual(hinta_eur, 55200.0, places=6)

    def test_oletuspolku_luetaan_kutsuhetkella(self):
        p = os.path.join(self.td, "muu", "vero.csv")
        with mock.patch.dict(os.environ, {"KAUPPA_VEROKIRJA": p}):
            self.assertEqual(verokirjanpito.Verokirja().polku, p)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KAUPPA_VEROKIRJA", None)
            self.assertEqual(verokirjanpito.oletuspolku(), verokirjanpito.OLETUSPOLKU)


if __name__ == "__main__":
    unittest.main()
