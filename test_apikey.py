"""
test_apikey.py
==============
Gemini API anahtarının geçerliliğini hızlıca doğrulayan bir test script'i.

NEDEN VAR?
----------
Streamlit uygulamasını çalıştırıp Master görsel + Master Prompt + 5 varyasyon
yazıp "Üretimi Başlat"a basıp 10 dakika bekledikten sonra "API Key Invalid"
hatası almak çok can sıkıcı. Bu script o tuzağı önler.

NE YAPAR?
---------
1. .env dosyasından GEMINI_API_KEY'i okur.
2. Placeholder mı diye kontrol eder.
3. Basit bir text generation isteği atar (ucuz, hızlı).
4. Batch API erişimini list() çağrısıyla doğrular.
5. Net bir özet basar.

NEYI YAPMAZ?
------------
- Görsel üretmez (pahalı, gereksiz).
- Batch job başlatmaz (uzun ve para harcar).

KULLANIM (PowerShell):
    python test_apikey.py
"""

from __future__ import annotations

import os
import sys
from typing import Final

from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Sabitler: Test akışında kullanılacak metinler ve kontrol değerleri.
# Final type hint: bu değişkenlerin DEĞİŞTİRİLMEMESİ gerektiğinin işareti.
# ---------------------------------------------------------------------------
PLACEHOLDER_TOKENS: Final = ("YOUR_API_KEY", "YOUR_API_KEY_HERE", "")
TEST_PROMPT: Final = "Sadece şu kelimeyi yaz: PONG"
TEST_MODEL: Final = "gemini-2.5-flash"  # Image değil text - daha ucuz
EXPECTED_RESPONSE_HINT: Final = "PONG"


# ---------------------------------------------------------------------------
# Yardımcı: Konsola hizalı format ile mesaj basar.
# Renk yerine ASCII marker kullanıyoruz - Windows terminal uyumluluğu için.
# ---------------------------------------------------------------------------
def _log(level: str, message: str) -> None:
    """
    Standart log formatı: [LEVEL] mesaj
    level: "OK", "FAIL", "WARN", "INFO"
    """
    # 6 karakterlik sabit hizalama - tablo gibi okunaklı çıktı
    marker = f"[{level}]".ljust(7)
    print(f"{marker} {message}")


# ---------------------------------------------------------------------------
# TEST 1: .env dosyasından API anahtarı okuma
# ---------------------------------------------------------------------------
def test_env_loading() -> str | None:
    """
    .env'den anahtarı yüklemeye çalışır.

    Returns:
        Bulunduysa anahtar string'i, bulunamadıysa None.
    """
    print("\n--- TEST 1: .env Dosyası ---")

    # load_dotenv: .env'deki değerleri os.environ'a taşır.
    # override=True: ortamda zaten varsa bile .env'inkiyle değiştirir.
    load_dotenv(override=True)

    api_key = os.getenv("GEMINI_API_KEY", "").strip()

    if not api_key:
        _log("FAIL", "GEMINI_API_KEY .env dosyasında bulunamadı.")
        _log("INFO", "Çözüm: .env dosyasına 'GEMINI_API_KEY=...' satırını ekle.")
        return None

    # Anahtarın ilk 8 ve son 4 karakterini gösteriyoruz - güvenlik vs okunabilirlik dengesi
    masked = (
        f"{api_key[:8]}...{api_key[-4:]}"
        if len(api_key) > 12
        else "[çok kısa anahtar]"
    )
    _log("OK", f".env okundu, anahtar bulundu: {masked}")
    return api_key


# ---------------------------------------------------------------------------
# TEST 2: Placeholder kontrolü
# ---------------------------------------------------------------------------
def test_not_placeholder(api_key: str) -> bool:
    """
    Anahtar hâlâ şablon değer mi kontrol eder.
    """
    print("\n--- TEST 2: Placeholder Kontrolü ---")

    # Yaygın placeholder kalıplarını upper-case karşılaştırma ile yakala
    upper_key = api_key.upper()
    for token in PLACEHOLDER_TOKENS:
        if token and token in upper_key:
            _log("FAIL", f"Anahtar hâlâ placeholder: '{token}' içeriyor.")
            _log(
                "INFO",
                "Çözüm: AI Studio'dan aldığın gerçek anahtarı .env'e yapıştır.",
            )
            return False

    # Gemini anahtarları "AIza" ile başlar ve 39 karakter civarındadır.
    # Bu kesin kural değil ama hatalı yapıştırmayı yakalar (örn: tırnak içerme).
    if not api_key.startswith("AIza"):
        _log(
            "WARN",
            "Anahtar 'AIza' ile başlamıyor. Yanlışlıkla tırnak veya boşluk eklemiş olabilirsin.",
        )
        # WARN - kesin başarısız değil, devam et

    _log("OK", "Anahtar gerçek bir değer gibi görünüyor.")
    return True


