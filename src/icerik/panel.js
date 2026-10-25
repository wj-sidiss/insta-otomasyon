const $ = (s) => document.querySelector(s);
const el = (etiket, sinif, metin) => {
  const e = document.createElement(etiket);
  if (sinif) e.className = sinif;
  if (metin !== undefined && metin !== null) e.textContent = metin;
  return e;
};
let aktifPaket = null, seciliBaslik = 0;

async function api(yol, secenek = {}) {
  const baslik = secenek.body ? { 'Content-Type': 'application/json' } : {};
  const yanit = await fetch(yol, { ...secenek, headers: { ...baslik, ...(secenek.headers || {}) } });
  let veri = null;
  try { veri = await yanit.json(); } catch (_) {}
  if (!yanit.ok) {
    const d = veri && (veri.detail || veri.hata);
    throw new Error(typeof d === 'string' ? d : (Array.isArray(d) ? 'Geçersiz girdi.' : 'İstek başarısız (' + yanit.status + ').'));
  }
  return veri;
}

function kopyalaDugmesi(metinAl, yazi = 'Kopyala') {
  const d = el('button', 'kucuk', yazi);
  d.onclick = async () => {
    try { await navigator.clipboard.writeText(metinAl()); d.textContent = 'Kopyalandı'; d.classList.add('tamam'); }
    catch (_) { d.textContent = 'Kopyalanamadı'; }
    setTimeout(() => { d.textContent = yazi; d.classList.remove('tamam'); }, 1400);
  };
  return d;
}

async function durumYukle() {
  const hap = $('#motorHap'), sec = $('#motor');
  try {
    const d = await api('/api/durum');
    sec.replaceChildren();
    const oto = el('option', null, 'Otomatik' + (d.varsayilan ? ' (' + d.varsayilan + ')' : ''));
    oto.value = 'auto'; sec.append(oto);
    d.saglayicilar.filter(s => s.ad !== 'ornek_dosya' || s.hazir).forEach(s => {
      const o = el('option', null, s.aciklama + (s.hazir ? '' : ' — ' + s.neden));
      o.value = s.ad; o.disabled = !s.hazir; sec.append(o);
    });
    if (d.varsayilan) { hap.textContent = 'Yazı motoru: ' + d.varsayilan; hap.className = 'hap iyi'; }
    else { hap.textContent = 'Yazı motoru bulunamadı'; hap.className = 'hap kotu'; }
  } catch (h) { hap.textContent = 'Panele ulaşılamadı'; hap.className = 'hap kotu'; }
}

function adimGoster(adimlar, bitti) {
  const kap = $('#adimlar'); kap.replaceChildren();
  adimlar.forEach((a, i) => {
    const son = i === adimlar.length - 1;
    const satir = el('div', 'adim ' + (son && !bitti ? 'suren' : 'bitti'));
    satir.append(el('span', 'nokta'), el('span', null, a.mesaj));
    kap.append(satir);
  });
}

async function uret() {
  const dugme = $('#uret'), hata = $('#hata');
  hata.classList.add('hidden');
  const govde = { konu: $('#konu').value.trim(), dil: $('#dil').value, ton: $('#ton').value, saglayici: $('#motor').value };
  if (govde.konu.length < 3) { hata.textContent = 'Konu en az 3 karakter olmalı.'; hata.classList.remove('hidden'); return; }
  dugme.disabled = true; dugme.textContent = 'Üretiliyor…';
  $('#ilerleKart').classList.remove('hidden'); $('#adimlar').replaceChildren();
  try {
    const { is_id } = await api('/api/uret', { method: 'POST', body: JSON.stringify(govde) });
    for (;;) {
      await new Promise(r => setTimeout(r, 700));
      const d = await api('/api/is/' + is_id);
      adimGoster(d.adimlar, d.durum !== 'calisiyor');
      if (d.durum === 'bitti') { await paketAc(d.paket_id); await gecmisYukle(); break; }
      if (d.durum === 'hata') { hata.textContent = d.hata || 'Bilinmeyen hata.'; hata.classList.remove('hidden'); break; }
    }
  } catch (h) { hata.textContent = h.message; hata.classList.remove('hidden'); }
  dugme.disabled = false; dugme.textContent = 'Paketi üret';
}

