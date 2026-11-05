"""Oikean rahan verokirjanpito: jokainen kauppa yhdeksi CSV-riviksi yksityiseen paikkaan.

Rivi: aika (UTC), pari, puoli, maara, hinta, kulu, euromaarainen arvo (+ kurssit ja tunnisteet jaljitettavyytta varten).
Rahamaarat Decimal-tyyppina (ei liukulukuvirheita).

Yksityisyys (fail closed):
  - tiedosto ei saa olla git-tyopuun sisalla (ei koskaan julkiseen repoon)
  - hakemisto luodaan oikeuksilla 700 ja tiedosto 600; jos olemassa olevan tiedoston oikeudet ovat
    laajemmat, kirjaus kieltaytyy
  - sama kauppa (porssi + kauppa_id) kirjataan vain kerran

EUR-kurssi annetaan funktiona eur_hinta(valuutta, aika_ms) -> Decimal (1 yksikko valuuttaa euroina kaupan hetkella).
Kurssin lahde on kutsujan vastuulla (esim. Binancen EURUSDT-kynttila tai EKP:n kurssi); se kirjataan riville.
"""
import csv
import datetime
import os
import re
import stat
from decimal import Decimal, InvalidOperation

OLETUSPOLKU = "/srv/kauppa/yksityinen/verokirjanpito.csv"


def oletuspolku():
    """Ymparistomuuttuja KAUPPA_VEROKIRJA luetaan kutsuhetkella, ei moduulin latausvaiheessa."""
    return os.environ.get("KAUPPA_VEROKIRJA") or OLETUSPOLKU
SARAKKEET = [
    "aika_utc", "porssi", "pari", "puoli", "maara", "hinta", "hinta_valuutta", "arvo",
    "kulu", "kulu_valuutta", "eur_kurssi", "arvo_eur", "kulu_eur", "kauppa_id", "toimeksianto_id",
]
# Pisimmat ensin, jotta esim. FDUSD ei tulkitu USD:ksi.
NOTEERAUKSET = ("FDUSD", "USDT", "USDC", "EUR", "BTC", "ETH", "BNB")
SYMBOLI_RE = re.compile(r"^[A-Z0-9]{2,20}$")


class Kirjausvirhe(Exception):
    pass


def jaa_pari(symboli):
    if not isinstance(symboli, str) or not SYMBOLI_RE.match(symboli):
        raise Kirjausvirhe("virheellinen pari")
    for q in NOTEERAUKSET:
        if symboli.endswith(q) and len(symboli) > len(q):
            return symboli[: -len(q)], q
    raise Kirjausvirhe("tuntematon noteerausvaluutta: %s" % symboli)


def _dec(arvo, nimi):
    try:
        d = Decimal(str(arvo))
    except (InvalidOperation, ValueError):
        raise Kirjausvirhe("virheellinen luku: %s" % nimi) from None
    if not d.is_finite() or d < 0:
        raise Kirjausvirhe("virheellinen luku: %s" % nimi)
    return d


def _git_tyopuussa(polku):
    d = os.path.dirname(os.path.abspath(polku))
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return True
        ylempi = os.path.dirname(d)
        if ylempi == d:
            return False
        d = ylempi


def rivi_binance_kaupasta(kauppa, eur_hinta, porssi="binance"):
    """kauppa: Binancen /api/v3/myTrades-alkio. Palauttaa sanakirjan SARAKKEET-avaimin."""
    if not isinstance(kauppa, dict):
        raise Kirjausvirhe("kauppa ei ole objekti")
    _, quote = jaa_pari(kauppa.get("symbol"))
    maara = _dec(kauppa.get("qty"), "qty")
    hinta = _dec(kauppa.get("price"), "price")
    kulu = _dec(kauppa.get("commission"), "commission")
    kulu_val = kauppa.get("commissionAsset")
    if not isinstance(kulu_val, str) or not SYMBOLI_RE.match(kulu_val):
        raise Kirjausvirhe("virheellinen kuluvaluutta")
    aika_ms = kauppa.get("time")
    if isinstance(aika_ms, bool) or not isinstance(aika_ms, int) or aika_ms <= 0:
        raise Kirjausvirhe("virheellinen aika")
    if not isinstance(kauppa.get("isBuyer"), bool):
        raise Kirjausvirhe("puoli puuttuu")
    for k in ("id", "orderId"):
        if isinstance(kauppa.get(k), bool) or not isinstance(kauppa.get(k), int):
            raise Kirjausvirhe("virheellinen %s" % k)
    arvo = kauppa.get("quoteQty")
    arvo = _dec(arvo, "quoteQty") if arvo is not None else maara * hinta
    kurssi = _dec(eur_hinta(quote, aika_ms), "eur_kurssi " + quote)
    kulu_kurssi = kurssi if kulu_val == quote else _dec(eur_hinta(kulu_val, aika_ms), "eur_kurssi " + kulu_val)
    if kurssi == 0 or kulu_kurssi == 0:
        raise Kirjausvirhe("eur-kurssi nolla")
    aika = datetime.datetime.fromtimestamp(aika_ms / 1000, datetime.timezone.utc)
    return {
        "aika_utc": aika.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (aika_ms % 1000),
        "porssi": porssi,
        "pari": kauppa["symbol"],
        "puoli": "BUY" if kauppa["isBuyer"] else "SELL",
        "maara": str(maara),
        "hinta": str(hinta),
        "hinta_valuutta": quote,
        "arvo": str(arvo),
        "kulu": str(kulu),
        "kulu_valuutta": kulu_val,
        "eur_kurssi": str(kurssi),
        "arvo_eur": str((arvo * kurssi).quantize(Decimal("0.01"))),
        "kulu_eur": str((kulu * kulu_kurssi).quantize(Decimal("0.0001"))),
        "kauppa_id": str(kauppa["id"]),
        "toimeksianto_id": str(kauppa["orderId"]),
    }


