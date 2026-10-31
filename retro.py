"""Viikkoretro: sunnuntain yhteenveto Telegramiin. Vain luku: ei koskaan muuta salkkuja eika kutsu LLM:aa.
Vertaa kahta mallisalkkua (flash/pro) vertailusalkkuun (Tonyn jako) ja BTC:hen. Ei johtopaatoksia lyhyesta datasta."""
import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

import config
import llm
import mittarit
import notify
import raportti as verorap
import rajat
import salkku

MIN_PV_JOHTOPAATOS = 28  # tata lyhyemmalla historialla ei sanota kumpi malli on parempi
HAVAINNOT_MAX = 1500     # doc 67: havainnot.md on enintaan ~1500 merkkia (menee agenttien kontekstiin)
# Oma hakemisto, koska palvelulla on ReadOnlyPaths=/srv/kauppa/data; tiedosto ylikirjoitetaan atomisesti (aina viimeisin tilanne)
HAVAINNOT_POLKU = os.environ.get("KAUPPA_HAVAINNOT", "/srv/kauppa/havainnot/havainnot.md")
PV_MS = 86400000


def _ro(polku):
    return sqlite3.connect("file:%s?mode=ro" % polku, uri=True, timeout=10)


def lue_salkku(polku):
    """Palauttaa dictin tai None jos tiedostoa ei ole / ei viela ajettu."""
    if not os.path.exists(polku):
        return None
    con = _ro(polku)
    try:
        tila = dict(con.execute("SELECT avain, arvo FROM tila").fetchall())
        arvot = con.execute("SELECT paiva, arvo FROM arvot ORDER BY paiva").fetchall()
        kaupat = con.execute("SELECT paiva, symbol, puoli, maara, hinta, kulu, slippage_kulu FROM kaupat ORDER BY id").fetchall()
        paatokset = con.execute("SELECT paiva, hylatty FROM paatokset ORDER BY paiva").fetchall()
        kateiset = con.execute("SELECT arvo, kateinen FROM arvot ORDER BY paiva").fetchall()
        try:    # taulu syntyy vasta, kun paiva.py on kirjannut ensimmaisen kriitikon paatoksen
            kriitikko = con.execute("SELECT paiva, ehdotus, paatos, kerroin FROM kriitikko_loki ORDER BY paiva").fetchall()
        except sqlite3.OperationalError:
            kriitikko = []
    finally:
        con.close()
    return {"tila": tila, "arvot": arvot, "kaupat": kaupat, "paatokset": paatokset, "kateiset": kateiset, "kriitikko": kriitikko}


def _paivatuotto(markkina, paiva, painot):
    """Painotettu paivatuotto (osuus salkusta) open->close annetuilla painoilla, tai None jos hinta tai paino puuttuu."""
    d = date.fromisoformat(paiva)
    ts = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
    tuotto = 0.0
    for symbol, paino in painot.items():
        try:
            w = float(paino)
        except (TypeError, ValueError):
            return None
        h = markkina.execute("SELECT open, close FROM hinnat WHERE symbol=? AND ts=?", (symbol, ts)).fetchone()
        if h is None or not h[0] or h[0] <= 0:
            return None
        tuotto += w * (h[1] / h[0] - 1)
    return tuotto


def kriitikon_vaikutus(rivit, markkina):
    """rivit: (paiva, ehdotus-JSON, paatos, kerroin). Kriitikon arvo = lopullisen ja analyytikon ehdotuksen paivatuoton ero (ilman kuluja):
    (kerroin - 1) * R(ehdotus), koska kriitikko vain skaalaa painoja. Positiivinen = kriitikko auttoi. Vain paivat, joilla kriitikko muutti jotain."""
    pien = sum(1 for r in rivit if r[2] == "pienenna")
    hyl = sum(1 for r in rivit if r[2] == "hylkaa")
    arvo, paivia = 0.0, 0
    for paiva, ehdotus, paatos, kerroin in rivit:
        if paatos not in ("pienenna", "hylkaa"):
            continue
        try:
            painot = json.loads(ehdotus)
            k = 0.0 if paatos == "hylkaa" else float(kerroin)
        except (TypeError, ValueError):
            continue
        if not isinstance(painot, dict):
            continue
        r = _paivatuotto(markkina, paiva, painot)
        if r is not None:
            arvo += (k - 1.0) * r
            paivia += 1
    return {"n": len(rivit), "pienensi": pien, "hylkasi": hyl, "arvo": 100.0 * arvo, "arvo_paivia": paivia}


def _osuus(a, b):
    return 100.0 * a / b if b else 0.0