# ---------------------------------------------------------------------------
# TEST 3: Basit bir text generation isteği at
# ---------------------------------------------------------------------------
def test_basic_generation(api_key: str) -> bool:
    """
    En küçük olası API çağrısıyla anahtarın authenticate olup olmadığını test eder.
    """
    print("\n--- TEST 3: Basit Text Generation ---")

    try:
        # Import'u burada yapıyoruz ki .env testi başarısız olduysa bu hata vermez.
        from google import genai
    except ImportError:
        _log("FAIL", "google-genai paketi kurulu değil.")
        _log("INFO", "Çözüm: pip install -r requirements.txt")
        return False

    try:
        # Client'ı doğrudan anahtar ile oluştur
        client = genai.Client(api_key=api_key)

        # En basit istek: tek prompt, kısa cevap bekleniyor
        response = client.models.generate_content(
            model=TEST_MODEL,
            contents=TEST_PROMPT,
        )

        # Response'tan text'i çek
        result_text = (response.text or "").strip()

        if not result_text:
            _log("WARN", "Yanıt boş geldi - anahtar çalışıyor ama sonuç şüpheli.")
            return True  # Auth çalıştığı için True dönüyoruz

        _log("OK", f"API yanıt verdi: '{result_text[:60]}'")

        # Beklenen kelime cevapta var mı? (model bazen ekstra şeyler ekliyor)
        if EXPECTED_RESPONSE_HINT in result_text.upper():
            _log("OK", f"Beklenen '{EXPECTED_RESPONSE_HINT}' cevapta bulundu.")

        return True

    except Exception as exc:
        # Hata mesajına göre kullanıcıya yönlendirme yapalım
        error_str = str(exc).lower()

        if "api key" in error_str or "api_key" in error_str or "401" in error_str:
            _log("FAIL", "Anahtar geçersiz veya yanlış.")
            _log("INFO", "Çözüm: AI Studio'dan yeni bir anahtar oluştur.")
        elif "quota" in error_str or "429" in error_str:
            _log("FAIL", "Kota dolmuş veya rate limit'e takılındı.")
            _log("INFO", "Çözüm: Birkaç dakika bekle veya billing aktif et.")
        elif "permission" in error_str or "403" in error_str:
            _log("FAIL", "Yetki yok - bu model/proje sana kapalı olabilir.")
        else:
            _log("FAIL", f"Beklenmedik hata: {exc}")

        return False


# ---------------------------------------------------------------------------
# TEST 4: Batch API erişimini list ile kontrol et
# ---------------------------------------------------------------------------
def test_batch_access(api_key: str) -> bool:
    """
    Batch API'ye list() çağrısı atar - read-only, kota harcamaz.
    """
    print("\n--- TEST 4: Batch API Erişimi ---")

    try:
        from google import genai
        client = genai.Client(api_key=api_key)

        # batches.list: var olan batch job'ları listeler.
        # Hiç yoksa boş liste döner, ama API çağrısı başarılı olur.
        # config={"page_size": 1}: minimum veri çek, hızlı dön.
        batches_pager = client.batches.list(config={"page_size": 1})

        # Pager'ı tüketmek için bir item çekmeyi deniyoruz.
        # Boş olsa bile iter() bir StopIteration'a düşer ki o da OK demektir.
        count = 0
        for _ in batches_pager:
            count += 1
            if count >= 1:
                break

        _log("OK", f"Batch API erişilebilir. (Geçmiş job sayısı en az: {count})")
        return True

    except Exception as exc:
        error_str = str(exc).lower()

        if "permission" in error_str or "403" in error_str:
            _log("FAIL", "Batch API'ye erişim YOK.")
            _log(
                "INFO",
                "Çözüm: Cloud Console'da projende billing aktif et "
                "(Batch API genellikle paid tier gerektirir).",
            )
        elif "not found" in error_str or "404" in error_str:
            _log("WARN", "Batch endpoint bulunamadı - SDK versiyonu eski olabilir.")
            _log("INFO", "Çözüm: pip install --upgrade google-genai")
        else:
            _log("FAIL", f"Batch API hatası: {exc}")

        return False


# ---------------------------------------------------------------------------
# Ana akış: Tüm testleri sırayla çalıştırır ve özet yazar.
# ---------------------------------------------------------------------------
def main() -> int:
    """
    Returns:
        Exit code: 0 = tüm kritik testler başarılı, 1 = en az bir kritik fail.
    """
    print("=" * 60)
    print("  Gemini API Key Doğrulama Testi")
    print("=" * 60)

    # TEST 1: .env okunabiliyor mu?
    api_key = test_env_loading()
    if not api_key:
        # Bundan sonraki testler anahtar olmadan anlamsız - erken çık
        print("\n" + "=" * 60)
        _log("FAIL", "Test başlatılamadı: API anahtarı yok.")
        return 1

    # TEST 2: Placeholder mı?
    if not test_not_placeholder(api_key):
        print("\n" + "=" * 60)
        _log("FAIL", "Test başlatılamadı: Anahtar şablonu değiştirilmemiş.")
        return 1

    # TEST 3: Basit istek - asıl auth doğrulaması burada
    auth_ok = test_basic_generation(api_key)

    # TEST 4: Batch API erişimi - asıl proje için kritik
    batch_ok = test_batch_access(api_key)

    # ÖZET
    print("\n" + "=" * 60)
    print("  ÖZET")
    print("=" * 60)
    _log("OK" if auth_ok else "FAIL", f"Temel API erişimi: {'BAŞARILI' if auth_ok else 'BAŞARISIZ'}")
    _log("OK" if batch_ok else "FAIL", f"Batch API erişimi: {'BAŞARILI' if batch_ok else 'BAŞARISIZ'}")

    if auth_ok and batch_ok:
        print("\nHerşey hazır. Şimdi ana uygulamayı çalıştırabilirsin:")
        print("    streamlit run main.py")
        return 0
    elif auth_ok and not batch_ok:
        print("\nAnahtar geçerli ama Batch API kapalı.")
        print("Cloud Console > Billing'i aktif et, sonra tekrar dene.")
        return 1
    else:
        print("\nÖnce yukarıdaki hataları çöz, sonra bu testi tekrar çalıştır.")
        return 1


# Script direkt çalıştırıldığında (import edilince değil) main'i çağır
if __name__ == "__main__":
    # sys.exit ile shell'e exit code dön - CI/CD'lerde işe yarar
    sys.exit(main())