class Verokirja:
    def __init__(self, polku=None):
        self.polku = polku or oletuspolku()
        if _git_tyopuussa(self.polku):
            raise Kirjausvirhe("verokirjanpito ei saa olla git-tyopuun sisalla")

    def _valmistele(self):
        hakemisto = os.path.dirname(os.path.abspath(self.polku))
        if not os.path.isdir(hakemisto):
            os.makedirs(hakemisto, mode=0o700)
        if os.path.lexists(self.polku):
            st = os.lstat(self.polku)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                raise Kirjausvirhe("verokirjanpito ei ole tavallinen tiedosto")
            if stat.S_IMODE(st.st_mode) & 0o077:
                raise Kirjausvirhe("verokirjanpidon oikeudet %o, vaaditaan 600" % stat.S_IMODE(st.st_mode))
            return False
        fd = os.open(self.polku, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", newline="") as f:
            csv.writer(f).writerow(SARAKKEET)
        return True

    def kirjatut(self):
        if not os.path.exists(self.polku):
            return set()
        with open(self.polku, newline="") as f:
            return {(r["porssi"], r["kauppa_id"]) for r in csv.DictReader(f)}

    def kirjaa(self, rivi):
        """Lisaa rivin, jos kauppaa ei ole jo kirjattu. Palauttaa True, jos lisattiin."""
        if set(rivi) != set(SARAKKEET):
            raise Kirjausvirhe("rivin sarakkeet eivat tasmaa")
        self._valmistele()
        if (rivi["porssi"], rivi["kauppa_id"]) in self.kirjatut():
            return False
        with open(self.polku, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=SARAKKEET).writerow(rivi)
            f.flush()
            os.fsync(f.fileno())
        return True

    def kirjaa_binance(self, kaupat, eur_hinta):
        """Kirjaa listan Binancen kauppoja aikajarjestyksessa. Palauttaa lisattyjen maaran."""
        rivit = [rivi_binance_kaupasta(k, eur_hinta) for k in kaupat]   # kaikki validoidaan ensin
        rivit.sort(key=lambda r: (r["aika_utc"], int(r["kauppa_id"])))
        return sum(1 for r in rivit if self.kirjaa(r))

    def fifo_syote(self):
        """Rivit kirjanpito.fifo-muotoon euroina: (paiva, kohde, puoli, maara, hinta_eur, kulu_eur).
        Ryhmittely kohdevaluutan mukaan (BTCUSDT ja BTCUSDC ovat samaa BTC:ta). HUOM: BNB:lla maksettu kulu on
        verotuksessa myos BNB:n luovutus; sita ei viela kirjata erikseen (ks. OIKEA-RAHA.md, avoimet)."""
        ulos = []
        with open(self.polku, newline="") as f:
            for r in csv.DictReader(f):
                maara = Decimal(r["maara"])
                if maara <= 0:
                    raise Kirjausvirhe("maara nolla rivilla %s" % r["kauppa_id"])
                # Pyoristamaton arvo (arvo * kurssi), ei senttiin pyoristetty arvo_eur: pienissa kaupoissa
                # sentin pyoristys jaettuna maaralla vaaristaisi yksikkohintaa.
                hinta_eur = Decimal(r["arvo"]) * Decimal(r["eur_kurssi"]) / maara
                ulos.append((r["aika_utc"][:10], jaa_pari(r["pari"])[0], r["puoli"], float(maara), float(hinta_eur),
                             float(Decimal(r["kulu_eur"]))))
        return ulos