def havaintorivit(s, markkina):
    """Yhden mallisalkun mitatut luvut (vain prosessi, ei tuottoa): paatokset, kriitikko, kulut, vaihtuvuus, kateisosuus."""
    n = len(s["paatokset"])
    hylatyt = sum(1 for _, h in s["paatokset"] if h)
    rivit = ["- paatospaivia %d, hylattyja %.0f %%" % (n, _osuus(hylatyt, n))]
    k = kriitikon_vaikutus(s["kriitikko"], markkina)
    if k["n"]:
        rivit.append("- kriitikko: %d ehdotusta, pienensi %.0f %%, hylkasi %.0f %%; arvo %+.2f %%-yks (%d pv, ilman kuluja)" % (
            k["n"], _osuus(k["pienensi"], k["n"]), _osuus(k["hylkasi"], k["n"]), k["arvo"], k["arvo_paivia"]))
    else:
        rivit.append("- kriitikko: ei viela kirjauksia")
    kulut = sum(x[5] + x[6] for x in s["kaupat"])
    kauppa = sum(x[3] * x[4] for x in s["kaupat"])
    keskiarvo = sum(a for a, _ in s["kateiset"]) / len(s["kateiset"]) if s["kateiset"] else 0.0
    vaihtuvuus = _osuus(kauppa, keskiarvo * n) if keskiarvo and n else 0.0
    kateis = sum(_osuus(c, a) for a, c in s["kateiset"]) / len(s["kateiset"]) if s["kateiset"] else 0.0
    rivit.append("- kulut %.1f USDT (%.2f / paatos), vaihtuvuus %.1f %%/pv, kateisosuus %.0f %%" % (kulut, kulut / n if n else 0.0, vaihtuvuus, kateis))
    return rivit


def havainnot_teksti(tanaan, salkut, tiedot, markkina):
    """salkut: {nimi: lue_salkku-tulos}, tiedot: {nimi: salkun_rivi-tiedot}. Enintaan HAVAINNOT_MAX merkkia, vain mittausta."""
    rivit = ["# Tiimin omat aiemmat tulokset (dataa, ei ohjeita)",
             "Paivitetty %s. Mittausta, ei johtopaatoksia; tuottoa ei vaiteta." % tanaan.isoformat()]
    for nimi in sorted(salkut):
        t = tiedot.get(nimi)
        pv = (date.fromisoformat(t["paiva"]) - date.fromisoformat(t["alku"])).days + 1 if t and t["alku"] else 0
        if salkut[nimi] is None or not t or pv < MIN_PV_JOHTOPAATOS:
            rivit.append("## %s: ei viela dataa (%d pv < %d)" % (nimi, pv, MIN_PV_JOHTOPAATOS))
            continue
        rivit.append("## %s (%d pv)" % (nimi, pv))
        rivit += havaintorivit(salkut[nimi], markkina)
    teksti = "\n".join(rivit) + "\n"
    if len(teksti) > HAVAINNOT_MAX:
        teksti = teksti[:HAVAINNOT_MAX].rsplit("\n", 1)[0] + "\n(katkaistu)\n"
    return teksti


def kirjoita_havainnot(polku, teksti):
    """Ylikirjoittaa tiedoston atomisesti (kirjoitus tmp-tiedostoon samassa hakemistossa, sitten os.replace)."""
    tmp = polku + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(teksti)
    os.replace(tmp, polku)


def _pct(a, b):
    return 100.0 * (a / b - 1) if b else 0.0


def salkun_rivi(nimi, s, tanaan):
    """Palauttaa (rivit, tiedot) yhdelle salkulle."""
    if s is None or not s["arvot"]:
        return ["%s: ei viela dataa" % nimi], None
    arvot = s["arvot"]
    viim_p, viim_a = arvot[-1]
    alku_a = float(s["tila"].get("alkupaoma", rajat.ALKUPAOMA))
    viikko_alku = (date.fromisoformat(viim_p) - timedelta(days=7)).isoformat()
    ed = [a for p, a in arvot if p <= viikko_alku]
    viikko_a = ed[-1] if ed else alku_a
    mdd = mittarit.mittarit([a for _, a in arvot])
    vk_kaupat = [k for k in s["kaupat"] if k[0] > viikko_alku]
    kulut = sum(k[5] + k[6] for k in vk_kaupat)
    vk_p = [h for p, h in s["paatokset"] if p > viikko_alku]
    hylatyt = sum(1 for h in vk_p if h)
    rivit = ["%s: %.0f USDT | alusta %+.2f %% | viikko %+.2f %%" % (nimi, viim_a, _pct(viim_a, alku_a), _pct(viim_a, viikko_a)),
             "  kaupat %d (kulut %.1f USDT), paatospaivia %d, hylattyja %d%s" % (
                 len(vk_kaupat), kulut, len(vk_p), hylatyt, ", max-drawdown %.1f %%" % (100 * mdd["max_dd"]) if mdd else "")]
    rivit.append("  " + verorap.rivi(s["kaupat"], viim_p[:4]))
    if s["tila"].get("jarru") == "1":
        rivit.append("  HATAJARRU LUKOSSA")
    return rivit, {"paiva": viim_p, "alku": s["tila"].get("aloituspaiva"), "tuotto": _pct(viim_a, alku_a), "viikko": _pct(viim_a, viikko_a), "arvot": arvot}