function blok(baslik, ...cocuklar) {
  const b = el('div', 'blok'), h = el('h3'); h.append(el('span', null, baslik));
  b.append(h); cocuklar.forEach(c => c && b.append(c)); return { b, h };
}

function tumMetin(p) {
  const et = p.hashtagler.slice(0, 10).map(h => h.etiket).join(' ');
  return 'BAŞLIK\n' + p.basliklar[seciliBaslik] + '\n\nCAPTION\n' + p.caption + '\n\nHASHTAG\n' + et +
         '\n\nSEO ANAHTAR KELİMELER\n' + p.seo.anahtar_kelimeler.join(', ') + '\n';
}

function paketCiz(p) {
  aktifPaket = p; seciliBaslik = p.secili_baslik || 0;
  $('#bos').classList.add('hidden');
  const kok = $('#sonuc'); kok.classList.remove('hidden'); kok.replaceChildren();

  p.uyarilar.forEach(u => kok.append(el('div', 'uyari', u)));

  const ust = el('div', 'blok'); ust.style.display = 'flex'; ust.style.justifyContent = 'space-between'; ust.style.alignItems = 'center';
  const k = el('div'); k.append(el('div', null, p.konu)); k.firstChild.style.fontWeight = '700'; k.firstChild.style.fontSize = '17px';
  const yan = el('div', 'not', new Date(p.olusturma).toLocaleString('tr-TR') + ' · ' + p.saglayici); k.append(yan);
  const dugmeler = el('div'); dugmeler.style.display = 'flex'; dugmeler.style.gap = '8px';
  dugmeler.append(kopyalaDugmesi(() => tumMetin(p), 'Tümünü kopyala'));
  const indir = el('a'); indir.href = '/paketler/' + p.id + '/kopyala.txt'; indir.append(el('button', 'kucuk', 'İndir (.txt)'));
  dugmeler.append(indir); ust.append(k, dugmeler); kok.append(ust);

  const izgara = el('div', 'sonuc');
  const sol = el('div', 'kapak');
  if (p.kapak.dosya) {
    const img = el('img'); img.alt = p.seo.alt_metin || 'Kapak'; img.src = '/paketler/' + p.id + '/kapak.jpg';
    const a = el('a'); a.href = img.src; a.download = 'kapak-' + p.id + '.jpg'; a.append(el('button', 'kucuk', 'Kapağı indir')); a.style.display = 'block'; a.style.marginTop = '10px';
    sol.append(img, a);
  } else sol.append(el('div', 'yok', 'Kapak çizilemedi (uyarılara bakın).'));
  const sag = el('div');

  const bas = el('div');
  p.basliklar.forEach((t, i) => {
    const satir = el('label', 'baslik' + (i === seciliBaslik ? ' secili' : '')); satir.style.margin = '0 0 8px';
    const r = el('input'); r.type = 'radio'; r.name = 'baslik'; r.checked = i === seciliBaslik;
    r.onchange = () => { seciliBaslik = i; bas.querySelectorAll('.baslik').forEach((x, j) => x.classList.toggle('secili', j === i)); };
    satir.append(r, el('span', null, t)); bas.append(satir);
  });
  const bBas = blok('Başlık', bas); bBas.h.append(kopyalaDugmesi(() => p.basliklar[seciliBaslik])); sag.append(bBas.b);

  const bCap = blok('Caption', el('div', 'metin', p.caption)); bCap.h.append(kopyalaDugmesi(() => p.caption)); sag.append(bCap.b);

  const et = el('div', 'etiketler');
  p.hashtagler.forEach(h => {
    const e = el('span', 'etiket' + (h.dogrulandi ? ' dogru' : '') + (h.oncelikli ? ' oncelik' : ''), h.etiket);
    e.title = (h.oncelikli ? 'Öncelikli · ' : '') + h.kaynak + (h.gerekce ? '\n' + h.gerekce : ''); et.append(e);
  });
  const dogruSay = p.hashtagler.filter(h => h.dogrulandi).length;
  const bEt = blok('Hashtag', et, el('div', 'not', 'Kalın: öncelikli ilk 5. Yeşil: gerçek Google/YouTube arama önerilerinde geçiyor (' + dogruSay + '/' + p.hashtagler.length + '). Bu bir hacim ya da popülerlik verisi değildir. Diğerleri model önerisi, doğrulanmadı. Üzerine gelince gerekçe görünür.'));
  bEt.h.append(kopyalaDugmesi(() => p.hashtagler.slice(0, 10).map(h => h.etiket).join(' '), 'İlk 10\'u kopyala')); sag.append(bEt.b);

  izgara.append(sol, sag); kok.append(izgara);

  const seoSatir = el('div', 'metin', p.seo.anahtar_kelimeler.join(' · ') + (p.seo.alt_metin ? '\n\nAlt metin: ' + p.seo.alt_metin : ''));
  const bSeo = blok('SEO', seoSatir); bSeo.h.append(kopyalaDugmesi(() => p.seo.anahtar_kelimeler.join(', ')));

  const an = el('div', 'analiz metin'); const etk = { aci: 'Açı', hedef_kitle: 'Hedef kitle', hook: 'İlk 3 saniye', neden_isler: 'Neden işe yarayabilir', riskler: 'Dikkat', trend_ozeti: 'Trend özeti' };
  Object.entries(etk).forEach(([k2, ad]) => { if (p.analiz[k2]) { const d = el('div'); d.append(el('b', null, ad), el('span', null, p.analiz[k2])); an.append(d); } });
  const bAn = blok('Analiz', an);

  const iki = el('div', 'iki'); iki.append(bSeo.b, bAn.b); kok.append(iki);

  if (p.senaryo.length) {
    const sen = el('div', 'metin');
    p.senaryo.forEach((sahne, i) => {
      const r = el('div', 'sahne'); r.append(el('div', 'sn', sahne.sure_sn ? sahne.sure_sn + ' sn' : '—'));
      const d = el('div'); d.append(el('span', null, sahne.anlatim));
      if (sahne.ekran_yazisi) d.append(el('em', null, 'Ekranda: ' + sahne.ekran_yazisi));
      d.append(sahneMedya(p, sahne, i + 1)); r.append(d); sen.append(r);
    });
    const bSen = blok('Senaryo ve sahne medyası', sen);
    bSen.h.append(kopyalaDugmesi(() => senaryoMetni(p), 'Metni kopyala'));
    const atifKap = el('div'); atifKap.id = 'atifKap'; kok.append(atifKap, bSen.b); atifGuncelle();
  }

  kok.append(carouselBlogu(p));
  kok.append(videoBlogu(p));

  if (p.trendler.length) {
    const tr = el('div', 'etiketler');
    p.trendler.forEach(t => { const e = el('span', 'etiket', t.kelime); e.title = t.kaynaklar.join(' + '); tr.append(e); });
    kok.append(blok('Trend arama ifadeleri (kaynak: Google / YouTube önerileri, hacim verisi yok)', tr).b);
  }
}

