# Gemini 2.5 Flash Image Batcher

Streamlit arayüzü ile referans görsel + master prompt + satır satır varyasyonlardan toplu görsel üretimi. **Standart API** (anında) ve **Batch API** (%50 indirim, yavaş) desteklenir.

## Yerelde çalıştırma (Windows / PowerShell)

```powershell
cd gemini_image_genarator
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
# .env dosyasını düzenle: GEMINI_API_KEY=...
streamlit run main.py
```

Tarayıcı: `http://localhost:8501`

## Arkadaşlarla paylaşım: Streamlit Community Cloud (önerilen)

Her kullanıcı **kendi** [Google AI Studio](https://aistudio.google.com/apikey) anahtarını sidebar’a yapıştırır; fatura **sana değil**, onlara yazılır.

### Adımlar (senin yapacakların)

1. Kodu **GitHub**’a push et (`.env` asla commit etme — `.gitignore`’da).
2. [share.streamlit.io](https://share.streamlit.io) ile GitHub hesabınla giriş yap.
3. **New app** → repo’yu seç → **Main file path:** `main.py` → Deploy.
4. Ücretsiz katman için repo genelde **public** olmalı.
5. Arkadaşlarına uygulama URL’sini ver (örn. `https://<isim>.streamlit.app`).

### Arkadaşların yapacakları

1. Linke tıkla (ilk açılış 30–60 sn sürebilir).
2. Sol menüden **Gemini API Key** alanına kendi anahtarını yapıştır.
3. Üretime başla.

### Güvenlik

- **Kendi API anahtarını** Streamlit Cloud “Secrets”a koyma: tüm ziyaretçiler aynı anahtarı kullanır; fatura sana gelir.
- Public repoda `.env` / gerçek key yok; sızıntı olursa Google anahtarı iptal eder.

Detaylı notlar ve sorun giderme için: [STREAMLIT_CLOUD_DEPLOY.md](STREAMLIT_CLOUD_DEPLOY.md).

## Lisans / sorumluluk

Google API kullanımı ve ücretlendirme [resmi fiyatlandırma](https://ai.google.dev/gemini-api/docs/pricing) sayfasına tabidir. Üretim öncesi faturalandırma ve kota limitlerini kontrol et.
