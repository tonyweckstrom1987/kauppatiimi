"""Minimaalinen Binance spot -asiakas oikean rahan suojille (vain stdlib, HMAC-SHA256-allekirjoitus).

EI OLE KAYTOSSA: mikaan ajastin tai salkkuajo ei kayta tata. Oikean rahan kaupankaynnin aloitus on Tonyn
erillinen paatos (kompassi: kauppatiimin portit 1-5).

Turva:
  - avainta tai salaisuutta ei koskaan tulosteta, kirjata eika laiteta poikkeuksen viestiin
  - virheista vain HTTP-tila ja tyypin nimi (Binancen virheviesti voi sisaltaa pyynnon tietoja)
  - jokainen toimeksianto tarkistaa hatapysaytystiedoston juuri ennen lahetysta (fail closed)
  - kuljetus (hae) on injektoitava, joten testit ajetaan feikki-API:a vastaan ilman verkkoa
"""
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

PAAVERKKO = "https://api.binance.com"
TESTIVERKKO = "https://testnet.binance.vision"   # HUOM: testiverkossa ei ole /sapi-polkuja (avaimen oikeustarkistus)
AIKAKATKAISU = 15
RECV_WINDOW = 5000


class PorssiVirhe(Exception):
    """Viesti sisaltaa vain polun, HTTP-tilan ja tyypin nimen, ei koskaan avainta eika vastauksen tekstia."""

    def __init__(self, polku, tila=None, tyyppi=None):
        self.polku, self.tila, self.tyyppi = polku, tila, tyyppi
        super().__init__("porssivirhe %s: HTTP %s %s" % (polku, tila, tyyppi or ""))


def _urllib_hae(metodi, url, otsikot, runko):
    """Oletuskuljetus. Palauttaa (http_tila, json-objekti)."""
    pyynto = urllib.request.Request(url, data=runko, headers=otsikot, method=metodi)
    try:
        with urllib.request.urlopen(pyynto, timeout=AIKAKATKAISU) as v:
            return v.status, json.loads(v.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None


class Asiakas:
    def __init__(self, avain, salaisuus, perus=PAAVERKKO, hae=None, kello=None, pysaytys_tarkistus=None):
        if not avain or not salaisuus:
            raise ValueError("avain puuttuu")
        self._avain = avain
        self._salaisuus = salaisuus.encode()
        self.perus = perus.rstrip("/")
        self._hae = hae or _urllib_hae
        self._kello = kello or (lambda: int(time.time() * 1000))
        # Kutsutaan ennen jokaista toimeksiantoa; nostaa poikkeuksen, jos kaupankaynti on pysaytetty.
        self._pysaytys_tarkistus = pysaytys_tarkistus

    def __repr__(self):
        return "Asiakas(perus=%r, avain=***)" % self.perus

    def _allekirjoitettu(self, metodi, polku, parametrit=None):
        p = dict(parametrit or {})
        p["timestamp"] = self._kello()
        p["recvWindow"] = RECV_WINDOW
        kysely = urllib.parse.urlencode(p)
        allek = hmac.new(self._salaisuus, kysely.encode(), hashlib.sha256).hexdigest()
        url = "%s%s?%s&signature=%s" % (self.perus, polku, kysely, allek)
        try:
            tila, data = self._hae(metodi, url, {"X-MBX-APIKEY": self._avain}, None)
        except Exception as e:
            raise PorssiVirhe(polku, None, type(e).__name__) from None
        if tila != 200 or data is None:
            raise PorssiVirhe(polku, tila, "vastaus")
        return data

    # --- luku ---
    def avaimen_rajoitukset(self):
        """GET /sapi/v1/account/apiRestrictions: avaimen oikeudet (nosto, IP-rajaus, siirrot)."""
        return self._allekirjoitettu("GET", "/sapi/v1/account/apiRestrictions")

    def avoimet_toimeksiannot(self, symboli=None):
        return self._allekirjoitettu("GET", "/api/v3/openOrders", {"symbol": symboli} if symboli else None)

    def kaupat(self, symboli, alkaen_id=None):
        p = {"symbol": symboli}
        if alkaen_id is not None:
            p["fromId"] = alkaen_id
        return self._allekirjoitettu("GET", "/api/v3/myTrades", p)

    # --- muutokset ---
    def peru_kaikki(self, symboli):
        return self._allekirjoitettu("DELETE", "/api/v3/openOrders", {"symbol": symboli})

    def uusi_toimeksianto(self, symboli, puoli, tyyppi, **parametrit):
        """Lahettaa toimeksiannon. Hatapysaytys tarkistetaan AINA ensin; ilman tarkistinta ei laheteta."""
        if self._pysaytys_tarkistus is None:
            raise PorssiVirhe("/api/v3/order", None, "ei hatapysaytyksen tarkistinta")
        self._pysaytys_tarkistus()
        p = {"symbol": symboli, "side": puoli, "type": tyyppi}
        p.update(parametrit)
        return self._allekirjoitettu("POST", "/api/v3/order", p)
