"""
check_batches.py
================
Hesabındaki TÜM batch job'larını listeler ve durumlarını gösterir.

NEDEN VAR?
----------
"Batch denedim ama çalıştı mı bilmiyorum" sorusunun cevabı.
Streamlit'i kapattıktan sonra batch arka planda devam edebilir.
Bu script senin yerine sorar:
    - Hesabımda kaç batch var?
    - Hangileri başarılı oldu?
    - Hangileri hâlâ çalışıyor?
    - Hangileri başarısız oldu?

KULLANIM (PowerShell):
    python check_batches.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# State -> kullanıcı dostu etiket eşleşmesi
# ---------------------------------------------------------------------------
STATE_LABELS = {
    "JOB_STATE_PENDING": "[BEKLIYOR]",
    "JOB_STATE_RUNNING": "[CALISIYOR]",
    "JOB_STATE_SUCCEEDED": "[BASARILI]",
    "JOB_STATE_FAILED": "[BASARISIZ]",
    "JOB_STATE_CANCELLED": "[IPTAL]",
    "JOB_STATE_EXPIRED": "[24SAAT_DOLDU]",
}


def _format_time(timestamp) -> str:
    """API'den gelen datetime'ı okunabilir Türkçe format'a çevirir."""
    if timestamp is None:
        return "?"
    # API datetime objesi veya string olabilir - iki durumu da hallet
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return str(timestamp)

    # UTC'den lokal zamana çevir (Türkiye için +3)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    # Lokalde göster - GG/AA HH:MM
    return timestamp.strftime("%d/%m %H:%M")


def main() -> int:
    """Batch job listesini çekip yazdırır."""
    print("=" * 70)
    print("  Gemini Batch Job Geçmişi")
    print("=" * 70)

    # 1) .env'den API key oku
    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY", "").strip()

    if not api_key or "YOUR_API_KEY" in api_key.upper():
        print("[HATA] .env'de gecerli GEMINI_API_KEY yok.")
        return 1

    # 2) SDK import
    try:
        from google import genai
    except ImportError:
        print("[HATA] google-genai yuklu degil. pip install -r requirements.txt")
        return 1

    # 3) Client oluştur
    try:
        client = genai.Client(api_key=api_key)
    except Exception as exc:
        print(f"[HATA] Client olusturulamadi: {exc}")
        return 1

    # 4) Batch job'ları listele
    print("\nBatch'ler cekiliyor...\n")

    try:
        # page_size verme - hepsini al (genelde az olur, sorun değil)
        batches = list(client.batches.list())
    except Exception as exc:
        print(f"[HATA] Batch listesi alinamadi: {exc}")
        return 1

    if not batches:
        print("Hesabinda hic batch job yok.")
        print("Demek ki ya hic batch denemedin ya da hepsi silindi.")
        return 0

    # 5) Tablo halinde yazdır
    print(f"Toplam {len(batches)} batch bulundu:\n")
    # Sütun başlıkları
    print(f"{'#':<3} {'Olusturma':<14} {'Durum':<18} {'Model':<28} {'Display Name'}")
    print("-" * 100)

    succeeded_count = 0
    running_count = 0
    failed_count = 0

    # En yeniden eskiye doğru göster
    for idx, batch in enumerate(batches, start=1):
        state_name = batch.state.name if batch.state else "?"
        state_label = STATE_LABELS.get(state_name, state_name)

        # Sayaçları güncelle
        if state_name == "JOB_STATE_SUCCEEDED":
            succeeded_count += 1
        elif state_name == "JOB_STATE_RUNNING" or state_name == "JOB_STATE_PENDING":
            running_count += 1
        elif state_name in ("JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"):
            failed_count += 1

        # Display name güvenli okuma (yoksa name'den parse)
        display_name = getattr(batch, "display_name", None) or "-"
        # Model adını kısalt (gemini-2.5-flash-image gibi)
        model = batch.model.replace("models/", "") if batch.model else "?"

        # Oluşturma zamanı
        create_time = _format_time(getattr(batch, "create_time", None))

        print(
            f"{idx:<3} {create_time:<14} {state_label:<18} "
            f"{model:<28} {display_name}"
        )

    # 6) Özet
    print("\n" + "=" * 70)
    print("  OZET")
    print("=" * 70)
    print(f"  Basarili:   {succeeded_count}")
    print(f"  Calisiyor:  {running_count}")
    print(f"  Basarisiz:  {failed_count}")

    # 7) Outputs klasörü kontrolü
    outputs_dir = Path("outputs")
    if outputs_dir.exists():
        png_count = len(list(outputs_dir.glob("*.png")))
        jpg_count = len(list(outputs_dir.glob("*.jpg")))
        print(f"\n  outputs/ klasorunde: {png_count} PNG + {jpg_count} JPG = {png_count + jpg_count} gorsel")

    # 8) Yorumlama
    print("\n" + "=" * 70)
    print("  YORUM")
    print("=" * 70)

    if running_count > 0:
        print(f"  {running_count} batch HALA CALISIYOR! Streamlit'te bekleyebilirsin.")
        print("  Bittiginde sonuclar otomatik gelecek (eger UI hala acikssa).")
    if succeeded_count > 0 and (outputs_dir.exists() and png_count == 0):
        print("  Basarili batch var ama outputs/ bos. Sonuclari indirmemissin.")
        print("  COZUM: Streamlit'te ayni master+prompt ile tekrar dene; bu defa")
        print("         daha az olacak ya da bir 'job indirme' ozelligi ekleyebiliriz.")
    if failed_count > 0:
        print(f"  {failed_count} batch BASARISIZ olmus. Detaylar Cloud Console'da.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
