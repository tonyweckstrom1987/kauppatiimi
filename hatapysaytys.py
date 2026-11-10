"""Hatapysaytys oikealle rahalle: pysaytystiedosto + Telegram-komento "seis kauppa" (vain Tonyn chat-id).

Pysaytys tekee kaksi asiaa tassa jarjestyksessa:
  1. kirjoittaa pysaytystiedoston (sama kuin paperisalkkujen kill switch, rajat.PYSAYTYS), jolloin
     porssi.Asiakas.uusi_toimeksianto kieltaytyy lahettamasta mitaan
  2. peruu kaikki avoimet toimeksiannot ja tarkistaa, ettei niita jaa
Pysaytys ei myy positioita (myynti markkinahintaan paniikissa voi olla huonompi kuin pito). Jatko: Tony poistaa
tiedoston kasin (rm), ei koskaan ohjelma.

Telegram-osa: kasittele_telegram(viesti, ...) on valmis kytkettavaksi Telegram-lukijaan (tehtava 2026-11-04-12).
Siihen asti pysaytys tehdaan tiedostolla:  python3 -m hatapysaytys --syy "kasin"
Viestin tekstia ei koskaan kirjoiteta tiedostoon; vain kiintea syy "telegram".
"""
import argparse
import datetime
import os
import re
import sys

import rajat


class Pysaytetty(Exception):
    pass


def oletuspolku():
    return rajat.PYSAYTYS


def pysaytetty(polku=None):
    return os.path.lexists(polku or oletuspolku())


def varmista_ei_pysaytetty(polku=None):
    """Kutsutaan ennen jokaista toimeksiantoa. Nostaa Pysaytetty, jos tiedosto on olemassa."""
    if pysaytetty(polku):
        raise Pysaytetty("hatapysaytys paalla")


def _siisti_syy(syy):
    return re.sub(r"[^A-Za-z0-9 ._:-]", "_", str(syy))[:80] or "ei syyta"


