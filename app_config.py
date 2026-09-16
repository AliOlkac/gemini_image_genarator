"""
app_config.py
=============
Uygulama genelinde paylaşılan sabitler ve API anahtarı yardımcıları.

NEDEN VAR?
    - Model adı, prompt öneki ve fiyatlar önceden iki handler'da kopya duruyordu.
    - API anahtarı sadece tarayıcı oturumunda tutuluyordu; sayfa yenilenince
      kayboluyordu. Artık proje kökündeki .env dosyasına kaydediliyor, böylece
      yenileme / yeniden başlatma sonrası da kullanılabiliyor ve arka plandaki
      batch takipçisi tarayıcı açık olmasa bile anahtara erişebiliyor.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from dotenv import dotenv_values, set_key

# ---------------------------------------------------------------------------
# Yollar
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
# Profil formları + iş kayıtları (JSON). Görseller outputs/ altında.
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_OUTPUT_DIR = "outputs"

# ---------------------------------------------------------------------------
# Model / fiyat
# ---------------------------------------------------------------------------
MODEL_NAME = "gemini-2.5-flash-image"

# Görsel başına: 1290 output token × $30/1M = $0.039. Batch %50 indirimli.
PRICE_PER_IMAGE_STANDARD = 0.039
PRICE_PER_IMAGE_BATCH = 0.0195

# Her HTTP isteği için üst süre (ms). Yoksa kopan bir bağlantı sonsuza kadar
# bekleyebiliyor ve arayüz "tepkisiz" görünüyor.
HTTP_TIMEOUT_MS = 180_000

# OTOMATİK GÖRSEL-ÜRET PREFIX'İ:
#   1) "Based on the provided reference image" → master görseli referans işaretler
#   2) "generate a new image" → imperatif komut (text mi image mi tereddüdünü kırar)
#   3) "that matches the description below" → açıklamayı görsel kriteri yapar
IMAGE_GENERATION_PREFIX = (
    "Based on the provided reference image, generate a new image "
    "that matches the description below.\n"
    "Do not answer with text only — output must include the generated image.\n\n"
)

API_KEY_ENV = "GEMINI_API_KEY"

# .env.example, README ve start.ps1'deki şablon değerlerin parçaları.
_PLACEHOLDER_TOKENS = ("YOUR_API_KEY", "YOUR_GEMINI_API_KEY", "API_KEY_HERE", "BURAYA")


def build_prompt(master_prompt: str, variation: str, use_auto_prefix: bool) -> str:
    """[opsiyonel önek] + master prompt + varyasyon (iki mod da aynısını kullanır)."""
    prefix = IMAGE_GENERATION_PREFIX if use_auto_prefix else ""
    return f"{prefix}{master_prompt.strip()}\n\nVaryasyon: {variation}"


# ---------------------------------------------------------------------------
# API anahtarı
# ---------------------------------------------------------------------------
def is_placeholder_key(api_key: str | None) -> bool:
    """Anahtar boş mu ya da şablondan kopyalanmış bir değer mi?"""
    if not api_key or not api_key.strip():
        return True
    upper = api_key.strip().upper()
    return any(token in upper for token in _PLACEHOLDER_TOKENS)


def get_api_key() -> str:
    """Kayıtlı anahtarı döndürür (.env öncelikli, sonra ortam değişkeni). Yoksa ''."""
    value = ""
    if ENV_PATH.exists():
        value = (dotenv_values(ENV_PATH).get(API_KEY_ENV) or "").strip()
    if not value:
        value = os.environ.get(API_KEY_ENV, "").strip()
    return "" if is_placeholder_key(value) else value


def save_api_key(api_key: str) -> None:
    """Anahtarı .env dosyasına yazar (dosya yoksa oluşturulur)."""
    cleaned = api_key.strip().strip("'\"").strip()
    if is_placeholder_key(cleaned):
        raise ValueError("Geçerli bir API anahtarı gir.")
    # Gemini anahtarları ~39 karakter; kısa ya da boşluklu değer yanlış yapıştırmadır.
    if len(cleaned) < 30 or any(ch.isspace() for ch in cleaned):
        raise ValueError(
            "Bu bir Gemini API anahtarına benzemiyor (çok kısa veya boşluk içeriyor). "
            "Anahtarı AI Studio'dan tekrar kopyala."
        )
    set_key(ENV_PATH, API_KEY_ENV, cleaned)


def key_fingerprint(api_key: str) -> str:
    """Anahtarın kısa parmak izi: iş kaydında anahtarın kendisini tutmadan
    'bu iş hangi anahtarla başlatıldı?' sorusunu cevaplamak için."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def mask_key(api_key: str) -> str:
    """Arayüzde gösterim için: AIza…abcd"""
    return f"{api_key[:4]}…{api_key[-4:]}" if len(api_key) > 12 else "****"
