# -*- coding: utf-8 -*-
"""
arsiv_topla.py — OddsPortal açılış oranı arşivleyici (OddsHarvester orkestratörü)

NE YAPAR:
  95 futbol ligini × son 1 yıl sezonlarını dolaşır, her lig-sezon için
  OddsHarvester'ı çağırır ve şunları çeker:
    - Büro: bet365 (BURO env ile değişir)
    - Açılış oranı: --odds-history (opening_odds alanı)
    - Tüm ana pazarlar: 1X2, KG var/yok, çifte şans, beraberlikte iade,
      alt/üst çizgileri, Asya + Avrupa handikap
    - İY + MS skorları: home_score / away_score / partial_results

NEDEN GÜVENİLİR (döngüyü kırar):
  1. DEVAM EDEBİLİR: biten lig-sezonu atlar (dosya varsa). Railway restart olsa
     kaldığı yerden devam eder. CMD'yi kapatabilirsin.
  2. KENDİ KENDİNE ÇÖZEN FALLBACK: bir çekim 0 maç dönerse (anti-bot işareti)
     otomatik sırayla dener: (a) tekrar dene, (b) bölgesel mirror (BASE_URL),
     (c) proxy (PROXY_URL). Hiçbiri yoksa net log basıp sonraki lige geçer —
     asla tüm çalışmayı çökertmez.
  3. AÇILIŞTA SMOKE TEST: önce tek lig/tek sayfa çeker, açılış oranı + skor
     geldi mi diye DOĞRULAR ve loga NET yazar. Gelmezse ne yapılacağını söyler.

ENV DEĞİŞKENLERİ (hepsi opsiyonel, mantıklı varsayılanlar var):
  BURO           bet365            hedef büro
  ARSIV_DIR      /app/arsiv        çıktı klasörü (Railway Volume mount et)
  LIGLER_DOSYA   futbol_ligler.txt lig listesi (satır satır slug)
  SEZONLAR       (oto)             örn "current,2024-2025"; boşsa oto son 1 yıl
  PAZARLAR       (geniş oto set)   virgülle -m token'ları; "hepsi" = tüm çizgiler
  CONCURRENCY    4                 eşzamanlı maç sayısı (hız)
  REQUEST_DELAY  0.5               maçlar arası bekleme (sn)
  MAX_PAGES      (yok)             lig başına maks sonuç sayfası (test için sınırla)
  BASE_URL       (yok)            bölgesel mirror domain (anti-bot fallback)
  PROXY_URL      (yok)            socks5://.. veya http://..  (anti-bot fallback)
  PROXY_USER / PROXY_PASS         proxy kimlik bilgisi
  SMOKE_ATLA     (yok)            "1" ise açılış smoke test'ini atla
"""

import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

# ------------------------------------------------------------------ ayarlar
BURO = os.environ.get("BURO", "bet365").strip()
ARSIV = Path(os.environ.get("ARSIV_DIR", "/app/arsiv"))
LIGLER_DOSYA = os.environ.get("LIGLER_DOSYA", "futbol_ligler.txt")
CONCURRENCY = os.environ.get("CONCURRENCY", "4").strip()
REQUEST_DELAY = os.environ.get("REQUEST_DELAY", "0.5").strip()
MAX_PAGES = os.environ.get("MAX_PAGES", "").strip()
BASE_URL = os.environ.get("BASE_URL", "").strip()
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
PROXY_USER = os.environ.get("PROXY_USER", "").strip()
PROXY_PASS = os.environ.get("PROXY_PASS", "").strip()
SMOKE_ATLA = os.environ.get("SMOKE_ATLA", "").strip() == "1"
SMOKE_SEZON = os.environ.get("SMOKE_SEZON", "2024-2025").strip()
SMOKE_LIG = os.environ.get("SMOKE_LIG", "england-premier-league").strip()
LIG_LIMIT = int(os.environ.get("LIG_LIMIT", "0") or "0")  # 0 = sınır yok

# Bir maça bet365'te AÇILAN TÜM PAZARLAR.
# 'over_under' ve 'asian_handicap' umbrella token'larıdır: o maçta açılmış
# BÜTÜN alt/üst ve Asya handikap çizgilerini otomatik çeker (elle çizgi
# saymaya gerek yok). Avrupa handikabın umbrella'sı yok, ana çizgileri eklenir.
_CEKIRDEK = ["1x2", "btts", "double_chance", "dnb"]
_UMBRELLA = ["over_under", "asian_handicap"]
_ANA_EH = ["european_handicap_-2", "european_handicap_-1",
           "european_handicap_+1", "european_handicap_+2"]
_TUM_EH = [f"european_handicap_{x}" for x in
           ["-4", "-3", "-2", "-1", "+1", "+2", "+3", "+4"]]

_pazar_env = os.environ.get("PAZARLAR", "").strip()
if _pazar_env.lower() == "hepsi":
    PAZARLAR = ",".join(_CEKIRDEK + _UMBRELLA + _TUM_EH)
