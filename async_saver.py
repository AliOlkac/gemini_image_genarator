"""
async_saver.py
==============
Bu modül, Gemini Batch API'den dönen base64 kodlanmış görselleri,
asyncio + aiofiles kullanarak PARALEL olarak diske yazmaktan sorumludur.

NEDEN ASENKRON?
---------------
Disk I/O işlemleri (dosya yazma) bloklayıcı (blocking) bir işlemdir.
50 görseli tek tek (senkron) yazsak, her birinin diske basılmasını
beklemek zorunda kalırız. asyncio sayesinde işletim sistemi diske
yazarken, biz bir sonraki görselin base64'ünü çözmeye başlarız.
Sonuç: Toplam süre büyük ölçüde kısalır.

NOT: Senin direktifinde aiohttp vardı; ancak Gemini Batch API URL
döndürmüyor (görsel inline base64 geliyor). Bu yüzden aiohttp yerine
aiofiles kullanmak doğru mühendislik tercihi.
"""

from __future__ import annotations

import asyncio
import base64
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import aiofiles  # Asenkron dosya yazma için


# ---------------------------------------------------------------------------
# Veri sınıfı: Tek bir görselin kayıt için ihtiyaç duyduğu bilgileri taşır.
# dataclass kullanarak boilerplate __init__/__repr__ kodunu otomatik üretiyoruz.
# ---------------------------------------------------------------------------
@dataclass
class ImagePayload:
    """Bir görselin diske yazılması için gereken minimum bilgi paketi."""

    key: str           # Batch isteğindeki benzersiz anahtar (ör: "req-3")
    base64_data: str   # Modelden dönen base64-encoded görsel binary'si
    mime_type: str     # MIME türü (ör: "image/png", "image/jpeg")


# ---------------------------------------------------------------------------
# Yardımcı: MIME tipinden doğru dosya uzantısını çıkarır.
# Gemini genellikle "image/png" döner ama farklı modlarda farklı olabilir.
# ---------------------------------------------------------------------------
def _extension_from_mime(mime_type: str) -> str:
    """
    MIME tipini dosya uzantısına çevirir.
    Örn: "image/png" -> ".png", "image/jpeg" -> ".jpg"
    """
    # Bilinen MIME -> uzantı eşleşmeleri sözlüğü
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    # Bilinmeyen bir tip gelirse generic ".bin" uzantısı koruyucudur
    return mapping.get(mime_type.lower(), ".bin")


# ---------------------------------------------------------------------------
# Tek görseli asenkron olarak diske yazan coroutine.
# Bu coroutine'ler asyncio.gather ile bir arada paralel çalışacak.
# ---------------------------------------------------------------------------
async def _save_single_image(
    payload: ImagePayload,
    output_dir: Path,
) -> Path:
    """
    Tek bir görseli base64'ten çözer ve diske yazar.

    Args:
        payload: Yazılacak görselin verisi (key + base64 + mime).
        output_dir: Hedef klasör (genellikle ./outputs).

    Returns:
        Yazılan dosyanın tam yolu (Path objesi).
    """
    # 1) Base64 stringini gerçek byte dizisine çeviriyoruz.
    #    Bu CPU işlemi, async değil ama çok hızlı (mikrosaniyeler).
    image_bytes = base64.b64decode(payload.base64_data)

    # 2) Uzantıyı belirleyip nihai dosya adını üretiyoruz.
    extension = _extension_from_mime(payload.mime_type)
    filename = f"{payload.key}{extension}"
    file_path = output_dir / filename

    # 3) Asenkron dosya yazma: aiofiles bu noktada bloklamadan diske yazar.
    #    "wb" = write binary (ikili modda yaz, encoding yok).
    async with aiofiles.open(file_path, "wb") as f:
        await f.write(image_bytes)

    return file_path


# ---------------------------------------------------------------------------
# Public API: Birden fazla görseli paralel olarak kaydeder.
# ---------------------------------------------------------------------------
async def save_all_images(
    payloads: Iterable[ImagePayload],
    output_dir: str | Path = "outputs",
) -> list[Path]:
    """
    Tüm görselleri ASYNC olarak paralel kaydeder.

    İşleyiş:
        1. Her görsel için bir coroutine (görev) hazırlanır.
        2. asyncio.gather() bunların hepsini aynı anda başlatır.
        3. En son tüm dosya yolları liste halinde döner.

    Args:
        payloads: Kaydedilecek görsel paketlerinin koleksiyonu.
        output_dir: Hedef klasör. Yoksa otomatik oluşturulur.

    Returns:
        Yazılmış dosyaların yolu (sırası gather sırasını korur).
    """
    # Çıktı klasörünü Path objesine çevir ve yoksa oluştur
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Her görsel için bir coroutine listesi hazırlıyoruz.
    # Bu noktada coroutine'ler henüz çalışmıyor - sadece schedule edilmeyi bekliyor.
    tasks = [_save_single_image(p, output_path) for p in payloads]

    # asyncio.gather: tüm tasks'leri paralel başlatır, hepsi bitince listeyi döner.
    # return_exceptions=False: Bir görselde hata olursa tümü iptal olur.
    # İstersen True yap, kısmi hata toleransı sağla.
    saved_paths = await asyncio.gather(*tasks, return_exceptions=False)

    return saved_paths


# ---------------------------------------------------------------------------
# Senkron kod (Streamlit) içinden çağrılabilen wrapper fonksiyon.
# Streamlit asenkron değil, bu yüzden async fonksiyonu sync gibi sarmalıyoruz.
# ---------------------------------------------------------------------------
def save_all_images_sync(
    payloads: Iterable[ImagePayload],
    output_dir: str | Path = "outputs",
) -> list[Path]:
    """
    save_all_images'in senkron sarmalayıcısı.

    Streamlit gibi senkron framework'lerden direkt çağırmak için.
    asyncio.run() yeni bir event loop oluşturur, async fonksiyonu çalıştırır
    ve sonucu döner. Streamlit thread'inde güvenle kullanılabilir.

    Args:
        payloads: Kaydedilecek görsellerin listesi.
        output_dir: Hedef klasör.

    Returns:
        Yazılan dosya yolları.
    """
    # asyncio.run(): event loop'u kurar, coroutine'i çalıştırır, sonra kapatır.
    return asyncio.run(save_all_images(payloads, output_dir))


# ---------------------------------------------------------------------------
# Tek görsel için senkron, hızlı kaydetme yardımcısı.
# Live grid akışı için: stream sırasında her gelen görseli ANINDA diske yaz.
# ---------------------------------------------------------------------------
def save_single_image_sync(
    payload: ImagePayload,
    output_dir: str | Path = "outputs",
) -> Path:
    """
    Tek bir görseli senkron olarak diske yazar.

    NEDEN ASYNC DEĞİL?
        Tek dosya için asyncio overhead gereksiz. Streaming akışta her
        gelen görseli anında yazmak için saf senkron kod yeterli.
        (50 görsel toplu yazılacaksa save_all_images_sync daha hızlı.)

    Args:
        payload: Kaydedilecek görselin verisi.
        output_dir: Hedef klasör (yoksa oluşturulur).

    Returns:
        Yazılan dosyanın yolu.
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    image_bytes = base64.b64decode(payload.base64_data)
    extension = _extension_from_mime(payload.mime_type)
    filename = f"{payload.key}{extension}"
    file_path = output_path / filename

    # Standart Python file write - hızlı ve basit
    with open(file_path, "wb") as f:
        f.write(image_bytes)

    return file_path
