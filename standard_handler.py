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

# Master görseli inline (bytes) gönderirken üst boyut sınırı.
# Gemini istek gövdesi sınırları için; aşarsak kullanıcıyı net uyarırız.
# (Çok büyük görsellerde yine de sıkıştırma / küçültme önerilir.)
MAX_MASTER_IMAGE_BYTES = 20 * 1024 * 1024  # ~20 MiB

# OTOMATİK GÖRSEL-ÜRET PREFIX'İ:
# Test edilmiş en güçlü formül - 3 katmanlı sinyal:
#   1) "Based on the provided reference image" → master image'ı referans olarak işaretler
#   2) "generate a new image" → imperatif komut (modelin "text mi image mi?" tereddüdünü kırar)
#   3) "that matches the description below" → açıklamayı GÖRSEL KRİTERİ olarak okutur
# İngilizce çünkü Gemini'nin imperatif komut anlama performansı İngilizce'de daha güçlü.
# Maliyeti: ~25 token (~$0.000003) → retry maliyeti yanında negligible.
IMAGE_GENERATION_PREFIX = (
    "Based on the provided reference image, generate a new image "
    "that matches the description below.\n"
    "Do not answer with text only — output must include the generated image.\n\n"
)


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

    Master görseli Files API ile YÜKLEMEZ: `client.files.upload` bazı
    ortamlarda (Windows, proxy, SDK resumable upload) 400
    "Upload has already been terminated" verebiliyor. Bunun yerine görsel
    baytlarını bellekte tutar; her `generate_content` çağrısında inline
    (`Part.from_bytes`) olarak gönderir. Ekstra input token maliyeti olur
    ama güvenilirlik artar.
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

        # Master görsel: Files API yok; bellekte ham baytlar + MIME.
        self._master_image_bytes: bytes | None = None
        self.master_mime_type: str | None = None

        # Üretim sonuçlarını tutmak için (generator yield'ları sırasında doluyor)
        self.payloads: list[ImagePayload] = []
        self.failed_keys: list[str] = []

    # -----------------------------------------------------------------------
    # Master görseli belleğe al (Files API YOK — inline gönderim)
    # -----------------------------------------------------------------------
    def upload_master_image(self, image_path: str | Path) -> None:
        """
        Master görseli diskten okuyup bellekte saklar.

        İsim `upload_master_image` kaldı (main.py ve alışkanlık uyumu).
        Gerçekte Files API'ye yükleme YAPILMAZ: `files.upload` resumable
        protokolünde 400 "Upload has already been terminated" hatası
        bazı kullanıcı ortamlarında sürekli tetiklenebiliyor.

        ÇÖZÜM: Baytlar `generate_content` içinde `Part.from_bytes` ile
        her istekte inline gider. ACTIVE bekleme gerekmez; yarış durumu yok.
        """
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"Master görsel bulunamadı: {path}")

        # MIME türünü uzantıdan tahmin et
        mime_type, _ = mimetypes.guess_type(str(path))
        if not mime_type:
            mime_type = "image/png"

        file_bytes = path.read_bytes()
        if not file_bytes:
            raise ValueError(f"Master görsel boş veya okunamadı: {path}")
        if len(file_bytes) > MAX_MASTER_IMAGE_BYTES:
            raise ValueError(
                f"Master görsel çok büyük ({len(file_bytes) // 1024 // 1024} MiB). "
                f"Maksimum ~{MAX_MASTER_IMAGE_BYTES // 1024 // 1024} MiB; "
                "görseli küçült veya sıkıştır."
            )

        self._master_image_bytes = file_bytes
        self.master_mime_type = mime_type

    # -----------------------------------------------------------------------
    # Tek bir varyasyonu üreten worker fonksiyonu (ThreadPool içinde çalışır).
    # ARTIK RETRY YOK - başarısızlık doğrudan UI'a iletilir.
    # Sebep: bug olduğunda istek sayısının patlamaması (defansif mühendislik).
    # Eski retry mekanizması git history'de mevcut.
    # -----------------------------------------------------------------------
    def _generate_one(
        self,
        idx: int,
        master_prompt: str,
        variation: str,
        use_auto_prefix: bool = True,
    ) -> ImagePayload:
        """
        Bir varyasyon için TEK API isteği atar ve parse eder.

        ThreadPool worker'ı bu metodu paralel olarak çağırır.
        Hata durumlarında AÇIKLAYICI exception fırlatır - generate_all_streaming
        bunları yakalayıp UI'a anlamlı mesaj olarak iletir.

        Args:
            idx: Varyasyon indeksi (key üretmek için).
            master_prompt: Sabit prompt.
            variation: Bu spesifik varyasyon metni.
            use_auto_prefix: True ise IMAGE_GENERATION_PREFIX prompt'un başına
                eklenir. Modelin "STOP-without-image" davranışını ~%80 azaltır.

        Returns:
            ImagePayload (görsel + metadata).

        Raises:
            RuntimeError: Model görsel üretmediyse, niye üretmediğini açıklar.
        """
        key = f"req-{idx:03d}"

        # ThreadPool içinden de çağrılabilir; master yükü kontrolü (tip güvenliği).
        master_bytes = self._master_image_bytes
        if master_bytes is None:
            raise RuntimeError(
                "Master görsel yüklenmemiş. Önce upload_master_image() çağrılmalı."
            )

        # PROMPT BİRLEŞTİRME: [opsiyonel auto prefix] + master_prompt + varyasyon.
        prefix = IMAGE_GENERATION_PREFIX if use_auto_prefix else ""
        combined = f"{prefix}{master_prompt.strip()}\n\nVaryasyon: {variation}"

        # Tek kullanıcı mesajı: önce referans görsel (inline), sonra metin.
        # Files API kullanmıyoruz — resumable upload hatalarından kaçınmak için.
        user_content = types.Content(
            role="user",
            parts=[
                types.Part.from_bytes(
                    data=master_bytes,
                    mime_type=self.master_mime_type or "image/png",
                ),
                types.Part.from_text(text=combined),
            ],
        )

        # response_modalities: Sadece IMAGE — Google'ın önerdiği mod.
        # TEXT+IMAGE bırakılırsa model bazen STOP ile sadece metin döndürüyor (~%25).
        # IMAGE tek başına çıktıyı görsele zorlar (metin üretimi devre dışı).
        # Kaynak: https://ai.google.dev/gemini-api/docs/image-generation
        response = self.client.models.generate_content(
            model=MODEL_NAME,
            contents=[user_content],
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                # Düşük sıcaklık: görsel üretimde daha tutarlı / daha az "metne kaçma"
                temperature=0.4,
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
            # STOP-without-image: artık retry yok. Kullanıcı manuel tekrar dener.
            raise RuntimeError(
                f"Model görsel dönmedi. finish_reasons: [{reasons_str}]. "
                "Auto-prefix'i sidebar'dan kontrol et veya prompt'u değiştir."
            )

    # -----------------------------------------------------------------------
    # Tüm varyasyonları paralel üretir, ilerleme yield eder.
    # -----------------------------------------------------------------------
    def generate_all_streaming(
        self,
        master_prompt: str,
        variations: list[str],
        max_workers: int = DEFAULT_MAX_WORKERS,
        use_auto_prefix: bool = True,
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
            use_auto_prefix: Görsel-üret prefix'ini ekle (varsayılan True).
                Modelin "STOP-without-image" davranışını ~%80 azaltır.

        Yields:
            Her tamamlanan istek için bir StandardProgress.
        """
        if self._master_image_bytes is None:
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
                executor.submit(
                    self._generate_one,
                    idx,
                    master_prompt,
                    var,
                    use_auto_prefix,
                ): (idx, var)
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
        """Bellekteki master görsel baytlarını serbest bırakır (Files API yok)."""
        self._master_image_bytes = None
        self.master_mime_type = None