elif _pazar_env:
    PAZARLAR = _pazar_env
else:
    # Varsayılan zaten "tüm pazarlar": umbrella'lar bütün çizgileri kapsar.
    PAZARLAR = ",".join(_CEKIRDEK + _UMBRELLA + _ANA_EH)


def log(*a):
    print(*a, flush=True)


def git_kaydet(mesaj):
    """GIT_COMMIT=1 ise arşivi repoya commit + push et (GitHub Actions için).
    Her lig sonrası çağrılır; 6 saat sınırına takılsa bile ilerleme kaybolmaz."""
    if os.environ.get("GIT_COMMIT", "").strip() != "1":
        return
    d = os.environ.get("REPO_DIR", ".")
    try:
        subprocess.run(["git", "add", "-A"], cwd=d, timeout=60)
        r = subprocess.run(["git", "commit", "-m", mesaj], cwd=d, timeout=60)
        if r.returncode != 0:
            return  # commit edilecek değişiklik yok
        subprocess.run(["git", "pull", "--rebase", "--autostash"], cwd=d, timeout=120)
        subprocess.run(["git", "push"], cwd=d, timeout=120)
        log(f"   [git] kaydedildi: {mesaj}")
    except Exception as e:
        log(f"   [git] hata (atlandı): {e}")


def sezonlari_belirle():
    env = os.environ.get("SEZONLAR", "").strip()
    if env:
        return [s.strip() for s in env.split(",") if s.strip()]
    # Son 1 yıl: içinde bulunulan sezon (current) + bir önceki sezon
    y = date.today().year
    ay = date.today().month
    # Sezon Ağustos'ta başlar: Ocak-Temmuz arası "current" = (y-1)-y
    if ay >= 8:
        onceki = f"{y - 1}-{y}"
    else:
        onceki = f"{y - 2}-{y - 1}"
    return ["current", onceki]


def ligleri_oku():
    p = Path(LIGLER_DOSYA)
    if not p.exists():
        # betikle aynı klasörde ara
        p = Path(__file__).parent / LIGLER_DOSYA
    if not p.exists():
        log(f"UYARI: {LIGLER_DOSYA} bulunamadı, gömülü kısa liste kullanılıyor.")
        return ["england-premier-league", "spain-laliga", "italy-serie-a",
                "germany-bundesliga", "france-ligue-1", "turkey-super-lig"]
    return [s.strip() for s in p.read_text(encoding="utf-8").splitlines()
            if s.strip() and not s.strip().startswith("#")]


def komut_kur(lig, sezon, cikti, max_pages=None, base_url=None, proxy=False):
    cmd = [sys.executable, "-m", "oddsharvester", "historic",
           "-s", "football", "-l", lig, "--season", sezon,
           "-m", PAZARLAR,
           "--target-bookmaker", BURO,
           "--odds-history",
           "--headless",
           "-c", CONCURRENCY,
           "--request-delay", REQUEST_DELAY,
           "-f", "json", "-o", str(cikti)]
    if max_pages:
        cmd += ["--max-pages", str(max_pages)]
    if base_url:
        cmd += ["--base-url", base_url]
    if proxy and PROXY_URL:
        cmd += ["--proxy-url", PROXY_URL]
        if PROXY_USER:
            cmd += ["--proxy-user", PROXY_USER]
        if PROXY_PASS:
            cmd += ["--proxy-pass", PROXY_PASS]
    return cmd


def cikti_oku(cikti):
    """JSON çıktıyı oku; (maç_sayısı, açılış_oranı_var_mı, skor_var_mı) döndür."""
    try:
        veri = json.loads(Path(cikti).read_text(encoding="utf-8"))
    except Exception:
        return 0, False, False
    maclar = veri if isinstance(veri, list) else veri.get("data", [veri])
    if not maclar:
        return 0, False, False
    acilis = False
    skor = False
    for m in maclar:
        if not isinstance(m, dict):
            continue
        if m.get("home_score") not in (None, "") or m.get("partial_results"):
            skor = True
        # açılış oranı market/odds içinde opening_odds olarak gömülü
        blob = json.dumps(m, ensure_ascii=False)
        if "opening_odds" in blob or "opening" in blob:
            acilis = True
        if acilis and skor:
            break
    return len(maclar), acilis, skor


def calistir(cmd, zaman_asimi=None):
    log(">>", " ".join(cmd))
    try:
        r = subprocess.run(cmd, timeout=zaman_asimi)
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        log("   (zaman aşımı — atlandı)")
        return False
    except Exception as e:
        log(f"   (hata: {e})")
        return False