async function paketAc(id) { if (!kaynaklar.length) await kaynaklariYukle(); paketCiz(await api('/api/paket/' + id)); }

async function gecmisYukle() {
  const kap = $('#gecmis'); kap.replaceChildren();
  const liste = await api('/api/paketler').catch(() => []);
  if (!liste.length) { kap.append(el('div', 'bos', 'Henüz paket yok.')); return; }
  liste.forEach(o => {
    const s = el('div', 'g-oge'); const ad = el('div', 'ad');
    ad.append(el('div', null, o.konu), el('small', null, new Date(o.olusturma).toLocaleString('tr-TR')));
    const sil = el('button', 'tehlike', 'Sil');
    sil.onclick = async (ev) => {
      ev.stopPropagation();
      if (!confirm('Bu paket silinsin mi?')) return;
      await api('/api/paket/' + o.id, { method: 'DELETE' }).catch(() => {});
      if (aktifPaket && aktifPaket.id === o.id) { $('#sonuc').classList.add('hidden'); $('#bos').classList.remove('hidden'); }
      gecmisYukle();
    };
    s.onclick = () => paketAc(o.id); s.append(ad, sil); kap.append(s);
  });
}

let anahtarDurumu = {};

function disLink(href, metin, sinif) {
  let guvenli = false;
  try { guvenli = new URL(href).protocol === 'https:'; } catch (_) {}
  if (!guvenli) return el('span', sinif, metin);
  const a = el('a', sinif, metin); a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer'; return a;
}

