# -*- coding: utf-8 -*-
"""Video uretmek icin dis siteler: ucretsiz baslangic katalogu.

KURAL: Burada yalnizca resmi fiyat sayfasinda ACIKCA yazan seyler vardir. Sayfada yazmayan
bir sinir 'belirtilmemis' diye isaretlenir, tahmin edilmez. Fiyatlar sik degisir; her kayit
kaynak sayfa ve dogrulama tarihi tasir. Maliyet ve kullanim sartlari ilgili siteye ve kullaniciya aittir.
"""
from __future__ import annotations

from urllib.parse import urlparse

DOGRULAMA_TARIHI = "2026-10-25"

# ucretsiz: "var" (net ucretsiz), "kisitli" (ucretsiz ama ciddi sinirli), "yok" (dogrulandi: ucretsiz degil),
#           "bilinmiyor" (resmi sayfa okunamadi / yazmiyor)
UCRETSIZ_DEGERLERI = ("var", "kisitli", "yok", "bilinmiyor")
TURLER = ("editor", "ai_video", "avatar", "ses")

SITELER: list[dict] = [
    {
        "ad": "Clipchamp", "tur": "editor", "ucretsiz": "var",
        "adres": "https://clipchamp.com/",
        "ozet": "Tarayıcıda çalışan video düzenleyici. Sayfaya göre 1080p'ye kadar dışa aktarma ücretsiz, filigran yok, indirme gerekmiyor.",
        "belirtilmemis": "Süre sınırı, yapay ses sayısı ve editörün Türkçe desteği sayfada yazmıyor.",
        "kaynak": "https://clipchamp.com/en/pricing/",
    },
    {
        "ad": "CapCut", "tur": "editor", "ucretsiz": "var",
        "adres": "https://www.capcut.com/",
        "ozet": "Ücretsiz plan var: temel düzenleme araçları ve ücretsiz şablonlar. Yapay zeka özellikleri ve kredi ücretli planlarda listeleniyor.",
        "belirtilmemis": "Ücretsiz plandaki çözünürlük, filigran ve süre sınırı sayfada yazmıyor; Türkçe ses/arayüz desteği belirtilmemiş.",
        "kaynak": "https://www.capcut.com/pricing",
    },
    {
        "ad": "VEED", "tur": "editor", "ucretsiz": "var",
        "adres": "https://www.veed.io/",
        "ozet": "Ücretsiz bir plan sunuyor (\"Start for free\").",
        "belirtilmemis": "Ücretsiz plandaki çözünürlük, filigran, süre ve altyazı sınırları sayfada yazmıyor; otomatik altyazı ücretli planlarda görünüyor.",
        "kaynak": "https://www.veed.io/pricing",
    },
    {
        "ad": "Runway", "tur": "ai_video", "ucretsiz": "kisitli",
        "adres": "https://runway.com/",
        "ozet": "Yapay zeka video üretimi. Ücretsiz planda 125 kredi var ve bu TEK SEFERLİK (aylık yenilenmez, süresi dolmaz).",
        "belirtilmemis": "Filigran, çözünürlük, azami süre ve hangi modellerin kullanılabildiği sayfada yazmıyor.",
        "kaynak": "https://runway.com/pricing",
    },
    {
        "ad": "HeyGen", "tur": "avatar", "ucretsiz": "kisitli",
        "adres": "https://www.heygen.com/",
        "ozet": "Konuşan avatar videoları. Ücretsiz plan: ayda 3 video, her biri en fazla 1 dakika, FİLİGRANLI. 30'dan fazla dil.",
        "belirtilmemis": "Türkçe ayrıca belirtilmemiş; ücretsiz dışa aktarma çözünürlüğü yazmıyor.",
        "kaynak": "https://www.heygen.com/pricing",
    },
    {
        "ad": "ElevenLabs", "tur": "ses", "ucretsiz": "kisitli",
        "adres": "https://elevenlabs.io/",
        "ozet": "Yapay zeka seslendirme. Ücretsiz planda ayda 10.000 kredi.",
        "belirtilmemis": "Ücretsiz planda ticari kullanım hakkı ve Türkçe desteği sayfada açıkça yazmıyor; kullanmadan önce lisansı okuyun.",
        "kaynak": "https://elevenlabs.io/pricing",
    },
    {
        "ad": "Luma Dream Machine", "tur": "ai_video", "ucretsiz": "yok",
        "adres": "https://lumalabs.ai/",
        "ozet": "Fiyat sayfasında ücretsiz plan görünmüyor, yalnızca ücretli planlar listeleniyor.",
        "belirtilmemis": "",
        "kaynak": "https://lumalabs.ai/pricing",
    },
    {
        "ad": "Pika", "tur": "ai_video", "ucretsiz": "yok",
        "adres": "https://pika.art/",
        "ozet": "\"Free\" plan var ama ayda 0 kredi içeriyor: üretim için kredi paketi satın almak gerekiyor.",
        "belirtilmemis": "",
        "kaynak": "https://pika.art/pricing",
    },
    {
        "ad": "InVideo", "tur": "editor", "ucretsiz": "yok",
        "adres": "https://www.invideo.io/",
        "ozet": "Fiyat sayfasında yalnızca Basic, Pro ve Ultra planları görünüyor; ücretsiz plan yok.",
        "belirtilmemis": "",
        "kaynak": "https://www.invideo.io/pricing",
    },
    {
        "ad": "Kling AI", "tur": "ai_video", "ucretsiz": "bilinmiyor",
        "adres": "https://kling.ai/",
        "ozet": "Yapay zeka video üretimi.",
        "belirtilmemis": "Ücretsiz kredi olup olmadığı resmî sayfada doğrulanamadı. Siteyi kendiniz kontrol edin.",
        "kaynak": "https://kling.ai/",
    },
    {
        "ad": "Canva", "tur": "editor", "ucretsiz": "bilinmiyor",
        "adres": "https://www.canva.com/",
        "ozet": "Tasarım ve video düzenleyici.",
        "belirtilmemis": "Fiyat sayfası otomatik okunamadı (erişim engellendi); ücretsiz plan sınırları doğrulanamadı. Siteyi kendiniz kontrol edin.",
        "kaynak": "https://www.canva.com/pricing/",
    },
]


