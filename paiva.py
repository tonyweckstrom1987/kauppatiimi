"""Paivittainen putki (yksi salkku / yksi malli): data -> taulukko -> analyytikko -> kriitikko -> riskivahti -> simulaatio -> raportti.
HUOM: vain eteenpain (forward) -paperikauppa: menneisyyden LLM-backtest on saastunut, koska malli on nahnyt vanhat hinnat."""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone

import agentit
import config
import db
import llm
import mittarit
import notify
import raportti as verorap
import rajat
import salkku
import silmukka
import taulukko
import tapahtumat
import tarkistus

PV = 86400000


def _ms(p):
    return taulukko.paiva_ms(p)


def viimeisin_paiva(markkina):
    ts = markkina.execute("SELECT MAX(ts) FROM hinnat WHERE symbol='BTCUSDT'").fetchone()[0]
    return datetime.fromtimestamp(ts / 1000, timezone.utc).date() if ts else None


def omistukset_teksti(sal, markkina, paiva):
    """Salkun omistukset painoina paivan D-1 sulkuhinnoilla."""
    d_ms = _ms(paiva) - PV
    pos = salkku.positiot(sal)
    arvot = {}
    for s, q in pos.items():
        r = markkina.execute("SELECT close FROM hinnat WHERE symbol=? AND ts=?", (s, d_ms)).fetchone()
        if r is None:
            raise salkku.Datavirhe("ei sulkuhintaa omistukselle %s" % s)
        arvot[s] = q * r[0]
    kassa = salkku.kateinen(sal)
    yht = kassa + sum(arvot.values())
    rivit = ["kateinen %.1f %%" % (100 * kassa / yht)] + ["%s %.1f %%" % (s, 100 * v / yht) for s, v in sorted(arvot.items())]
    return "salkun arvo %.0f USDT; " % yht + "; ".join(rivit)


def _kutsut_tanaan(sal, paiva):
    sal.execute("CREATE TABLE IF NOT EXISTS llm_kutsut (paiva TEXT, rooli TEXT, malli TEXT, tokens_in INTEGER, tokens_out INTEGER)")
    return sal.execute("SELECT COUNT(*) FROM llm_kutsut WHERE paiva=?", (paiva.isoformat(),)).fetchone()[0]


def _kirjaa_kutsu(sal, paiva, rooli, malli, kaytto):
    sal.execute("INSERT INTO llm_kutsut VALUES (?,?,?,?,?)",
                (paiva.isoformat(), rooli, malli, kaytto.get("prompt_tokens", 0) or 0, kaytto.get("completion_tokens", 0) or 0))
    sal.commit()


def _kirjaa_kriitikko(sal, paiva, painot, kritiikki):
    """Tallentaa analyytikon ehdotuksen ja kriitikon paatoksen retron havaintoja varten (doc 67 vaihe 1). Vain kirjaus: ei koskaan kaada ajoa."""
    try:
        sal.execute("CREATE TABLE IF NOT EXISTS kriitikko_loki (paiva TEXT PRIMARY KEY, ehdotus TEXT NOT NULL, paatos TEXT NOT NULL, kerroin REAL NOT NULL)")
        sal.execute("INSERT OR REPLACE INTO kriitikko_loki VALUES (?,?,?,?)",
                    (paiva.isoformat(), json.dumps(painot, sort_keys=True), kritiikki["paatos"], float(kritiikki["kerroin"])))
        sal.commit()
    except Exception:
        pass