function senaryoMetni(p) { return p.senaryo.map((s, i) => (i + 1) + '. ' + s.anlatim).join('\n'); }
function istemMetni(p) { return p.senaryo.filter(s => s.gorsel_istemi).map((s, i) => 'Sahne ' + (i + 1) + ': ' + s.gorsel_istemi).join('\n'); }

const ROZET = { var: ['Ücretsiz', 'iyi'], kisitli: ['Ücretsiz (sınırlı)', 'orta'], yok: ['Ücretsiz değil', 'kotu'], bilinmiyor: ['Doğrulanamadı', ''] };
const TUR_AD = { editor: 'Düzenleyici', ai_video: 'Yapay zeka video', avatar: 'Avatar', ses: 'Seslendirme' };

async function siteleriYukle() {
  const kap = $('#siteler');
  try {
    const k = await api('/api/video-siteleri');
    kap.replaceChildren();
    kap.append(el('div', 'uyari', k.uyari + ' Son doğrulama: ' + k.dogrulama_tarihi + '.'));
    const araclar = el('div', 'linkler'); araclar.style.marginBottom = '4px';
    araclar.append(kopyalaDugmesi(() => aktifPaket ? senaryoMetni(aktifPaket) : '', 'Senaryoyu kopyala'),
                   kopyalaDugmesi(() => aktifPaket ? istemMetni(aktifPaket) : '', 'Yapay zeka istemlerini kopyala'));
    kap.append(araclar);
    k.gruplar.forEach(g => {
      if (!g.siteler.length) return;
      kap.append(el('div', 'grup-baslik', g.baslik));
      g.siteler.forEach(site => {
        const kart = el('div', 'site'); const baslik = el('h4'); baslik.append(el('span', null, site.ad));
        const [yazi, sinif] = ROZET[site.ucretsiz] || ['?', ''];
        baslik.append(el('span', 'rozet ' + sinif, yazi), el('span', 'rozet', TUR_AD[site.tur] || site.tur));
        kart.append(baslik, el('div', null, site.ozet));
        if (site.belirtilmemis) kart.append(el('div', 'nota', 'Belirtilmemiş / dikkat: ' + site.belirtilmemis));
        const l = el('div', 'linkler'); l.append(disLink(site.adres, 'Siteyi aç ↗'), disLink(site.kaynak, 'Kaynak sayfa ↗'), el('span', 'nota', 'Doğrulama: ' + site.dogrulama));
        kart.append(l); kap.append(kart);
      });
    });
  } catch (h) { kap.replaceChildren(el('div', 'hata', h.message)); }
}

