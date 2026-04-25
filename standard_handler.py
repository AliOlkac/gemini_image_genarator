"""
standard_handler.py
===================
FREE TIER fallback - Standart (non-batch) görsel üretim akışı.

NEDEN VAR?
----------
Batch API paid tier (billing aktif) gerektirir. Free tier kullanıcı
Batch'te 429 RESOURCE_EXHAUSTED yer çünkü kotası sıfırdır.
Bu modül her varyasyonu standart `generate_content` ile teker teker
(paralel ThreadPool ile) üretir. Free tier'da çalışır.

BATCH'E GÖRE FARKLAR:
    [-] %50 indirim YOK (tam fiyat ödenir)
    [+] Sonuç anlık (24 saat beklemek yok)
    [+] Free tier'da çalışır
    [+] Hata ayıklama daha kolay (her istek izole)
    [-] Rate limit'e dikkat (dakikada 5-15 istek free'de)

TASARIM PRENSİPLERİ:
    - batch_handler.py'a DOKUNULMAZ (bozma riskini sıfırlamak için).
    - Generator pattern: UI tarafı her iki handler'ı da aynı şekilde tüketebilir.
    - ThreadPoolExecutor: paralel istek + ilerleme tracking için.
"""

from __future__ import annotations

import base64
import mimetypes
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from google import genai
from google.genai import types

from async_saver import ImagePayload


# ---------------------------------------------------------------------------
# Sabitler - batch_handler ile aynı modeli kullanıyoruz, ekstra mantık yok.
# ---------------------------------------------------------------------------
MODEL_NAME = "gemini-2.5-flash-image"

# Free tier'a saygılı varsayılan: dakikada 5-10 istek limit'inde 2 worker güvenli.
# Kullanıcı UI'dan değiştirebilir (1-5 aralığında).
DEFAULT_MAX_WORKERS = 2

# Files API: yüklenen dosya ACTIVE state'e gelene kadar maksimum bekleme.
# Genelde 1-3 saniye yeterli, 30 saniye güvenli üst sınır.
FILE_ACTIVE_TIMEOUT_SECONDS = 30
FILE_POLL_INTERVAL_SECONDS = 0.5  # Hızlı polling - dosyalar genelde çabuk hazır


# ---------------------------------------------------------------------------
# Standart modun ilerleme paketi.
# Batch'tekinden farklı çünkü ölçütümüz "tamamlanan istek sayısı"
# (state polling değil).
# ---------------------------------------------------------------------------
@dataclass
class StandardProgress:
    """Standart modun her tamamlanan istekte yield ettiği durum paketi."""

    completed: int                   # Şu ana kadar biten istek sayısı
    total: int                       # Toplam istek sayısı
    last_message: str                # Son tamamlanan istek hakkında kısa not
    # field(default_factory=list) → mutable default için doğru kullanım.
    # Yoksa tüm instance'lar AYNI listeyi paylaşır (klasik Python tuzağı).
    failed_keys: list[str] = field(default_factory=list)


