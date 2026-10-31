import os
import random
import sqlite3
import tempfile
import unittest

import ab_tilasto as ab


def sarja(n, mu, sd, siemen, alku=1000.0):
    rng = random.Random(siemen)
    v = [alku]
    for _ in range(n):
        v.append(v[-1] * (1 + rng.gauss(mu, sd)))
    return {"2026-01-%03d" % i: x for i, x in enumerate(v)}


class Perus(unittest.TestCase):
    def test_max_drawdown(self):
        self.assertAlmostEqual(ab.max_drawdown([100, 120, 90, 110, 60]), 0.5)
        self.assertEqual(ab.max_drawdown([1, 2, 3]), 0.0)

    def test_sharpe(self):
        self.assertIsNone(ab.sharpe([0.01, 0.01, 0.01]))
        self.assertIsNone(ab.sharpe([0.01]))
        self.assertGreater(ab.sharpe([0.01, -0.005, 0.02, 0.0]), 0)

    def test_n_kaava(self):
        # (1.96+0.8416)^2 * (0.02/0.001)^2 = 7.849 * 400 = 3140
        self.assertAlmostEqual(ab.tarvittavat_paivat(0.02, 0.001), 3140, delta=3)
        self.assertIsNone(ab.tarvittavat_paivat(0.02, 0.0))

    def test_mde_ja_n_yhdenmukaisia(self):
        mde = ab.pienin_havaittava_ero(0.02, 400)
        self.assertLessEqual(ab.tarvittavat_paivat(0.02, mde), 400)


class Analyysi(unittest.TestCase):
    def test_ei_eroa_ci_sisaltaa_nollan(self):
        r = ab.analysoi(sarja(300, 0.0005, 0.02, 1), sarja(300, 0.0005, 0.02, 2), toistoja=1000)
        lo, hi = r["ci"]["ero_ka"]
        self.assertLess(lo, 0)
        self.assertGreater(hi, 0)
        self.assertEqual(r["paivia"], 300)

    def test_selva_ero_havaitaan(self):
        rng = random.Random(5)
        fl, pr = [1000.0], [1000.0]
        for _ in range(200):
            x = rng.gauss(0, 0.02)
            fl.append(fl[-1] * (1 + x))
            pr.append(pr[-1] * (1 + x + 0.005 + rng.gauss(0, 0.001)))
        d = lambda v: {"p%04d" % i: y for i, y in enumerate(v)}
        r = ab.analysoi(d(fl), d(pr), toistoja=1000)
        lo, hi = r["ci"]["ero_ka"]
        self.assertGreater(lo, 0)
        self.assertAlmostEqual(r["ero_ka_pv"], 0.005, delta=0.0005)
        self.assertLess(r["n_tarvitaan_havaitulle"], 20)
        self.assertIn("ON tilastollisesti", ab.muotoile(r))

    def test_toistettava_siemenella(self):
        a, b = sarja(100, 0, 0.02, 3), sarja(100, 0, 0.02, 4)
        self.assertEqual(ab.analysoi(a, b, toistoja=200, siemen=7)["ci"], ab.analysoi(a, b, toistoja=200, siemen=7)["ci"])

    def test_liian_vahan_dataa(self):
        r = ab.analysoi(sarja(5, 0, 0.01, 1), sarja(5, 0, 0.01, 2))
        self.assertIn("data ei riita", r["virhe"])

    def test_vain_yhteiset_paivat(self):
        a = sarja(50, 0, 0.01, 1)
        b = {k: v for k, v in sarja(50, 0, 0.01, 2).items() if k > "2026-01-010"}
        self.assertEqual(ab.analysoi(a, b, toistoja=100)["paivia"], len(b) - 1)

    def test_identtiset_salkut(self):
        a = sarja(40, 0, 0.01, 1)
        r = ab.analysoi(a, dict(a), toistoja=100)
        self.assertEqual(r["ero_ka_pv"], 0.0)
        self.assertIsNone(r["n_tarvitaan_havaitulle"])
        ab.muotoile(r)


class Cli(unittest.TestCase):
    def _db(self, d, nimi, arvot):
        p = os.path.join(d, nimi)
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE arvot (paiva TEXT PRIMARY KEY, arvo REAL NOT NULL, kateinen REAL NOT NULL, positiot_arvo REAL NOT NULL)")
        con.executemany("INSERT INTO arvot VALUES (?,?,0,0)", list(arvot.items()))
        con.commit()
        con.close()
        return p

    def test_main_synteettisilla_tietokannoilla(self):
        with tempfile.TemporaryDirectory() as d:
            f = self._db(d, "f.db", sarja(60, 0, 0.02, 1))
            p = self._db(d, "p.db", sarja(60, 0, 0.02, 2))
            self.assertEqual(ab.main(["--flash", f, "--pro", p, "--toistoja", "200"]), 0)

    def test_puuttuva_polku_antaa_virheen_ilman_luontia(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ab.main(["--flash", d + "/ei.db", "--pro", d + "/ei2.db"]), 1)
            self.assertEqual(os.listdir(d), [])


if __name__ == "__main__":
    unittest.main()