async function anahtarlariYukle() {
  const kap = $('#anahtarlar');
  let liste;
  try { liste = await api('/api/anahtarlar'); } catch (h) { kap.replaceChildren(el('div', 'hata', h.message)); return; }
  anahtarDurumu = Object.fromEntries(liste.map(a => [a.ad, a.tanimli]));
  await kaynaklariYukle();           // acik paketteki sahne bloklarinin kaynak etiketleri de guncellenir
  kap.replaceChildren();
  kap.append(el('div', 'uyari', 'Anahtarlar yalnızca bu bilgisayardaki config/anahtarlar.json dosyasında düz metin olarak saklanır; bize hiçbir şey gönderilmez. Kullanım ve maliyet size aittir.'));
  liste.forEach(a => {
    const k = el('div', 'anahtar'); const b = el('h4'); b.append(el('span', null, a.baslik));
    b.append(el('span', 'rozet ' + (a.tanimli ? 'iyi' : ''), a.tanimli ? 'Tanımlı ' + (a.maske || '') : 'Tanımlı değil'));
    k.append(b, el('div', null, a.ne_icin), el('div', 'nota', 'Maliyet: ' + a.maliyet));
    if (a.kaynak === 'ortam') {
      k.append(el('div', 'nota', 'Ortam değişkeninden okunuyor (' + a.ortam_degiskeni + '); buradan değiştirilemez.'));
    } else {
      const f = el('form'); const giris = el('input'); giris.type = 'password'; giris.placeholder = 'Anahtarı yapıştırın'; giris.autocomplete = 'off'; giris.maxLength = 300;
      const kaydet = el('button', 'kucuk', 'Kaydet'); kaydet.type = 'submit'; f.append(giris, kaydet);
      if (a.tanimli) { const sil = el('button', 'kucuk', 'Sil'); sil.type = 'button'; sil.onclick = async () => { try { await api('/api/anahtar/' + a.ad, { method: 'DELETE' }); await anahtarlariYukle(); await durumYukle(); } catch (h) { k.append(el('div', 'hata', h.message)); } }; f.append(sil); }
      f.onsubmit = async (ev) => {
        ev.preventDefault();
        try { await api('/api/anahtar/' + a.ad, { method: 'PUT', body: JSON.stringify({ deger: giris.value.trim() }) }); giris.value = ''; await anahtarlariYukle(); await durumYukle(); }
        catch (h) { giris.value = ''; k.append(el('div', 'hata', h.message)); }
      };
      k.append(f);
    }
    const l = el('div', 'linkler'); l.append(disLink(a.anahtar_al, 'Anahtar al ↗')); k.append(l); kap.append(k);
  });
}

let kaynaklar = [];

function kaynakEtiketi(k) { return k.baslik + (k.anahtar_gerekli && !k.anahtar_tanimli ? ' (anahtar yok)' : ''); }

function kaynakSecenekleri(sel, varsayilan) {
  const onceki = sel.value; sel.replaceChildren();
  [['Stok · video ve görsel (API anahtarı gerekir)', k => k.anahtar_gerekli], ['Anahtarsız · yalnızca görsel', k => !k.anahtar_gerekli]].forEach(([ad, filtre]) => {
    const g = document.createElement('optgroup'); g.label = ad;
    kaynaklar.filter(filtre).forEach(k => { const o = el('option', null, kaynakEtiketi(k)); o.value = k.ad; o.title = k.aciklama; g.append(o); });
    if (g.children.length) sel.append(g);
  });
  const mevcut = [...sel.options].map(o => o.value);
  sel.value = mevcut.includes(onceki) ? onceki : (varsayilan || mevcut[0] || '');
}

async function kaynaklariYukle() {
  try { kaynaklar = await api('/api/kaynaklar'); } catch (_) { kaynaklar = []; }
  document.querySelectorAll('.medya select.kaynak-sec').forEach(sel => kaynakSecenekleri(sel));
}

const varsayilanKaynak = () => (kaynaklar.find(k => k.anahtar_gerekli && k.anahtar_tanimli) || kaynaklar.find(k => !k.anahtar_gerekli) || {}).ad;