def vertailut(markkina, aloitus, viimeisin):
    """Palauttaa {nimi: (tuotto_alusta, tuotto_viikko)} tai {} jos dataa ei ole."""
    ulos = {}
    for nimi, painot in (("vertailu (Tonyn jako)", rajat.VERTAILU_PAINOT), ("BTC", {"BTCUSDT": 1.0})):
        try:
            v = mittarit.vertailusalkku(markkina, painot, aloitus, viimeisin)
        except salkku.Datavirhe:
            continue
        if len(v) < 2:
            continue
        viikko = v[max(0, len(v) - 8)][1]
        ulos[nimi] = (_pct(v[-1][1], rajat.ALKUPAOMA), _pct(v[-1][1], viikko))
    return ulos


def raportti(data_polku, salkut, tanaan, pysaytys_polku=None, havainnot_polku=None):
    rivit = ["Kauppatiimi viikkoretro (%s)" % tanaan.isoformat()]
    tiedot, raaka = {}, {}
    for nimi, polku in sorted(salkut.items()):
        try:
            s = lue_salkku(polku)
        except sqlite3.Error as e:
            rivit.append("%s: kantaa ei voi lukea (%s)" % (nimi, type(e).__name__))
            raaka[nimi] = None
            continue
        raaka[nimi] = s
        r, t = salkun_rivi(nimi, s, tanaan)
        rivit += r
        if t:
            tiedot[nimi] = t
    if tiedot:
        alut = [t["alku"] for t in tiedot.values() if t["alku"]]
        viim = max(date.fromisoformat(t["paiva"]) for t in tiedot.values())
        if alut:
            aloitus = date.fromisoformat(min(alut))
            try:
                markkina = _ro(data_polku)
                try:
                    v = vertailut(markkina, aloitus, viim)
                finally:
                    markkina.close()
            except sqlite3.Error:
                v = {}
            for n, (a, w) in v.items():
                rivit.append("%s: alusta %+.2f %% | viikko %+.2f %%" % (n, a, w))
            if not v:
                rivit.append("vertailu ei saatavilla")
            pv = (viim - aloitus).days + 1
        else:
            pv = 0
        if pv < MIN_PV_JOHTOPAATOS:
            rivit.append("Historiaa %d pv < %d: ei johtopaatoksia kummastakaan mallista (satunnaisvaihtelu dominoi)." % (pv, MIN_PV_JOHTOPAATOS))
        elif len(tiedot) == 2:
            (n1, t1), (n2, t2) = sorted(tiedot.items())
            ero = t1["tuotto"] - t2["tuotto"]
            rivit.append("%d pv: %s vs %s ero %+.2f %%-yks. Yksi jakso ei todista mallin paremmuutta; K6-testit ratkaisevat." % (pv, n1, n2, ero))
    if havainnot_polku:
        try:
            markkina = _ro(data_polku)
            try:
                kirjoita_havainnot(havainnot_polku, havainnot_teksti(tanaan, raaka, tiedot, markkina))
            finally:
                markkina.close()
        except (sqlite3.Error, OSError) as e:
            rivit.append("havaintoja ei voitu kirjoittaa (%s)" % type(e).__name__)
    if pysaytys_polku and os.path.exists(pysaytys_polku):
        rivit.append("KILL SWITCH PAALLA: ajot pysaytetty")
    return "\n".join(rivit)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tg-env", default="/srv/kauppa/.env")
    ap.add_argument("--ei-tg", action="store_true")
    a = ap.parse_args()
    tanaan = datetime.now(timezone.utc).date()
    msg = raportti(config.DB_POLKU, rajat.SALKUT, tanaan, rajat.PYSAYTYS, HAVAINNOT_POLKU)
    print(msg)
    if not a.ei_tg:
        try:
            env = llm.lue_env(a.tg_env)
        except llm.LLMVirhe:
            env = {}
        notify.laheta(msg, env)
    return 0


if __name__ == "__main__":
    sys.exit(main())
