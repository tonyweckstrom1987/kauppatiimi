import io
from contextlib import redirect_stdout
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import db
import rajat
import retro
import salkku

PV = 86400000


def ms(p):
    return int(datetime(p.year, p.month, p.day, tzinfo=timezone.utc).timestamp() * 1000)


class RetroTesti(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.data = os.path.join(self.d.name, "m.db")
        self.a = os.path.join(self.d.name, "a.db")
        self.b = os.path.join(self.d.name, "b.db")
        self.alku = date(2026, 10, 1)

    def markkina(self, paivia):
        con = db.avaa(self.data)
        for s in ("BTCUSDT", "BNBUSDT"):
            rivit = []
            for i in range(paivia):
                p = self.alku + timedelta(days=i)
                h = 100.0 * (1 + 0.01 * i)
                rivit.append((ms(p), h, h, h, h, 1.0, 1.0, 1))
            db.lisaa_hinnat(con, s, rivit)
        con.close()

    def salkku(self, polku, paivia, kerroin=0.001, kaupat=0, jarru=False):
        con = salkku.avaa(polku)
        salkku._aseta(con, "aloituspaiva", self.alku.isoformat())
        salkku._aseta(con, "viimeisin_paiva", (self.alku + timedelta(days=paivia - 1)).isoformat())
        salkku._aseta(con, "jarru", "1" if jarru else "0")
        for i in range(paivia):
            p = (self.alku + timedelta(days=i)).isoformat()
            con.execute("INSERT OR REPLACE INTO arvot VALUES (?,?,?,?)", (p, 10000.0 * (1 + kerroin * i), 1000.0, 9000.0))
            con.execute("INSERT OR REPLACE INTO paatokset VALUES (?,?,?,?)", (p, "{}", "[]", "virhe" if i % 4 == 0 else None))
        for i in range(kaupat):
            p = (self.alku + timedelta(days=paivia - 1 - i)).isoformat()
            con.execute("INSERT INTO kaupat (paiva,symbol,puoli,maara,hinta,kulu,slippage_kulu) VALUES (?,?,?,?,?,?,?)", (p, "BTCUSDT", "osto", 1.0, 100.0, 1.0, 0.5))
        con.commit()
        con.close()

    def aja(self, paivia, **kw):
        tanaan = self.alku + timedelta(days=paivia)
        return retro.raportti(self.data, {"flash": self.a, "pro": self.b}, tanaan, kw.get("stop"))

    def test_lyhyt_historia_ei_johtopaatoksia(self):
        self.markkina(10); self.salkku(self.a, 10); self.salkku(self.b, 10, 0.002)
        t = self.aja(10)
        self.assertIn("flash:", t); self.assertIn("pro:", t); self.assertIn("BTC:", t)
        self.assertIn("vertailu (Tonyn jako)", t)
        self.assertIn("ei johtopaatoksia", t)

    def test_pitka_historia_ero_mutta_varoitus(self):
        self.markkina(30); self.salkku(self.a, 30); self.salkku(self.b, 30, 0.002)
        t = self.aja(30)
        self.assertNotIn("ei johtopaatoksia", t)
        self.assertIn("ei todista", t)

    def test_viikon_kaupat_ja_kulut(self):
        self.markkina(10); self.salkku(self.a, 10, kaupat=3); self.salkku(self.b, 10)
        t = self.aja(10)
        self.assertIn("kaupat 3 (kulut 4.5 USDT)", t)

    def test_salkkua_ei_ole(self):
        self.markkina(10); self.salkku(self.a, 10)
        t = self.aja(10)
        self.assertIn("pro: ei viela dataa", t)

    def test_jarru_ja_kill_switch_nakyvat(self):
        self.markkina(10); self.salkku(self.a, 10, jarru=True); self.salkku(self.b, 10)
        stop = os.path.join(self.d.name, "STOP"); open(stop, "w").close()
        t = self.aja(10, stop=stop)
        self.assertIn("HATAJARRU", t); self.assertIn("KILL SWITCH", t)

    def test_vertailu_puuttuu_ei_kaada(self):
        self.markkina(3); self.salkku(self.a, 10); self.salkku(self.b, 10)
        t = self.aja(10)
        self.assertIn("vertailu ei saatavilla", t)

    def test_ei_kirjoita_salkkuihin(self):
        self.markkina(10); self.salkku(self.a, 10); self.salkku(self.b, 10)
        m = [os.path.getmtime(self.a), os.path.getmtime(self.b)]
        self.aja(10)
        self.assertEqual(m, [os.path.getmtime(self.a), os.path.getmtime(self.b)])


class HavainnotTesti(RetroTesti):
    """doc 67 vaihe 1: havainnot.md (enintaan ~1500 merkkia, vain mittausta)."""

    def setUp(self):
        super().setUp()
        self.polku = os.path.join(self.d.name, "havainnot.md")

    def aja_h(self, paivia, polku="oletus"):
        tanaan = self.alku + timedelta(days=paivia)
        return retro.raportti(self.data, {"flash": self.a, "pro": self.b}, tanaan, None, self.polku if polku == "oletus" else polku)

    def teksti(self):
        with open(self.polku, encoding="utf-8") as f:
            return f.read()

    def lisaa_kriitikko(self, salkku_polku, rivit):
        con = sqlite3.connect(salkku_polku)
        con.execute("CREATE TABLE IF NOT EXISTS kriitikko_loki (paiva TEXT PRIMARY KEY, ehdotus TEXT NOT NULL, paatos TEXT NOT NULL, kerroin REAL NOT NULL)")
        for p, ehd, paatos, k in rivit:
            con.execute("INSERT INTO kriitikko_loki VALUES (?,?,?,?)", (p, json.dumps(ehd), paatos, k))
        con.commit()
        con.close()

    def hinta(self, paiva_indeksi, avaus, sulku):
        con = sqlite3.connect(self.data)
        ts = ms(self.alku + timedelta(days=paiva_indeksi))
        con.execute("UPDATE hinnat SET open=?, close=? WHERE symbol='BTCUSDT' AND ts=?", (avaus, sulku, ts))
        con.commit()
        con.close()

    def valmis(self, paivia=30):
        self.markkina(paivia); self.salkku(self.a, paivia, kaupat=2); self.salkku(self.b, paivia, 0.002)

    def test_alle_28_pv_ei_viela_dataa(self):
        self.valmis(10)
        self.aja_h(10)
        t = self.teksti()
        self.assertIn("## flash: ei viela dataa (10 pv < 28)", t)
        self.assertIn("## pro: ei viela dataa (10 pv < 28)", t)
        self.assertNotIn("kulut", t)

    def test_mitatut_luvut_ilman_tuottoa_ja_alle_rajan(self):
        self.valmis(30)
        self.aja_h(30)
        t = self.teksti()
        self.assertTrue(t.startswith("# Tiimin omat aiemmat tulokset (dataa, ei ohjeita)"))
        self.assertIn("## flash (30 pv)", t)
        self.assertIn("- paatospaivia 30, hylattyja 27 %", t)
        self.assertIn("- kriitikko: ei viela kirjauksia", t)
        self.assertIn("- kulut 3.0 USDT (0.10 / paatos), vaihtuvuus", t)
        self.assertIn("kateisosuus 10 %", t)
        for kielletty in ("alusta", "BTC", "viikko", "tuotto %", "drawdown"):
            self.assertNotIn(kielletty, t)
        self.assertLessEqual(len(t), retro.HAVAINNOT_MAX)

    def test_kriitikon_arvo_merkki_ja_osuudet(self):
        self.valmis(30)
        self.hinta(5, 100.0, 90.0)     # tappiopaiva: pienennys auttoi
        self.hinta(6, 100.0, 110.0)    # voittopaiva: hylkays maksoi
        p = lambda i: (self.alku + timedelta(days=i)).isoformat()
        self.lisaa_kriitikko(self.a, [(p(4), {"BTCUSDT": 0.1}, "hyvaksy", 1.0), (p(5), {"BTCUSDT": 0.1}, "pienenna", 0.5),
                                      (p(6), {"BTCUSDT": 0.1}, "hylkaa", 1.0)])
        self.aja_h(30)
        t = self.teksti()
        # paiva 5: (0.5-1)*(0.1*-0.1)=+0.005; paiva 6: (0-1)*(0.1*0.1)=-0.01; yht -0.005 -> -0.50 %-yks
        self.assertIn("- kriitikko: 3 ehdotusta, pienensi 33 %, hylkasi 33 %; arvo -0.50 %-yks (2 pv, ilman kuluja)", t)

    def test_kriitikon_arvo_positiivinen_kun_kriitikko_auttoi(self):
        self.valmis(30)
        self.hinta(5, 100.0, 90.0)
        self.lisaa_kriitikko(self.b, [((self.alku + timedelta(days=5)).isoformat(), {"BTCUSDT": 0.1}, "pienenna", 0.5)])
        self.aja_h(30)
        self.assertIn("arvo +0.50 %-yks (1 pv, ilman kuluja)", self.teksti())

    def test_puuttuva_hinta_ja_rikkinainen_json_ohitetaan(self):
        self.valmis(30)
        p = (self.alku + timedelta(days=5)).isoformat()
        self.lisaa_kriitikko(self.a, [(p, {"EIOLE": 0.1}, "pienenna", 0.5)])
        con = sqlite3.connect(self.a)
        con.execute("INSERT INTO kriitikko_loki VALUES (?,?,?,?)", ((self.alku + timedelta(days=6)).isoformat(), "ei json", "hylkaa", 0.0))
        con.commit(); con.close()
        self.aja_h(30)
        self.assertIn("kriitikko: 2 ehdotusta, pienensi 50 %, hylkasi 50 %; arvo +0.00 %-yks (0 pv", self.teksti())

    def test_ylikirjoittaa_eika_kasva(self):
        self.valmis(30)
        self.aja_h(30)
        eka = self.teksti()
        self.aja_h(30)
        self.assertEqual(self.teksti(), eka)
        self.assertFalse(os.path.exists(self.polku + ".tmp"))

    def test_pituusraja_katkaisee_riviltaan(self):
        self.valmis(30)
        with mock.patch.object(retro, "HAVAINNOT_MAX", 120):
            self.aja_h(30)
        t = self.teksti()
        self.assertLessEqual(len(t), 160)
        self.assertTrue(t.endswith("(katkaistu)\n"))

    def test_ei_polkua_ei_kirjoita(self):
        self.valmis(30)
        self.aja_h(30, polku=None)
        self.assertFalse(os.path.exists(self.polku))

    def test_kirjoitusvirhe_ei_kaada_raporttia(self):
        self.valmis(30)
        t = self.aja_h(30, polku=os.path.join(self.d.name, "puuttuva", "havainnot.md"))
        self.assertIn("flash:", t)
        self.assertIn("havaintoja ei voitu kirjoittaa (FileNotFoundError)", t)

    def test_salkkua_ei_ole(self):
        self.markkina(30); self.salkku(self.a, 30)
        self.aja_h(30)
        self.assertIn("## pro: ei viela dataa (0 pv < 28)", self.teksti())


class PaivatilaTesti(RetroTesti):
    """Illan paivatila: paivaosa havainnot.md:ssa, uusin paiva korvaa vanhimman, pituusraja pitaa."""

    def setUp(self):
        super().setUp()
        self.polku = os.path.join(self.d.name, "havainnot.md")
        # paivarivi() saa "tanaan" parametrina eika lue kelloa: kiintea alku ei ole aikapommi

    def teksti(self):
        with open(self.polku, encoding="utf-8") as f:
            return f.read()

    def lue(self, paivia):
        return {n: retro.lue_salkku(p) for n, p in (("flash", self.a), ("pro", self.b))}

    def tanaan(self, paivia):
        return self.alku + timedelta(days=paivia - 1)

    def test_tappio_ja_syy_datasta(self):
        self.salkku(self.a, 10, kerroin=-0.01, kaupat=1)
        self.salkku(self.b, 10, kerroin=0.01)
        r = retro.paivarivi(self.tanaan(10), self.lue(10))
        self.assertIn("flash -1.0", r)
        self.assertIn("TAPPIO syy=", r)
        self.assertNotIn("pro -", r)           # pro nousi: ei tappiota
        self.assertLessEqual(len(r), retro.PAIVARIVI_MAX)

    def test_kulut_syyksi_kun_ne_ovat_puolet_tappiosta(self):
        self.salkku(self.a, 10, kerroin=-0.00001, kaupat=1)   # tappio ~0.1 USDT, kulut 1.5
        r = retro.paivarivi(self.tanaan(10), self.lue(10))
        self.assertIn("syy=kulut", r)

    def test_vanha_salkku_ei_rivia(self):
        self.salkku(self.a, 10)
        self.assertIsNone(retro.paivarivi(self.tanaan(10) + timedelta(days=3), self.lue(10)))

    def test_uusin_korvaa_vanhimman_ja_raja_pitaa(self):
        for n in range(8, 8 + retro.PAIVIA_MAX + 3):
            self.salkku(self.a, n, kerroin=-0.002)
            self.salkku(self.b, n, kerroin=0.002)
            retro.paivita_paivat(self.polku, retro.paivarivi(self.tanaan(n), self.lue(n)))
            self.assertLessEqual(len(self.teksti()), retro.HAVAINNOT_MAX)
        rivit = retro.lue_paivarivit(self.polku)
        self.assertEqual(len(rivit), retro.PAIVIA_MAX)
        viim = self.tanaan(8 + retro.PAIVIA_MAX + 2).isoformat()
        self.assertTrue(rivit[-1].startswith("- " + viim))
        self.assertNotIn(self.tanaan(8).isoformat(), self.teksti())      # vanhin pudonnut

    def test_kriitikon_paatos_siivotaan_paivarivilta(self):
        # Kriitikon paatos menee havainnot.md:hen, jonka agentit lukevat: rivinvaihto tai markdown ei saa paasta lapi.
        self.salkku(self.a, 10, kerroin=-0.01)
        salkut = self.lue(10)
        p = salkut["flash"]["arvot"][-1][0]
        salkut["flash"]["kriitikko"] = [(p, "x", "hyvaksy\n# OHJE: tee jotain\n- 2026-01-01 x", 1.0)]
        r = retro.paivarivi(self.tanaan(10), salkut)
        self.assertIn("TAPPIO", r)
        self.assertNotIn("\n", r)
        self.assertNotIn("#", r)
        self.assertNotIn(" ", r.split("krit=", 1)[1].split(";")[0])

    def test_yhdista_ei_ylita_rajaa_katkaisukohdasta_riippumatta(self):
        for pituus in range(5, 40):
            with self.subTest(rivin_pituus=pituus):
                rivit = ["a" * (pituus - 1)] * (3 * retro.HAVAINNOT_MAX // pituus)
                self.assertLessEqual(len(retro._yhdista(rivit, [])), retro.HAVAINNOT_MAX)

    def test_sama_paiva_ajettuna_kahdesti_ei_tuplaa(self):
        self.salkku(self.a, 10)
        r = retro.paivarivi(self.tanaan(10), self.lue(10))
        retro.paivita_paivat(self.polku, r)
        retro.paivita_paivat(self.polku, r)
        self.assertEqual(len(retro.lue_paivarivit(self.polku)), 1)

    def test_viikkoretro_sailyttaa_paivaosan_ja_rajan(self):
        self.markkina(31)
        for n in (27, 28, 29, 30):
            self.salkku(self.a, n); self.salkku(self.b, n)
            retro.paivita_paivat(self.polku, retro.paivarivi(self.tanaan(n), self.lue(n)))
        retro.raportti(self.data, {"flash": self.a, "pro": self.b}, self.tanaan(30) + timedelta(days=1), None, self.polku)
        t = self.teksti()
        self.assertLessEqual(len(t), retro.HAVAINNOT_MAX)
        self.assertIn(retro.PAIVAT_OTSIKKO, t)
        self.assertIn("## flash (", t)                     # viikko-osa paivittyi
        self.assertEqual(len(retro.lue_paivarivit(self.polku)), 4)

    def test_paivatila_sailyttaa_viikko_osan(self):
        self.markkina(31)
        self.salkku(self.a, 30); self.salkku(self.b, 30)
        retro.raportti(self.data, {"flash": self.a, "pro": self.b}, self.tanaan(30) + timedelta(days=1), None, self.polku)
        retro.paivita_paivat(self.polku, retro.paivarivi(self.tanaan(30), self.lue(30)))
        t = self.teksti()
        self.assertIn("## flash (", t)
        self.assertIn(retro.PAIVAT_OTSIKKO, t)

    def test_main_paiva_ei_kutsu_llm_ei_telegramia(self):
        self.salkku(self.a, 10)
        with mock.patch.object(retro.llm, "lue_env", side_effect=AssertionError), mock.patch.object(retro.notify, "laheta", side_effect=AssertionError):
            self.assertEqual(retro.main_paiva(self.tanaan(10), {"flash": self.a}, self.polku), 0)
        self.assertIn("- " + self.tanaan(10).isoformat(), self.teksti())

    def test_main_paiva_ilman_uutta_paivaa_ei_kirjoita(self):
        self.salkku(self.a, 10)
        self.assertEqual(retro.main_paiva(self.tanaan(10) + timedelta(days=5), {"flash": self.a}, self.polku), 0)
        self.assertFalse(os.path.exists(self.polku))

    def test_main_paiva_palauttaa_virhekoodin_kun_yhtaan_salkkua_ei_voi_lukea(self):
        puuttuva = os.path.join(self.d.name, "ei-ole.db")
        rikki = os.path.join(self.d.name, "rikki.db")
        with open(rikki, "wb") as f:
            f.write(b"ei ole sqlite-kanta" * 50)
        for salkut in ({"flash": puuttuva}, {"flash": rikki, "pro": puuttuva}):
            with self.subTest(salkut=sorted(salkut)), redirect_stdout(io.StringIO()) as ulos:
                self.assertEqual(retro.main_paiva(self.tanaan(10), salkut, self.polku), 1)
            self.assertFalse(os.path.exists(self.polku))
            self.assertIn("ennallaan", ulos.getvalue())

    def test_main_paiva_yksi_salkku_riittaa_nollakoodiin(self):
        self.salkku(self.a, 10)
        puuttuva = os.path.join(self.d.name, "ei-ole.db")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(retro.main_paiva(self.tanaan(10), {"flash": self.a, "pro": puuttuva}, self.polku), 0)
        self.assertIn("- " + self.tanaan(10).isoformat(), self.teksti())

    def test_paivarivi_ensimmaisella_paivalla_kayttaa_tilan_alkupaomaa(self):
        # s["tila"]-haara: vain yksi arvorivi -> edellinen arvo tulee tilan "alkupaoma"-avaimesta
        self.salkku(self.a, 1, kerroin=0.0)
        s = retro.lue_salkku(self.a)
        s["tila"]["alkupaoma"] = "20000"          # arvo 10000 -> -50 % alkupaomaan nahden
        r = retro.paivarivi(self.tanaan(1), {"flash": s})
        self.assertIn("flash -50.00 %", r)
        self.assertIn("TAPPIO", r)
        del s["tila"]["alkupaoma"]                # puuttuva avain -> rajat.ALKUPAOMA
        r2 = retro.paivarivi(self.tanaan(1), {"flash": s})
        self.assertIn("flash %+.2f %%" % (100 * (10000.0 / float(rajat.ALKUPAOMA) - 1)), r2)


class VainLukuPohja(unittest.TestCase):
    """ReadOnlyPaths-ajo: WAL-tilainen kanta ilman -shm/-wal-tiedostoja vain luku -hakemistossa."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.hak = os.path.join(self.d, "data")
        os.mkdir(self.hak)
        self.polku = os.path.join(self.hak, "s.db")
        con = sqlite3.connect(self.polku)
        con.execute("PRAGMA journal_mode=wal")
        con.execute("CREATE TABLE t(x INTEGER)")
        con.executemany("INSERT INTO t VALUES(?)", [(1,), (2,), (3,)])
        con.commit()
        con.close()  # viimeisen yhteyden sulkeminen siivoaa -wal ja -shm
        os.chmod(self.polku, 0o444)
        os.chmod(self.hak, 0o555)
        self.addCleanup(self._siivoa)

    def _siivoa(self):
        os.chmod(self.hak, 0o755)
        os.chmod(self.polku, 0o644)
        shutil.rmtree(self.d, ignore_errors=True)


class VainLukuWalTesti(VainLukuPohja):
    def test_ilman_wal_tiedostoja_luku_onnistuu(self):
        if os.geteuid() == 0:
            self.skipTest("root ohittaa hakemiston kirjoitusoikeuden")
        self.assertEqual(sorted(os.listdir(self.hak)), ["s.db"])
        con = retro._ro(self.polku)
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM t").fetchone()[0], 3)
            # varatie palauttaa muistikopion: tiedostopolku on tyhja eika tavallinen avaus ole tuottanut tulosta
            self.assertEqual(con.execute("PRAGMA database_list").fetchone()[2], "")
        finally:
            con.close()

    def test_wal_jossa_dataa_ei_lueta_immutablella(self):
        if os.geteuid() == 0:
            self.skipTest("root ohittaa hakemiston kirjoitusoikeuden")
        os.chmod(self.hak, 0o755)
        # tuore rivi vain -wal:ssa: pidetaan kirjoittaja auki, jolloin immutable ei sita nakisi
        w = sqlite3.connect(self.polku, timeout=10)
        os.chmod(self.polku, 0o644)
        w.close()
        w = sqlite3.connect(self.polku, timeout=10)
        w.execute("INSERT INTO t VALUES(4)")
        w.commit()
        self.assertTrue(os.path.getsize(self.polku + "-wal") > 0)
        con = retro._ro(self.polku)   # -shm on olemassa, tavallinen vain luku -avaus nakee wal:n
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM t").fetchone()[0], 4)
        finally:
            con.close()
            w.close()


    def test_lukitusvirhe_ei_johda_immutable_lukuun(self):
        """Kirjoittaja pitaa lukkoa (rollback-tila): 'database is locked' nostetaan, vanhaa tilaa ei lueta."""
        hak = os.path.join(self.d, "lukko")
        os.mkdir(hak)
        polku = os.path.join(hak, "l.db")
        c = sqlite3.connect(polku)
        c.execute("CREATE TABLE t(x INTEGER)")
        c.execute("INSERT INTO t VALUES(1)")
        c.commit()
        c.close()
        w = sqlite3.connect(polku, isolation_level=None)
        self.addCleanup(w.close)
        w.execute("BEGIN EXCLUSIVE")
        w.execute("INSERT INTO t VALUES(2)")
        with mock.patch.object(sqlite3, "connect", wraps=sqlite3.connect) as m:
            with self.assertRaises(sqlite3.OperationalError) as ctx:
                retro._ro(polku)
        self.assertIn("locked", str(ctx.exception))
        self.assertFalse(any("immutable" in str(k.args[0]) for k in m.call_args_list))

    def _wal_dataa_ilman_shm(self):
        """Palauttaa vain luku -hakemiston, jossa on kanta ja -wal (sisaltaa rivin 4), mutta ei -shm:aa."""
        lahde = os.path.join(self.d, "lahde")
        os.mkdir(lahde)
        lp = os.path.join(lahde, "w.db")
        w = sqlite3.connect(lp)
        w.execute("PRAGMA journal_mode=wal")
        w.execute("PRAGMA wal_autocheckpoint=0")
        w.execute("CREATE TABLE t(x INTEGER)")
        w.execute("INSERT INTO t VALUES(1)")
        w.commit()
        w.execute("INSERT INTO t VALUES(4)")
        w.commit()
        self.addCleanup(w.close)
        hak = os.path.join(self.d, "walhak")
        os.mkdir(hak)
        shutil.copy(lp, os.path.join(hak, "w.db"))
        shutil.copy(lp + "-wal", os.path.join(hak, "w.db-wal"))
        self.assertGreater(os.path.getsize(os.path.join(hak, "w.db-wal")), 0)
        os.chmod(os.path.join(hak, "w.db"), 0o444)
        os.chmod(os.path.join(hak, "w.db-wal"), 0o444)
        os.chmod(hak, 0o555)
        self.addCleanup(os.chmod, hak, 0o755)
        return os.path.join(hak, "w.db")

    def test_wal_dataa_ilman_shm_nostaa_virheen(self):
        if os.geteuid() == 0:
            self.skipTest("root ohittaa hakemiston kirjoitusoikeuden")
        polku = self._wal_dataa_ilman_shm()
        self.assertFalse(os.path.exists(polku + "-shm"))
        with self.assertRaises(retro.VanhentunutLukuVirhe):
            retro._ro(polku)

    def test_kirjoittaja_kopioinnin_aikana_nostaa_virheen(self):
        """Kilpatilanne: -wal ilmestyy tarkistuksen jalkeen, kesken immutable-kopioinnin."""
        if os.geteuid() == 0:
            self.skipTest("root ohittaa hakemiston kirjoitusoikeuden")
        alkuperainen = retro._kopioi_muistiin

        def kopioi_ja_kirjoita(polku):
            kopio = alkuperainen(polku)
            os.chmod(self.hak, 0o755)
            with open(polku + "-wal", "wb") as f:   # kirjoittaja aloittaa kopioinnin jalkeen
                f.write(b"x" * 32)
            os.chmod(self.hak, 0o555)
            return kopio

        with mock.patch.object(retro, "_kopioi_muistiin", kopioi_ja_kirjoita):
            with self.assertRaises(retro.VanhentunutLukuVirhe):
                retro._ro(self.polku)


class SeurantaYhteys(sqlite3.Connection):
    """Kirjaa, mitka yhteydet suljettiin; backup voidaan pakottaa epaonnistumaan."""
    kaikki = []
    backup_virhe = False

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.suljettu = False
        SeurantaYhteys.kaikki.append(self)

    def close(self):
        self.suljettu = True
        super().close()

    def backup(self, *a, **kw):
        if SeurantaYhteys.backup_virhe:
            raise sqlite3.OperationalError("backup epaonnistui")
        return super().backup(*a, **kw)


def seuranta(testi):
    """Patchaa sqlite3.connect kayttamaan SeurantaYhteys-luokkaa; palauttaa yhteyslistan."""
    SeurantaYhteys.kaikki = []
    SeurantaYhteys.backup_virhe = False
    alku = sqlite3.connect
    testi.addCleanup(setattr, SeurantaYhteys, "backup_virhe", False)

    def yhdista(*a, **kw):
        kw.setdefault("factory", SeurantaYhteys)
        return alku(*a, **kw)
    p = mock.patch.object(sqlite3, "connect", yhdista)
    p.start()
    testi.addCleanup(p.stop)
    return SeurantaYhteys.kaikki


class VaratieEhdotTesti(VainLukuPohja):
    """PR #39:n toinen hylkays: varatie vain WAL-kannalle, ei kuumalle journalille, yhteydet suljetaan, Python 3.10."""

    def _kuuma_journal(self):
        """Rollback-tilan kanta + kuuma -journal vain luku -hakemistossa (SQLITE_READONLY_ROLLBACK, 776)."""
        lahde = os.path.join(self.d, "rb_lahde")
        os.mkdir(lahde)
        lp = os.path.join(lahde, "r.db")
        c = sqlite3.connect(lp, isolation_level=None)
        self.addCleanup(c.close)
        c.execute("CREATE TABLE t(x INTEGER)")
        c.execute("BEGIN")
        c.executemany("INSERT INTO t VALUES(?)", [(i,) for i in range(2000)])
        c.execute("COMMIT")
        c.execute("PRAGMA synchronous=OFF")
        c.execute("BEGIN")
        c.execute("UPDATE t SET x=x+1")   # journal syntyy, kirjoittaja jaa kesken
        self.assertGreater(os.path.getsize(lp + "-journal"), 0)
        hak = os.path.join(self.d, "rb_hak")
        os.mkdir(hak)
        shutil.copy(lp, os.path.join(hak, "r.db"))
        shutil.copy(lp + "-journal", os.path.join(hak, "r.db-journal"))
        os.chmod(hak, 0o555)
        self.addCleanup(os.chmod, hak, 0o755)
        return os.path.join(hak, "r.db")

    def test_kuuma_journal_ro_hakemistossa_ei_immutablea(self):
        if os.geteuid() == 0:
            self.skipTest("root ohittaa hakemiston kirjoitusoikeuden")
        polku = self._kuuma_journal()
        with mock.patch.object(retro, "_kopioi_muistiin") as kopio:
            with self.assertRaises(sqlite3.OperationalError):
                retro._ro(polku)
        kopio.assert_not_called()

    def test_rollback_tila_ei_wal_otsikkoa_hylkaa_varatien(self):
        """Vaikka virhekoodi olisi sallittu (1544), rollback-tilainen kanta (tavut 18-19 = 1) ei kayta varatieta."""
        polku = os.path.join(self.d, "rb.db")
        c = sqlite3.connect(polku)
        c.execute("CREATE TABLE t(x INTEGER)")
        c.commit()
        c.close()
        self.assertEqual(retro._wal_tila(polku), False)
        virhe = sqlite3.OperationalError("attempt to write a readonly database")
        virhe.sqlite_errorcode = 1544
        alku = sqlite3.connect
        kutsut = []

        def yhdista(*a, **kw):
            kutsut.append(a[0])
            if len(kutsut) == 1:
                raise virhe
            return alku(*a, **kw)
        with mock.patch.object(sqlite3, "connect", yhdista), mock.patch.object(retro, "_kopioi_muistiin") as kopio:
            with self.assertRaises(sqlite3.OperationalError):
                retro._ro(polku)
        kopio.assert_not_called()

    def test_journal_ilmestyy_kopioinnin_aikana_nostaa_virheen(self):
        if os.geteuid() == 0:
            self.skipTest("root ohittaa hakemiston kirjoitusoikeuden")
        alkuperainen = retro._kopioi_muistiin

        def kopioi_ja_journal(polku):
            kopio = alkuperainen(polku)
            os.chmod(self.hak, 0o755)
            with open(polku + "-journal", "wb") as f:
                f.write(b"x")
            os.chmod(self.hak, 0o555)
            return kopio
        with mock.patch.object(retro, "_kopioi_muistiin", kopioi_ja_journal):
            with self.assertRaises(retro.VanhentunutLukuVirhe):
                retro._ro(self.polku)

    def test_journal_olemassa_hylkaa_varatien_wal_kannallakin(self):
        if os.geteuid() == 0:
            self.skipTest("root ohittaa hakemiston kirjoitusoikeuden")
        os.chmod(self.hak, 0o755)
        with open(self.polku + "-journal", "wb") as f:
            f.write(b"x")
        os.chmod(self.hak, 0o555)
        # tosiasiallinen avaus hylkaa jo itse (kuuma journal); tama testaa vartijan, kun koodi on sallittu (1544)
        virhe = sqlite3.OperationalError("attempt to write a readonly database")
        virhe.sqlite_errorcode = 1544
        alku = sqlite3.connect
        kutsut = []

        def yhdista(*a, **kw):
            kutsut.append(1)
            if len(kutsut) == 1:
                raise virhe
            return alku(*a, **kw)
        with mock.patch.object(sqlite3, "connect", yhdista), mock.patch.object(retro, "_kopioi_muistiin") as kopio:
            with self.assertRaises(retro.VanhentunutLukuVirhe):
                retro._ro(self.polku)
        kopio.assert_not_called()

    def test_wal_tila_otsikon_tavuista(self):
        self.assertTrue(retro._wal_tila(self.polku))
        roska = os.path.join(self.d, "roska.db")
        with open(roska, "wb") as f:
            f.write(b"x" * 10)
        self.assertFalse(retro._wal_tila(roska))

    def test_sallitut_virhekoodit(self):
        def virhe(koodi, viesti="x"):
            e = sqlite3.OperationalError(viesti)
            if koodi is not None:
                e.sqlite_errorcode = koodi
            return e
        for koodi in (8, 14, 1544):
            self.assertTrue(retro._shm_virhe(virhe(koodi)), koodi)
        for koodi in (5, 6, 776, 264, 1288, 3338):    # 776 = kuuma journal; 5/6 = lukitus
            self.assertFalse(retro._shm_virhe(virhe(koodi)), koodi)

    def test_python310_ilman_sqlite_errorcode(self):
        """Python 3.10:ssa sqlite_errorcode puuttuu: viestiin perustuva paatelma, WAL-otsikko varmistaa loput."""
        def virhe(viesti):
            e = sqlite3.OperationalError(viesti)
            if hasattr(e, "sqlite_errorcode"):
                del e.sqlite_errorcode
            return e
        self.assertTrue(retro._shm_virhe(virhe("attempt to write a readonly database")))
        self.assertTrue(retro._shm_virhe(virhe("unable to open database file")))
        self.assertFalse(retro._shm_virhe(virhe("database is locked")))
        # kokonaisuus: ensimmainen avaus epaonnistuu ilman koodia, WAL-kanta ilman -shm:aa luetaan muistikopiona
        alku = sqlite3.connect
        kutsut = []

        def yhdista(*a, **kw):
            kutsut.append(1)
            if len(kutsut) == 1:
                raise virhe("attempt to write a readonly database")
            return alku(*a, **kw)
        with mock.patch.object(sqlite3, "connect", yhdista):
            con = retro._ro(self.polku)
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM t").fetchone()[0], 3)
        finally:
            con.close()

    def test_python310_kuuma_journal_ei_johda_varatielle(self):
        if os.geteuid() == 0:
            self.skipTest("root ohittaa hakemiston kirjoitusoikeuden")
        polku = self._kuuma_journal()
        e = sqlite3.OperationalError("attempt to write a readonly database")
        alku = sqlite3.connect
        kutsut = []

        def yhdista(*a, **kw):
            kutsut.append(1)
            if len(kutsut) == 1:
                raise e
            return alku(*a, **kw)
        with mock.patch.object(sqlite3, "connect", yhdista), mock.patch.object(retro, "_kopioi_muistiin") as kopio:
            with self.assertRaises(sqlite3.OperationalError):
                retro._ro(polku)
        kopio.assert_not_called()

    def test_databaseerror_sulkee_yhteyden(self):
        roska = os.path.join(self.d, "ei_kanta.db")
        with open(roska, "wb") as f:
            f.write(b"tama ei ole sqlite-tiedosto " * 20)
        yhteydet = seuranta(self)
        with self.assertRaises(sqlite3.DatabaseError):
            retro._ro(roska)
        self.assertTrue(yhteydet)
        self.assertTrue(all(c.suljettu for c in yhteydet))

    def test_epaonnistunut_backup_sulkee_kohteen(self):
        if os.geteuid() == 0:
            self.skipTest("root ohittaa hakemiston kirjoitusoikeuden")
        yhteydet = seuranta(self)
        SeurantaYhteys.backup_virhe = True
        with self.assertRaises(sqlite3.OperationalError):
            retro._ro(self.polku)
        self.assertGreaterEqual(len(yhteydet), 3)    # ro-avaus, lahde, kohde
        self.assertTrue(all(c.suljettu for c in yhteydet))

    def test_virhe_nousee_ro_kutsussa_ja_raportti_kestaa_sen(self):
        """Kutsujat: lue_salkku/raportti ottavat sqlite3.Error-virheet kiinni, joten avauksessa nouseva virhe ei kaada viikkoretroa."""
        roska = os.path.join(self.d, "roska_salkku.db")
        with open(roska, "wb") as f:
            f.write(b"tama ei ole sqlite-tiedosto " * 20)
        t = retro.raportti(roska, {"flash": roska}, date(2026, 11, 1))
        self.assertIn("flash: kantaa ei voi lukea (DatabaseError)", t)


if __name__ == "__main__":
    unittest.main()