def _https_adres_mi(adres: str) -> bool:
    ayrik = urlparse(adres)
    return ayrik.scheme == "https" and bool(ayrik.hostname) and not ayrik.username


def gecerli_mi(site: dict) -> list[str]:
    """Katalog kaydindaki kusurlari dondurur (bos liste = gecerli)."""
    sorunlar = []
    for alan in ("ad", "tur", "ucretsiz", "adres", "ozet", "kaynak"):
        if not site.get(alan):
            sorunlar.append(f"{site.get('ad', '?')}: '{alan}' bos")
    if site.get("tur") not in TURLER:
        sorunlar.append(f"{site.get('ad')}: gecersiz tur")
    if site.get("ucretsiz") not in UCRETSIZ_DEGERLERI:
        sorunlar.append(f"{site.get('ad')}: gecersiz ucretsiz degeri")
    for alan in ("adres", "kaynak"):
        if site.get(alan) and not _https_adres_mi(site[alan]):
            sorunlar.append(f"{site.get('ad')}: '{alan}' https degil")
    return sorunlar


def katalog() -> dict:
    """Arayuze verilen bicim: gruplanmis liste + uyari metni."""
    def grup(*degerler):
        return [dict(s, dogrulama=DOGRULAMA_TARIHI) for s in SITELER if s["ucretsiz"] in degerler]

    return {
        "dogrulama_tarihi": DOGRULAMA_TARIHI,
        "uyari": (
            "Ücretsiz katmanlar ve fiyatlar sık değişir. Kayıtlar yalnızca resmî fiyat sayfasında açıkça yazanlara dayanır; "
            "yazmayan bir sınır 'belirtilmemiş' diye işaretlidir. Kayıt olmadan önce siteyi kontrol edin. "
            "Kullanım şartları ve olası maliyet ilgili siteye ve size aittir. Yapay zeka ile üretilmiş içerik paylaşırken "
            "Instagram'ın güncel yapay zeka etiketi kurallarını paylaşmadan önce kontrol edin."
        ),
        "gruplar": [
            {"anahtar": "ucretsiz", "baslik": "Ücretsiz başlangıç", "siteler": grup("var", "kisitli")},
            {"anahtar": "yok", "baslik": "Artık ücretsiz değil (doğrulandı)", "siteler": grup("yok")},
            {"anahtar": "bilinmiyor", "baslik": "Doğrulanamadı", "siteler": grup("bilinmiyor")},
        ],
    }