def cek_fallback_zinciri(lig, sezon, cikti, max_pages=None):
    """
    Kendi kendine çözen zincir:
      1) direkt (www.oddsportal.com, built-in stealth)
      2) 0 maç dönerse tekrar dene (anti-bot geçici olabilir)
      3) hâlâ 0 ise BASE_URL mirror (varsa)
      4) hâlâ 0 ise PROXY_URL (varsa)
    Herhangi biri maç döndürürse durur. Hiçbiri olmazsa False.
    """
    denemeler = [
        ("direkt", dict()),
        ("direkt-tekrar", dict()),
    ]
    if BASE_URL:
        denemeler.append(("mirror", dict(base_url=BASE_URL)))
    if PROXY_URL:
        denemeler.append(("proxy", dict(proxy=True)))
        if BASE_URL:
            denemeler.append(("mirror+proxy", dict(base_url=BASE_URL, proxy=True)))

    for etiket, kw in denemeler:
        cmd = komut_kur(lig, sezon, cikti, max_pages=max_pages, **kw)
        calistir(cmd)
        n, acilis, skor = cikti_oku(cikti)
        if n > 0:
            log(f"   [{etiket}] OK — {n} maç (açılış={acilis} skor={skor})")
            return True
        log(f"   [{etiket}] 0 maç (anti-bot olası), sıradaki yöntem deneniyor...")
        # başarısız çıktıyı sil ki 'atla' mantığı yanlış tetiklenmesin
        try:
            Path(cikti).unlink()
        except Exception:
            pass
        time.sleep(3)

    log(f"   !! {lig} {sezon}: tüm yöntemler 0 maç döndü.")
    if not BASE_URL and not PROXY_URL:
        log("      ÇÖZÜM: Railway'de BASE_URL (mirror) veya PROXY_URL ekle. "
            "Detay TALIMAT.md'de.")
    return False


def smoke_test():
    log("=" * 64)
    log("SMOKE TEST: OddsHarvester gerçekten açılış oranı + skor çekiyor mu?")
    log("=" * 64)
    test_cikti = ARSIV / "_smoke_test.json"
    try:
        test_cikti.unlink()
    except Exception:
        pass
    log(f"(smoke: {SMOKE_LIG} / {SMOKE_SEZON})")
    ok = cek_fallback_zinciri(SMOKE_LIG, SMOKE_SEZON,
                              test_cikti, max_pages=1)
    if not ok:
        log("SMOKE TEST BAŞARISIZ: hiç maç gelmedi. Muhtemelen anti-bot/IP.")
        log("Yine de arşiv döngüsüne geçiliyor (fallback'ler orada da devrede).")
        return False
    n, acilis, skor = cikti_oku(test_cikti)
    log(f"SONUÇ: {n} maç | açılış oranı: {'VAR ✓' if acilis else 'YOK ✗'} | "
        f"skor: {'VAR ✓' if skor else 'YOK ✗'}")
    # örnek bir maçı yazdır
    try:
        veri = json.loads(test_cikti.read_text(encoding="utf-8"))
        maclar = veri if isinstance(veri, list) else veri.get("data", [])
        if maclar:
            m = maclar[0]
            log(f"ÖRNEK: {m.get('home_team')} {m.get('home_score')}-"
                f"{m.get('away_score')} {m.get('away_team')} "
                f"(devre: {m.get('partial_results')})")
    except Exception:
        pass
    log("=" * 64)
    return acilis and skor


def main():
    if os.environ.get("DURDUR", "").strip() == "1":
        log("DURDUR=1 — çalışma atlandı.")
        return
    ARSIV.mkdir(parents=True, exist_ok=True)
    ligler = ligleri_oku()
    if LIG_LIMIT > 0:
        ligler = ligler[:LIG_LIMIT]
        log(f"(LIG_LIMIT={LIG_LIMIT}: sadece ilk {LIG_LIMIT} lig)")
    sezonlar = sezonlari_belirle()
    log(f"BAŞLIYOR | büro={BURO} | {len(ligler)} lig × {len(sezonlar)} sezon "
        f"= {len(ligler) * len(sezonlar)} çekim")
    log(f"Sezonlar: {sezonlar}")
    log(f"Pazarlar: {PAZARLAR}")
    log(f"Arşiv: {ARSIV}")

    if not SMOKE_ATLA:
        smoke_test()
        git_kaydet("smoke test sonucu")

    toplam_mac = 0
    for i, lig in enumerate(ligler, 1):
        for sezon in sezonlar:
            cikti = ARSIV / f"{lig}__{sezon}.json"
            if cikti.exists():
                n, _, _ = cikti_oku(cikti)
                log(f"[{i}/{len(ligler)}] ATLA (zaten var): {cikti.name} ({n} maç)")
                toplam_mac += n
                continue
            log(f"[{i}/{len(ligler)}] ÇEK: {lig} / {sezon}")
            ok = cek_fallback_zinciri(lig, sezon, cikti,
                                      max_pages=int(MAX_PAGES) if MAX_PAGES else None)
            if ok:
                n, _, _ = cikti_oku(cikti)
                toplam_mac += n
                git_kaydet(f"arsiv: {lig} {sezon} ({n} mac)")
    git_kaydet("arsiv: tamamlandi")
    log(f"BİTTİ | toplam {toplam_mac} maç arşivlendi -> {ARSIV}")


if __name__ == "__main__":
    main()