function atifGuncelle() {
  const kap = document.querySelector('#atifKap');
  if (!kap || !aktifPaket) return;
  kap.replaceChildren();
  const zorunlu = Object.entries(aktifPaket.medya || {}).filter(([, m]) => m.atif_gerekli).sort((a, b) => Number(a[0]) - Number(b[0]));
  if (!zorunlu.length) return;
  const metin = zorunlu.map(([, m]) => m.atif).join('\n');
  const kutu = el('div', 'uyari');
  kutu.append(el('div', null, 'Seçtiğiniz bazı görsellerin lisansı atıf ZORUNLU kılıyor. Paylaşırken şu satırları açıklamaya ekleyin:'));
  const satirlar = el('div', 'metin', metin); satirlar.style.margin = '8px 0';
  kutu.append(satirlar, kopyalaDugmesi(() => metin, 'Atıfları kopyala')); kap.append(kutu);
}

function sahneMedya(p, sahne, no) {
  const kap = el('div', 'medya'); const secili = el('div'); const sonuc = el('div'); const mesaj = el('div', 'not');
  const goster = (kayit) => {
    secili.replaceChildren();
    if (kayit) {
      const kutu = el('div', 'secili-medya'); const yol = '/paketler/' + p.id + '/medya/' + encodeURIComponent(kayit.dosya);
      if (kayit.tur === 'video') { const v = el('video'); v.src = yol; v.controls = true; v.muted = true; v.preload = 'metadata'; kutu.append(v); }
      else { const i = el('img'); i.src = yol; i.alt = ''; kutu.append(i); }
      const yan = el('div', 'nota'); yan.append(el('div', null, 'Seçili: ' + kayit.kredi_metni));
      if (kayit.lisans) yan.append(el('div', null, 'Lisans: ' + kayit.lisans + (kayit.atif_gerekli ? ' (atıf zorunlu)' : '')));
      if (kayit.uyari) yan.append(el('div', null, kayit.uyari));
      const l = el('div', 'linkler'); l.append(disLink(kayit.sayfa, 'Kaynak sayfa ↗'));
      const kaldir = el('button', 'kucuk', 'Kaldır');
      kaldir.onclick = async () => { try { await api('/api/paket/' + p.id + '/medya/' + no, { method: 'DELETE' }); delete p.medya[String(no)]; goster(null); } catch (h) { mesaj.textContent = h.message; } };
      l.append(kaldir); yan.append(l); kutu.append(yan); secili.append(kutu);
    }
    atifGuncelle();
  };
  p.medya = p.medya || {}; goster(p.medya[String(no)]);

  const satir = el('div', 'satir');
  const sorgu = el('input'); sorgu.type = 'text'; sorgu.maxLength = 100; sorgu.value = sahne.gorsel_aramasi || ''; sorgu.placeholder = 'Arama (İngilizce önerilir)';
  const kaynak = el('select', 'kaynak-sec'); kaynakSecenekleri(kaynak, varsayilanKaynak());
  const vb = el('button', 'kucuk', 'Stok video'); const gb = el('button', 'kucuk', 'Stok görsel');
  const videoDurumu = () => { const k = kaynaklar.find(x => x.ad === kaynak.value); const yok = !!k && !k.video; vb.disabled = yok; vb.title = yok ? 'Bu kaynak yalnızca görsel arar' : ''; };
  kaynak.onchange = videoDurumu; videoDurumu();
  const ara = async (tur) => {
    mesaj.textContent = 'Aranıyor…'; sonuc.replaceChildren();
    try {
      const r = await api('/api/paket/' + p.id + '/stok', { method: 'POST', body: JSON.stringify({ sahne: no, tur, kaynak: kaynak.value, sorgu: sorgu.value.trim() }) });
      mesaj.replaceChildren();
      if (!r.adaylar.length) { mesaj.textContent = 'Sonuç bulunamadı; aramayı değiştirin (lisans süzgeci NC/ND ve telifli görselleri eler).'; return; }
      const izgara = el('div', 'adaylar');
      r.adaylar.forEach(a => {
        const b = el('button', 'aday');
        if (a.onizleme) { const i = el('img'); i.src = a.onizleme; i.alt = ''; i.loading = 'lazy'; i.referrerPolicy = 'no-referrer'; b.append(i); } else b.append(el('div', 'bos-onizleme', 'önizleme yok'));
        b.append(el('small', null, (a.kredi || '—') + (a.sure_sn ? ' · ' + a.sure_sn + ' sn' : '') + (a.dikey === false ? ' · yatay' : '')));
        b.append(el('small', 'lisans' + (a.atif_gerekli ? ' uyari-lisans' : ''), (a.lisans || '') + (a.atif_gerekli ? ' · atıf zorunlu' : '')));
        b.title = [a.baslik, a.lisans, a.uyari].filter(Boolean).join('\n');
        b.onclick = async () => {
          mesaj.textContent = 'İndiriliyor…';
          try { const kayit = await api('/api/paket/' + p.id + '/medya', { method: 'POST', body: JSON.stringify({ sahne: no, aday_kimligi: a.kimlik }) }); p.medya[String(no)] = kayit; goster(kayit); sonuc.replaceChildren(); mesaj.replaceChildren(); }
          catch (h) { mesaj.textContent = h.message; }
        };
        izgara.append(b);
      });
      const atif = el('div', 'nota'); atif.append(disLink(r.kaynak_adresi, r.kaynak_adi + ' ↗'), document.createTextNode(' kaynağından. Seçtiğiniz görselin lisansı ve atıf satırı krediler.txt dosyasına yazılır.'));
      sonuc.append(izgara, atif);
    } catch (h) { mesaj.textContent = h.message; }
  };
  vb.onclick = () => ara('video'); gb.onclick = () => ara('gorsel');
  satir.append(sorgu, kaynak, vb, gb);
  if (sahne.gorsel_istemi) satir.append(kopyalaDugmesi(() => sahne.gorsel_istemi, 'AI istemi'));
  kap.append(satir, mesaj, sonuc, secili);
  return kap;
}