def kirjoita_tiedosto(syy, polku=None, nyt=None):
    p = polku or oletuspolku()
    nyt = nyt or datetime.datetime.now(datetime.timezone.utc)
    hakemisto = os.path.dirname(p)
    if hakemisto:
        os.makedirs(hakemisto, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    with os.fdopen(fd, "a") as f:
        f.write("%s %s\n" % (nyt.strftime("%Y-%m-%dT%H:%M:%SZ"), _siisti_syy(syy)))
        f.flush()
        os.fsync(f.fileno())
    return p


def peru_avoimet(asiakas):
    """Peruu kaikki avoimet toimeksiannot. Palauttaa (peruttu_symbolit, jaljella_lkm, virheet)."""
    virheet = []
    try:
        avoimet = asiakas.avoimet_toimeksiannot()
    except Exception as e:
        return [], None, ["avoimet: %s" % type(e).__name__]
    symbolit = sorted({o.get("symbol") for o in avoimet if isinstance(o, dict) and o.get("symbol")})
    peruttu = []
    for s in symbolit:
        try:
            asiakas.peru_kaikki(s)
            peruttu.append(s)
        except Exception as e:
            virheet.append("peru %s: %s" % (s, type(e).__name__))
    try:
        jaljella = len(asiakas.avoimet_toimeksiannot())
    except Exception as e:
        jaljella = None
        virheet.append("tarkistus: %s" % type(e).__name__)
    return peruttu, jaljella, virheet


def pysayta(syy, asiakas=None, polku=None, nyt=None):
    """Tiedosto ensin (estaa uudet kaupat heti), sitten avointen peruutus. Palauttaa raportin sanakirjana."""
    p = kirjoita_tiedosto(syy, polku, nyt)
    raportti = {"tiedosto": p, "peruttu": [], "jaljella": None, "virheet": []}
    if asiakas is not None:
        raportti["peruttu"], raportti["jaljella"], raportti["virheet"] = peru_avoimet(asiakas)
    return raportti


# --- Telegram ---
KOMENTO = "seis kauppa"


def on_seis_komento(viesti, sallittu_chat_id):
    """viesti: Telegramin message-objekti. Tosi vain, jos chat.id JA from.id ovat Tonyn chat-id ja
    teksti on tasan "seis kauppa" (kirjainkoko ja ylimaaraiset valilyonnit sallitaan)."""
    if not isinstance(viesti, dict) or isinstance(sallittu_chat_id, bool):
        return False
    try:
        sallittu = int(sallittu_chat_id)
    except (TypeError, ValueError):
        return False
    chat, lahettaja = viesti.get("chat") or {}, viesti.get("from") or {}
    if not isinstance(chat, dict) or not isinstance(lahettaja, dict):
        return False
    if chat.get("id") != sallittu or lahettaja.get("id") != sallittu:
        return False
    teksti = viesti.get("text")
    if not isinstance(teksti, str) or len(teksti) > 40:
        return False
    return " ".join(teksti.lower().split()) == KOMENTO


def kasittele_telegram(viesti, sallittu_chat_id, asiakas=None, polku=None, nyt=None):
    """Telegram-lukijan kytkentapiste. Palauttaa raportin, jos pysaytettiin, muuten None."""
    if not on_seis_komento(viesti, sallittu_chat_id):
        return None
    return pysayta("telegram", asiakas, polku, nyt)


def _ei_toimeksiantoja():
    """Hatapysaytyksen asiakas vain peruu. Jos se joskus yrittaisi uutta toimeksiantoa, se kielletaan aina."""
    raise Pysaytetty("hatapysaytyksen asiakas ei laheta toimeksiantoja")


# Paluukoodit: 0 = pysaytys onnistui kokonaan. Nollasta poikkeava aina, kun jokin osa jai tekematta.
OK, PERUUTUS_KESKEN, AVAIN_HYLATTY, TIEDOSTO_EPAONNISTUI = 0, 1, 2, 3


def main(argv=None):
    ap = argparse.ArgumentParser(description="Hatapysaytys: pysaytystiedosto ja (avaimella) avointen peruutus.")
    ap.add_argument("--syy", default="kasin")
    ap.add_argument("--polku", default=None)
    ap.add_argument("--avain", default=None, help="avaintiedosto; jos annettu, peruu myos avoimet toimeksiannot")
    a = ap.parse_args(argv)
    asiakas, avainvirhe = None, None
    if a.avain:
        import avaintarkistus   # myohaisesti: avaintarkistus tuo taman moduulin
        import porssi
        try:
            k, s = avaintarkistus.lue_avain(a.avain)
            asiakas = porssi.Asiakas(k, s, pysaytys_tarkistus=_ei_toimeksiantoja)
            del k, s
        except avaintarkistus.Kieltaytyy as e:
            avainvirhe = str(e)   # Kieltaytyy-viesti ei koskaan sisalla avaimen arvoa
        except Exception as e:
            avainvirhe = type(e).__name__
    # Tiedosto kirjoitetaan aina, vaikka avain hylattaisiin: uudet kaupat estyvat silti.
    try:
        r = pysayta(a.syy, asiakas, a.polku)
    except Exception as e:
        print("VIRHE: pysaytystiedostoa ei voitu kirjoittaa (%s). Pysaytys EI ole paalla." % type(e).__name__)
        return TIEDOSTO_EPAONNISTUI
    print("PYSAYTETTY: %s (jatko: Tony poistaa tiedoston kasin)" % r["tiedosto"])
    if avainvirhe is not None:
        print("VIRHE: avainta ei voitu kayttaa, avoimia toimeksiantoja EI peruttu: %s" % avainvirhe)
        return AVAIN_HYLATTY
    if asiakas is not None:
        print("peruttu: %s, jaljella: %s, virheet: %s" % (",".join(r["peruttu"]) or "-", r["jaljella"],
                                                         "; ".join(r["virheet"]) or "-"))
        if r["virheet"] or r["jaljella"] != 0:
            return PERUUTUS_KESKEN
    return OK


if __name__ == "__main__":
    sys.exit(main())