def aja_paiva(paiva, malli, markkina, sal, env, hae_llm=None, malli_avain=None):
    """Ajaa yhden simulaatiopaivan. Palauttaa raporttidictin. Fail closed: virhe = ei kauppoja, tila sailyy."""
    tulos = {"paiva": paiva.isoformat(), "malli": malli, "analyytikko": None, "kriitikko": None, "virhe": None}
    btc_edell = markkina.execute("SELECT 1 FROM hinnat WHERE symbol='BTCUSDT' AND ts=?", (_ms(paiva) - PV,)).fetchone()
    if not btc_edell:
        raise salkku.Datavirhe("BTC-data puuttuu paivalta ennen %s" % paiva)
    teksti, tiedot = taulukko.rakenna(markkina, paiva)
    sallitut = set(tiedot)
    if not sallitut:
        tulos["virhe"] = "tyhja universumi"
    ehdotus = None
    omistukset = omistukset_teksti(sal, markkina, paiva)
    if not tulos["virhe"]:
        if _kutsut_tanaan(sal, paiva) + 2 > rajat.MAX_LLM_KUTSUJA_PAIVA:
            tulos["virhe"] = "paivan LLM-kutsukatto"
    if not tulos["virhe"]:
        rooli = "analyytikko"
        try:
            a, kaytto = agentit.kysy_analyytikko(teksti, omistukset, malli, env, hae_llm,
                                                 silmukka.havainnot_analyytikolle(malli_avain, paiva),   # oletuksena "" (silmukka pois / ei pro)
                                                 silmukka.analyytikko_ohje(malli_avain))
            if silmukka.kaytossa() and malli_avain == silmukka.PRO_AVAIN and "datapyynto" in a:
                try:
                    silmukka.kirjaa_pyynto(silmukka.PYYNTO_POLKU, a["datapyynto"], paiva)
                except OSError:
                    pass   # kirjaus ei saa kaataa ajoa
            _kirjaa_kutsu(sal, paiva, "analyytikko", malli, kaytto)
            tulos["analyytikko"] = a
            tulos["keksitty"] = len(tarkistus.tarkista_perustelu(a.get("perustelu"), teksti))   # vain havainto, ei vaikuta kauppoihin
            if "paino" in a:
                rooli = "kriitikko"
                k, kaytto = agentit.kysy_kriitikko(teksti, omistukset, a["paino"], malli, env, hae_llm)
                _kirjaa_kutsu(sal, paiva, "kriitikko", malli, kaytto)
                tulos["kriitikko"] = k
                _kirjaa_kriitikko(sal, paiva, a["paino"], k)
                ehdotus = agentit.sovella_kriitikko(a["paino"], k)
        except (llm.LLMVirhe, agentit.Vastausvirhe) as e:
            if getattr(e, "kaytto", None) is not None:
                _kirjaa_kutsu(sal, paiva, rooli, malli, e.kaytto)   # kelvoton vastaus kuluttaa silti paivakiintiota
            tulos["virhe"] = "%s: %s" % (type(e).__name__, e)
            ehdotus = None
    sim = salkku.suorita_paiva(sal, markkina, paiva, ehdotus, sallitut)
    tulos["sim"] = sim
    return tulos


def _kirjaa_tapahtuma(sal, paiva, ryhma, malli, t, kesto):
    """Prosessin mittarit tapahtumalokiin (tapahtumat.py). Ei koskaan kaada ajoa."""
    try:
        r = sal.execute("SELECT COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0) FROM llm_kutsut WHERE paiva=?", (paiva.isoformat(),)).fetchone()
        tapahtumat.kirjaa("paiva", tapahtumat.paivan_tulos(t), ryhma=ryhma, malli=malli, virhekoodi=tapahtumat.virhekoodi(t.get("virhe")),
                          tokens_in=r[0], tokens_out=r[1], kesto_s=round(kesto, 1), keksitty=t.get("keksitty"))
    except Exception:
        pass