function videoBlogu(p) {
  const sec = el('select'); const dugme = el('button', 'kucuk', p.video ? 'Yeniden üret' : 'Videoyu birleştir');
  const durum = el('div', 'not'); const icerik = el('div');
  const kontrol = el('div', 'carousel-kontrol'); kontrol.append(sec, dugme);
  const bl = blok('Reels videosu · otomatik birleştirme',
    el('div', 'not', "Senaryo + sesli anlatım + sahnelere seçtiğin medya + ekran yazısı → 1080×1920 (9:16) mp4. Medya seçilmemiş sahneler düz kart olur. Sesli anlatım, anlatım metnini Microsoft'un okuma servisine (edge-tts) gönderir; \"Sessiz\" seçersen hiçbir şey gönderilmez."),
    kontrol, durum, icerik);
  api('/api/sesler').then(d => d.sesler.forEach(s => { const o = el('option', null, s.etiket); o.value = s.ad; if (s.ad === d.varsayilan) o.selected = true; sec.append(o); })).catch(() => {});

  const goster = async () => {
    icerik.replaceChildren();
    let v;
    try { v = await api('/api/paket/' + p.id + '/video'); } catch (_) { return; }
    v.uyarilar.forEach(u => icerik.append(el('div', 'uyari', u)));
    const yol = '/paketler/' + p.id + '/video/reels.mp4?v=' + encodeURIComponent(v.olusturma);
    const vid = el('video', 'video-onizleme'); vid.controls = true; vid.preload = 'metadata'; vid.src = yol;
    const islem = el('div', 'carousel-kontrol');
    const a = el('a'); a.href = yol; a.download = 'reels-' + p.id + '.mp4'; a.append(el('button', 'kucuk', 'mp4 indir'));
    islem.append(a, el('span', 'not', v.sure_sn + ' sn · ' + v.sahne_sayisi + ' sahne · ' + v.medyali_sahne + ' sahnede medya'));
    icerik.append(vid, islem);
  };

  dugme.onclick = async () => {
    dugme.disabled = true; durum.textContent = 'Başlatılıyor…';
    try {
      const { is_id } = await api('/api/paket/' + p.id + '/video', { method: 'POST', body: JSON.stringify({ ses: sec.value }) });
      for (;;) {
        await new Promise(r => setTimeout(r, 1000));
        const d = await api('/api/is/' + is_id);
        const son = d.adimlar[d.adimlar.length - 1]; if (son) durum.textContent = son.mesaj;
        if (d.durum === 'bitti') { await goster(); durum.replaceChildren(); dugme.textContent = 'Yeniden üret'; break; }
        if (d.durum === 'hata') { durum.textContent = d.hata || 'Bilinmeyen hata.'; break; }
      }
    } catch (h) { durum.textContent = h.message; }
    dugme.disabled = false;
  };
  if (p.video) goster();
  return bl.b;
}

