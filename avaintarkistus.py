"""Oikean rahan kaynnistystarkistus: avaintiedosto ja avaimen oikeudet. Fail closed: epaselva = kielto.

Kaytto (Tony, kun avain on luotu; EI tee kauppoja, lukee vain avaimen oikeudet):
    python3 -m avaintarkistus --avain /srv/kauppa/salaiset/binance.key

Avaintiedosto (oikeudet 600, omistaja ajava kayttaja, ei git-tyopuun sisalla, ei symlinkki):
    BINANCE_API_KEY=...
    BINANCE_API_SECRET=...

Ohjelma kieltaytyy kaynnistymasta, jos:
  - hatapysaytystiedosto on olemassa
  - avaintiedoston oikeudet eivat ole 600, omistaja on vaara, se on symlinkki tai git-tyopuun sisalla
  - avaimella on nosto-oikeus tai siirto-oikeus (sisainen tai universal transfer)
  - avainta ei ole rajattu IP-osoitteisiin
  - avaimella on margin-, futuuri- tai optio-oikeus (vain spot sallittu)
  - oikeuksia ei saada luettua tai jokin kentta puuttuu
Avaimen arvoa ei koskaan tulosteta. Virheista vain tyyppi ja HTTP-tila.
"""
import argparse
import os
import stat
import sys

import hatapysaytys
import porssi

# Kentta -> vaadittu arvo. Puuttuva kentta tai muu kuin bool = kielto.
VAADITUT = {
    "enableWithdrawals": False,            # nosto-oikeus: ehdoton kielto
    "enableInternalTransfer": False,       # siirto toiselle tilille
    "permitsUniversalTransfer": False,     # siirto tilityyppien valilla
    "ipRestrict": True,                    # avain toimii vain sallituista IP-osoitteista
    "enableMargin": False,
    "enableFutures": False,
    "enableVanillaOptions": False,
    "enablePortfolioMarginTrading": False,
    "enableReading": True,
}
# Valinnaiset: jos kentta puuttuu vastauksesta, sita ei vaadita (Binance lisaa kenttia vaiheittain).
VALINNAISET = {"enablePortfolioMarginTrading", "enableVanillaOptions"}


class Kieltaytyy(Exception):
    """Kaynnistys kielletty. Viesti ei koskaan sisalla avaimen arvoa."""


def _git_tyopuussa(polku):
    d = os.path.dirname(os.path.abspath(polku))
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return True
        ylempi = os.path.dirname(d)
        if ylempi == d:
            return False
        d = ylempi


def lue_avain(polku):
    """Palauttaa (avain, salaisuus). Tarkistaa tiedoston oikeudet ennen lukua."""
    try:
        st = os.lstat(polku)
    except OSError as e:
        raise Kieltaytyy("avaintiedostoa ei voi lukea (%s)" % type(e).__name__) from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise Kieltaytyy("avaintiedosto ei ole tavallinen tiedosto")
    if stat.S_IMODE(st.st_mode) != 0o600:
        raise Kieltaytyy("avaintiedoston oikeudet %o, vaaditaan 600" % stat.S_IMODE(st.st_mode))
    if st.st_uid != os.geteuid():
        raise Kieltaytyy("avaintiedoston omistaja ei ole ajava kayttaja")
    if _git_tyopuussa(polku):
        raise Kieltaytyy("avaintiedosto on git-tyopuun sisalla (avain ei koskaan repoon)")
    arvot = {}
    try:
        fd = os.open(polku, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, encoding="utf-8") as f:
            for rivi in f:
                rivi = rivi.strip()
                if not rivi or rivi.startswith("#") or "=" not in rivi:
                    continue
                k, v = rivi.split("=", 1)
                arvot[k.strip()] = v.strip()
    except (OSError, UnicodeDecodeError) as e:
        raise Kieltaytyy("avaintiedostoa ei voi lukea (%s)" % type(e).__name__) from None
    avain, salaisuus = arvot.get("BINANCE_API_KEY"), arvot.get("BINANCE_API_SECRET")
    if not avain or not salaisuus:
        raise Kieltaytyy("avaintiedostosta puuttuu BINANCE_API_KEY tai BINANCE_API_SECRET")
    return avain, salaisuus


def tarkista_rajoitukset(rajoitukset):
    """Palauttaa listan rikkeista (tyhja = ok). Vain kenttien nimet, ei arvoja avaimesta."""
    if not isinstance(rajoitukset, dict):
        return ["oikeuksia ei saatu (vastaus ei ole objekti)"]
    rikkeet = []
    for kentta, vaadittu in VAADITUT.items():
        if kentta not in rajoitukset:
            if kentta not in VALINNAISET:
                rikkeet.append("%s puuttuu" % kentta)
            continue
        arvo = rajoitukset[kentta]
        if not isinstance(arvo, bool):
            rikkeet.append("%s ei ole tosi/epatosi" % kentta)
        elif arvo != vaadittu:
            rikkeet.append("%s=%s, vaaditaan %s" % (kentta, arvo, vaadittu))
    return rikkeet


def kaynnistys(avainpolku, pysaytyspolku=None, asiakas_tehdas=None):
    """Kaikki tarkistukset jarjestyksessa. Palauttaa valmiin Asiakkaan tai nostaa Kieltaytyy.

    asiakas_tehdas(avain, salaisuus, pysaytys_tarkistus) -> Asiakas (testeissa feikki-API)."""
    pp = pysaytyspolku or hatapysaytys.oletuspolku()
    if hatapysaytys.pysaytetty(pp):
        raise Kieltaytyy("hatapysaytys paalla (%s)" % pp)
    avain, salaisuus = lue_avain(avainpolku)

    def tarkistin():
        hatapysaytys.varmista_ei_pysaytetty(pp)

    tehdas = asiakas_tehdas or (lambda a, s, t: porssi.Asiakas(a, s, pysaytys_tarkistus=t))
    asiakas = tehdas(avain, salaisuus, tarkistin)
    del avain, salaisuus
    try:
        rajoitukset = asiakas.avaimen_rajoitukset()
    except porssi.PorssiVirhe as e:
        raise Kieltaytyy("avaimen oikeuksia ei voitu lukea (HTTP %s %s)" % (e.tila, e.tyyppi)) from None
    rikkeet = tarkista_rajoitukset(rajoitukset)
    if rikkeet:
        raise Kieltaytyy("avaimen oikeudet eivat kelpaa: " + "; ".join(rikkeet))
    return asiakas


def main(argv=None):
    ap = argparse.ArgumentParser(description="Tarkistaa oikean rahan avaimen. Ei tee kauppoja.")
    ap.add_argument("--avain", required=True, help="avaintiedoston polku (oikeudet 600)")
    ap.add_argument("--pysaytys", default=None)
    a = ap.parse_args(argv)
    try:
        kaynnistys(a.avain, a.pysaytys)
    except Kieltaytyy as e:
        print("KIELLETTY:", e)
        return 2
    print("OK: avain kelpaa (ei nosto- eika siirto-oikeutta, IP-rajattu, vain spot). Kauppoja ei tehty.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