def raportti(tulos, sal, markkina):
    sim = tulos["sim"]
    rivit = ["Kauppatiimi (%s) %s" % (tulos["malli"], tulos["paiva"])]
    a, k = tulos["analyytikko"], tulos["kriitikko"]
    if tulos["virhe"]:
        rivit.append("VIRHE: %s -> ei kauppoja" % tulos["virhe"])
    elif a and a.get("en_tieda"):
        rivit.append("Analyytikko: en tieda (%s)" % a.get("syy", ""))
    elif a:
        rivit.append("Analyytikko (%s): %s" % (a["luottamus"], a["perustelu"]))
        if k:
            rivit.append("Kriitikko: %s%s %s" % (k["paatos"], (" x%.2f" % k["kerroin"]) if k["paatos"] == "pienenna" else "", "; ".join(k["ongelmat"])))
    if sim["kaupat"]:
        rivit.append("Kaupat: " + ", ".join("%s %s %.1f" % (x[2], x[1].replace("USDT", ""), x[3] * x[4]) for x in sim["kaupat"]))
    else:
        rivit.append("Ei kauppoja.")
    for h in sim["huomautukset"][:4]:
        rivit.append("Huom: " + h)
    alku = salkku._hae(sal, "alkupaoma")
    tuotto = sim["v_close"] / float(alku) - 1
    rivi = "Salkku %.0f USDT (%+.2f %% alusta)" % (sim["v_close"], 100 * tuotto)
    aloitus = salkku._hae(sal, "aloituspaiva")
    try:
        if aloitus and aloitus <= tulos["paiva"]:
            a0 = date.fromisoformat(aloitus)
            p = date.fromisoformat(tulos["paiva"])
            v = mittarit.vertailusalkku(markkina, rajat.VERTAILU_PAINOT, a0, p)
            b = mittarit.vertailusalkku(markkina, {"BTCUSDT": 1.0}, a0, p)
            rivi += " | vertailu %+.2f %% | BTC %+.2f %%" % (100 * (v[-1][1] / rajat.ALKUPAOMA - 1), 100 * (b[-1][1] / rajat.ALKUPAOMA - 1))
    except salkku.Datavirhe:
        rivi += " | vertailu ei saatavilla"
    rivit.append(rivi)
    try:
        kaupat = sal.execute("SELECT paiva, symbol, puoli, maara, hinta, kulu FROM kaupat ORDER BY id").fetchall()
        rivit.append(verorap.rivi(kaupat, tulos["paiva"][:4]))
    except sqlite3.Error as e:
        rivit.append("Realisoitunut tulos: ei saatavilla (%s)" % type(e).__name__)
    return "\n".join(rivit)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--malli", required=True, help="avain rajat.MALLIT-listasta, esim. flash tai pro")
    ap.add_argument("--salkku-db", required=True)
    ap.add_argument("--llm-env", default="/srv/kauppa/llm.env")
    ap.add_argument("--tg-env", default="/srv/kauppa/.env")
    ap.add_argument("--ei-tg", action="store_true")
    a = ap.parse_args()
    if a.malli not in rajat.MALLIT:
        print("tuntematon malli"); return 3
    malli = rajat.MALLIT[a.malli]
    if os.path.exists(rajat.PYSAYTYS):
        msg = "Kauppatiimi (%s): PYSAYTETTY (kill switch %s), ei ajoa" % (malli, rajat.PYSAYTYS)
        print(msg)
        if not a.ei_tg:
            try:
                notify.laheta(msg, llm.lue_env(a.tg_env))
            except llm.LLMVirhe:
                pass
        return 0
    markkina = db.avaa(config.DB_POLKU)
    sal = salkku.avaa(a.salkku_db)
    try:
        env = llm.lue_env(a.llm_env)
        try:
            env_tg = llm.lue_env(a.tg_env)
        except llm.LLMVirhe:
            env_tg = {}
    except llm.LLMVirhe as e:
        print("VIRHE:", e); return 2
    viim = viimeisin_paiva(markkina)
    if viim is None:
        print("ei markkinadataa"); return 2
    edellinen = salkku._hae(sal, "viimeisin_paiva")
    if edellinen is None:
        paivat = [viim]               # ensimmainen ajo: aloita uusimmasta paivasta (forward-only)
        salkku._aseta(sal, "aloituspaiva", viim.isoformat())
    else:
        p = date.fromisoformat(edellinen) + timedelta(days=1)
        paivat = []
        while p <= viim:
            paivat.append(p); p += timedelta(days=1)
    if not paivat:
        print("ei uutta paivaa"); return 0
    virheita = 0
    for p in paivat:
        alkoi = time.monotonic()
        try:
            t = aja_paiva(p, malli, markkina, sal, env, malli_avain=a.malli)
        except salkku.Datavirhe as e:
            msg = "Kauppatiimi (%s) %s: DATAVIRHE, keskeytetty: %s" % (malli, p, e)
            print(msg)
            if not a.ei_tg: notify.laheta(msg, env_tg)
            return 2
        _kirjaa_tapahtuma(sal, p, a.malli, malli, t, time.monotonic() - alkoi)
        msg = raportti(t, sal, markkina)
        print(msg)
        if not a.ei_tg:
            notify.laheta(msg, env_tg)
        virheita += 1 if t["virhe"] else 0
    return 1 if virheita else 0


if __name__ == "__main__":
    sys.exit(main())