function carouselBlogu(p) {
  const sec = el('select'); [10, 8, 7, 6, 5, 4].forEach(n => { const o = el('option', null, n + ' slayt'); o.value = String(n); sec.append(o); });
  const dugme = el('button', 'kucuk', p.carousel ? 'Yeniden üret' : 'Carousel üret');
  const durum = el('div', 'not'); const icerik = el('div');
  const kontrol = el('div', 'carousel-kontrol'); kontrol.append(sec, dugme);
  const bl = blok('Carousel · kaydırmalı gönderi',
    el('div', 'not', "Aynı paketten 4-10 slaytlık bir gönderi üretir (1080×1350, 4:5). Instagram API'si carousel için en fazla 10 görsel kabul eder."), kontrol, durum, icerik);

  const goster = async () => {
    icerik.replaceChildren();
    let c;
    try { c = await api('/api/paket/' + p.id + '/carousel'); } catch (_) { return; }
    c.uyarilar.forEach(u => icerik.append(el('div', 'uyari', u)));
    if (c.dosyalar.length) {
      const izgara = el('div', 'carousel-izgara');
      c.dosyalar.forEach((ad, i) => {
        const a = el('a'); a.href = '/paketler/' + p.id + '/carousel/' + encodeURIComponent(ad); a.target = '_blank'; a.rel = 'noopener noreferrer';
        const img = el('img'); img.src = a.href; img.alt = (c.alt_metinler && c.alt_metinler[i]) || ('Slayt ' + (i + 1)); img.loading = 'lazy'; a.append(img); izgara.append(a);
      });
      const islem = el('div', 'carousel-kontrol');
      const z = el('a'); z.href = '/paketler/' + p.id + '/carousel.zip'; z.append(el('button', 'kucuk', 'Tümünü indir (.zip)'));
      islem.append(z, kopyalaDugmesi(() => c.caption, 'Caption\'ı kopyala'));
      icerik.append(izgara, islem);
    }
    icerik.append(el('div', 'metin', c.caption));
  };

  dugme.onclick = async () => {
    dugme.disabled = true; durum.textContent = 'Başlatılıyor…';
    try {
      const { is_id } = await api('/api/paket/' + p.id + '/carousel', { method: 'POST', body: JSON.stringify({ adet: Number(sec.value), saglayici: $('#motor').value }) });
      for (;;) {
        await new Promise(r => setTimeout(r, 800));
        const d = await api('/api/is/' + is_id);
        const son = d.adimlar[d.adimlar.length - 1]; if (son) durum.textContent = son.mesaj;
        if (d.durum === 'bitti') { p.carousel = p.carousel || {}; await goster(); durum.replaceChildren(); dugme.textContent = 'Yeniden üret'; break; }
        if (d.durum === 'hata') { durum.textContent = d.hata || 'Bilinmeyen hata.'; break; }
      }
    } catch (h) { durum.textContent = h.message; }
    dugme.disabled = false;
  };
  if (p.carousel) goster();
  return bl.b;
}

$('#uret').onclick = uret;
durumYukle(); gecmisYukle(); anahtarlariYukle(); siteleriYukle();