# ===========================================================================
#                              ANA SINIF
# ===========================================================================
class GeminiStandardHandler:
    """
    Standart (non-batch) Gemini görsel üretim akışını yöneten sınıf.

    Master görseli bir kez yükler (Files API), sonra her varyasyonu
    paralel olarak generate_content ile üretir.
    """

    def __init__(self, api_key: str) -> None:
        """
        Args:
            api_key: Gemini API anahtarı.
        """
        # Placeholder kontrolü - batch_handler ile aynı mantık
        if not api_key or "YOUR_API_KEY" in api_key.upper():
            raise ValueError(
                "API anahtarı geçerli değil. .env dosyasındaki "
                "GEMINI_API_KEY değerini kendi anahtarınla değiştir."
            )

        self.client = genai.Client(api_key=api_key)

        # State değişkenleri
        self.master_file_obj = None        # Files API'den dönen File nesnesi
        self.master_mime_type: str | None = None

        # Üretim sonuçlarını tutmak için (generator yield'ları sırasında doluyor)
        self.payloads: list[ImagePayload] = []
        self.failed_keys: list[str] = []

    # -----------------------------------------------------------------------
    # Master görseli Files API'ye yükle + ACTIVE durumuna gelene kadar bekle
    # -----------------------------------------------------------------------
    def upload_master_image(self, image_path: str | Path) -> None:
        """
        Master görseli Files API'ye yükler ve dosya ACTIVE state'e gelene
        kadar bekler.

        NEDEN ACTIVE BEKLEMESİ GEREKLİ?
        --------------------------------
        client.files.upload() dosyayı yükler ama Google sunucusunda dosya
        önce PROCESSING state'inde başlar. Birkaç saniye sonra ACTIVE'e
        geçer. Eğer ACTIVE olmadan generate_content çağırırsak:
            - Hata fırlatmaz (sessiz başarısızlık)
            - Model görseli "okuyamaz", text-only response döner
            - Kullanıcı [BOS] görür ama nedenini bilmez
        Bu fonksiyon o yarış durumunu (race condition) önler.
        """
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"Master görsel bulunamadı: {path}")

        # MIME türünü uzantıdan tahmin et
        mime_type, _ = mimetypes.guess_type(str(path))
        if not mime_type:
            mime_type = "image/png"

        # 1) Yükle (initial state genelde "PROCESSING")
        uploaded = self.client.files.upload(
            file=str(path),
            config=types.UploadFileConfig(
                display_name=path.name,
                mime_type=mime_type,
            ),
        )

        # 2) ACTIVE state polling - dosya hazır olana kadar bekle
        deadline = time.time() + FILE_ACTIVE_TIMEOUT_SECONDS
        while True:
            # state.name değerleri: "PROCESSING", "ACTIVE", "FAILED"
            current_state = uploaded.state.name if uploaded.state else "UNKNOWN"

            if current_state == "ACTIVE":
                break  # Hazır - çık

            if current_state == "FAILED":
                raise RuntimeError(
                    f"Master görsel Files API'de işlenemedi (FAILED): {path.name}"
                )

            if time.time() > deadline:
                raise TimeoutError(
                    f"Master görsel {FILE_ACTIVE_TIMEOUT_SECONDS} saniyede "
                    f"ACTIVE olmadı. Son durum: {current_state}"
                )

            # Kısa bekleme + tekrar sorgula
            time.sleep(FILE_POLL_INTERVAL_SECONDS)
            uploaded = self.client.files.get(name=uploaded.name)

        # File nesnesini saklıyoruz - generate_content'e direkt geçilebilir
        self.master_file_obj = uploaded
        self.master_mime_type = mime_type

    # -----------------------------------------------------------------------
    # Tek bir varyasyonu üreten worker fonksiyonu (ThreadPool içinde çalışır)
    # -----------------------------------------------------------------------
    def _generate_one(
        self,
        idx: int,
        master_prompt: str,
        variation: str,
    ) -> ImagePayload:
        """
        Bir varyasyon için tek istek atar ve ImagePayload döner.

        ThreadPool worker'ı bu metodu paralel olarak çağıracak.
        Hata durumlarında AÇIKLAYICI exception fırlatır - generate_all_streaming
        bunları yakalayıp UI'a anlamlı mesaj olarak iletir.

        Args:
            idx: Varyasyon indeksi (key üretmek için).
            master_prompt: Sabit prompt.
            variation: Bu spesifik varyasyon metni.

        Returns:
            ImagePayload (görsel + metadata).

        Raises:
            RuntimeError: Model görsel üretmediyse, niye üretmediğini açıklar.
        """
        combined = f"{master_prompt.strip()}\n\nVaryasyon: {variation}"
        key = f"req-{idx:03d}"

        response = self.client.models.generate_content(
            model=MODEL_NAME,
            contents=[self.master_file_obj, combined],
            config=types.GenerateContentConfig(
                # ŞART: response_modalities olmadan model görsel üretmez!
                response_modalities=["TEXT", "IMAGE"],
            ),
        )

        # ----- TEŞHİS ADIM 1: Prompt Feedback (input bloklandı mı?) -----
        # Eğer modelin safety filter'ı PROMPT'u (yani master + varyasyon kombosu)
        # bloklarsa, candidates boş gelir ve prompt_feedback dolar.
        prompt_feedback = getattr(response, "prompt_feedback", None)
        if prompt_feedback is not None:
            block_reason = getattr(prompt_feedback, "block_reason", None)
            if block_reason:
                raise RuntimeError(
                    f"Prompt safety ile bloklandı: {block_reason}. "
                    f"Master prompt veya varyasyonu yumuşat."
                )

        # ----- TEŞHİS ADIM 2: Hiç candidate yok mu? -----
        if not response.candidates:
            raise RuntimeError(
                "Yanıt boş: hiç candidate dönmedi. "
                "Genelde safety blok veya geçici sunucu sorunu."
            )

        # ----- ASIL İŞ: candidates içinde görsel ara -----
        # Aynı zamanda finish_reason'ları topluyoruz - hiç görsel yoksa
        # bu listeyi hata mesajında göstereceğiz (teşhis için).
        finish_reasons: list[str] = []

        for candidate in response.candidates:
            # finish_reason: STOP (normal), SAFETY, MAX_TOKENS, RECITATION, OTHER
            finish = getattr(candidate, "finish_reason", None)
            if finish is not None:
                # Enum'u string'e çeviriyoruz (loglamak için)
                finish_str = getattr(finish, "name", str(finish))
                finish_reasons.append(finish_str)

            if not candidate.content or not candidate.content.parts:
                continue

            for part in candidate.content.parts:
                inline = getattr(part, "inline_data", None)
                if inline is None or not inline.data:
                    continue

                # NOT: Standart API'de inline_data.data RAW BYTES gelir.
                # async_saver base64 bekliyor → encode ediyoruz.
                b64_str = base64.b64encode(inline.data).decode("utf-8")
                mime = inline.mime_type or "image/png"

                return ImagePayload(
                    key=key,
                    base64_data=b64_str,
                    mime_type=mime,
                )

        # ----- TEŞHİS ADIM 3: Görsel yok ama candidate var → niye? -----
        # finish_reason'a göre kullanıcıya AÇIKLAYICI mesaj
        reasons_str = ", ".join(finish_reasons) if finish_reasons else "?"
        if "SAFETY" in reasons_str:
            raise RuntimeError(
                f"Safety filter görseli bloklandı (finish_reason: {reasons_str}). "
                f"Prompt'u yumuşat veya farklı varyasyon dene."
            )
        elif "RECITATION" in reasons_str:
            raise RuntimeError(
                "Model telif hakkı endişesiyle üretmedi (RECITATION). "
                "Prompt'u daha jenerik yap."
            )
        elif "MAX_TOKENS" in reasons_str:
            raise RuntimeError(
                "Token limiti dolduğu için görsel oluşmadı (MAX_TOKENS)."
            )
        else:
            # Bilinmeyen sebep - en azından finish_reason'ları söyle
            raise RuntimeError(
                f"Model görsel dönmedi. finish_reasons: [{reasons_str}]. "
                "Tekrar dene veya prompt'u değiştir."
            )

    # -----------------------------------------------------------------------
    # Tüm varyasyonları paralel üretir, ilerleme yield eder.
    # -----------------------------------------------------------------------
    def generate_all_streaming(
        self,
        master_prompt: str,
        variations: list[str],
        max_workers: int = DEFAULT_MAX_WORKERS,
    ) -> Iterator[StandardProgress]:
        """
        Tüm varyasyonları ThreadPoolExecutor ile paralel üretir.

        GENERATOR PATTERN:
            Her tamamlanan istekte bir StandardProgress yield eder.
            UI bu generator'u for döngüsünde tüketip ilerleme barını günceller.

        Bittikten sonra sonuçlar self.payloads ve self.failed_keys'te durur.
        Generator dışından bunlara erişerek async_saver'a verebilirsin.

        Args:
            master_prompt: Sabit metin.
            variations: Varyasyon listesi.
            max_workers: Eşzamanlı thread sayısı (1-5 önerilir).

        Yields:
            Her tamamlanan istek için bir StandardProgress.
        """
        if not self.master_file_obj:
            raise RuntimeError("Önce upload_master_image() çağırmalısın.")

        cleaned = [v.strip() for v in variations if v.strip()]
        if not cleaned:
            raise ValueError("En az bir varyasyon gerekli.")

        # State'i sıfırla (aynı handler iki kez kullanılabilsin)
        self.payloads = []
        self.failed_keys = []

        total = len(cleaned)
        completed = 0

        # ThreadPoolExecutor: I/O-bound iş için ideal (HTTP isteği bekliyoruz çoğunu).
        # GIL CPU-bound'da problem olur ama burada thread'ler çoğu zaman uyukluyor.
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Tüm istekleri schedule ediyoruz - hepsi worker'lara dağılacak.
            # future -> (idx, variation) eşleşmesini sözlükte tutuyoruz ki
            # tamamlandığında hangi istek olduğunu bilelim.
            future_map = {
                executor.submit(self._generate_one, idx, master_prompt, var): (idx, var)
                for idx, var in enumerate(cleaned, start=1)
            }

            # as_completed: future'ları TAMAMLANMA SIRASINA göre yield eder
            # (submit sırasına göre değil!). Bu sayede ilk biten ilk gösterilir.
            for future in as_completed(future_map):
                idx, var = future_map[future]
                key = f"req-{idx:03d}"
                completed += 1

                try:
                    # _generate_one ya ImagePayload döner ya da raise eder.
                    # Artık None dönmüyor → "BOS" log'u kalktı, hep net mesaj var.
                    result = future.result()
                    self.payloads.append(result)
                    # Varyasyonun ilk 40 karakterini göster (UI sığması için)
                    msg = f"[OK] {key}: {var[:40]}"
                except Exception as exc:
                    # API hatası, safety, rate limit vb. - hepsi exception olarak gelir
                    self.failed_keys.append(key)
                    # Hata mesajını 100 karakterle sınırla (eski 60 çok kısaydı)
                    msg = f"[HATA] {key}: {str(exc)[:100]}"

                # UI'a tek bir ilerleme paketi yield et
                yield StandardProgress(
                    completed=completed,
                    total=total,
                    last_message=msg,
                    failed_keys=self.failed_keys.copy(),
                )

    # -----------------------------------------------------------------------
    # Temizlik (best-effort, batch_handler ile simetrik)
    # -----------------------------------------------------------------------
    def cleanup(self) -> None:
        """Files API'ye yüklenen master görseli silmeye çalışır."""
        if self.master_file_obj is not None:
            try:
                self.client.files.delete(name=self.master_file_obj.name)
            except Exception:
                # Silinemese de ana akış zaten bitti - sessiz yut
                pass
